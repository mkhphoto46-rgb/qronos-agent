from __future__ import annotations

import unittest

from core.voice_trigger import (
    VoiceTriggerEvent,
    VoiceTriggerService,
    VoiceTriggerState,
)


class FakeWakeWordEngine:
    def __init__(self) -> None:
        self.running = False
        self.paused = False
        self.detect_next = False

    def start(self) -> None:
        self.running = True
        self.paused = False

    def stop(self) -> None:
        self.running = False
        self.paused = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def is_running(self) -> bool:
        return self.running

    def process_audio(self, audio_data: bytes) -> bool:
        return self.detect_next


class TestVoiceTriggerService(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FakeWakeWordEngine()
        self.service = VoiceTriggerService(
            wake_word="Qronos",
            engine=self.engine,
        )

    def test_initial_state_is_disabled(self) -> None:
        self.assertEqual(
            self.service.state,
            VoiceTriggerState.DISABLED,
        )

        self.assertFalse(
            self.service.is_running(),
        )

    def test_start_changes_state_to_listening(self) -> None:
        self.service.start()

        self.assertEqual(
            self.service.state,
            VoiceTriggerState.LISTENING,
        )

        self.assertTrue(
            self.service.is_running(),
        )

    def test_stop_changes_state_to_disabled(self) -> None:
        self.service.start()
        self.service.stop()

        self.assertEqual(
            self.service.state,
            VoiceTriggerState.DISABLED,
        )

        self.assertFalse(
            self.service.is_running(),
        )

    def test_pause_changes_state_to_paused(self) -> None:
        self.service.start()
        self.service.pause()

        self.assertEqual(
            self.service.state,
            VoiceTriggerState.PAUSED,
        )

    def test_resume_changes_state_to_listening(self) -> None:
        self.service.start()
        self.service.pause()
        self.service.resume()

        self.assertEqual(
            self.service.state,
            VoiceTriggerState.LISTENING,
        )

    def test_non_matching_audio_does_not_trigger(self) -> None:
        self.service.start()

        self.engine.detect_next = False

        event = self.service.process_audio(
            b"fake audio",
            timestamp=100.0,
        )

        self.assertIsNone(event)

        self.assertEqual(
            self.service.state,
            VoiceTriggerState.LISTENING,
        )

    def test_matching_audio_creates_event(self) -> None:
        self.service.start()

        self.engine.detect_next = True

        event = self.service.process_audio(
            b"fake audio",
            timestamp=100.0,
        )

        self.assertIsInstance(
            event,
            VoiceTriggerEvent,
        )

        self.assertEqual(
            event.event_type,
            "wake_word_detected",
        )

        self.assertEqual(
            event.wake_word,
            "Qronos",
        )

        self.assertEqual(
            event.timestamp,
            100.0,
        )

        self.assertEqual(
            self.service.state,
            VoiceTriggerState.TRIGGERED,
        )

    def test_audio_is_ignored_when_disabled(self) -> None:
        self.engine.detect_next = True

        event = self.service.process_audio(
            b"fake audio",
            timestamp=100.0,
        )

        self.assertIsNone(event)

    def test_audio_is_ignored_when_paused(self) -> None:
        self.service.start()
        self.service.pause()

        self.engine.detect_next = True

        event = self.service.process_audio(
            b"fake audio",
            timestamp=100.0,
        )

        self.assertIsNone(event)

        self.assertEqual(
            self.service.state,
            VoiceTriggerState.PAUSED,
        )

    def test_start_without_engine_fails(self) -> None:
        service = VoiceTriggerService(
            wake_word="Qronos",
            engine=None,
        )

        with self.assertRaises(RuntimeError):
            service.start()


if __name__ == "__main__":
    unittest.main()