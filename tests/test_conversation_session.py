from __future__ import annotations

import unittest

from core.conversation_session import (
    ConversationRole,
    ConversationSession,
    ConversationSessionConfig,
    ConversationState,
    DEFAULT_INACTIVITY_TIMEOUT_SECONDS,
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


class TestConversationSession(
    unittest.TestCase
):
    def setUp(self) -> None:
        self.clock = FakeClock()

        self.session = ConversationSession(
            clock=self.clock
        )

    def test_default_timeout_is_60_seconds(
        self,
    ) -> None:
        self.assertEqual(
            DEFAULT_INACTIVITY_TIMEOUT_SECONDS,
            60.0,
        )

        self.assertEqual(
            self.session.config.inactivity_timeout_seconds,
            60.0,
        )

    def test_timeout_must_be_positive(
        self,
    ) -> None:
        with self.assertRaises(
            ValueError
        ):
            ConversationSessionConfig(
                inactivity_timeout_seconds=0.0
            )

    def test_new_session_requires_wake_word(
        self,
    ) -> None:
        self.assertFalse(
            self.session.is_active
        )

        self.assertTrue(
            self.session.requires_wake_word()
        )

        self.assertEqual(
            self.session.state,
            ConversationState.WAITING_FOR_WAKE,
        )

    def test_start_opens_conversation(
        self,
    ) -> None:
        self.session.start()

        self.assertTrue(
            self.session.is_active
        )

        self.assertFalse(
            self.session.requires_wake_word()
        )

        self.assertEqual(
            self.session.state,
            ConversationState.LISTENING,
        )

        self.assertEqual(
            self.session.started_at,
            100.0,
        )

        self.assertEqual(
            self.session.idle_since,
            100.0,
        )

    def test_active_session_does_not_require_wake_word(
        self,
    ) -> None:
        self.session.start()

        self.clock.advance(
            20.0
        )

        self.assertFalse(
            self.session.requires_wake_word()
        )

        self.assertTrue(
            self.session.is_active
        )

    def test_session_expires_after_60_seconds_of_listening(
        self,
    ) -> None:
        self.session.start()

        self.clock.advance(
            60.0
        )

        self.assertTrue(
            self.session.requires_wake_word()
        )

        self.assertFalse(
            self.session.is_active
        )

        self.assertEqual(
            self.session.state,
            ConversationState.WAITING_FOR_WAKE,
        )

    def test_session_does_not_expire_before_timeout(
        self,
    ) -> None:
        self.session.start()

        self.clock.advance(
            59.9
        )

        self.assertTrue(
            self.session.is_active
        )

        self.assertFalse(
            self.session.requires_wake_word()
        )

    def test_processing_pauses_timeout(
        self,
    ) -> None:
        self.session.start()

        self.clock.advance(
            50.0
        )

        self.session.begin_processing()

        self.clock.advance(
            120.0
        )

        self.assertTrue(
            self.session.is_active
        )

        self.assertEqual(
            self.session.state,
            ConversationState.PROCESSING,
        )

        self.assertIsNone(
            self.session.seconds_until_timeout()
        )

    def test_responding_pauses_timeout(
        self,
    ) -> None:
        self.session.start()

        self.session.begin_responding()

        self.clock.advance(
            300.0
        )

        self.assertTrue(
            self.session.is_active
        )

        self.assertEqual(
            self.session.state,
            ConversationState.RESPONDING,
        )

        self.assertIsNone(
            self.session.seconds_until_timeout()
        )

    def test_follow_up_listening_restarts_timeout(
        self,
    ) -> None:
        self.session.start()

        self.clock.advance(
            30.0
        )

        self.session.begin_processing()

        self.clock.advance(
            100.0
        )

        self.session.begin_responding()

        self.clock.advance(
            100.0
        )

        self.session.begin_listening()

        self.assertEqual(
            self.session.state,
            ConversationState.LISTENING,
        )

        self.assertAlmostEqual(
            self.session.seconds_until_timeout()
            or 0.0,
            60.0,
        )

        self.clock.advance(
            59.0
        )

        self.assertTrue(
            self.session.is_active
        )

        self.clock.advance(
            1.0
        )

        self.assertFalse(
            self.session.is_active
        )

    def test_user_message_is_added_to_history(
        self,
    ) -> None:
        self.session.start()

        message = (
            self.session.add_user_message(
                "  Hello Qronos  "
            )
        )

        self.assertEqual(
            message.role,
            ConversationRole.USER,
        )

        self.assertEqual(
            message.content,
            "Hello Qronos",
        )

        self.assertEqual(
            self.session.message_count,
            1,
        )

    def test_assistant_message_is_added_to_history(
        self,
    ) -> None:
        self.session.start()

        message = (
            self.session.add_assistant_message(
                "Hello from Qronos."
            )
        )

        self.assertEqual(
            message.role,
            ConversationRole.ASSISTANT,
        )

        self.assertEqual(
            message.content,
            "Hello from Qronos.",
        )

    def test_history_preserves_message_order(
        self,
    ) -> None:
        self.session.start()

        self.session.add_user_message(
            "What is two plus two?"
        )

        self.session.add_assistant_message(
            "Four."
        )

        self.session.add_user_message(
            "Multiply that by two."
        )

        messages = (
            self.session.messages
        )

        self.assertEqual(
            [
                message.content
                for message in messages
            ],
            [
                "What is two plus two?",
                "Four.",
                "Multiply that by two.",
            ],
        )

    def test_empty_message_is_rejected(
        self,
    ) -> None:
        self.session.start()

        with self.assertRaises(
            ValueError
        ):
            self.session.add_user_message(
                "   "
            )

    def test_messages_cannot_be_added_when_inactive(
        self,
    ) -> None:
        with self.assertRaises(
            RuntimeError
        ):
            self.session.add_user_message(
                "Hello"
            )

    def test_close_requires_wake_word_again(
        self,
    ) -> None:
        self.session.start()

        self.session.add_user_message(
            "Hello"
        )

        self.session.close()

        self.assertFalse(
            self.session.is_active
        )

        self.assertTrue(
            self.session.requires_wake_word()
        )

        self.assertEqual(
            self.session.state,
            ConversationState.WAITING_FOR_WAKE,
        )

        self.assertEqual(
            self.session.message_count,
            1,
        )

    def test_new_conversation_clears_previous_active_context(
        self,
    ) -> None:
        self.session.start()

        self.session.add_user_message(
            "Old conversation"
        )

        self.session.add_assistant_message(
            "Old answer"
        )

        self.session.close()

        self.clock.advance(
            10.0
        )

        self.session.start()

        self.assertEqual(
            self.session.message_count,
            0,
        )

        self.assertEqual(
            self.session.messages,
            (),
        )

        self.assertTrue(
            self.session.is_active
        )

    def test_seconds_until_timeout_counts_down(
        self,
    ) -> None:
        self.session.start()

        self.assertAlmostEqual(
            self.session.seconds_until_timeout()
            or 0.0,
            60.0,
        )

        self.clock.advance(
            17.5
        )

        self.assertAlmostEqual(
            self.session.seconds_until_timeout()
            or 0.0,
            42.5,
        )


if __name__ == "__main__":
    unittest.main()