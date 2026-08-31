from __future__ import annotations

import threading
import time
from pathlib import Path

from core.whisper_hybrid_runtime import (
    WhisperHybridRuntime,
)


class FakeServerRuntime:
    def __init__(
        self,
        transcript: str = "server transcript",
        error: Exception | None = None,
    ) -> None:
        self.transcript = transcript
        self.error = error

        self.prepare_calls = 0
        self.transcribe_calls = 0
        self.shutdown_calls = 0

        self.is_running = False

    def health_check(
        self,
    ) -> bool:
        return True

    def prepare(
        self,
    ) -> None:
        self.prepare_calls += 1

        if self.error is not None:
            raise self.error

        self.is_running = True

    def transcribe_file(
        self,
        audio_path,
        language="auto",
    ) -> str:
        self.transcribe_calls += 1

        if self.error is not None:
            raise self.error

        self.is_running = True

        return self.transcript

    def shutdown(
        self,
    ) -> None:
        self.shutdown_calls += 1
        self.is_running = False


class FakeFallbackRuntime:
    def __init__(
        self,
        transcript: str = "fallback transcript",
        healthy: bool = True,
    ) -> None:
        self.transcript = transcript
        self.healthy = healthy

        self.calls = 0

    def health_check(
        self,
    ) -> bool:
        return self.healthy

    def transcribe_file(
        self,
        audio_path,
        language="auto",
    ) -> str:
        self.calls += 1
        return self.transcript


def make_audio(
    tmp_path: Path,
) -> Path:
    audio = (
        tmp_path
        / "command.wav"
    )

    audio.write_bytes(
        b"RIFF-test"
    )

    return audio


def test_server_is_primary_backend(
    tmp_path: Path,
) -> None:
    server = FakeServerRuntime(
        transcript="سلام",
    )

    fallback = FakeFallbackRuntime()

    runtime = WhisperHybridRuntime(
        server_runtime=server,
        fallback_runtime=fallback,
        temp_dir=tmp_path,
    )

    transcript = runtime.transcribe_file(
        make_audio(tmp_path),
        language="fa",
    )

    assert transcript == "سلام"
    assert runtime.last_backend == "whisper_server"
    assert server.transcribe_calls == 1
    assert fallback.calls == 0


def test_cli_is_used_when_server_fails(
    tmp_path: Path,
) -> None:
    server = FakeServerRuntime(
        error=RuntimeError(
            "server failed"
        )
    )

    fallback = FakeFallbackRuntime(
        transcript="سلام از fallback",
    )

    runtime = WhisperHybridRuntime(
        server_runtime=server,
        fallback_runtime=fallback,
        temp_dir=tmp_path,
    )

    transcript = runtime.transcribe_file(
        make_audio(tmp_path),
        language="fa",
    )

    assert transcript == "سلام از fallback"
    assert (
        runtime.last_backend
        == "whisper_cli_fallback"
    )

    assert fallback.calls == 1
    assert server.shutdown_calls == 1


def test_warm_async_starts_server(
    tmp_path: Path,
) -> None:
    server = FakeServerRuntime()

    runtime = WhisperHybridRuntime(
        server_runtime=server,
        fallback_runtime=FakeFallbackRuntime(),
        temp_dir=tmp_path,
    )

    runtime.warm_async()

    deadline = (
        time.monotonic()
        + 1.0
    )

    while (
        server.prepare_calls == 0
        and time.monotonic() < deadline
    ):
        time.sleep(
            0.01
        )

    assert server.prepare_calls == 1


def test_warm_failure_does_not_raise_to_caller(
    tmp_path: Path,
) -> None:
    server = FakeServerRuntime(
        error=RuntimeError(
            "warm failed"
        )
    )

    runtime = WhisperHybridRuntime(
        server_runtime=server,
        fallback_runtime=FakeFallbackRuntime(),
        temp_dir=tmp_path,
    )

    runtime.warm_async()

    deadline = (
        time.monotonic()
        + 1.0
    )

    while (
        runtime.warm_error is None
        and time.monotonic() < deadline
    ):
        time.sleep(
            0.01
        )

    assert isinstance(
        runtime.warm_error,
        RuntimeError,
    )


def test_shutdown_cancels_a_stale_warm_worker(
    tmp_path: Path,
) -> None:
    prepare_started = threading.Event()
    allow_prepare_to_finish = threading.Event()

    class DelayedServerRuntime(
        FakeServerRuntime
    ):
        def prepare(
            self,
        ) -> None:
            self.prepare_calls += 1
            prepare_started.set()

            allow_prepare_to_finish.wait(
                timeout=1.0
            )

            self.is_running = True

    server = DelayedServerRuntime()

    runtime = WhisperHybridRuntime(
        server_runtime=server,
        fallback_runtime=FakeFallbackRuntime(),
        temp_dir=tmp_path,
    )

    runtime.warm_async()

    assert prepare_started.wait(
        timeout=1.0
    )

    runtime.shutdown()

    allow_prepare_to_finish.set()

    deadline = (
        time.monotonic()
        + 1.0
    )

    while (
        server.is_running
        and time.monotonic() < deadline
    ):
        time.sleep(
            0.01
        )

    warm_thread = runtime._warm_thread

    if warm_thread is not None:
        warm_thread.join(
            timeout=1.0
        )

    assert server.is_running is False

    # One call is the explicit shutdown above. The second is the stale warm
    # worker cleaning up the process it finished starting afterwards.
    assert server.shutdown_calls >= 2


def test_shutdown_releases_server(
    tmp_path: Path,
) -> None:
    server = FakeServerRuntime()

    runtime = WhisperHybridRuntime(
        server_runtime=server,
        fallback_runtime=FakeFallbackRuntime(),
        temp_dir=tmp_path,
    )

    runtime.shutdown()

    assert server.shutdown_calls == 1
