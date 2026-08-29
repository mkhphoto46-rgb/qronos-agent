"""
Qronos's voice: Chatterbox Persian, through the CrispASR runtime.

The settings here are not guesses. A sweep of thirty-eight runs across six
quantisations, four step counts and four device placements was measured on the
development card, and the numbers below are its conclusion. Two of them are
worth reading before changing anything.

**q4_k weights, not something larger.** Across the sweep the quantisations were
indistinguishable on quality and clearly separated on cost. The seed-to-seed
spread of the error rate *within* one quantisation was 0.069 to 0.138, while
the whole spread *between* quantisations was 0.075 to 0.283 — so the apparent
quality ranking was noise, and choosing on it would have been choosing on
nothing. Cost was not noise: q4_k was simultaneously the fastest, the lightest
on the graphics card and the lightest on host memory. When one option wins on
every axis that can be measured and ties on the axis that cannot, it wins.

**The whole pipeline on the graphics card.** The runtime's default puts the
text model on the processor and only the vocoder on the card. Measured, that
default is the slow choice: real-time factor 0.74 against 0.40 with everything
on the card, for *less* graphics memory in the second case, because the
processor path holds far more host memory instead. Entirely on the processor is
2.4 times slower than real time and unusable.

**Ten diffusion steps.** Twenty is slower for no measurable gain and four is no
faster than ten, because at that point the fixed costs dominate.

The one thing the sweep did not examine is where the rest of the time goes, and
it turned out to be most of it. Every one of those runs launched the executable
afresh, so each sentence paid to load the model again: across fifteen sentences
the gap between wall time and generation time was a constant 1.6 seconds. For a
short acknowledgement — which is most of what an assistant actually says — that
is three times the cost of the work itself.

So this module does not launch anything per sentence. It runs CrispASR as a
local server with the model resident, and speaks over HTTP. Measured on the
same card, a short reply went from 2.13 seconds to 0.79.

Holding a model is exactly what Qronos stopped doing for its brains, and the
reasoning that applied there applies here in reverse. The fast brain cost 3,442
MiB to save 1.7 seconds and was not worth it. The voice costs a fraction of
that, saves a comparable amount, and is asked for far more often — but it is
still the user's graphics card, so it is released after a spell of silence
rather than held forever.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Callable, Optional, Protocol
from uuid import uuid4

from core.config import CONFIG
from core.resource_guard import read_gpu_status
from core.voice_runtime import Utterance, VoiceRuntime


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


DEFAULT_ROOT = CONFIG.paths.root / "runtime" / "chatterbox"

DEFAULT_EXECUTABLE = (
    DEFAULT_ROOT / "bin" / "crispasr-windows-x86_64-vulkan" / "crispasr.exe"
)

#: The text model. q4_k for the reasons in the module docstring.
DEFAULT_VOICE_MODEL = DEFAULT_ROOT / "models" / "t3-fa-q4_k.gguf"

#: The vocoder. Only ever measured at q8_0, so it is not a choice, it is what
#: was tested.
DEFAULT_CODEC_MODEL = DEFAULT_ROOT / "models" / "chatterbox-s3gen-q8_0.gguf"

#: Diffusion steps. Ten. Twenty costs more for no measurable gain.
DEFAULT_STEPS = 10

#: Where the resident server listens. High and local-only: this speaks to
#: nothing but Qronos, on this machine.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8231

#: How long the voice may stay loaded with nothing to say before it gives the
#: graphics card back. Long enough to cover the gaps in a conversation, short
#: enough that walking away releases it.
DEFAULT_IDLE_SECONDS = 120.0

#: How long to wait for the server to become able to speak. Generous: it is
#: reading roughly 750 MB from disk the first time, and a cold filesystem is
#: not quick.
DEFAULT_STARTUP_TIMEOUT = 180.0

#: How long a single utterance may take before something is assumed wrong.
DEFAULT_REQUEST_TIMEOUT = 120.0

#: How long to wait between asking a starting server whether it can speak yet.
#: Long enough not to hammer a process that is reading 750 MB off a disk.
DEFAULT_RETRY_SECONDS = 0.5

#: The runtime's own cap is 4096 characters. Qronos speaks answers, not
#: documents, and anything near this is a bug upstream rather than a request.
MAX_TEXT_CHARACTERS = 2000

#: What the voice needs on the graphics card while it is working, in MiB.
#:
#: The number that matters is the peak during generation, not the footprint
#: after loading, and the difference between those two is the whole reason
#: this constant is worth a paragraph. Loading the model moves the card by
#: 542 MiB, which is what a naive reading gives — but the sweep measured the
#: peak for these exact settings at 1,497 MiB, because the vocoder and the
#: diffusion buffers are allocated while it speaks and not before.
#:
#: Getting that wrong is not a rounding error. With 1,739 MiB free, which
#: comfortably exceeds 542 and even exceeds 1,497, the voice still crawled:
#: real-time factors of 2.9 to 7.1 against 0.33 to 0.65 on a card with room.
REQUIRED_VRAM_MB = 1_497

#: Spare graphics memory required beyond that peak.
#:
#: Deliberately generous, and honest about why. The voice was measured fast
#: with roughly 12,000 MiB free and slow with 1,739 and with 931. Nothing was
#: measured in between, so where it stops being fast is bracketed rather than
#: known. This sits outside the bracket that was observed to fail: below about
#: 2,500 MiB free, Qronos would rather say nothing than take a card it will
#: only crawl on. A card that is not fully understood is one to be cautious
#: with, and being too polite costs a message the user can read.
VRAM_HEADROOM_MB = 1_024

#: Refuse to add work to a card at or above this. Matches
#: ActivityGuard.CRITICAL_GPU_TEMP_C, which is the only critical temperature
#: this codebase already has an opinion about.
CRITICAL_GPU_TEMP_C = 85


class VoiceUnavailable(RuntimeError):
    """The voice could not be made to work."""


class ChatterboxRuntime(VoiceRuntime):
    """
    Chatterbox Persian behind a resident CrispASR server.

    Thread-safe. The desktop can ask for the queue while a voice turn is in
    flight, and the idle timer runs on a thread of its own.
    """

    def __init__(
        self,
        executable_path: str | Path = DEFAULT_EXECUTABLE,
        voice_model_path: str | Path = DEFAULT_VOICE_MODEL,
        codec_model_path: str | Path = DEFAULT_CODEC_MODEL,
        temp_dir: str | Path | None = None,
        steps: int = DEFAULT_STEPS,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        required_vram_mb: int = REQUIRED_VRAM_MB,
        check_room: bool = True,
        clock: Clock | None = None,
        launch: Optional[Callable[[list[str], dict], subprocess.Popen]] = None,
    ) -> None:
        if steps <= 0:
            raise ValueError("steps must be greater than zero.")

        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be greater than zero.")

        self.executable_path = Path(executable_path)
        self.voice_model_path = Path(voice_model_path)
        self.codec_model_path = Path(codec_model_path)
        self.temp_dir = Path(temp_dir) if temp_dir else CONFIG.paths.temp
        self.steps = steps
        self.host = host
        self.port = port
        self.idle_seconds = idle_seconds
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.retry_seconds = retry_seconds
        self.required_vram_mb = required_vram_mb
        self.check_room = check_room
        self.clock: Clock = clock or time.monotonic

        self._launch = launch or self._launch_process
        self._process: subprocess.Popen | None = None
        self._last_spoke_at: float | None = None

        self._lock = threading.RLock()
        self._idle_timer: threading.Timer | None = None

    # ------------------------------------------------------------- the seam

    def health_check(self) -> bool:
        """Whether the pieces are on disk. Does not start anything."""
        return (
            self.executable_path.is_file()
            and self.voice_model_path.is_file()
            and self.codec_model_path.is_file()
        )

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def speak_to_file(
        self,
        text: str,
        destination: str | Path | None = None,
    ) -> Utterance:
        """Say ``text`` and write the audio out."""
        spoken = self._require_speakable(text)

        self._ensure_running()

        started = self.clock()
        audio = self._request(spoken)
        took = self.clock() - started

        target = self._resolve_destination(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(audio)

        with self._lock:
            self._last_spoke_at = self.clock()
            self._arm_idle_timer()

        return Utterance(
            audio_path=target,
            audio_seconds=_wav_seconds(target),
            took_seconds=took,
            text=spoken,
        )

    def release(self) -> None:
        """
        Stop the server and give the graphics card back.

        Safe at any time, including while nothing is running. Speaking again
        starts it afresh, paying the load once more.
        """
        with self._lock:
            self._cancel_idle_timer()
            process, self._process = self._process, None

        if process is None or process.poll() is not None:
            return

        process.terminate()

        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def __enter__(self) -> "ChatterboxRuntime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # -------------------------------------------------------------- talking

    def _request(self, text: str) -> bytes:
        body = json.dumps(
            {
                "model": "chatterbox",
                "input": text,
                "response_format": "wav",
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            f"{self._base_url}/v1/audio/speech",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request, timeout=self.request_timeout
            ) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:400]
            raise VoiceUnavailable(
                f"The voice refused to speak ({error.code}): {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise VoiceUnavailable(
                f"The voice did not answer: {error.reason}"
            ) from error

    # ------------------------------------------------------------ lifecycle

    def _ensure_running(self) -> None:
        with self._lock:
            self._cancel_idle_timer()

            if self._process is not None and self._process.poll() is None:
                return

            self._process = None

        if not self.health_check():
            raise VoiceUnavailable(self._what_is_missing())

        self._require_room()

        command = [
            str(self.executable_path),
            "--server",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--backend",
            "chatterbox",
            "-m",
            str(self.voice_model_path),
            "--codec-model",
            str(self.codec_model_path),
            "--tts-steps",
            str(self.steps),
        ]

        environment = dict(os.environ)

        # The whole pipeline on the graphics card. Measured at real-time
        # factor 0.40 against the default placement's 0.74, for less graphics
        # memory rather than more.
        environment["CRISPASR_CHATTERBOX_FORCE_GPU"] = "1"

        process = self._launch(command, environment)

        with self._lock:
            self._process = process

        try:
            self._wait_until_speakable(process)
        except Exception:
            self.release()
            raise

    def _wait_until_speakable(self, process: subprocess.Popen) -> None:
        """
        Wait until it can actually speak, not merely until it is listening.

        The port opens before the model has finished loading, so a readiness
        check that only asks whether something answers will hand back a server
        that then blocks for a second and a half on the first real request.
        Asking it to say one word is the only check that means anything.
        """
        deadline = self.clock() + self.startup_timeout

        while self.clock() < deadline:
            if process.poll() is not None:
                raise VoiceUnavailable(
                    "The voice runtime stopped while starting up "
                    f"(exit {process.returncode}). {self._stderr_tail(process)}"
                )

            try:
                self._request("سلام")
                return
            except VoiceUnavailable:
                time.sleep(self.retry_seconds)

        raise VoiceUnavailable(
            f"The voice runtime did not become ready within "
            f"{self.startup_timeout:.0f} seconds."
        )

    def _arm_idle_timer(self) -> None:
        """Release the card after a spell of silence. Called with the lock."""
        self._cancel_idle_timer()

        timer = threading.Timer(self.idle_seconds, self._release_if_idle)
        timer.name = "qronos-voice-idle"
        timer.daemon = True

        self._idle_timer = timer
        timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _release_if_idle(self) -> None:
        with self._lock:
            spoke_at = self._last_spoke_at

            if spoke_at is None:
                return

            if self.clock() - spoke_at < self.idle_seconds:
                # Something was said while the timer was counting down.
                self._arm_idle_timer()
                return

        self.release()

    def _require_room(self) -> None:
        """
        Refuse to start when the card has no room for the voice.

        Not a guess about what might go wrong. Measured on this card: with
        931 MiB free, the same line that costs 0.33 seconds of work per second
        of speech cost 3.01. The runtime does not report an error in that
        state — it spills and crawls — so nothing downstream would know
        anything was wrong except the person waiting.

        Refusing is the kinder answer. A voice that says it cannot speak can be
        reported; a voice that takes thirty-five seconds to say a sentence
        merely looks broken, and all the while it is taking the graphics card
        from whatever was using it first.

        Two deliberate limits on how far this goes:

            Only on the way in. A voice already speaking is not cut off
            because something else grew.

            A card that cannot be read is not a card with no room.
            ``read_gpu_status`` returns nothing both when the read fails and
            when the machine has no NVIDIA card at all, and refusing on that
            would leave Qronos permanently mute on every AMD and Intel
            machine — a total, silent, permanent failure guarding against a
            transient one.

        This asks its own small question rather than borrowing one. There is a
        general safety floor in the resource work — ``core/hard_floor.py`` —
        which asks exactly this and more besides, and when the two are on one
        branch they should become one. The numbers here are deliberately
        identical to that module's, so merging them is a deletion rather than
        a decision.
        """
        if not self.check_room:
            return

        try:
            gpu = read_gpu_status()
        except Exception:
            return

        if gpu is None:
            return

        if (
            gpu.temperature_c is not None
            and gpu.temperature_c >= CRITICAL_GPU_TEMP_C
        ):
            raise VoiceUnavailable(
                f"Qronos cannot speak right now. The graphics card is at "
                f"{gpu.temperature_c} C, and Qronos waits for it to cool "
                "before adding work to it."
            )

        if gpu.vram_used_mb is None or gpu.vram_total_mb is None:
            return

        free = max(0, gpu.vram_total_mb - gpu.vram_used_mb)

        if free < self.required_vram_mb + VRAM_HEADROOM_MB:
            raise VoiceUnavailable(
                f"Qronos cannot speak right now. The voice needs "
                f"{self.required_vram_mb} MB of graphics memory and "
                f"{free} MB is free. Speaking anyway would push another "
                "application off the card, so it waits instead."
            )

    # --------------------------------------------------------------- detail

    @property
    def _base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @staticmethod
    def _launch_process(
        command: list[str],
        environment: dict,
    ) -> subprocess.Popen:
        creation_flags = 0

        if os.name == "nt":
            # No console window. The desktop starts this behind the interface
            # and a flashing black box would be alarming.
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=str(Path(command[0]).parent),
            creationflags=creation_flags,
        )

    def _require_speakable(self, text: str) -> str:
        spoken = (text or "").strip()

        if not spoken:
            raise ValueError("There is nothing to say.")

        if len(spoken) > MAX_TEXT_CHARACTERS:
            raise ValueError(
                f"Qronos speaks answers, not documents: {len(spoken)} "
                f"characters is beyond the {MAX_TEXT_CHARACTERS} it will say "
                "in one go."
            )

        return spoken

    def _resolve_destination(self, destination: str | Path | None) -> Path:
        if destination is not None:
            return Path(destination)

        return self.temp_dir / f"qronos_voice_{uuid4().hex}.wav"

    def _what_is_missing(self) -> str:
        missing = [
            str(path)
            for path in (
                self.executable_path,
                self.voice_model_path,
                self.codec_model_path,
            )
            if not path.is_file()
        ]

        return (
            "Qronos has no voice because these are not on disk: "
            + ", ".join(missing)
        )

    @staticmethod
    def _stderr_tail(process: subprocess.Popen) -> str:
        if process.stderr is None:
            return ""

        try:
            return process.stderr.read().decode("utf-8", "replace")[-400:]
        except Exception:
            return ""


def _wav_seconds(path: Path) -> float:
    """How long a wave file plays. Zero if it cannot be read as one."""
    try:
        with wave.open(str(path)) as handle:
            rate = handle.getframerate()

            return handle.getnframes() / rate if rate else 0.0
    except (wave.Error, OSError):
        return 0.0
