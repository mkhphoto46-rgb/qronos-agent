from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.speech_runtime import SpeechRuntime
from core.whisper_cpp_runtime import (
    WhisperCppRuntime,
)


class FakeSpeechRuntime(SpeechRuntime):
    def health_check(self) -> bool:
        return True

    def transcribe_file(
        self,
        audio_path: str | Path,
        language: str = "auto",
    ) -> str:
        return (
            f"{Path(audio_path).name}|"
            f"{language}"
        )


class TestSpeechRuntime(unittest.TestCase):
    def test_fake_runtime_health_check(
        self,
    ) -> None:
        runtime = FakeSpeechRuntime()

        self.assertTrue(
            runtime.health_check()
        )

    def test_fake_runtime_transcribe_file(
        self,
    ) -> None:
        runtime = FakeSpeechRuntime()

        result = runtime.transcribe_file(
            "command.wav",
            language="fa",
        )

        self.assertEqual(
            result,
            "command.wav|fa",
        )


class TestWhisperCppRuntime(
    unittest.TestCase
):
    def setUp(self) -> None:
        self.temp_directory = (
            tempfile.TemporaryDirectory()
        )

        self.root = Path(
            self.temp_directory.name
        )

        self.executable = (
            self.root
            / "whisper-cli.exe"
        )

        self.model = (
            self.root
            / "model.bin"
        )

        self.audio = (
            self.root
            / "command.wav"
        )

        self.output_dir = (
            self.root
            / "output"
        )

        self.executable.write_bytes(
            b"runtime"
        )

        self.model.write_bytes(
            b"model"
        )

        self.audio.write_bytes(
            b"audio"
        )

        self.runtime = WhisperCppRuntime(
            executable_path=self.executable,
            model_path=self.model,
            temp_dir=self.output_dir,
            timeout_seconds=10,
        )

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_whisper_cpp_implements_speech_runtime(
        self,
    ) -> None:
        self.assertIsInstance(
            self.runtime,
            SpeechRuntime,
        )

    def test_health_check_when_runtime_is_ready(
        self,
    ) -> None:
        self.assertTrue(
            self.runtime.health_check()
        )

    def test_health_check_when_model_is_missing(
        self,
    ) -> None:
        self.model.unlink()

        self.assertFalse(
            self.runtime.health_check()
        )

    def test_timeout_must_be_positive(
        self,
    ) -> None:
        with self.assertRaises(
            ValueError
        ):
            WhisperCppRuntime(
                executable_path=self.executable,
                model_path=self.model,
                temp_dir=self.output_dir,
                timeout_seconds=0,
            )

    def test_missing_audio_file_is_rejected(
        self,
    ) -> None:
        missing_audio = (
            self.root
            / "missing.wav"
        )

        with self.assertRaises(
            FileNotFoundError
        ):
            self.runtime.transcribe_file(
                missing_audio
            )

    @patch(
        "core.whisper_cpp_runtime.subprocess.run"
    )
    def test_successful_transcription(
        self,
        mock_run,
    ) -> None:
        def fake_run(
            command,
            **kwargs,
        ):
            output_base = Path(
                command[
                    command.index("-of") + 1
                ]
            )

            Path(
                f"{output_base}.txt"
            ).write_text(
                "سلام کرونوس",
                encoding="utf-8",
            )

            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="",
                stderr="",
            )

        mock_run.side_effect = fake_run

        result = self.runtime.transcribe_file(
            self.audio,
            language="fa",
        )

        self.assertEqual(
            result,
            "سلام کرونوس",
        )

        command = (
            mock_run.call_args.args[0]
        )

        self.assertIn(
            str(self.executable),
            command,
        )

        self.assertIn(
            str(self.model),
            command,
        )

        self.assertIn(
            str(self.audio),
            command,
        )

        self.assertIn(
            "fa",
            command,
        )

        remaining = list(
            self.output_dir.glob(
                "qronos_stt_*.txt"
            )
        )

        self.assertEqual(
            remaining,
            [],
        )

    @patch(
        "core.whisper_cpp_runtime.subprocess.run"
    )
    def test_runtime_failure_is_reported(
        self,
        mock_run,
    ) -> None:
        mock_run.return_value = (
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="CUDA failure",
            )
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "CUDA failure",
        ):
            self.runtime.transcribe_file(
                self.audio
            )

    @patch(
        "core.whisper_cpp_runtime.subprocess.run"
    )
    def test_timeout_is_reported(
        self,
        mock_run,
    ) -> None:
        mock_run.side_effect = (
            subprocess.TimeoutExpired(
                cmd="whisper-cli.exe",
                timeout=10,
            )
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "timed out",
        ):
            self.runtime.transcribe_file(
                self.audio
            )


if __name__ == "__main__":
    unittest.main()