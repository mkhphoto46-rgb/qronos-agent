from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.brain_runtime import (
    BrainMessage,
    BrainMessageRole,
)
from core.command_recorder import (
    CommandRecordingResult,
)
from core.conversation_session import (
    ConversationRole,
    ConversationSession,
    ConversationSessionConfig,
)
from core.orchestrator import (
    StepResult,
)
from core.task_router import (
    RouteDecision,
    TaskType,
)
from core.voice_pipeline import (
    VoicePipeline,
)
from core.voice_trigger import (
    VoiceTriggerEvent,
)


class FakeClock:
    def __init__(
        self,
        initial_time: float = 100.0,
    ) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time

    def advance(
        self,
        seconds: float,
    ) -> None:
        self.current_time += seconds


class FakeAudioInput:
    def __init__(self) -> None:
        self.running = False
        self.start_calls = 0
        self.stop_calls = 0
        self.read_calls = 0

    def start(self) -> None:
        self.running = True
        self.start_calls += 1

    def stop(self) -> None:
        if self.running:
            self.stop_calls += 1

        self.running = False

    def is_running(self) -> bool:
        return self.running

    def read_frame(self) -> bytes:
        if not self.running:
            raise RuntimeError(
                "Audio input is not running."
            )

        self.read_calls += 1

        return b"wake-frame"


class FakeVoiceTrigger:
    def __init__(self) -> None:
        self.running = False
        self.paused = False

        self.start_calls = 0
        self.stop_calls = 0
        self.pause_calls = 0
        self.resume_calls = 0
        self.process_calls = 0

    def start(self) -> None:
        self.running = True
        self.paused = False
        self.start_calls += 1

    def stop(self) -> None:
        self.running = False
        self.paused = False
        self.stop_calls += 1

    def pause(self) -> None:
        self.paused = True
        self.pause_calls += 1

    def resume(self) -> None:
        self.paused = False
        self.resume_calls += 1

    def is_running(self) -> bool:
        return self.running

    def process_audio(
        self,
        audio_data: bytes,
        timestamp: float,
    ) -> VoiceTriggerEvent | None:
        del audio_data

        self.process_calls += 1

        return VoiceTriggerEvent(
            event_type="wake_word_detected",
            wake_word="Qronos",
            timestamp=timestamp,
        )


class FakeVADRuntime:
    def __init__(
        self,
        ready: bool = True,
    ) -> None:
        self.ready = ready
        self.prepare_calls = 0
        self.close_calls = 0

    @property
    def sample_rate(self) -> int:
        return 16_000

    def health_check(self) -> bool:
        return self.ready

    def prepare(self) -> None:
        self.prepare_calls += 1

    def process_pcm16(
        self,
        audio_data: bytes,
    ) -> tuple[float, ...]:
        del audio_data

        return (
            1.0,
        )

    def reset(self) -> None:
        return None

    def close(self) -> None:
        self.close_calls += 1


class FakeCommandRecorder:
    def __init__(
        self,
        audio_path: Path,
    ) -> None:
        self.audio_path = audio_path
        self.calls = 0

    def record_to_file(
        self,
        output_path: str | Path,
    ) -> CommandRecordingResult:
        self.calls += 1

        destination = Path(
            output_path
        )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        destination.write_bytes(
            b"fake wav"
        )

        return CommandRecordingResult(
            audio_path=destination,
            duration_seconds=3.0,
            speech_seconds=2.0,
            stopped_by_silence=True,
            peak_speech_probability=1.0,
        )


class FakeSpeechRuntime:
    def __init__(
        self,
        transcripts: list[str] | None = None,
        ready: bool = True,
    ) -> None:
        self.ready = ready

        self.transcripts = (
            transcripts
            if transcripts is not None
            else [
                "Hello Qronos",
            ]
        )

        self.calls = 0

    def health_check(self) -> bool:
        return self.ready

    def transcribe_file(
        self,
        audio_path: str | Path,
        language: str = "auto",
    ) -> str:
        del audio_path
        del language

        index = min(
            self.calls,
            len(self.transcripts) - 1,
        )

        transcript = (
            self.transcripts[index]
        )

        self.calls += 1

        return transcript


class FakeTaskRouter:
    def __init__(
        self,
        task_type: TaskType = TaskType.FAST,
    ) -> None:
        self.task_type = task_type
        self.inputs: list[str] = []

    def route(
        self,
        user_input: str,
    ) -> RouteDecision:
        self.inputs.append(
            user_input
        )

        return RouteDecision(
            task_type=self.task_type,
            reason="fake route",
        )


class FakeOrchestrator:
    def __init__(
        self,
        outputs: list[str] | None = None,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        self.outputs = (
            outputs
            if outputs is not None
            else [
                "Brain response",
            ]
        )

        self.success = success
        self.error = error

        self.plans = []

        self.conversation_contexts: list[
            list[BrainMessage]
        ] = []

    def execute_plan(
        self,
        plan,
        conversation_messages=None,
    ) -> list[StepResult]:
        self.plans.append(
            plan
        )

        self.conversation_contexts.append(
            list(
                conversation_messages
                or []
            )
        )

        index = min(
            len(self.plans) - 1,
            len(self.outputs) - 1,
        )

        output = (
            self.outputs[index]
        )

        return [
            StepResult(
                order=1,
                success=self.success,
                output=(
                    output
                    if self.success
                    else ""
                ),
                error=(
                    None
                    if self.success
                    else self.error
                ),
            )
        ]


class TestVoicePipeline(
    unittest.TestCase
):
    def setUp(self) -> None:
        self.temp_directory = (
            tempfile.TemporaryDirectory()
        )

        self.addCleanup(
            self.temp_directory.cleanup
        )

        self.root = Path(
            self.temp_directory.name
        )

        self.clock = FakeClock()

        self.session = ConversationSession(
            config=ConversationSessionConfig(
                inactivity_timeout_seconds=60.0
            ),
            clock=self.clock,
        )

        self.audio = FakeAudioInput()
        self.trigger = FakeVoiceTrigger()
        self.vad = FakeVADRuntime()

        self.recorder = FakeCommandRecorder(
            self.root
            / "recorded.wav"
        )

        self.speech = FakeSpeechRuntime(
            transcripts=[
                "What is two plus two?",
                "What is four plus four?",
            ]
        )

        self.router = FakeTaskRouter(
            TaskType.FAST
        )

        self.orchestrator = (
            FakeOrchestrator(
                outputs=[
                    "Four.",
                    "Eight.",
                ]
            )
        )

        self.wake_events: list[
            VoiceTriggerEvent
        ] = []

        self.pipeline = VoicePipeline(
            audio_input=self.audio,
            voice_trigger=self.trigger,
            command_recorder=self.recorder,
            vad_runtime=self.vad,
            speech_runtime=self.speech,
            task_router=self.router,
            orchestrator=self.orchestrator,
            conversation_session=self.session,
            command_audio_path=(
                self.root
                / "command.wav"
            ),
            on_wake_detected=(
                self.wake_events.append
            ),
        )

    def test_pipeline_starts_unprepared(
        self,
    ) -> None:
        self.assertFalse(
            self.pipeline.is_prepared
        )

        self.assertFalse(
            self.pipeline.is_closed
        )

    def test_prepare_warms_vad_and_starts_listening(
        self,
    ) -> None:
        self.pipeline.prepare()

        self.assertTrue(
            self.pipeline.is_prepared
        )

        self.assertEqual(
            self.vad.prepare_calls,
            1,
        )

        self.assertEqual(
            self.trigger.start_calls,
            1,
        )

        self.assertEqual(
            self.audio.start_calls,
            1,
        )

        self.assertTrue(
            self.audio.is_running()
        )

    def test_prepare_is_idempotent(
        self,
    ) -> None:
        self.pipeline.prepare()
        self.pipeline.prepare()

        self.assertEqual(
            self.vad.prepare_calls,
            1,
        )

        self.assertEqual(
            self.trigger.start_calls,
            1,
        )

        self.assertEqual(
            self.audio.start_calls,
            1,
        )

    def test_listen_requires_prepare(
        self,
    ) -> None:
        with self.assertRaises(
            RuntimeError
        ):
            self.pipeline.listen_once()

    def test_first_turn_requires_wake_word(
        self,
    ) -> None:
        self.pipeline.prepare()

        result = (
            self.pipeline.listen_once()
        )

        self.assertTrue(
            result.success
        )

        self.assertIsNotNone(
            result.wake_event
        )

        self.assertEqual(
            self.trigger.process_calls,
            1,
        )

        self.assertEqual(
            len(
                self.wake_events
            ),
            1,
        )

        self.assertTrue(
            self.session.is_active
        )

        self.assertFalse(
            self.session.requires_wake_word()
        )

    def test_second_turn_does_not_require_wake_word(
        self,
    ) -> None:
        self.pipeline.prepare()

        first = (
            self.pipeline.listen_once()
        )

        wake_process_calls_after_first = (
            self.trigger.process_calls
        )

        second = (
            self.pipeline.listen_once()
        )

        self.assertTrue(
            first.success
        )

        self.assertTrue(
            second.success
        )

        self.assertIsNotNone(
            first.wake_event
        )

        self.assertIsNone(
            second.wake_event
        )

        self.assertEqual(
            self.trigger.process_calls,
            wake_process_calls_after_first,
        )

        self.assertEqual(
            len(
                self.wake_events
            ),
            1,
        )

    def test_two_turns_are_recorded_in_session_history(
        self,
    ) -> None:
        self.pipeline.prepare()

        first = (
            self.pipeline.listen_once()
        )

        second = (
            self.pipeline.listen_once()
        )

        self.assertTrue(
            first.success
        )

        self.assertTrue(
            second.success
        )

        messages = (
            self.session.messages
        )

        self.assertEqual(
            len(messages),
            4,
        )

        self.assertEqual(
            messages[0].role,
            ConversationRole.USER,
        )

        self.assertEqual(
            messages[0].content,
            "What is two plus two?",
        )

        self.assertEqual(
            messages[1].role,
            ConversationRole.ASSISTANT,
        )

        self.assertEqual(
            messages[1].content,
            "Four.",
        )

        self.assertEqual(
            messages[2].role,
            ConversationRole.USER,
        )

        self.assertEqual(
            messages[2].content,
            "What is four plus four?",
        )

        self.assertEqual(
            messages[3].role,
            ConversationRole.ASSISTANT,
        )

        self.assertEqual(
            messages[3].content,
            "Eight.",
        )

    def test_first_turn_has_no_previous_conversation_context(
        self,
    ) -> None:
        self.pipeline.prepare()

        result = (
            self.pipeline.listen_once()
        )

        self.assertTrue(
            result.success
        )

        self.assertEqual(
            len(
                self.orchestrator.conversation_contexts
            ),
            1,
        )

        self.assertEqual(
            self.orchestrator.conversation_contexts[0],
            [],
        )

    def test_second_turn_receives_previous_conversation_context(
        self,
    ) -> None:
        self.pipeline.prepare()

        first = (
            self.pipeline.listen_once()
        )

        second = (
            self.pipeline.listen_once()
        )

        self.assertTrue(
            first.success
        )

        self.assertTrue(
            second.success
        )

        self.assertEqual(
            len(
                self.orchestrator.conversation_contexts
            ),
            2,
        )

        second_context = (
            self.orchestrator.conversation_contexts[1]
        )

        self.assertEqual(
            len(second_context),
            2,
        )

        self.assertEqual(
            second_context[0].role,
            BrainMessageRole.USER,
        )

        self.assertEqual(
            second_context[0].content,
            "What is two plus two?",
        )

        self.assertEqual(
            second_context[1].role,
            BrainMessageRole.ASSISTANT,
        )

        self.assertEqual(
            second_context[1].content,
            "Four.",
        )

    def test_current_user_turn_is_not_duplicated_in_context(
        self,
    ) -> None:
        self.pipeline.prepare()

        self.pipeline.listen_once()
        self.pipeline.listen_once()

        second_context = (
            self.orchestrator.conversation_contexts[1]
        )

        contents = [
            message.content
            for message in second_context
        ]

        self.assertNotIn(
            "What is four plus four?",
            contents,
        )

    def test_second_turn_routes_second_transcript(
        self,
    ) -> None:
        self.pipeline.prepare()

        self.pipeline.listen_once()
        self.pipeline.listen_once()

        self.assertEqual(
            self.router.inputs,
            [
                "What is two plus two?",
                "What is four plus four?",
            ],
        )

    def test_each_turn_reaches_orchestrator(
        self,
    ) -> None:
        self.pipeline.prepare()

        self.pipeline.listen_once()
        self.pipeline.listen_once()

        self.assertEqual(
            len(
                self.orchestrator.plans
            ),
            2,
        )

        self.assertEqual(
            self.orchestrator.plans[0].goal,
            "What is two plus two?",
        )

        self.assertEqual(
            self.orchestrator.plans[1].goal,
            "What is four plus four?",
        )

    def test_successful_first_turn_returns_response(
        self,
    ) -> None:
        self.pipeline.prepare()

        result = (
            self.pipeline.listen_once()
        )

        self.assertTrue(
            result.success
        )

        self.assertEqual(
            result.transcript,
            "What is two plus two?",
        )

        self.assertEqual(
            result.response,
            "Four.",
        )

        self.assertIsNotNone(
            result.recording
        )

        self.assertIsNotNone(
            result.route
        )

        self.assertEqual(
            result.route.task_type,
            TaskType.FAST,
        )

    def test_successful_second_turn_returns_second_response(
        self,
    ) -> None:
        self.pipeline.prepare()

        self.pipeline.listen_once()

        result = (
            self.pipeline.listen_once()
        )

        self.assertTrue(
            result.success
        )

        self.assertEqual(
            result.transcript,
            "What is four plus four?",
        )

        self.assertEqual(
            result.response,
            "Eight.",
        )

        self.assertIsNone(
            result.wake_event
        )

    def test_session_timeout_requires_wake_word_again(
        self,
    ) -> None:
        self.pipeline.prepare()

        self.pipeline.listen_once()

        self.assertFalse(
            self.session.requires_wake_word()
        )

        first_wake_calls = (
            self.trigger.process_calls
        )

        self.clock.advance(
            60.0
        )

        self.assertTrue(
            self.session.requires_wake_word()
        )

        result = (
            self.pipeline.listen_once()
        )

        self.assertTrue(
            result.success
        )

        self.assertIsNotNone(
            result.wake_event
        )

        self.assertGreater(
            self.trigger.process_calls,
            first_wake_calls,
        )

        self.assertEqual(
            len(
                self.wake_events
            ),
            2,
        )

    def test_new_session_does_not_receive_old_context(
        self,
    ) -> None:
        self.pipeline.prepare()

        first = (
            self.pipeline.listen_once()
        )

        self.assertTrue(
            first.success
        )

        self.clock.advance(
            60.0
        )

        self.assertTrue(
            self.session.requires_wake_word()
        )

        second = (
            self.pipeline.listen_once()
        )

        self.assertTrue(
            second.success
        )

        second_context = (
            self.orchestrator.conversation_contexts[1]
        )

        self.assertEqual(
            second_context,
            [],
        )

    def test_processing_time_does_not_expire_session(
        self,
    ) -> None:
        self.pipeline.prepare()

        self.session.start()
        self.session.begin_processing()

        self.clock.advance(
            600.0
        )

        self.assertTrue(
            self.session.is_active
        )

        self.assertFalse(
            self.session.requires_wake_word()
        )

    def test_follow_up_listening_restores_timeout_window(
        self,
    ) -> None:
        self.pipeline.prepare()

        self.pipeline.listen_once()

        remaining = (
            self.session.seconds_until_timeout()
        )

        self.assertIsNotNone(
            remaining
        )

        self.assertAlmostEqual(
            remaining or 0.0,
            60.0,
        )

    def test_empty_transcript_fails_cleanly_but_keeps_session_active(
        self,
    ) -> None:
        self.speech.transcripts = [
            "   ",
        ]

        self.pipeline.prepare()

        result = (
            self.pipeline.listen_once()
        )

        self.assertFalse(
            result.success
        )

        self.assertEqual(
            result.transcript,
            "",
        )

        self.assertIn(
            "empty command",
            result.error or "",
        )

        self.assertEqual(
            self.orchestrator.plans,
            [],
        )

        self.assertTrue(
            self.session.is_active
        )

        self.assertFalse(
            self.session.requires_wake_word()
        )

    def test_orchestrator_failure_keeps_conversation_open(
        self,
    ) -> None:
        self.orchestrator.success = False
        self.orchestrator.error = (
            "Resource policy blocked task."
        )

        self.pipeline.prepare()

        result = (
            self.pipeline.listen_once()
        )

        self.assertFalse(
            result.success
        )

        self.assertEqual(
            result.error,
            "Resource policy blocked task.",
        )

        self.assertEqual(
            result.response,
            "",
        )

        self.assertTrue(
            self.session.is_active
        )

    def test_prepare_fails_when_vad_is_not_ready(
        self,
    ) -> None:
        self.vad.ready = False

        with self.assertRaisesRegex(
            RuntimeError,
            "VAD runtime is not ready",
        ):
            self.pipeline.prepare()

        self.assertFalse(
            self.pipeline.is_prepared
        )

    def test_prepare_fails_when_stt_is_not_ready(
        self,
    ) -> None:
        self.speech.ready = False

        with self.assertRaisesRegex(
            RuntimeError,
            "Speech runtime is not ready",
        ):
            self.pipeline.prepare()

        self.assertFalse(
            self.pipeline.is_prepared
        )

    def test_stop_stops_listening_but_keeps_session_object(
        self,
    ) -> None:
        self.pipeline.prepare()

        self.pipeline.listen_once()

        self.pipeline.stop()

        self.assertFalse(
            self.pipeline.is_prepared
        )

        self.assertFalse(
            self.pipeline.is_closed
        )

        self.assertFalse(
            self.audio.is_running()
        )

        self.assertEqual(
            self.vad.close_calls,
            0,
        )

    def test_close_closes_conversation_session(
        self,
    ) -> None:
        self.pipeline.prepare()

        self.pipeline.listen_once()

        self.pipeline.close()

        self.assertFalse(
            self.pipeline.is_prepared
        )

        self.assertTrue(
            self.pipeline.is_closed
        )

        self.assertFalse(
            self.audio.is_running()
        )

        self.assertEqual(
            self.vad.close_calls,
            1,
        )

        self.assertFalse(
            self.session.is_active
        )

        self.assertTrue(
            self.session.requires_wake_word()
        )

    def test_closed_pipeline_cannot_be_prepared_again(
        self,
    ) -> None:
        self.pipeline.close()

        with self.assertRaisesRegex(
            RuntimeError,
            "closed",
        ):
            self.pipeline.prepare()


if __name__ == "__main__":
    unittest.main()