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
from core.openwakeword_engine import (
    OpenWakeWordEngine,
)
from core.orchestrator import (
    Orchestrator,
)
from core.task_router import (
    TaskRouter,
)
from core.voice_pipeline import (
    VoicePipeline,
)
from core.voice_trigger import (
    VoiceTriggerService,
)
from core.whisper_cpp_runtime import (
    WhisperCppRuntime,
)
from core.whisper_cpp_vad_runtime import (
    WhisperCppVADRuntime,
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

    wake_word_engine = (
        OpenWakeWordEngine(
            frame_size=(
                audio_config.frame_size
            ),
        )
    )

    voice_trigger = (
        VoiceTriggerService(
            wake_word="Qronos",
            engine=wake_word_engine,
        )
    )

    vad_runtime = (
        WhisperCppVADRuntime(
            thread_count=2,
            use_gpu=False,
        )
    )

    recorder_config = (
        CommandRecorderConfig(
            speech_start_threshold=0.50,
            speech_continue_threshold=0.50,
            silence_seconds=1.0,
            max_duration_seconds=15.0,
            start_timeout_seconds=8.0,
            pre_roll_seconds=0.32,
            min_speech_seconds=0.24,
        )
    )

    command_recorder = (
        CommandRecorder(
            audio_input=audio_input,
            vad_runtime=vad_runtime,
            config=recorder_config,
        )
    )

    speech_runtime = (
        WhisperCppRuntime()
    )

    task_router = TaskRouter()

    orchestrator = (
        Orchestrator()
    )

    pipeline = VoicePipeline(
        audio_input=audio_input,
        voice_trigger=voice_trigger,
        command_recorder=command_recorder,
        vad_runtime=vad_runtime,
        speech_runtime=speech_runtime,
        task_router=task_router,
        orchestrator=orchestrator,
    )

    print()
    print("=" * 60)
    print(
        "QRONOS VOICE -> BRAIN "
        "LIVE TEST"
    )
    print("=" * 60)
    print()

    print(
        "Preparing Qronos..."
    )
    print()

    try:
        pipeline.prepare()

    except Exception as exc:
        print(
            "STARTUP ERROR:"
        )

        print(
            exc
        )

        pipeline.close()

        return 1

    print()
    print(
        "Qronos is READY."
    )
    print()

    print(
        "1. Say: Qronos"
    )

    print(
        "2. Pause briefly."
    )

    print(
        "3. Then say one simple command."
    )

    print()

    print(
        "Example command: "
        "Hello, introduce yourself "
        "in one short sentence."
    )

    print()
    print(
        "Listening..."
    )
    print()

    result = None
    wake_score = None

    try:
        result = (
            pipeline.listen_once()
        )

        # Capture the real detection score before pipeline.close()
        # clears the wake-word engine diagnostics.
        wake_score = (
            wake_word_engine.last_score
        )

    except KeyboardInterrupt:
        print()
        print(
            "Cancelled."
        )

        return 130

    finally:
        pipeline.close()

    if result is None:
        print()
        print(
            "No pipeline result "
            "was produced."
        )

        return 2

    print()
    print("=" * 60)
    print(
        "VOICE PIPELINE RESULT"
    )
    print("=" * 60)
    print()

    if result.wake_event is not None:
        print(
            "Wake word detected: YES"
        )

        print(
            "Wake word: "
            f"{result.wake_event.wake_word}"
        )

        if wake_score is not None:
            print(
                "Wake score: "
                f"{wake_score:.6f}"
            )

        else:
            print(
                "Wake score: unavailable"
            )

    else:
        print(
            "Wake word detected: NO"
        )

    print()

    if result.recording is not None:
        print(
            "Recorded duration: "
            f"{result.recording.duration_seconds:.2f} sec"
        )

        print(
            "Detected speech: "
            f"{result.recording.speech_seconds:.2f} sec"
        )

        print(
            "Peak speech probability: "
            f"{result.recording.peak_speech_probability:.4f}"
        )

        print(
            "Stopped by silence: "
            f"{result.recording.stopped_by_silence}"
        )

        print()

    print("-" * 60)
    print(
        "TRANSCRIPT"
    )
    print("-" * 60)
    print()

    print(
        result.transcript
        if result.transcript
        else "<empty>"
    )

    print()

    if result.route is not None:
        print("-" * 60)
        print(
            "ROUTING"
        )
        print("-" * 60)
        print()

        print(
            "Task type: "
            f"{result.route.task_type.value}"
        )

        print(
            "Reason: "
            f"{result.route.reason}"
        )

        print()

    if not result.success:
        print("=" * 60)
        print(
            "PIPELINE FAILED"
        )
        print("=" * 60)
        print()

        print(
            result.error
            or (
                "Unknown voice pipeline "
                "error."
            )
        )

        return 2

    print("=" * 60)
    print(
        "QRONOS RESPONSE"
    )
    print("=" * 60)
    print()

    print(
        result.response
    )

    print()
    print("=" * 60)
    print(
        "VOICE -> BRAIN SUCCESS"
    )
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(
        main()
    )