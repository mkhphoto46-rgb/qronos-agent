from __future__ import annotations

import math
import subprocess
import wave
from pathlib import Path

from core.audio_input import AudioConfig, AudioInput
from core.config import CONFIG


RECORD_SECONDS = 6

WHISPER_ROOT = (
    CONFIG.paths.root
    / "runtime"
    / "whisper"
)

WHISPER_EXE = (
    WHISPER_ROOT
    / "bin"
    / "Release"
    / "whisper-cli.exe"
)

WHISPER_MODEL = (
    WHISPER_ROOT
    / "models"
    / "ggml-large-v3-turbo-q5_0.bin"
)

TEST_WAV = (
    CONFIG.paths.temp
    / "qronos_stt_test.wav"
)

TRANSCRIPT_BASE = (
    CONFIG.paths.temp
    / "qronos_stt_test_transcript"
)

TRANSCRIPT_FILE = Path(
    f"{TRANSCRIPT_BASE}.txt"
)


def record_test_audio() -> None:
    config = AudioConfig(
        sample_rate=16_000,
        channels=1,
        frame_size=1_280,
        sample_width=2,
    )

    audio = AudioInput(config)

    frame_count = math.ceil(
        RECORD_SECONDS
        * config.sample_rate
        / config.frame_size
    )

    frames: list[bytes] = []

    print()
    print(
        f"Recording for {RECORD_SECONDS} seconds."
    )
    print("Speak now...")
    print()

    audio.start()

    try:
        for _ in range(frame_count):
            frames.append(
                audio.read_frame()
            )
    finally:
        audio.stop()

    CONFIG.paths.temp.mkdir(
        parents=True,
        exist_ok=True,
    )

    with wave.open(
        str(TEST_WAV),
        "wb",
    ) as wav_file:
        wav_file.setnchannels(
            config.channels
        )
        wav_file.setsampwidth(
            config.sample_width
        )
        wav_file.setframerate(
            config.sample_rate
        )
        wav_file.writeframes(
            b"".join(frames)
        )

    print(
        f"Audio saved: {TEST_WAV}"
    )


def transcribe_test_audio() -> str:
    if not WHISPER_EXE.is_file():
        raise FileNotFoundError(
            "whisper-cli.exe was not found: "
            f"{WHISPER_EXE}"
        )

    if not WHISPER_MODEL.is_file():
        raise FileNotFoundError(
            "Whisper model was not found: "
            f"{WHISPER_MODEL}"
        )

    if TRANSCRIPT_FILE.exists():
        TRANSCRIPT_FILE.unlink()

    command = [
        str(WHISPER_EXE),
        "-m",
        str(WHISPER_MODEL),
        "-f",
        str(TEST_WAV),
        "-l",
        "auto",
        "-nt",
        "-otxt",
        "-of",
        str(TRANSCRIPT_BASE),
        "-fa",
    ]

    print()
    print("Transcribing...")
    print()

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Whisper transcription failed.\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}"
        )

    if not TRANSCRIPT_FILE.is_file():
        raise RuntimeError(
            "Whisper finished but did not "
            "create the transcript file."
        )

    return TRANSCRIPT_FILE.read_text(
        encoding="utf-8",
    ).strip()


def main() -> None:
    record_test_audio()

    transcript = (
        transcribe_test_audio()
    )

    print("=" * 60)
    print("FINAL TRANSCRIPT")
    print("=" * 60)
    print()
    print(transcript)
    print()
    print("=" * 60)


if __name__ == "__main__":
    main()