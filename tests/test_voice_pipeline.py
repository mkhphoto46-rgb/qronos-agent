from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.command_recorder import (
    CommandRecordingResult,
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
        transcript: str = "Hello Qronos",
        ready: bool = True,
    ) -> None:
        self.transcript = transcript
        self.ready = ready
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

        self.calls += 1

        return self.transcript


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
        success: bool = True,
        output: str = "Brain response",
        error: str | None = None,
    ) -> None:
        self.success = success
        self.output = output
        self.error = error
        self.plans = []

    def execute_plan(
        self,
        plan,
    ) -> list[StepResult]:
        self.plans.append(
            plan
        )

        return [
            StepResult(
                order=1,
                success=self.success,
                output=(
                    self.output
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

        self.audio = FakeAudioInput()
        self.trigger = FakeVoiceTrigger()
        self.vad = FakeVADRuntime()

        self.recorder = FakeCommandRecorder(
            self.root
            / "recorded.wav"
        )

        self.speech = FakeSpeechRuntime(
            transcript="Hello Qronos"
        )

        self.router = FakeTaskRouter(
            TaskType.FAST
        )

        self.orchestrator = (
            FakeOrchestrator(
                success=True,
                output="Hello from Qronos.",
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

    def test_wake_callback_is_called(
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
                self.wake_events
            ),
            1,
        )

        self.assertEqual(
            self.wake_events[0].wake_word,
            "Qronos",
        )

        self.assertEqual(
            self.trigger.pause_calls,
            1,
        )

    def test_successful_voice_command_reaches_brain(
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
            "Hello Qronos",
        )

        self.assertEqual(
            result.response,
            "Hello from Qronos.",
        )

        self.assertIsNotNone(
            result.wake_event
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

        self.assertEqual(
            self.router.inputs,
            [
                "Hello Qronos",
            ],
        )

        self.assertEqual(
            len(
                self.orchestrator.plans
            ),
            1,
        )

        plan = (
            self.orchestrator.plans[0]
        )

        self.assertEqual(
            plan.goal,
            "Hello Qronos",
        )

        self.assertEqual(
            len(plan.steps),
            1,
        )

        self.assertEqual(
            plan.steps[0].task_type,
            TaskType.FAST,
        )

    def test_microphone_is_paused_during_brain_work_and_rearmed(
        self,
    ) -> None:
        self.pipeline.prepare()

        self.pipeline.listen_once()

        self.assertEqual(
            self.trigger.pause_calls,
            1,
        )

        self.assertEqual(
            self.trigger.resume_calls,
            1,
        )

        self.assertGreaterEqual(
            self.audio.stop_calls,
            1,
        )

        self.assertTrue(
            self.audio.is_running()
        )

    def test_empty_transcript_fails_cleanly(
        self,
    ) -> None:
        self.speech.transcript = "   "

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

        self.assertEqual(
            self.trigger.resume_calls,
            1,
        )

    def test_orchestrator_failure_is_returned(
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

    def test_stop_stops_listening_but_does_not_close_vad(
        self,
    ) -> None:
        self.pipeline.prepare()

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

    def test_close_releases_pipeline_resources(
        self,
    ) -> None:
        self.pipeline.prepare()

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