from __future__ import annotations

import json
import unittest
from unittest.mock import PropertyMock, patch

from core.runtime_bridge import (
    PUSH_TO_TALK_ACTION,
    QronosRuntime,
    handle_action,
    handle_queue_set_paused,
    handle_runtime_set_paused,
    handle_text_message,
)


class FakeScheduler:
    def __init__(self) -> None:
        self.pause_calls: list[bool] = []

    def set_paused(self, paused: bool) -> None:
        self.pause_calls.append(paused)


class RuntimePauseV1Tests(unittest.TestCase):
    def make_runtime(self):
        events: list[tuple[str, str, str]] = []
        scheduler = FakeScheduler()
        runtime = QronosRuntime(
            scheduler=scheduler,
            notify=lambda event, status, message: events.append(
                (event, status, message)
            ),
        )
        return runtime, scheduler, events

    def test_runtime_pause_defaults_to_active_and_is_not_persisted(self) -> None:
        first, _, _ = self.make_runtime()
        self.assertFalse(first.runtime_paused)

        first.set_runtime_paused(True)
        self.assertTrue(first.runtime_paused)

        restarted, _, _ = self.make_runtime()
        self.assertFalse(restarted.runtime_paused)

    def test_runtime_pause_pauses_existing_scheduler_and_emits_state(self) -> None:
        runtime, scheduler, events = self.make_runtime()

        runtime.set_runtime_paused(True)

        self.assertEqual(scheduler.pause_calls, [True])
        self.assertEqual(events[-1][0], "runtime_pause_changed")
        payload = json.loads(events[-1][2])
        self.assertTrue(payload["paused"])
        self.assertTrue(payload["temporary"])
        self.assertTrue(payload["resetsOnRestart"])

    def test_text_message_is_rejected_while_runtime_is_paused(self) -> None:
        runtime, _, events = self.make_runtime()
        runtime.set_runtime_paused(True)
        events.clear()

        handle_text_message(
            runtime,
            {
                "text": "سلام",
                "requestId": "pause-test-1",
            },
        )

        self.assertEqual(events[-1][0], "text_error")
        self.assertEqual(events[-1][1], "paused")
        payload = json.loads(events[-1][2])
        self.assertEqual(payload["requestId"], "pause-test-1")
        self.assertIn("temporarily paused", payload["error"])

    def test_push_to_talk_action_is_rejected_while_paused(self) -> None:
        runtime, _, events = self.make_runtime()
        runtime.set_runtime_paused(True)
        events.clear()

        handle_action(
            runtime,
            {
                "actionId": PUSH_TO_TALK_ACTION,
            },
        )

        self.assertTrue(
            any(event == "runtime_paused_rejected" for event, _, _ in events)
        )

    def test_queue_cannot_be_resumed_under_global_pause(self) -> None:
        runtime, scheduler, events = self.make_runtime()
        runtime.set_runtime_paused(True)
        events.clear()

        handle_queue_set_paused(
            runtime,
            {"paused": False},
        )

        self.assertEqual(scheduler.pause_calls, [True])
        self.assertTrue(
            any(event == "runtime_paused_rejected" for event, _, _ in events)
        )

    def test_resume_restores_wake_when_pause_stopped_it(self) -> None:
        runtime, scheduler, _ = self.make_runtime()

        with (
            patch.object(
                QronosRuntime,
                "wake_listener_running",
                new_callable=PropertyMock,
                return_value=True,
            ),
            patch.object(runtime, "stop_wake_listener") as stop_wake,
            patch.object(runtime, "start_wake_listener") as start_wake,
        ):
            runtime.set_runtime_paused(True)
            runtime.set_runtime_paused(False)

        stop_wake.assert_called_once_with()
        start_wake.assert_called_once_with()
        self.assertEqual(scheduler.pause_calls, [True, False])
        self.assertFalse(runtime.runtime_paused)

    def test_repeated_pause_preserves_wake_resume_intent(self) -> None:
        runtime, _, _ = self.make_runtime()

        with (
            patch.object(
                QronosRuntime,
                "wake_listener_running",
                new_callable=PropertyMock,
                side_effect=[True, False],
            ),
            patch.object(runtime, "stop_wake_listener"),
            patch.object(runtime, "start_wake_listener") as start_wake,
        ):
            runtime.set_runtime_paused(True)
            runtime.set_runtime_paused(True)
            runtime.set_runtime_paused(False)

        start_wake.assert_called_once_with()

    def test_runtime_pause_command_resumes_scheduler(self) -> None:
        runtime, scheduler, _ = self.make_runtime()
        handle_runtime_set_paused(
            runtime,
            {"paused": True},
        )
        handle_runtime_set_paused(
            runtime,
            {"paused": False},
        )

        self.assertEqual(scheduler.pause_calls, [True, False])
        self.assertFalse(runtime.runtime_paused)


if __name__ == "__main__":
    unittest.main()
