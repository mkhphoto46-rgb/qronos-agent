from __future__ import annotations

import sys

from core.audio_input import (
    AudioConfig,
    AudioInput,
)
from core.command_recorder import (
    CommandRecorder,
    CommandRecorderConfig,
)
from core.config import CONFIG
from core.whisper_cpp_runtime import (
    WhisperCppRuntime,
)
from core.whisper_cpp_vad_runtime import (
    WhisperCppVADRuntime,
)


OUTPUT_WAV = (
    CONFIG.paths.temp
    / "qronos_command_live.wav"
)


def main() -> int:
    audio_config = AudioConfig(
        sample_rate=16_000,
        channels=1,
        frame_size=1_280,
        sample_width=2,
    )

    audio_input = AudioInput(
        audio_config
    )

    vad_runtime = WhisperCppVADRuntime(
        thread_count=2,
        use_gpu=False,
    )

    speech_runtime = WhisperCppRuntime()

    recorder_config = CommandRecorderConfig(
        speech_start_threshold=0.50,
        speech_continue_threshold=0.50,
        silence_seconds=1.0,
        max_duration_seconds=15.0,
        start_timeout_seconds=8.0,
        pre_roll_seconds=0.32,
        min_speech_seconds=0.24,
    )

    recorder = CommandRecorder(
        audio_input=audio_input,
        vad_runtime=vad_runtime,
        config=recorder_config,
    )

    print()
    print("=" * 60)
    print("QRONOS SILERO COMMAND + STT LIVE TEST")
    print("=" * 60)
    print()

    print(
        "Speech start threshold: "
        f"{recorder_config.speech_start_threshold}"
    )

    print(
        "Speech continue threshold: "
        f"{recorder_config.speech_continue_threshold}"
    )

    print(
        "Trailing silence: "
        f"{recorder_config.silence_seconds} sec"
    )

    print(
        "Start timeout: "
        f"{recorder_config.start_timeout_seconds} sec"
    )

    print(
        "Maximum command: "
        f"{recorder_config.max_duration_seconds} sec"
    )

    print()

    if not vad_runtime.health_check():
        print(
            "ERROR: Silero VAD runtime "
            "is not ready."
        )
        return 1

    if not speech_runtime.health_check():
        print(
            "ERROR: Whisper STT runtime "
            "is not ready."
        )
        return 1

    print("Preparing Silero VAD...")

    try:
        vad_runtime.prepare()

    except Exception as exc:
        print()
        print("VAD STARTUP ERROR:")
        print(exc)

        vad_runtime.close()

        return 1

    print("Silero VAD: READY")
    print("Whisper STT: READY")

    print()
    print("Speak one complete command.")
    print(
        "When you finish speaking, "
        "stay silent."
    )
    print()

    try:
        result = recorder.record_to_file(
            OUTPUT_WAV
        )

    except TimeoutError:
        print()
        print(
            "RESULT: No speech detected "
            "before timeout."
        )

        return 2

    except KeyboardInterrupt:
        print()
        print("Cancelled.")

        return 130

    finally:
        vad_runtime.close()

    print()
    print("-" * 60)
    print("RECORDING RESULT")
    print("-" * 60)

    print(
        "Audio file: "
        f"{result.audio_path}"
    )

    print(
        "Recorded duration: "
        f"{result.duration_seconds:.2f} sec"
    )

    print(
        "Detected speech: "
        f"{result.speech_seconds:.2f} sec"
    )

    print(
        "Peak speech probability: "
        f"{result.peak_speech_probability:.4f}"
    )

    print(
        "Stopped by silence: "
        f"{result.stopped_by_silence}"
    )

    print()
    print("Transcribing...")
    print()

    try:
        transcript = (
            speech_runtime.transcribe_file(
                result.audio_path,
                language="auto",
            )
        )

    except Exception as exc:
        print("STT ERROR:")
        print(exc)

        return 3

    print("=" * 60)
    print("FINAL TRANSCRIPT")
    print("=" * 60)
    print()
    print(transcript)
    print()
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(
        main()
    )