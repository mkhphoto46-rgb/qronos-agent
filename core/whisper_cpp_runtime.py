from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

from core.config import CONFIG
from core.speech_runtime import SpeechRuntime


DEFAULT_WHISPER_EXE = (
    CONFIG.paths.root
    / "runtime"
    / "whisper"
    / "bin"
    / "Release"
    / "whisper-cli.exe"
)

DEFAULT_WHISPER_MODEL = (
    CONFIG.paths.root
    / "runtime"
    / "whisper"
    / "models"
    / "ggml-large-v3-turbo-q5_0.bin"
)

DEFAULT_TIMEOUT_SECONDS = 120


class WhisperCppRuntime(SpeechRuntime):
    """
    Local whisper.cpp adapter used by Qronos speech-to-text.

    The adapter invokes the bundled whisper-cli executable without requiring
    Python packages or a separately installed Whisper application.
    """

    def __init__(
        self,
        executable_path: str | Path = DEFAULT_WHISPER_EXE,
        model_path: str | Path = DEFAULT_WHISPER_MODEL,
        temp_dir: str | Path = CONFIG.paths.temp,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(
                "timeout_seconds must be greater than zero."
            )

        self.executable_path = Path(
            executable_path
        )
        self.model_path = Path(
            model_path
        )
        self.temp_dir = Path(
            temp_dir
        )
        self.timeout_seconds = (
            timeout_seconds
        )

    def health_check(self) -> bool:
        return (
            self.executable_path.is_file()
            and self.model_path.is_file()
        )

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

        if not self.executable_path.is_file():
            raise FileNotFoundError(
                "whisper-cli.exe was not found: "
                f"{self.executable_path}"
            )

        if not self.model_path.is_file():
            raise FileNotFoundError(
                "Whisper model was not found: "
                f"{self.model_path}"
            )

        language = language.strip()

        if not language:
            raise ValueError(
                "language must not be empty."
            )

        self.temp_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_base = (
            self.temp_dir
            / f"qronos_stt_{uuid4().hex}"
        )

        transcript_path = Path(
            f"{output_base}.txt"
        )

        command = [
            str(self.executable_path),
            "-m",
            str(self.model_path),
            "-f",
            str(source),
            "-l",
            language,
            "-nt",
            "-otxt",
            "-of",
            str(output_base),
            "-fa",
        ]

        try:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=self.timeout_seconds,
                )

            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "Whisper transcription timed out."
                ) from exc

            except OSError as exc:
                raise RuntimeError(
                    "Could not start whisper.cpp."
                ) from exc

            if result.returncode != 0:
                details = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or "No error details were returned."
                )

                raise RuntimeError(
                    "Whisper transcription failed: "
                    f"{details}"
                )

            if not transcript_path.is_file():
                raise RuntimeError(
                    "Whisper finished successfully but "
                    "did not create a transcript file."
                )

            return transcript_path.read_text(
                encoding="utf-8",
            ).strip()

        finally:
            if transcript_path.exists():
                transcript_path.unlink()