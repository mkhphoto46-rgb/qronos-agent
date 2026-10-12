from __future__ import annotations

import unittest

from core.runtime_bridge import QronosRuntime


class AliveThread:
    def is_alive(self) -> bool:
        return True


class TestWakeReadinessContract(unittest.TestCase):
    def test_duplicate_start_during_startup_does_not_claim_listening(
        self,
    ) -> None:
        events: list[tuple[str, str, str]] = []

        runtime = QronosRuntime(
            notify=lambda event_type, status, message: events.append(
                (event_type, status, message)
            )
        )

        runtime._wake_thread = AliveThread()
        runtime._wake_ready.clear()

        runtime.start_wake_listener()

        self.assertEqual(events, [])

    def test_duplicate_start_after_true_ready_can_report_already_active(
        self,
    ) -> None:
        events: list[tuple[str, str, str]] = []

        runtime = QronosRuntime(
            notify=lambda event_type, status, message: events.append(
                (event_type, status, message)
            )
        )

        runtime._wake_thread = AliveThread()
        runtime._wake_ready.set()

        runtime.start_wake_listener()

        self.assertEqual(
            events,
            [
                (
                    "wake_word_listening",
                    "listening",
                    "Qronos wake-word listener is already active.",
                )
            ],
        )

    def test_ready_property_requires_live_thread_and_ready_event(
        self,
    ) -> None:
        runtime = QronosRuntime()

        runtime._wake_thread = AliveThread()
        runtime._wake_ready.clear()

        self.assertTrue(runtime.wake_listener_running)
        self.assertFalse(runtime.wake_listener_ready)

        runtime._wake_ready.set()

        self.assertTrue(runtime.wake_listener_ready)

        runtime._wake_thread = None

        self.assertFalse(runtime.wake_listener_running)
        self.assertFalse(runtime.wake_listener_ready)

    def test_stop_clears_wake_readiness(self) -> None:
        runtime = QronosRuntime()
        runtime._wake_ready.set()

        runtime.stop_wake_listener()

        self.assertFalse(runtime._wake_ready.is_set())


if __name__ == "__main__":
    unittest.main()
