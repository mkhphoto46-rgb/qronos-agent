from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from core.brain_runtime import BrainMessageRole
from core.conversation_history import ConversationHistory


class TestConversationHistory(unittest.TestCase):
    def test_text_and_voice_share_one_ordered_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = ConversationHistory(
                Path(tmp) / "history.sqlite3"
            )

            first = history.append(
                role="user",
                text="سلام",
                source="voice",
                request_id="voice-1",
                timestamp=1.0,
            )
            second = history.append(
                role="assistant",
                text="سلام",
                source="voice",
                request_id="voice-1",
                timestamp=2.0,
            )
            third = history.append(
                role="user",
                text="اسمم چیه؟",
                source="text",
                request_id="text-1",
                timestamp=3.0,
            )

            messages = history.all_messages()

            self.assertEqual(
                [message.id for message in messages],
                [first.id, second.id, third.id],
            )
            self.assertEqual(
                [message.source for message in messages],
                ["voice", "voice", "text"],
            )

    def test_brain_context_is_bounded_and_preserves_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = ConversationHistory(
                Path(tmp) / "history.sqlite3"
            )

            history.append(
                role="user",
                text="اسم من آراد است.",
                source="text",
                timestamp=1.0,
            )
            history.append(
                role="assistant",
                text="متوجه شدم.",
                source="text",
                timestamp=2.0,
            )
            history.append(
                role="user",
                text="این را یادت بماند.",
                source="voice",
                timestamp=3.0,
            )

            context = history.brain_context(
                max_messages=2,
                max_chars=1000,
            )

            self.assertEqual(
                [message.role for message in context],
                [
                    BrainMessageRole.ASSISTANT,
                    BrainMessageRole.USER,
                ],
            )
            self.assertEqual(
                [message.content for message in context],
                [
                    "متوجه شدم.",
                    "این را یادت بماند.",
                ],
            )

    def test_public_snapshot_maps_assistant_to_qronos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = ConversationHistory(
                Path(tmp) / "history.sqlite3"
            )

            history.append(
                role="assistant",
                text="پاسخ",
                source="voice",
                request_id="voice-9",
                timestamp=10.0,
            )

            payload = history.all_messages()[0].to_public_dict()

            self.assertEqual(payload["role"], "qronos")
            self.assertEqual(payload["source"], "voice")
            self.assertEqual(payload["requestId"], "voice-9")


if __name__ == "__main__":
    unittest.main()
