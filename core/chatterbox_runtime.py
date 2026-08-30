"""
Production Chatterbox Persian runtime for Qronos.

Measured local baseline:
    GPU: NVIDIA RTX 3070 Ti 8 GB
    T3: Persian Q4_K
    S3Gen: Q8_0
    diffusion steps: 10
    placement: full GPU / Vulkan

Measured on this machine:
    warm ready-to-speak: ~1.95 s
    resident VRAM: ~1.21 GiB
    generation peak delta: ~1.43 GiB
    normal warm unload: ~0.08 s

Design rules:
    - lazy-load; constructing this object starts nothing
    - keep TTS resident temporarily during a voice conversation
    - idle resident resources are disposable
    - only manage the CrispASR process started by this runtime
    - every admitted TTS workload is registered as QRONOS_OWNED
    - Qronos-owned load must not later be mistaken for user-owned pressure
    - all Chatterbox cache/temp data stays under runtime/chatterbox
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from enum import Enum
from pathlib import Path
from typing import Callable
from uuid import uuid4

from core.config import CONFIG
from core.resource_guard import read_gpu_status
from core.resource_ownership import (
    GLOBAL_RESOURCE_LEDGER,
    ResourceBudget,
    ResourceLedger,
    ResourceOwner,
    Reservation,
    WorkloadPriority,
    WorkloadState,
)
from core.voice_runtime import Utterance, VoiceRuntime


DEFAULT_ROOT = CONFIG.paths.root / "runtime" / "chatterbox"

DEFAULT_EXECUTABLE = (
    DEFAULT_ROOT
    / "bin"
    / "crispasr-windows-x86_64-vulkan"
    / "crispasr.exe"
)

DEFAULT_VOICE_MODEL = (
    DEFAULT_ROOT
    / "models"
    / "t3-fa-q4_k.gguf"
)

DEFAULT_CODEC_MODEL = (
    DEFAULT_ROOT
    / "models"
    / "chatterbox-s3gen-q8_0.gguf"
)

DEFAULT_TEMP_DIR = DEFAULT_ROOT / "temp"
DEFAULT_CACHE_DIR = DEFAULT_ROOT / "cache"

DEFAULT_HOST = "127.0.0.1"

DEFAULT_STEPS = 10
DEFAULT_IDLE_SECONDS = 120.0
DEFAULT_STARTUP_TIMEOUT = 180.0
DEFAULT_REQUEST_TIMEOUT = 120.0
DEFAULT_RETRY_SECONDS = 0.10

# Local RTX 3070 Ti benchmark:
# maximum measured generation delta was 1431 MiB.
#
# Use a rounded reservation instead of treating one benchmark sample as an
# exact physical maximum.
TTS_GENERATION_RESERVATION_MB = 1536

# Additional admission margin. This is checked before creating new Qronos
# load. It is not a self-eviction threshold after TTS starts.
TTS_ADMISSION_HEADROOM_MB = 1024

TTS_MIN_FREE_TO_START_MB = (
    TTS_GENERATION_RESERVATION_MB
    + TTS_ADMISSION_HEADROOM_MB
)

CRITICAL_GPU_TEMP_C = 85

MAX_TEXT_CHARACTERS = 2000

NORMAL_TERMINATE_GRACE_SECONDS = 0.50
EMERGENCY_KILL_WAIT_SECONDS = 1.0

TTS_WORKLOAD_NAME = "chatterbox_tts"


class VoiceUnavailable(RuntimeError):
    """The local voice runtime cannot safely or correctly speak."""


class VoiceLifecycleState(str, Enum):
    UNLOADED = "unloaded"
    STARTING = "starting"
    READY = "ready"
    SPEAKING = "speaking"
    HOT_IDLE = "hot_idle"
    RELEASING = "releasing"
    FAILED = "failed"


AdmissionCheck = Callable[[ResourceBudget], None]


class ChatterboxRuntime(VoiceRuntime):
    """
    Persian Chatterbox behind one locally-owned CrispASR server process.

    The runtime is lazy: no executable or model is loaded until prewarm() or
    speak_to_file() is called.

    Once admitted, the TTS workload is registered in the shared ResourceLedger
    as QRONOS_OWNED. That reservation remains active while the runtime is
    starting, speaking, or resident, and is released when the runtime unloads.
    """

    def __init__(
        self,
        executable_path: str | Path = DEFAULT_EXECUTABLE,
        voice_model_path: str | Path = DEFAULT_VOICE_MODEL,
        codec_model_path: str | Path = DEFAULT_CODEC_MODEL,
        temp_dir: str | Path = DEFAULT_TEMP_DIR,
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
        steps: int = DEFAULT_STEPS,
        host: str = DEFAULT_HOST,
        port: int | None = None,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        admission_check: AdmissionCheck | None = None,
        resource_ledger: ResourceLedger | None = None,
    ) -> None:
        if steps <= 0:
            raise ValueError("steps must be greater than zero")

        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be greater than zero")

        self.executable_path = Path(executable_path)
        self.voice_model_path = Path(voice_model_path)
        self.codec_model_path = Path(codec_model_path)

        self.temp_dir = Path(temp_dir)
        self.cache_dir = Path(cache_dir)

        self.steps = steps
        self.host = host
        self.port = port

        self.idle_seconds = idle_seconds
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.retry_seconds = retry_seconds

        self._ledger = (
            resource_ledger
            if resource_ledger is not None
            else GLOBAL_RESOURCE_LEDGER
        )

        self._admission_check = (
            admission_check
            if admission_check is not None
            else self._default_admission_check
        )

        self._process: subprocess.Popen | None = None
        self._active_port: int | None = None
        self._reservation_id: str | None = None

        self._state = VoiceLifecycleState.UNLOADED
        self._last_activity = 0.0

        self._lock = threading.RLock()
        self._speak_lock = threading.Lock()
        self._idle_timer: threading.Timer | None = None

        # Single-flight startup coordination.
        #
        # Exactly one caller is allowed to launch/load CrispASR. Any other
        # caller that arrives while the runtime is STARTING waits on the same
        # event and reuses the result instead of starting a second process or
        # creating a second resource reservation.
        self._startup_event = threading.Event()
        self._startup_event.set()
        self._startup_error: Exception | None = None

    @property
    def state(self) -> VoiceLifecycleState:
        with self._lock:
            return self._state

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return (
                self._process is not None
                and self._process.poll() is None
                and self._state
                in {
                    VoiceLifecycleState.READY,
                    VoiceLifecycleState.SPEAKING,
                    VoiceLifecycleState.HOT_IDLE,
                }
            )

    @property
    def process_id(self) -> int | None:
        """
        PID owned by this runtime.

        Qronos must never terminate arbitrary crispasr.exe processes by name.
        """
        with self._lock:
            if self._process is None:
                return None

            if self._process.poll() is not None:
                return None

            return self._process.pid

    @property
    def active_port(self) -> int | None:
        with self._lock:
            return self._active_port

    @property
    def reservation_id(self) -> str | None:
        with self._lock:
            return self._reservation_id

    @property
    def reservation(self) -> Reservation | None:
        reservation_id = self.reservation_id

        if reservation_id is None:
            return None

        return self._ledger.get(reservation_id)

    @property
    def resource_budget(self) -> ResourceBudget:
        return ResourceBudget(
            vram_mb=TTS_GENERATION_RESERVATION_MB,
            ram_mb=0,
        )

    def health_check(self) -> bool:
        """
        Check installation only.

        This method deliberately does not start CrispASR or load a model.
        """
        return (
            self.executable_path.is_file()
            and self.voice_model_path.is_file()
            and self.codec_model_path.is_file()
        )

    def prewarm(self) -> None:
        """
        Make the voice genuinely ready without producing user-visible output.

        Safe to call when already resident.
        """
        self._ensure_running()

        with self._lock:
            if self._state is VoiceLifecycleState.READY:
                self._state = VoiceLifecycleState.HOT_IDLE

            self._update_reservation_state_locked(
                WorkloadState.HOT_IDLE
            )

            self._last_activity = time.monotonic()
            self._arm_idle_timer_locked()

    def speak_to_file(
        self,
        text: str,
        destination: str | Path | None = None,
    ) -> Utterance:
        spoken = self._validate_text(text)

        # One generated utterance at a time per runtime instance.
        with self._speak_lock:
            self._ensure_running()

            with self._lock:
                self._cancel_idle_timer_locked()
                self._state = VoiceLifecycleState.SPEAKING
                self._update_reservation_state_locked(
                    WorkloadState.RUNNING
                )

            started = time.perf_counter()

            try:
                audio = self._request_speech(spoken)
            except Exception:
                with self._lock:
                    process_alive = (
                        self._process is not None
                        and self._process.poll() is None
                    )

                    if process_alive:
                        self._state = VoiceLifecycleState.READY
                        self._update_reservation_state_locked(
                            WorkloadState.RUNNING
                        )
                    else:
                        self._state = VoiceLifecycleState.FAILED
                        self._fail_reservation_locked()

                raise

            took_seconds = time.perf_counter() - started

            target = self._resolve_destination(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(audio)

            audio_seconds = _wav_seconds(target)

            with self._lock:
                self._last_activity = time.monotonic()
                self._state = VoiceLifecycleState.HOT_IDLE
                self._update_reservation_state_locked(
                    WorkloadState.HOT_IDLE
                )
                self._arm_idle_timer_locked()

            return Utterance(
                audio_path=target,
                audio_seconds=audio_seconds,
                took_seconds=took_seconds,
                text=spoken,
            )

    def release(self) -> None:
        """
        Normal release.

        Give the card back quickly and release the shared Qronos reservation.
        """
        self._release(emergency=False)

    def emergency_release(self) -> None:
        """
        Immediate resource eviction for a hard safety condition.
        """
        self._release(emergency=True)

    def close(self) -> None:
        self.release()

    def __enter__(self) -> "ChatterboxRuntime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def _ensure_running(self) -> None:
        """
        Ensure one shared Chatterbox server is ready.

        Startup is single-flight:
        - READY / SPEAKING / HOT_IDLE returns immediately.
        - STARTING joins the already-running startup attempt.
        - UNLOADED / dead / FAILED starts exactly one new attempt.

        This prevents duplicate CrispASR processes, duplicate reservations,
        and accidental VRAM spikes when prewarm() and speak_to_file() race.
        """
        join_event: threading.Event | None = None
        should_start = False

        with self._lock:
            self._cancel_idle_timer_locked()

            process_alive = (
                self._process is not None
                and self._process.poll() is None
            )

            if (
                process_alive
                and self._state
                in {
                    VoiceLifecycleState.READY,
                    VoiceLifecycleState.SPEAKING,
                    VoiceLifecycleState.HOT_IDLE,
                }
            ):
                return

            if self._state is VoiceLifecycleState.STARTING:
                join_event = self._startup_event
            else:
                # A dead prior process must not leave an active reservation
                # behind before a new startup attempt begins.
                if (
                    self._process is not None
                    and self._process.poll() is not None
                ):
                    self._fail_reservation_locked()

                self._process = None
                self._active_port = None
                self._state = VoiceLifecycleState.STARTING
                self._startup_error = None
                self._startup_event.clear()

                should_start = True
                join_event = self._startup_event

        if not should_start:
            assert join_event is not None

            completed = join_event.wait(
                timeout=self.startup_timeout
            )

            if not completed:
                raise VoiceUnavailable(
                    "Timed out while waiting for the existing "
                    "Chatterbox startup attempt."
                )

            with self._lock:
                process_alive = (
                    self._process is not None
                    and self._process.poll() is None
                )

                if (
                    process_alive
                    and self._state
                    in {
                        VoiceLifecycleState.READY,
                        VoiceLifecycleState.SPEAKING,
                        VoiceLifecycleState.HOT_IDLE,
                    }
                ):
                    return

                startup_error = self._startup_error

            if startup_error is not None:
                raise VoiceUnavailable(
                    "The shared Chatterbox startup attempt failed: "
                    f"{startup_error}"
                ) from startup_error

            raise VoiceUnavailable(
                "The shared Chatterbox startup attempt finished "
                "without a usable voice runtime."
            )

        try:
            if not self.health_check():
                raise VoiceUnavailable(
                    self._missing_components_message()
                )

            budget = self.resource_budget

            # Admission checks external capacity BEFORE this workload
            # contributes its own expected GPU load.
            self._admission_check(budget)

            # Register expected load before process launch so the Governor has
            # an explanation for Qronos-owned GPU/VRAM growth.
            with self._lock:
                self._create_reservation_locked()

            chosen_port = (
                self.port
                if self.port is not None
                else _choose_free_loopback_port(
                    self.host
                )
            )

            command = [
                str(self.executable_path),
                "--server",
                "--host",
                self.host,
                "--port",
                str(chosen_port),
                "--backend",
                "chatterbox",
                "-m",
                str(self.voice_model_path),
                "--codec-model",
                str(self.codec_model_path),
                "--tts-steps",
                str(self.steps),
            ]

            environment = (
                self._runtime_environment()
            )

            try:
                process = self._launch_process(
                    command=command,
                    environment=environment,
                )
            except Exception:
                with self._lock:
                    self._state = (
                        VoiceLifecycleState.FAILED
                    )
                    self._fail_reservation_locked()
                raise

            with self._lock:
                self._process = process
                self._active_port = chosen_port
                self._update_reservation_state_locked(
                    WorkloadState.STARTING
                )

            try:
                self._wait_until_speakable(
                    process
                )
            except Exception:
                with self._lock:
                    self._state = (
                        VoiceLifecycleState.FAILED
                    )

                self._release(
                    emergency=True,
                    startup_failed=True,
                )
                raise

            with self._lock:
                self._state = (
                    VoiceLifecycleState.READY
                )
                self._last_activity = (
                    time.monotonic()
                )
                self._update_reservation_state_locked(
                    WorkloadState.RUNNING
                )
                self._startup_error = None

        except Exception as exc:
            with self._lock:
                self._startup_error = exc

                if (
                    self._state
                    is VoiceLifecycleState.STARTING
                ):
                    self._state = (
                        VoiceLifecycleState.FAILED
                    )

            raise

        finally:
            # Every waiter must be released whether startup succeeds or fails.
            self._startup_event.set()

    def _wait_until_speakable(
        self,
        process: subprocess.Popen,
    ) -> None:
        """
        Readiness means actual TTS works.

        CrispASR may open its TCP port before Chatterbox is fully usable.
        A tiny Persian synthesis proves the full pipeline.
        """
        deadline = (
            time.monotonic()
            + self.startup_timeout
        )

        last_error: Exception | None = None

        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise VoiceUnavailable(
                    "CrispASR stopped during voice startup "
                    f"(exit code {process.returncode})."
                )

            try:
                audio = self._request_speech("سلام")

                if len(audio) > 1000:
                    return

            except VoiceUnavailable as exc:
                last_error = exc

            time.sleep(self.retry_seconds)

        detail = (
            f" Last error: {last_error}"
            if last_error is not None
            else ""
        )

        raise VoiceUnavailable(
            "Chatterbox did not become ready within "
            f"{self.startup_timeout:.1f} seconds."
            + detail
        )

    def _request_speech(
        self,
        text: str,
    ) -> bytes:
        port = self.active_port

        if port is None:
            raise VoiceUnavailable(
                "The local voice server has no active port."
            )

        body = json.dumps(
            {
                "model": "chatterbox",
                "input": text,
                "response_format": "wav",
            },
            ensure_ascii=False,
        ).encode("utf-8")

        request = urllib.request.Request(
            f"http://{self.host}:{port}/v1/audio/speech",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.request_timeout,
            ) as response:
                audio = response.read()

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(
                "utf-8",
                errors="replace",
            )[:500]

            raise VoiceUnavailable(
                f"Chatterbox HTTP error {exc.code}: {detail}"
            ) from exc

        except urllib.error.URLError as exc:
            raise VoiceUnavailable(
                f"Chatterbox server unavailable: {exc.reason}"
            ) from exc

        if len(audio) <= 1000:
            raise VoiceUnavailable(
                "Chatterbox returned an invalid or empty audio response."
            )

        return audio

    def _release(
        self,
        emergency: bool,
        startup_failed: bool = False,
    ) -> None:
        with self._lock:
            self._cancel_idle_timer_locked()

            process = self._process

            self._process = None
            self._active_port = None
            self._state = VoiceLifecycleState.RELEASING

            if self._reservation_id is not None:
                try:
                    self._update_reservation_state_locked(
                        WorkloadState.EVICTING
                    )
                except Exception:
                    pass

        if process is not None and process.poll() is None:
            try:
                if emergency:
                    process.kill()

                    try:
                        process.wait(
                            timeout=EMERGENCY_KILL_WAIT_SECONDS
                        )
                    except subprocess.TimeoutExpired:
                        pass

                else:
                    process.terminate()

                    try:
                        process.wait(
                            timeout=NORMAL_TERMINATE_GRACE_SECONDS
                        )
                    except subprocess.TimeoutExpired:
                        process.kill()

                        try:
                            process.wait(
                                timeout=EMERGENCY_KILL_WAIT_SECONDS
                            )
                        except subprocess.TimeoutExpired:
                            pass
            finally:
                pass

        with self._lock:
            if startup_failed:
                self._fail_reservation_locked()
                self._state = VoiceLifecycleState.FAILED
            else:
                self._release_reservation_locked()
                self._state = VoiceLifecycleState.UNLOADED

    def _release_if_idle(self) -> None:
        with self._lock:
            if self._state is not VoiceLifecycleState.HOT_IDLE:
                return

            elapsed = (
                time.monotonic()
                - self._last_activity
            )

            if elapsed < self.idle_seconds:
                self._arm_idle_timer_locked()
                return

        self.release()

    def _arm_idle_timer_locked(self) -> None:
        self._cancel_idle_timer_locked()

        timer = threading.Timer(
            self.idle_seconds,
            self._release_if_idle,
        )

        timer.name = "qronos-tts-idle"
        timer.daemon = True

        self._idle_timer = timer
        timer.start()

    def _cancel_idle_timer_locked(self) -> None:
        if self._idle_timer is None:
            return

        self._idle_timer.cancel()
        self._idle_timer = None

    def _runtime_environment(
        self,
    ) -> dict[str, str]:
        """
        CrispASR environment.

        Chatterbox cache and temporary storage stays under runtime/chatterbox.
        """
        self.temp_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.cache_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        hub_cache = self.cache_dir / "hub"
        hub_cache.mkdir(
            parents=True,
            exist_ok=True,
        )

        environment = dict(os.environ)

        environment["TEMP"] = str(
            self.temp_dir
        )
        environment["TMP"] = str(
            self.temp_dir
        )

        environment["HF_HOME"] = str(
            self.cache_dir
        )

        environment["HUGGINGFACE_HUB_CACHE"] = str(
            hub_cache
        )

        environment["XDG_CACHE_HOME"] = str(
            self.cache_dir
        )

        environment[
            "CRISPASR_CHATTERBOX_FORCE_GPU"
        ] = "1"

        return environment

    def _default_admission_check(
        self,
        budget: ResourceBudget,
    ) -> None:
        """
        Temporary local safety gate.

        This runs before creating new Qronos GPU load. It never watches the
        expected post-admission increase and then treats that increase as
        external pressure.
        """
        try:
            gpu = read_gpu_status()
        except Exception:
            return

        if gpu is None:
            return

        if (
            gpu.temperature_c is not None
            and gpu.temperature_c
            >= CRITICAL_GPU_TEMP_C
        ):
            raise VoiceUnavailable(
                "Qronos will not start TTS because the GPU is at "
                f"{gpu.temperature_c} C."
            )

        if (
            gpu.vram_total_mb is None
            or gpu.vram_used_mb is None
        ):
            return

        free_vram = max(
            0,
            gpu.vram_total_mb
            - gpu.vram_used_mb,
        )

        required_free = (
            budget.vram_mb
            + TTS_ADMISSION_HEADROOM_MB
        )

        if free_vram < required_free:
            raise VoiceUnavailable(
                "Qronos will not start a new TTS workload. "
                f"{free_vram} MiB VRAM is free; "
                f"{required_free} MiB is required by the "
                "current TTS admission policy."
            )

    def _create_reservation_locked(
        self,
    ) -> None:
        if self._reservation_id is not None:
            existing = self._ledger.get(
                self._reservation_id
            )

            if (
                existing is not None
                and existing.state
                not in {
                    WorkloadState.RELEASED,
                    WorkloadState.FAILED,
                }
            ):
                return

            self._reservation_id = None

        reservation = self._ledger.reserve(
            owner=ResourceOwner.QRONOS,
            workload=TTS_WORKLOAD_NAME,
            priority=WorkloadPriority.VOICE_OUTPUT,
            budget=self.resource_budget,
            state=WorkloadState.ADMITTED,
        )

        self._reservation_id = (
            reservation.reservation_id
        )

    def _update_reservation_state_locked(
        self,
        state: WorkloadState,
    ) -> None:
        if self._reservation_id is None:
            return

        reservation = self._ledger.get(
            self._reservation_id
        )

        if reservation is None:
            self._reservation_id = None
            return

        if reservation.state in {
            WorkloadState.RELEASED,
            WorkloadState.FAILED,
        }:
            return

        self._ledger.update_state(
            self._reservation_id,
            state,
        )

    def _release_reservation_locked(
        self,
    ) -> None:
        reservation_id = self._reservation_id
        self._reservation_id = None

        if reservation_id is None:
            return

        reservation = self._ledger.get(
            reservation_id
        )

        if reservation is None:
            return

        if reservation.state in {
            WorkloadState.RELEASED,
            WorkloadState.FAILED,
        }:
            return

        self._ledger.release(
            reservation_id
        )

    def _fail_reservation_locked(
        self,
    ) -> None:
        reservation_id = self._reservation_id
        self._reservation_id = None

        if reservation_id is None:
            return

        reservation = self._ledger.get(
            reservation_id
        )

        if reservation is None:
            return

        if reservation.state in {
            WorkloadState.RELEASED,
            WorkloadState.FAILED,
        }:
            return

        self._ledger.fail(
            reservation_id
        )

    def _launch_process(
        self,
        command: list[str],
        environment: dict[str, str],
    ) -> subprocess.Popen:
        creation_flags = 0

        if os.name == "nt":
            creation_flags = getattr(
                subprocess,
                "CREATE_NO_WINDOW",
                0,
            )

        return subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            cwd=str(
                self.executable_path.parent
            ),
            creationflags=creation_flags,
        )

    def _validate_text(
        self,
        text: str,
    ) -> str:
        spoken = (
            text or ""
        ).strip()

        if not spoken:
            raise ValueError(
                "There is nothing to say."
            )

        if len(spoken) > MAX_TEXT_CHARACTERS:
            raise ValueError(
                "A single TTS utterance cannot exceed "
                f"{MAX_TEXT_CHARACTERS} characters."
            )

        return spoken

    def _resolve_destination(
        self,
        destination: str | Path | None,
    ) -> Path:
        if destination is not None:
            return Path(destination)

        return (
            self.temp_dir
            / f"qronos_voice_{uuid4().hex}.wav"
        )

    def _missing_components_message(
        self,
    ) -> str:
        missing = []

        for item in (
            self.executable_path,
            self.voice_model_path,
            self.codec_model_path,
        ):
            if not item.is_file():
                missing.append(
                    str(item)
                )

        return (
            "Qronos TTS is missing required local components: "
            + ", ".join(missing)
        )


def _choose_free_loopback_port(
    host: str,
) -> int:
    """
    Ask Windows for an unused local TCP port.
    """
    with socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    ) as probe:
        probe.bind(
            (host, 0)
        )

        return int(
            probe.getsockname()[1]
        )


def _wav_seconds(
    path: Path,
) -> float:
    try:
        with wave.open(
            str(path),
            "rb",
        ) as handle:
            rate = handle.getframerate()

            if rate <= 0:
                return 0.0

            return (
                handle.getnframes()
                / rate
            )

    except (
        wave.Error,
        OSError,
    ):
        return 0.0
