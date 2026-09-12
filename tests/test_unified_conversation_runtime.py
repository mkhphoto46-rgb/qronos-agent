from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from core.conversation_history import ConversationHistory
from core.orchestrator import StepResult
from core.runtime_bridge import QronosRuntime
from core.task_router import TaskRouter


class FakeContextOrchestrator:
    def __init__(self) -> None:
        self.contexts = []

    def execute_plan(
        self,
        plan,
        conversation_messages=None,
    ):
        self.contexts.append(
            tuple(
                conversation_messages or ()
            )
        )
        return [
            StepResult(
                order=1,
                success=True,
                output="اسم شما آراد است.",
            )
        ]


class TestUnifiedConversationRuntime(unittest.TestCase):
    def test_text_turn_uses_prior_voice_history_as_model_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            runtime = QronosRuntime(
                notify=lambda event_type, status, message: events.append(
                    {
                        "eventType": event_type,
                        "status": status,
                        "message": message,
                    }
                )
            )
            runtime.conversation_history = ConversationHistory(
                Path(tmp) / "history.sqlite3"
            )
            runtime.task_router = TaskRouter()
            runtime.orchestrator = FakeContextOrchestrator()

            runtime.conversation_history.append(
                role="user",
                text="اسم من آراد است.",
                source="voice",
                request_id="voice-1",
                timestamp=1.0,
            )
            runtime.conversation_history.append(
                role="assistant",
                text="متوجه شدم.",
                source="voice",
                request_id="voice-1",
                timestamp=2.0,
            )

            runtime.run_text_turn(
                "اسمم چیه؟",
                "text-1",
            )

            self.assertEqual(
                len(runtime.orchestrator.contexts),
                1,
            )

            context = runtime.orchestrator.contexts[0]

            self.assertEqual(
                [message.content for message in context],
                [
                    "اسم من آراد است.",
                    "متوجه شدم.",
                ],
            )

            history = runtime.conversation_history.all_messages()

            self.assertEqual(
                [message.source for message in history],
                [
                    "voice",
                    "voice",
                    "text",
                    "text",
                ],
            )
            self.assertEqual(
                [message.role for message in history],
                [
                    "user",
                    "assistant",
                    "user",
                    "assistant",
                ],
            )
            self.assertEqual(
                history[-1].text,
                "اسم شما آراد است.",
            )

            canonical_events = [
                json.loads(event["message"])
                for event in events
                if event["eventType"]
                == "conversation_message_added"
            ]

            self.assertEqual(
                [event["role"] for event in canonical_events],
                ["user", "qronos"],
            )

    def test_history_snapshot_contains_one_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            runtime = QronosRuntime(
                notify=lambda event_type, status, message: events.append(
                    {
                        "eventType": event_type,
                        "status": status,
                        "message": message,
                    }
                )
            )
            runtime.conversation_history = ConversationHistory(
                Path(tmp) / "history.sqlite3"
            )

            runtime.conversation_history.append(
                role="user",
                text="پیام صوتی",
                source="voice",
                timestamp=1.0,
            )
            runtime.conversation_history.append(
                role="assistant",
                text="پاسخ متنی",
                source="text",
                timestamp=2.0,
            )

            runtime.emit_conversation_history_snapshot()

            snapshot = next(
                event
                for event in events
                if event["eventType"]
                == "conversation_history_snapshot"
            )

            payload = json.loads(
                snapshot["message"]
            )

            self.assertEqual(
                [item["source"] for item in payload["messages"]],
                ["voice", "text"],
            )
            self.assertEqual(
                [item["role"] for item in payload["messages"]],
                ["user", "qronos"],
            )


if __name__ == "__main__":
    unittest.main()
