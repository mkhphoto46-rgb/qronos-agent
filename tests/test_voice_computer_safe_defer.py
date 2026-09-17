from __future__ import annotations

import unittest

import core.runtime_bridge as runtime_bridge
from core.task_router import TaskType
from tests.test_runtime_bridge import (
    FakeOrchestrator,
    FakeSpeechRuntime,
    captured_events,
    prepared_runtime,
)


REAL_COMPUTER_TRANSCRIPT = (
    "میتونی یه فایل به اسم ZW توی دسکتاپ برام درست کنی "
    "یه فولدر در واقعی یه فولدر درست کن توی دسکتاپ به اسم ZW"
)


class TestVoiceComputerSafeDefer(unittest.TestCase):
    def test_fixture_is_really_classified_as_computer(self) -> None:
        runtime = prepared_runtime(
            speech=FakeSpeechRuntime(
                transcript=REAL_COMPUTER_TRANSCRIPT
            )
        )

        route = runtime.task_router.route(
            REAL_COMPUTER_TRANSCRIPT
        )

        self.assertIs(
            route.task_type,
            TaskType.COMPUTER,
        )

    def test_voice_computer_route_never_enters_orchestrator_or_runtime_error(self) -> None:
        orchestrator = FakeOrchestrator()
        runtime = prepared_runtime(
            speech=FakeSpeechRuntime(
                transcript=REAL_COMPUTER_TRANSCRIPT
            ),
            orchestrator=orchestrator,
        )
        runtime.conversation_session.start()

        # Keep this unit test deterministic and thread-free. Production may
        # warm STT after TTS; that is unrelated to the routing safety contract.
        runtime._warm_speech_runtime_async = lambda: None

        reports: list[dict] = []
        runtime._write_latency_report = lambda payload: reports.append(dict(payload))

        playback: list[float] = []
        events = captured_events(
            lambda: playback.append(
                runtime._run_voice_turn(
                    trigger_source="wake_word"
                )
            )
        )

        event_types = [
            event["eventType"]
            for event in events
        ]
        responses = [
            event["message"]
            for event in events
            if event["eventType"] == "voice_response"
        ]

        self.assertEqual(orchestrator.plans, [])
        self.assertNotIn("runtime_error", event_types)
        self.assertGreater(playback[0], 0.0)
        self.assertIn(
            runtime_bridge.VOICE_COMPUTER_DEFERRED_RESPONSE,
            responses,
        )
        self.assertTrue(
            runtime.conversation_session.is_active
        )
        self.assertTrue(reports)
        self.assertEqual(
            reports[-1].get("route"),
            TaskType.COMPUTER.value,
        )
        self.assertEqual(
            reports[-1].get("selectedRoute"),
            TaskType.COMPUTER.value,
        )
        self.assertTrue(
            reports[-1].get("voiceComputerDeferred")
        )

    def test_existing_text_file_control_is_not_changed_by_voice_defer(self) -> None:
        source = (
            runtime_bridge.QronosRuntime
            .run_text_turn
            .__code__
            .co_names
        )
        self.assertIn(
            "_begin_text_file_action",
            source,
        )


if __name__ == "__main__":
    unittest.main()
