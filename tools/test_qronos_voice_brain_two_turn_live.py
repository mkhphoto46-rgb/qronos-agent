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
    VoicePipelineResult,
)
from core.voice_trigger import (
    VoiceTriggerEvent,
    VoiceTriggerService,
)
from core.whisper_cpp_runtime import (
    WhisperCppRuntime,
)
from core.whisper_cpp_vad_runtime import (
    WhisperCppVADRuntime,
)


TOTAL_TURNS = 2


def wake_detected_feedback(
    event: VoiceTriggerEvent,
) -> None:
    print()
    print()
    print("!" * 60)
    print(
        f"WAKE DETECTED: {event.wake_word}"
    )
    print("NOW SAY YOUR COMMAND.")
    print("DO NOT SAY QRONOS AGAIN.")
    print("!" * 60)
    print()


def print_turn_result(
    turn_number: int,
    result: VoicePipelineResult,
    wake_score: float,
) -> None:
    print()
    print("=" * 60)
    print(
        f"TURN {turn_number} RESULT"
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

        print(
            "Wake score: "
            f"{wake_score:.6f}"
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
    print("TRANSCRIPT")
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
        print("ROUTING")
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

    if result.success:
        print("-" * 60)
        print("QRONOS RESPONSE")
        print("-" * 60)
        print()

        print(
            result.response
        )

    else:
        print("-" * 60)
        print("TURN FAILED")
        print("-" * 60)
        print()

        print(
            result.error
            or "Unknown pipeline error."
        )

    print()


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
        on_wake_detected=(
            wake_detected_feedback
        ),
    )

    print()
    print("=" * 60)
    print(
        "QRONOS TWO-TURN "
        "VOICE -> BRAIN STRESS TEST"
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

    successful_turns = 0

    try:
        for turn_number in range(
            1,
            TOTAL_TURNS + 1,
        ):
            print()
            print("#" * 60)
            print(
                f"TURN {turn_number}/{TOTAL_TURNS}"
            )
            print("#" * 60)
            print()

            print(
                "Say Qronos ONCE."
            )

            print(
                "Then WAIT for:"
            )

            print(
                "NOW SAY YOUR COMMAND."
            )

            print()

            if turn_number == 1:
                print(
                    "Turn 1 command:"
                )

                print(
                    "Tell me what two plus two is."
                )

            else:
                print(
                    "Turn 2 command:"
                )

                print(
                    "Tell me what three plus three is."
                )

            print()
            print(
                "Listening for Qronos..."
            )
            print()

            result = (
                pipeline.listen_once()
            )

            wake_score = (
                wake_word_engine.last_score
            )

            print_turn_result(
                turn_number=turn_number,
                result=result,
                wake_score=wake_score,
            )

            if not result.success:
                print(
                    "Stress test stopped "
                    "because this turn failed."
                )

                return 2

            successful_turns += 1

            if turn_number < TOTAL_TURNS:
                print("=" * 60)
                print(
                    "Qronos has been re-armed."
                )
                print(
                    "Wait for TURN 2 before "
                    "saying Qronos again."
                )
                print("=" * 60)
                print()

    except KeyboardInterrupt:
        print()
        print(
            "Test cancelled."
        )

        return 130

    finally:
        pipeline.close()

    print()
    print("=" * 60)

    if successful_turns == TOTAL_TURNS:
        print(
            "TWO-TURN VOICE -> BRAIN SUCCESS"
        )

    else:
        print(
            "TWO-TURN TEST FAILED"
        )

    print("=" * 60)
    print()

    print(
        "Successful turns: "
        f"{successful_turns}/{TOTAL_TURNS}"
    )

    print()

    return (
        0
        if successful_turns == TOTAL_TURNS
        else 3
    )


if __name__ == "__main__":
    sys.exit(
        main()
    )