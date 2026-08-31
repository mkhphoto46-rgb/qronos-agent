from __future__ import annotations

import threading
from pathlib import Path

from core.config import CONFIG
from core.speech_runtime import SpeechRuntime
from core.whisper_cpp_runtime import WhisperCppRuntime
from core.whisper_server_runtime import WhisperServerRuntime


class WhisperHybridRuntime(SpeechRuntime):
    """
    Reliability-first Qronos STT runtime.

    Primary path:
        persistent whisper-server.exe

    Fallback:
        existing whisper-cli.exe per request

    The persistent server can be warmed asynchronously while the user is
    speaking. If the server cannot start or a server request fails, Qronos
    retains the previously validated CLI transcription path.
    """

    def __init__(
        self,
        server_runtime: WhisperServerRuntime | None = None,
        fallback_runtime: WhisperCppRuntime | None = None,
        temp_dir: str | Path = CONFIG.paths.temp,
    ) -> None:
        self.temp_dir = Path(
            temp_dir
        )

        self.server_runtime = (
            server_runtime
            if server_runtime is not None
            else WhisperServerRuntime(
                log_dir=self.temp_dir,
            )
        )

        self.fallback_runtime = (
            fallback_runtime
            if fallback_runtime is not None
            else WhisperCppRuntime(
                temp_dir=self.temp_dir,
            )
        )

        self._warm_lock = (
            threading.Lock()
        )

        self._warm_thread: threading.Thread | None = None
        self._warm_error: Exception | None = None

        # Monotonic lifecycle generation used to cancel stale warm workers.
        #
        # Without this guard, shutdown() could run before a newly scheduled
        # warm thread actually enters prepare(). The stale worker could then
        # start whisper-server.exe after the voice turn had already ended,
        # leaving GPU memory resident while Qronos was idle.
        self._lifecycle_generation = 0

        self.last_backend: str | None = None

    def health_check(self) -> bool:
        return (
            self.server_runtime.health_check()
            or self.fallback_runtime.health_check()
        )

    @property
    def server_running(self) -> bool:
        return (
            self.server_runtime.is_running
        )

    @property
    def warm_error(
        self,
    ) -> Exception | None:
        return self._warm_error

    def prepare(
        self,
    ) -> None:
        self.server_runtime.prepare()

    def _warm_worker(
        self,
        generation: int,
    ) -> None:
        try:
            self.server_runtime.prepare()

            with self._warm_lock:
                stale = (
                    generation
                    != self._lifecycle_generation
                )

            if stale:
                # shutdown() happened while this worker was starting.
                # Never allow a stale warm request to resurrect the server.
                self.server_runtime.shutdown()
                return

            self._warm_error = None

        except Exception as exc:
            # Warming is opportunistic.
            #
            # A failure here must not break the voice turn because
            # transcribe_file() still has the validated CLI fallback.
            self._warm_error = exc

    def warm_async(
        self,
    ) -> None:
        if self.server_runtime.is_running:
            return

        with self._warm_lock:
            thread = self._warm_thread

            if (
                thread is not None
                and thread.is_alive()
            ):
                return

            generation = (
                self._lifecycle_generation
            )

            thread = threading.Thread(
                target=self._warm_worker,
                args=(
                    generation,
                ),
                name="qronos-whisper-warm",
                daemon=True,
            )

            self._warm_thread = thread
            thread.start()

    def transcribe_file(
        self,
        audio_path: str | Path,
        language: str = "auto",
    ) -> str:
        source = Path(
            audio_path
        )

        if not source.is_file():
            raise FileNotFoundError(
                f"Audio file was not found: {source}"
            )

        language = (
            language or ""
        ).strip()

        if not language:
            raise ValueError(
                "language must not be empty."
            )

        try:
            transcript = (
                self.server_runtime.transcribe_file(
                    source,
                    language=language,
                )
            )

            self.last_backend = (
                "whisper_server"
            )

            return transcript

        except Exception as server_exc:
            # Release any half-started or failed persistent process before
            # falling back. This avoids leaving VRAM reserved by a degraded
            # server while the CLI path is running.
            try:
                self.server_runtime.shutdown()
            except Exception:
                pass

            if not self.fallback_runtime.health_check():
                raise RuntimeError(
                    "Persistent Whisper STT failed and "
                    "the CLI fallback is unavailable."
                ) from server_exc

            transcript = (
                self.fallback_runtime.transcribe_file(
                    source,
                    language=language,
                )
            )

            self.last_backend = (
                "whisper_cli_fallback"
            )

            return transcript

    def shutdown(
        self,
    ) -> None:
        # Invalidate every already-scheduled warm worker before touching the
        # server process. A stale worker that finishes later will observe the
        # generation mismatch and immediately release anything it started.
        with self._warm_lock:
            self._lifecycle_generation += 1

        self.server_runtime.shutdown()

    def __enter__(
        self,
    ) -> "WhisperHybridRuntime":
        self.prepare()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.shutdown()

    def __del__(
        self,
    ) -> None:
        try:
            self.shutdown()
        except Exception:
            pass
