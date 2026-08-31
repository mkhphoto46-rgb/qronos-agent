from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from core.whisper_hybrid_runtime import WhisperHybridRuntime


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

    def health_check(self) -> bool:
        return True

    def prepare(self) -> None:
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

    def shutdown(self) -> None:
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

    def health_check(self) -> bool:
        return self.healthy

    def transcribe_file(
        self,
        audio_path,
        language="auto",
    ) -> str:
        self.calls += 1
        return self.transcript


def make_audio(tmp_path: Path) -> Path:
    audio = tmp_path / "command.wav"
    audio.write_bytes(b"RIFF-test")
    return audio


class TestWhisperHybridRuntime(unittest.TestCase):
    def test_server_is_primary_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)

            server = FakeServerRuntime(transcript="سلام")
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

            self.assertEqual(transcript, "سلام")
            self.assertEqual(
                runtime.last_backend,
                "whisper_server",
            )
            self.assertEqual(server.transcribe_calls, 1)
            self.assertEqual(fallback.calls, 0)

    def test_cli_is_used_when_server_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)

            server = FakeServerRuntime(
                error=RuntimeError("server failed")
            )

            fallback = FakeFallbackRuntime(
                transcript="سلام از fallback"
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

            self.assertEqual(
                transcript,
                "سلام از fallback",
            )
            self.assertEqual(
                runtime.last_backend,
                "whisper_cli_fallback",
            )
            self.assertEqual(fallback.calls, 1)
            self.assertEqual(server.shutdown_calls, 1)

    def test_warm_async_starts_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = FakeServerRuntime()

            runtime = WhisperHybridRuntime(
                server_runtime=server,
                fallback_runtime=FakeFallbackRuntime(),
                temp_dir=Path(temp_dir),
            )

            runtime.warm_async()

            deadline = time.monotonic() + 1.0

            while (
                server.prepare_calls == 0
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            self.assertEqual(server.prepare_calls, 1)

    def test_warm_failure_does_not_raise_to_caller(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = FakeServerRuntime(
                error=RuntimeError("warm failed")
            )

            runtime = WhisperHybridRuntime(
                server_runtime=server,
                fallback_runtime=FakeFallbackRuntime(),
                temp_dir=Path(temp_dir),
            )

            runtime.warm_async()

            deadline = time.monotonic() + 1.0

            while (
                runtime.warm_error is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            self.assertIsInstance(
                runtime.warm_error,
                RuntimeError,
            )

    def test_shutdown_cancels_a_stale_warm_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            prepare_started = threading.Event()
            allow_prepare_to_finish = threading.Event()

            class DelayedServerRuntime(FakeServerRuntime):
                def prepare(self) -> None:
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
                temp_dir=Path(temp_dir),
            )

            runtime.warm_async()

            self.assertTrue(
                prepare_started.wait(timeout=1.0)
            )

            runtime.shutdown()
            allow_prepare_to_finish.set()

            deadline = time.monotonic() + 1.0

            while (
                server.is_running
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            warm_thread = runtime._warm_thread

            if warm_thread is not None:
                warm_thread.join(timeout=1.0)

            self.assertFalse(server.is_running)
            self.assertGreaterEqual(
                server.shutdown_calls,
                2,
            )

    def test_shutdown_releases_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = FakeServerRuntime()

            runtime = WhisperHybridRuntime(
                server_runtime=server,
                fallback_runtime=FakeFallbackRuntime(),
                temp_dir=Path(temp_dir),
            )

            runtime.shutdown()

            self.assertEqual(server.shutdown_calls, 1)


if __name__ == "__main__":
    unittest.main()
