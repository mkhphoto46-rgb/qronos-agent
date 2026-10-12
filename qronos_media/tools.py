"""ffmpeg and ffprobe as bounded, disposable children limited to local files.

Every container in the archive is untrusted input to a native decoder, so the
decoder runs in its own process and is treated as something to contain:

* a fixed argument list, never a shell, and filter graphs assembled only from
  numbers this module has range-checked;
* ``-protocol_whitelist file`` plus an explicit ``file:`` input, so neither the
  name nor anything inside the container can open a network or pipe protocol;
* ``-format_whitelist`` limited to real local containers, so playlists, concat
  lists, image sequences and other indirection demuxers are refused;
* a timeout, cancellation, and hard caps on what it may print;
* an OS job with a memory limit, a one-process limit and whole-job teardown
  (:mod:`qronos_media.containment`);
* below-normal priority and a minimal environment (no inherited proxies).

None of this is a security sandbox. The whitelists stop protocol and
playlist indirection; they do not stop a decoder vulnerability from reading
what the user can read. Stronger isolation is a separate deployment gate.

The development machine's ffmpeg build is GPL and compiles in network
protocols; the whitelist is what keeps them unused. Which build may ship is a
licence decision recorded in docs/media_index.md, not made here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Callable, Mapping, Protocol, Sequence

from qronos_media.containment import DECODER_LIMITS, ChildLimits, Containment
from qronos_media.paths import decoder_input, local_path

FFMPEG_DIR_ENV = "QRONOS_MEDIA_FFMPEG_DIR"
#: The shipped FFmpeg (Main28 r28 contract A2; staged set verified by Root): 9.0.2 built by Qronos from the signed
#: official source https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz, LGPL v2.1 or later, no GPL, version3, nonfree or network
#: code; protocols file and pipe only; reproduced byte-for-byte by a second clean build. Only these exact files are
#: admitted from <runtime>/ffmpeg/bin. Known limit: AV1 video does not decode (no libdav1d); probing still works.
SHIPPED_FFMPEG_IDENTITY = "ffmpeg 9.0.2 LGPL-2.1-or-later (Qronos build of the signed official source)"
SHIPPED_FFMPEG = {"ffmpeg": (21947392, "6c3fc768ef1de9ee4d514ae15b789ed77dc39f95a3a379b75bec4fcf4e0c65b9"),
                  "ffprobe": (21726208, "ef43e6e32a54646d1ba38957ebba4396699a1032f9be1e6c402bcb87c2ac6406")}
SHIPPED_FFMPEG_LIMITS = ("av1_video_decode_unavailable",)
#: A developer/test override (QRONOS_MEDIA_FFMPEG_DIR) is never pinned and is reported as such.
UNPINNED_IDENTITY = "development override (unpinned; not a shipping build)"
_PINNED_OK: dict[tuple, bool] = {}

# Demuxers for real local containers. Deliberately absent: concat, hls, dash,
# image2, sdp, rtsp, tee and every other demuxer that opens further inputs.
VIDEO_DEMUXERS = ("matroska", "mov", "avi", "mpegts", "mpeg", "flv", "asf", "ogg")
ASS_DEMUXERS = ("ass",)

_WINDOWS_FLAGS = 0x08000000 | 0x00004000  # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS


class MediaToolError(RuntimeError):
    """A child process did not produce a usable result."""


class MediaToolUnavailable(MediaToolError):
    """ffmpeg/ffprobe are not provisioned or cannot start."""


class ChildCancelled(MediaToolError):
    """The caller cancelled; the child was killed."""


class ChildTimedOut(MediaToolError):
    """The child exceeded its time budget and was killed."""


class ChildOutputExceeded(MediaToolError):
    """The child printed more than its cap and was killed."""


@dataclass(frozen=True)
class ChildResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    peak_memory_bytes: int = 0
    pid: int = 0


def _reader(stream, sink: bytearray, limit: int, overflow: threading.Event) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            if len(sink) + len(chunk) > limit:
                sink.extend(chunk[: max(0, limit - len(sink))])
                overflow.set()
                return
            sink.extend(chunk)
    except (OSError, ValueError):
        return


class ChildTerminationUnproven(MediaToolError):
    """The child was told to stop but its exit (and its whole job's) could not be proven.

    The job handle is NOT closed: closing would end the only way to prove the
    exit later. The spawn observer that owns the child keeps it.
    """


class SpawnObserver(Protocol):
    """Who owns a child for its whole life (the model lease). Proof comes from the handles it is given."""

    def begin_spawn(self) -> None: ...
    def spawn_failed(self) -> None: ...
    def attach(self, child: subprocess.Popen, containment: Containment | None) -> None: ...
    def finish(self, child: subprocess.Popen, containment: Containment | None) -> bool: ...


def run_bounded(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
    cancelled: Callable[[], bool] | None = None,
    env: Mapping[str, str] | None = None,
    poll_seconds: float = 0.05,
    limits: ChildLimits | None = DECODER_LIMITS,
    observer: SpawnObserver | None = None,
    containment_factory: Callable[[ChildLimits], Containment] | None = None,
    cwd: Path | None = None,
) -> ChildResult:
    """Run ``argv`` with a deadline, a cancel check, output caps and OS limits.

    Non-zero exit codes are returned, not raised: callers know what a failure
    of their command means. Cancellation, timeout and overflow tear down the
    child's whole job and raise; the exception carries ``pid``. ``limits`` is
    the memory and process-count containment (see :mod:`qronos_media.containment`);
    if it cannot be applied the child is killed before it runs.

    ``observer`` (a model lease) is told before the process is created
    (``begin_spawn``; it may refuse), gets the owned process and job handles
    right after creation (``attach``) and, after teardown, decides from those
    handles whether the exit is proven (``finish``). When it is not, the job
    stays open in the observer's hands and :class:`ChildTerminationUnproven`
    is raised. ``containment_factory`` is a test seam for the job type only.
    ``cwd`` is a caller-chosen, already verified folder (never a request value); by default the child
    inherits this process's working directory.
    """
    if not argv or not all(isinstance(part, str) for part in argv):
        raise ValueError("argv must be a non-empty list of strings")
    if cwd is not None and (not isinstance(cwd, Path) or not cwd.is_absolute() or not cwd.is_dir()):
        raise ValueError("cwd must be an existing absolute folder")
    if not (0 < timeout_seconds <= 24 * 3600) or not math.isfinite(timeout_seconds):
        raise ValueError("timeout_seconds out of range")
    if observer is not None:
        observer.begin_spawn()
    try:
        factory = containment_factory or Containment  # looked up per call (module seam)
        containment = factory(limits) if limits is not None else None
    except OSError as exc:
        if observer is not None:
            observer.spawn_failed()
        raise MediaToolUnavailable(f"child containment unavailable: {exc}") from None
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = _WINDOWS_FLAGS | (containment.spawn_flags() if containment else 0)
    elif containment is not None:  # pragma: no cover - non-Windows CI
        kwargs["preexec_fn"] = containment.preexec()
        kwargs["start_new_session"] = True
    try:
        child = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=dict(env) if env is not None else None,
            cwd=str(cwd) if cwd is not None else None,
            **kwargs,
        )
    except OSError as exc:
        if containment is not None:
            containment.close()
        if observer is not None:
            observer.spawn_failed()
        raise MediaToolUnavailable(f"could not start {Path(argv[0]).name}: {type(exc).__name__}") from None
    if observer is not None:
        observer.attach(child, containment)

    def stop_everything() -> None:
        if containment is not None:
            containment.terminate()
        if child.poll() is None:
            child.kill()

    kept_open = False
    reason: type[MediaToolError] | None = None
    adopt_error: OSError | None = None
    out, err = bytearray(), bytearray()
    overflow = threading.Event()
    peak = 0
    try:
        if containment is not None:
            try:
                containment.adopt(child)
            except OSError as exc:
                adopt_error = exc
        if adopt_error is None:
            readers = [
                threading.Thread(target=_reader, args=(child.stdout, out, stdout_limit, overflow), daemon=True),
                threading.Thread(target=_reader, args=(child.stderr, err, stderr_limit, overflow), daemon=True),
            ]
            for thread in readers:
                thread.start()
            deadline = time.monotonic() + timeout_seconds
            while child.poll() is None:
                if overflow.is_set():
                    reason = ChildOutputExceeded
                elif cancelled is not None and cancelled():
                    reason = ChildCancelled
                elif time.monotonic() >= deadline:
                    reason = ChildTimedOut
                if reason is not None:
                    break
                time.sleep(poll_seconds)
        else:
            readers = []
        # Always end the whole job: a descendant that outlived the child
        # would otherwise keep the pipes open and the readers blocked.
        stop_everything()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - terminate did not take
            pass
        for thread in readers:
            thread.join(timeout=5)
        peak = containment.peak_memory() if containment is not None else 0
    finally:
        if observer is not None:
            # The observer reads proof from the handles it holds; an unproven
            # job stays open with it (closing would make proof impossible).
            kept_open = not observer.finish(child, containment)
        if containment is not None and not kept_open:
            containment.close()
    if not kept_open:
        # With a descendant still alive (unproven), closing a pipe would wait for the blocked
        # reader thread, that is, for the descendant: the pipes stay with the observer instead.
        for stream in (child.stdout, child.stderr):
            try:
                stream.close()
            except OSError:
                pass
    if kept_open:
        error = ChildTerminationUnproven(f"{Path(argv[0]).name}: exit not proven; still counted")
        error.pid = child.pid
        raise error
    if adopt_error is not None:
        raise MediaToolUnavailable(f"child containment failed: {adopt_error}") from None
    if reason is None and overflow.is_set():
        reason = ChildOutputExceeded
    if reason is not None:
        error = reason(f"{Path(argv[0]).name}: {reason.__name__}")
        error.pid = child.pid
        raise error
    return ChildResult(child.returncode, bytes(out), bytes(err), peak, child.pid)


def _number(value: float, low: float, high: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not (low <= number <= high):
        raise ValueError(f"{name} out of range")
    return number


def _seconds(value: float) -> str:
    return f"{_number(value, 0.0, 10 * 24 * 3600, 'time'):.3f}"


def _plain_files(*paths: Path) -> bool:
    return all(path.is_file() and not path.is_symlink() for path in paths)


def _pinned(path: Path, name: str) -> bool:
    """The file is exactly the pinned shipping executable (size, then SHA-256; cached per file state)."""
    import hashlib

    size, sha256 = SHIPPED_FFMPEG[name]
    try:
        info = os.stat(path)
    except OSError:
        return False
    if info.st_size != size:
        return False
    key = (str(path), info.st_size, info.st_mtime_ns, getattr(info, "st_ino", 0), sha256)
    if key not in _PINNED_OK:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:  # read-only; never executed here
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        _PINNED_OK[key] = digest.hexdigest() == sha256
    return _PINNED_OK[key]


@dataclass(frozen=True)
class MediaTools:
    """Located ffmpeg/ffprobe plus the only command lines this package runs."""

    ffmpeg: Path
    ffprobe: Path
    identity: str = UNPINNED_IDENTITY
    pinned: bool = False

    @property
    def limits(self) -> tuple[str, ...]:
        return SHIPPED_FFMPEG_LIMITS if self.pinned else ()

    @classmethod
    def locate(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        runtime_assets: Path | None = None,
    ) -> "MediaTools | None":
        """Find a provisioned build. ``PATH`` is never searched.

        Order: ``QRONOS_MEDIA_FFMPEG_DIR`` (a development/test override, reported as unpinned), then the
        provisioned ``<runtime>/ffmpeg/bin``, admitted ONLY when both files are exactly the pinned shipping build
        (size and SHA-256, read-only; never executed here). Both executables must be regular files.
        """
        environment = os.environ if environ is None else environ
        suffix = ".exe" if os.name == "nt" else ""
        configured = environment.get(FFMPEG_DIR_ENV, "").strip()
        if configured:
            try:
                directory = local_path(configured)
            except ValueError:
                return None
            ffmpeg, ffprobe = directory / f"ffmpeg{suffix}", directory / f"ffprobe{suffix}"
            if _plain_files(ffmpeg, ffprobe):
                return cls(ffmpeg, ffprobe)
        if runtime_assets is not None:
            directory = runtime_assets / "ffmpeg" / "bin"
            ffmpeg, ffprobe = directory / f"ffmpeg{suffix}", directory / f"ffprobe{suffix}"
            if _plain_files(ffmpeg, ffprobe) and _pinned(ffmpeg, "ffmpeg") and _pinned(ffprobe, "ffprobe"):
                return cls(ffmpeg, ffprobe, SHIPPED_FFMPEG_IDENTITY, True)
        return None

    def provenance(self) -> dict[str, object]:
        """Which build is in use (no execution): the pinned shipping identity, or an unpinned override."""
        return {"identity": self.identity, "pinned": self.pinned, "limits": list(self.limits)}

    def child_env(self) -> dict[str, str]:
        env = {"PATH": str(self.ffmpeg.parent)}
        if os.name == "nt":
            env["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
        return env

    def describe(self, timeout_seconds: float = 15.0) -> dict[str, object]:
        """Build identity and licence-relevant configure flags, for provenance."""
        result = run_bounded(
            [str(self.ffprobe), "-hide_banner", "-version"],
            timeout_seconds=timeout_seconds, stdout_limit=256 * 1024, stderr_limit=64 * 1024,
            env=self.child_env(),
        )
        text = result.stdout.decode("utf-8", "replace")
        first = text.splitlines()[0] if text else ""
        flags = sorted(
            flag for flag in ("--enable-gpl", "--enable-version3", "--enable-nonfree") if flag in text
        )
        return {"version": first, "licenceFlags": flags, "ffmpeg": str(self.ffmpeg), "ffprobe": str(self.ffprobe)}

    # ------------------------------------------------------------ command lines

    @staticmethod
    def _input(path: Path, demuxers: Sequence[str]) -> list[str]:
        return [
            "-protocol_whitelist", "file",
            "-format_whitelist", ",".join(demuxers),
            "-i", decoder_input(path),
        ]

    def probe_argv(self, path: Path) -> list[str]:
        return [
            str(self.ffprobe), "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file",
            "-format_whitelist", ",".join(VIDEO_DEMUXERS),
            "-show_format", "-show_streams", "-of", "json",
            decoder_input(path),
        ]

    def subtitle_argv(self, path: Path, stream_index: int, *, demuxers: Sequence[str] = VIDEO_DEMUXERS) -> list[str]:
        index = int(_number(stream_index, 0, 4096, "stream_index"))
        return [
            str(self.ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error",
            *self._input(path, demuxers),
            "-map", f"0:{index}", "-c:s", "srt", "-f", "srt", "pipe:1",
        ]

    def scenes_argv(
        self,
        path: Path,
        *,
        start_seconds: float,
        length_seconds: float,
        threshold: float,
        width: int = 160,
        threads: int = 2,
    ) -> list[str]:
        graph = (
            f"scale=w={int(_number(width, 32, 1920, 'width'))}:h=-2,"
            f"scdet=threshold={_number(threshold, 0.1, 100.0, 'threshold'):.3f}"
        )
        thread_count = str(int(_number(threads, 1, 16, "threads")))
        return [
            # Progress lines stay on: the last "time=" is where decoding ended.
            str(self.ffmpeg), "-hide_banner", "-nostdin", "-stats_period", "1", "-loglevel", "info",
            "-threads", thread_count,
            "-ss", _seconds(start_seconds), "-t", _seconds(max(0.001, length_seconds)),
            *self._input(path, VIDEO_DEMUXERS),
            "-map", "0:v:0", "-an", "-sn", "-dn",
            "-filter_threads", thread_count,
            "-vf", graph, "-f", "null", "-",
        ]

    def audio_chunk_argv(self, path: Path, *, stream_index: int, start_seconds: float, length_seconds: float,
                         output: Path) -> list[str]:
        """ONE explicitly chosen audio stream (absolute index) -> 16 kHz mono 16-bit PCM WAV in an owned file."""
        target = local_path(output)
        if target.suffix.lower() != ".wav":
            raise ValueError("audio chunks are written as .wav")
        index = int(_number(stream_index, 0, 4096, "stream_index"))
        return [
            str(self.ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y",
            "-threads", "1",
            "-ss", _seconds(start_seconds), "-t", _seconds(max(0.001, length_seconds)),
            *self._input(path, VIDEO_DEMUXERS),
            "-map", f"0:{index}", "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav",
            "file:" + str(target),
        ]

    def keyframe_argv(self, path: Path, *, time_seconds: float, output: Path, width: int = 320) -> list[str]:
        target = local_path(output)
        if target.suffix.lower() != ".jpg":
            raise ValueError("keyframes are written as .jpg")
        return [
            str(self.ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y",
            "-threads", "1",
            "-ss", _seconds(time_seconds),
            *self._input(path, VIDEO_DEMUXERS),
            "-map", "0:v:0", "-frames:v", "1",
            "-vf", f"scale=w={int(_number(width, 32, 1920, 'width'))}:h=-2",
            "-c:v", "mjpeg", "-q:v", "5", "-f", "image2", "-update", "1",
            "file:" + str(target),
        ]
