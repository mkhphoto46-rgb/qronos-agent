"""Path admission, the bounded child runner and the decoder command lines."""
from __future__ import annotations

import os
from pathlib import Path
import time

import psutil
import pytest

from qronos_media.paths import UnsafeMediaPath, decoder_input, has_link_component, local_path, readable_media_file
from qronos_media.probe import ProbeFailed, probe
from qronos_media.stages import StageContext, _extract_embedded
from qronos_media.subtitles import SubtitleError
from qronos_media.containment import ChildLimits
from qronos_media.tools import (
    ASS_DEMUXERS, VIDEO_DEMUXERS, ChildCancelled, ChildOutputExceeded, ChildTimedOut, MediaTools,
    MediaToolUnavailable, run_bounded,
)
from tests.fixtures.child_python import child_python
from tests.media.media_support import TOOLS, base_video, copy_video, requires_ffmpeg  # noqa: F401

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")


# ------------------------------------------------------------------- paths

@windows_only
@pytest.mark.parametrize("raw", [
    "", "   ", "relative\\film.mkv", "\\\\server\\share\\film.mkv", "//server/share/film.mkv",
    "\\\\?\\C:\\film.mkv", "\\\\.\\COM1", "http://example.com/film.mp4", "file:C:/film.mkv",
    "concat:C:/a.mkv|C:/b.mkv", "C:\\movies\\..\\secret.mkv", "C:\\fixture\\video.mp4:other",
    "C:\\fixture\\video.mp4::$DATA", "C:\\x\\NUL", "C:\\x\\nul.mkv", "C:\\x\\com1.mkv", "C:\\x\\LPT\u00b9.mkv",
    "C:\\x\\con .srt", "C:\\x\\CONIN$", "C:\\x\\aux\\film.mkv", "C:\\x\\name.", "C:\\x\\name ", "C:\\x\\dir.\\a.mkv",
    "C:\\x\\a\x00.mkv",
])
def test_unsafe_paths_are_refused(raw: str) -> None:
    with pytest.raises(UnsafeMediaPath):
        local_path(raw)


@windows_only
@pytest.mark.parametrize("raw", ["C:\\Movies\\Film (2001) [1080p].mkv", "D:/archive/console.mkv",
                                 "C:\\x\\nullable\\comet.mp4", "C:\\x\\film.fa.srt"])
def test_ordinary_paths_are_accepted(raw: str) -> None:
    assert local_path(raw) == Path(os.path.normpath(raw))


@windows_only
def test_decoder_input_pins_the_file_protocol() -> None:
    assert decoder_input(Path("C:/Movies/a.mkv")) == "file:C:\\Movies\\a.mkv"
    with pytest.raises(UnsafeMediaPath):
        decoder_input(Path("C:/Movies/a.mkv:hidden"))


@windows_only
def test_junction_above_the_file_is_refused(tmp_path: Path) -> None:
    import _winapi

    real = tmp_path / "real" / "library"
    real.mkdir(parents=True)
    film = real / "film.mkv"
    film.write_bytes(b"x")
    junction = tmp_path / "jn"
    _winapi.CreateJunction(str(tmp_path / "real"), str(junction))
    through = junction / "library" / "film.mkv"
    assert through.is_file()
    assert has_link_component(through)
    with pytest.raises(UnsafeMediaPath):
        readable_media_file(through)
    # Even when the caller claims the junction-free root as the stopping point.
    with pytest.raises(UnsafeMediaPath):
        readable_media_file(through, root=junction / "library")
    assert readable_media_file(film) == film


def test_symlinked_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target.mkv"
    target.write_bytes(b"x")
    link = tmp_path / "link.mkv"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs privileges this session does not have")
    with pytest.raises(UnsafeMediaPath):
        readable_media_file(link)


# ------------------------------------------------------------ child runner
#
# Children are the base interpreter (tests/fixtures/child_python.py), so the
# PID the runner sees is the process that runs; assertions are about that
# process and anything it started being gone, not only about elapsed time.

CHILD, CHILD_ENV = child_python()


def _python(code: str) -> list[str]:
    return [CHILD, "-I", "-c", code]


def _gone(pid: int, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def _run(code: str, **kwargs):
    options = {"timeout_seconds": 60, "stdout_limit": 1 << 16, "stderr_limit": 1 << 16, "env": CHILD_ENV}
    options.update(kwargs)
    return run_bounded(_python(code), **options)


def test_child_exit_status_and_output_are_returned() -> None:
    result = _run("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)")
    assert result.returncode == 3
    assert result.stdout.strip() == b"out" and result.stderr.strip() == b"err"
    assert _gone(result.pid)


def test_timeout_kills_the_child() -> None:
    started = time.monotonic()
    with pytest.raises(ChildTimedOut) as stopped:
        _run("import time; time.sleep(60)", timeout_seconds=0.5)
    assert time.monotonic() - started < 15
    assert _gone(stopped.value.pid)


def test_cancel_kills_the_child() -> None:
    started = time.monotonic()
    with pytest.raises(ChildCancelled) as stopped:
        _run("import time; time.sleep(60)", cancelled=lambda: time.monotonic() - started > 0.3)
    assert time.monotonic() - started < 15
    assert _gone(stopped.value.pid)


def test_output_beyond_the_cap_kills_the_child() -> None:
    started = time.monotonic()
    with pytest.raises(ChildOutputExceeded) as stopped:
        _run("import sys, time\nsys.stdout.write('x' * 200000); sys.stdout.flush(); time.sleep(60)",
             stdout_limit=1000)
    assert time.monotonic() - started < 15
    assert _gone(stopped.value.pid)


def test_a_descendant_holding_the_pipes_is_torn_down_with_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        f"g = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(g.pid))\n"
        "time.sleep(120)\n"
    )
    started = time.monotonic()
    with pytest.raises(ChildTimedOut) as stopped:
        _run(code, timeout_seconds=3, limits=ChildLimits(memory_bytes=512 * 1024**2, max_processes=2))
    assert time.monotonic() - started < 20  # teardown did not hang on the inherited pipes
    grandchild = int(pid_file.read_text())
    assert _gone(stopped.value.pid) and _gone(grandchild)


def test_child_exiting_early_does_not_leave_its_descendant_running(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys\n"
        f"g = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(g.pid))\n"
    )
    result = _run(code, timeout_seconds=30, limits=ChildLimits(memory_bytes=512 * 1024**2, max_processes=2))
    assert result.returncode == 0
    assert _gone(int(pid_file.read_text()))


@windows_only
def test_decoder_children_cannot_start_further_processes() -> None:
    code = (
        "import subprocess, sys\n"
        "try:\n"
        "    subprocess.run([sys.executable, '-c', 'pass'])\n"
        "    print('spawned')\n"
        "except OSError:\n"
        "    print('refused')\n"
    )
    assert _run(code).stdout.strip() == b"refused"


def test_memory_limit_stops_a_runaway_allocation() -> None:
    result = _run("x = bytearray(600 * 1024 * 1024); print('allocated')",
                  limits=ChildLimits(memory_bytes=128 * 1024**2))
    assert result.returncode != 0 and b"allocated" not in result.stdout
    assert b"MemoryError" in result.stderr


def test_failed_containment_means_the_child_never_runs(monkeypatch, tmp_path: Path) -> None:
    import qronos_media.tools as tools

    marker = tmp_path / "ran"

    class Broken(tools.Containment):
        def adopt(self, child):
            raise OSError(5, "simulated assignment failure")

    monkeypatch.setattr(tools, "Containment", Broken)
    with pytest.raises(MediaToolUnavailable):
        _run(f"open({str(marker)!r}, 'w').write('x')")
    time.sleep(0.5)
    assert not marker.exists()


def test_runner_rejects_malformed_argv() -> None:
    with pytest.raises(ValueError):
        run_bounded([], timeout_seconds=1, stdout_limit=1, stderr_limit=1)
    with pytest.raises(ValueError):
        run_bounded(["x", 1], timeout_seconds=1, stdout_limit=1, stderr_limit=1)  # type: ignore[list-item]
    with pytest.raises(ValueError):
        run_bounded(["x"], timeout_seconds=float("nan"), stdout_limit=1, stderr_limit=1)


# ----------------------------------------------------------- command lines

FAKE_TOOLS = MediaTools(Path("C:/tools/ffmpeg.exe"), Path("C:/tools/ffprobe.exe"))
INDIRECTION_DEMUXERS = {"concat", "hls", "dash", "image2", "sdp", "rtsp", "tee", "lavfi", "data", "subfile"}


def _decoder_lines() -> list[list[str]]:
    film = Path("C:/m/film.mkv")
    return [
        FAKE_TOOLS.probe_argv(film),
        FAKE_TOOLS.subtitle_argv(film, 2),
        FAKE_TOOLS.subtitle_argv(Path("C:/m/film.ass"), 0, demuxers=ASS_DEMUXERS),
        FAKE_TOOLS.scenes_argv(film, start_seconds=10, length_seconds=600, threshold=10),
        FAKE_TOOLS.keyframe_argv(film, time_seconds=12.5, output=Path("C:/cache/k.jpg")),
    ]


@windows_only
@pytest.mark.parametrize("argv", _decoder_lines())
def test_every_decoder_line_is_file_only_and_whitelisted(argv: list[str]) -> None:
    assert argv[argv.index("-protocol_whitelist") + 1] == "file"
    demuxers = set(argv[argv.index("-format_whitelist") + 1].split(","))
    assert demuxers and not demuxers & INDIRECTION_DEMUXERS
    inputs = [part for part in argv if part.startswith("file:")]
    assert inputs and all(":" not in part[len("file:C:"):] for part in inputs)
    if "-i" in argv:
        assert argv.index("-protocol_whitelist") < argv.index("-i")
        assert argv[argv.index("-i") + 1].startswith("file:")
    assert not any(part in {"-hwaccel", "-init_hw_device"} for part in argv)


def test_whitelists_name_only_real_containers() -> None:
    assert set(VIDEO_DEMUXERS) == {"matroska", "mov", "avi", "mpegts", "mpeg", "flv", "asf", "ogg"}
    assert ASS_DEMUXERS == ("ass",)


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), -1.0, 0.0, 101.0])
def test_filter_graph_numbers_are_range_checked(threshold: float) -> None:
    with pytest.raises(ValueError):
        FAKE_TOOLS.scenes_argv(Path("C:/m/a.mkv"), start_seconds=0, length_seconds=1, threshold=threshold)


def test_keyframes_are_only_written_as_jpg_through_file_protocol() -> None:
    with pytest.raises(ValueError):
        FAKE_TOOLS.keyframe_argv(Path("C:/m/a.mkv"), time_seconds=1, output=Path("C:/cache/k.png"))
    with pytest.raises(ValueError):
        FAKE_TOOLS.scenes_argv(Path("C:/m/a.mkv"), start_seconds=-5, length_seconds=1, threshold=10)


def test_locate_never_searches_path(tmp_path: Path) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    (tmp_path / f"ffmpeg{suffix}").write_bytes(b"")
    assert MediaTools.locate(environ={"PATH": str(tmp_path)}) is None
    assert MediaTools.locate(environ={"QRONOS_MEDIA_FFMPEG_DIR": str(tmp_path)}) is None  # ffprobe missing
    (tmp_path / f"ffprobe{suffix}").write_bytes(b"")
    found = MediaTools.locate(environ={"QRONOS_MEDIA_FFMPEG_DIR": str(tmp_path)})
    assert found is not None and found.ffprobe.parent == tmp_path
    assert MediaTools.locate(environ={"QRONOS_MEDIA_FFMPEG_DIR": "relative/dir"}) is None


def test_child_environment_does_not_inherit_proxies(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    env = FAKE_TOOLS.child_env()
    assert set(env) <= {"PATH", "SystemRoot"}
    assert env["PATH"] == str(FAKE_TOOLS.ffmpeg.parent)


# ------------------------------------------- masquerading containers (real ffmpeg)

HLS = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:4,\nsibling.mkv\n#EXTINF:4,\nhttp://127.0.0.1:9/x.ts\n#EXT-X-ENDLIST\n"
CONCAT = "ffconcat version 1.0\nfile 'sibling.mkv'\n"
SDP = "v=0\no=- 0 0 IN IP4 127.0.0.1\ns=x\nc=IN IP4 127.0.0.1\nt=0 0\nm=video 5004 RTP/AVP 96\n"


@requires_ffmpeg
@pytest.mark.parametrize("name,content", [("evil.mp4", HLS), ("evil.mkv", CONCAT), ("evil.mp4", SDP),
                                          ("evil.ts", HLS), ("evil.avi", CONCAT)])
def test_playlists_disguised_as_videos_are_refused_before_following_references(
        tmp_path: Path, base_video: Path, name: str, content: str) -> None:
    copy_video(base_video, tmp_path / "sibling.mkv")
    disguised = tmp_path / name
    disguised.write_text(content, encoding="utf-8")
    with pytest.raises(ProbeFailed):
        probe(TOOLS, disguised)


@requires_ffmpeg
def test_playlist_disguised_as_ass_subtitle_is_refused(tmp_path: Path, base_video: Path) -> None:
    copy_video(base_video, tmp_path / "sibling.mkv")
    disguised = tmp_path / "film.ass"
    disguised.write_text(HLS, encoding="utf-8")
    ctx = StageContext(db=None, asset_id="a", files=[], tools=TOOLS, cache_dir=tmp_path, cache_quota_bytes=0,
                       cancelled=lambda: False, renew=lambda: None, save_checkpoint=lambda data: None)
    with pytest.raises(SubtitleError):
        _extract_embedded(ctx, disguised, 0, demuxers=ASS_DEMUXERS)


@requires_ffmpeg
def test_real_container_probes_with_the_same_restrictions(base_video: Path) -> None:
    result = probe(TOOLS, base_video)
    kinds = [stream.kind for stream in result.streams]
    assert kinds == ["video", "audio", "subtitle"]
    assert result.streams[2].language == "fa" and result.streams[2].subtitle_kind == "text"
    assert abs(result.duration_ms - 4_000) <= 100
