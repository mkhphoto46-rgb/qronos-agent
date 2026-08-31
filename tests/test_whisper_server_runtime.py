from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.whisper_server_runtime import WhisperServerRuntime


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._raw = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def read(self) -> bytes:
        return self._raw


def build_runtime(tmp_path: Path) -> WhisperServerRuntime:
    executable = tmp_path / "whisper-server.exe"
    model = tmp_path / "model.bin"

    executable.write_bytes(b"server")
    model.write_bytes(b"model")

    return WhisperServerRuntime(
        executable_path=executable,
        model_path=model,
        log_dir=tmp_path,
        startup_timeout_seconds=0.5,
        request_timeout_seconds=0.5,
    )


class TestWhisperServerRuntime(unittest.TestCase):
    def test_health_check_requires_server_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)

            runtime = WhisperServerRuntime(
                executable_path=tmp_path / "missing.exe",
                model_path=tmp_path / "missing.bin",
                log_dir=tmp_path,
            )

            self.assertFalse(runtime.health_check())

    def test_rejects_non_localhost_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)

            with self.assertRaisesRegex(ValueError, "127.0.0.1"):
                WhisperServerRuntime(
                    executable_path=tmp_path / "server.exe",
                    model_path=tmp_path / "model.bin",
                    host="0.0.0.0",
                )

    def test_command_preserves_decoder_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = build_runtime(Path(temp_dir))
            command = runtime._build_command()

            self.assertIn("-bo", command)
            self.assertEqual(command[command.index("-bo") + 1], "5")

            self.assertIn("-bs", command)
            self.assertEqual(command[command.index("-bs") + 1], "5")

            self.assertIn("--host", command)
            self.assertEqual(
                command[command.index("--host") + 1],
                "127.0.0.1",
            )

    def test_prepare_starts_server_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = build_runtime(Path(temp_dir))

            fake_process = FakeProcess()
            popen = MagicMock(return_value=fake_process)

            readiness = iter((False, True))

            with patch(
                "core.whisper_server_runtime.subprocess.Popen",
                popen,
            ), patch.object(
                runtime,
                "_server_is_ready",
                side_effect=lambda: next(readiness, True),
            ):
                runtime.prepare()

                self.assertEqual(popen.call_count, 1)

                runtime.prepare()

                self.assertEqual(popen.call_count, 1)

                runtime.shutdown()

    def test_transcribe_file_decodes_utf8_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            runtime = build_runtime(tmp_path)

            audio = tmp_path / "voice.wav"
            audio.write_bytes(b"RIFF-test")

            with patch.object(
                runtime,
                "prepare",
                return_value=None,
            ), patch(
                "core.whisper_server_runtime.urllib.request.urlopen",
                side_effect=lambda *args, **kwargs: FakeResponse(
                    {"text": "دو به علاوه دو چند می شه؟"}
                ),
            ):
                transcript = runtime.transcribe_file(
                    audio,
                    language="fa",
                )

            self.assertEqual(
                transcript,
                "دو به علاوه دو چند می شه؟",
            )

    def test_transcribe_file_rejects_missing_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            runtime = build_runtime(tmp_path)

            with self.assertRaises(FileNotFoundError):
                runtime.transcribe_file(
                    tmp_path / "missing.wav"
                )

    def test_shutdown_terminates_running_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = build_runtime(Path(temp_dir))

            fake_process = FakeProcess()

            runtime._process = fake_process
            runtime._stdout_file = io.BytesIO()
            runtime._stderr_file = io.BytesIO()

            runtime.shutdown()

            self.assertTrue(fake_process.terminated)
            self.assertFalse(runtime.is_running)


if __name__ == "__main__":
    unittest.main()
