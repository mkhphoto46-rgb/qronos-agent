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
from core.conversation_session import (
    ConversationSession,
    ConversationSessionConfig,
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


TOTAL_TURNS = 3


def wake_detected_feedback(
    event: VoiceTriggerEvent,
) -> None:
    print()
    print("!" * 60)
    print(
        f"WAKE DETECTED: {event.wake_word}"
    )
    print("CONVERSATION STARTED.")
    print("!" * 60)
    print()


def print_result(
    turn_number: int,
    result: VoicePipelineResult,
    session: ConversationSession,
) -> None:
    print()
    print("=" * 60)
    print(
        f"TURN {turn_number} RESULT"
    )
    print("=" * 60)
    print()

    print(
        "Wake word used: "
        + (
            "YES"
            if result.wake_event is not None
            else "NO"
        )
    )

    print(
        "Conversation active: "
        f"{session.is_active}"
    )

    print(
        "Conversation messages: "
        f"{session.message_count}"
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
        print(
            "Task type: "
            f"{result.route.task_type.value}"
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
            or "Unknown error."
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

    recorder = (
        CommandRecorder(
            audio_input=audio_input,
            vad_runtime=vad_runtime,
            config=CommandRecorderConfig(
                speech_start_threshold=0.50,
                speech_continue_threshold=0.50,
                silence_seconds=2.0,
                max_duration_seconds=60.0,
                start_timeout_seconds=8.0,
                pre_roll_seconds=0.32,
                min_speech_seconds=0.24,
            ),
        )
    )

    speech_runtime = (
        WhisperCppRuntime()
    )

    router = TaskRouter()

    orchestrator = (
        Orchestrator()
    )

    session = (
        ConversationSession(
            config=ConversationSessionConfig(
                inactivity_timeout_seconds=60.0
            )
        )
    )

    pipeline = VoicePipeline(
        audio_input=audio_input,
        voice_trigger=voice_trigger,
        command_recorder=recorder,
        vad_runtime=vad_runtime,
        speech_runtime=speech_runtime,
        task_router=router,
        orchestrator=orchestrator,
        conversation_session=session,
        on_wake_detected=(
            wake_detected_feedback
        ),
    )

    print()
    print("=" * 60)
    print(
        "QRONOS VOICE MEMORY + IDENTITY LIVE TEST"
    )
    print("=" * 60)
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
        return 1

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

            if turn_number == 1:
                print(
                    "Say Qronos once."
                )
                print()
                print(
                    "Then say clearly:"
                )
                print()
                print(
                    "Remember the number seven four two nine."
                )

            elif turn_number == 2:
                print(
                    "DO NOT say Qronos."
                )
                print()
                print(
                    "Say:"
                )
                print()
                print(
                    "What number did I ask you to remember?"
                )

            else:
                print(
                    "DO NOT say Qronos."
                )
                print()
                print(
                    "Say:"
                )
                print()
                print(
                    "What AI model are you?"
                )

            print()
            print(
                "Listening..."
            )
            print()

            result = (
                pipeline.listen_once()
            )

            print_result(
                turn_number=turn_number,
                result=result,
                session=session,
            )

            if not result.success:
                print(
                    "TEST FAILED."
                )
                return 2

            successful_turns += 1

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
    print(
        "TEST COMPLETE"
    )
    print("=" * 60)
    print()

    print(
        "Successful turns: "
        f"{successful_turns}/{TOTAL_TURNS}"
    )

    print()
    print(
        "Manual checks:"
    )

    print(
        "1. Turn 1 transcript should contain 7429 "
        "or seven four two nine."
    )

    print(
        "2. Turn 2 should recall the same number."
    )

    print(
        "3. Turn 3 should identify as Qronos."
    )

    print(
        "4. Turn 3 should not mention Qwen, Alibaba, or Ollama."
    )

    print()

    return 0


if __name__ == "__main__":
    sys.exit(
        main()
    )