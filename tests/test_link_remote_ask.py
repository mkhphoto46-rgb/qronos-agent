"""
A phone outside the house can ask questions and nothing more.

The remote capability ceiling is ASK, SEARCH_WEB and READ_STATUS. ``ask`` is a
natural-language channel, so the ceiling would be hollow if a remote question
could reach the full assistant, which can open applications, change files and
queue work. These tests put a hostile remote session in front of a stand-in
for that full assistant and check it is never reached.
"""

from __future__ import annotations

import threading
import unittest

from core.link_capability import LinkOp
from core.link_server import LinkSettings
from core.link_handlers import (
    REMOTE_ACTION_REFUSAL,
    REMOTE_SYSTEM_PROMPT,
    LinkOrigin,
    ToolFreeAsk,
    ask_handler,
    default_handlers,
)
from tests.fixtures.link_remote_rig import RemoteRig


ACTION_REQUESTS = (
    "open notepad",
    "delete my report file",
    "print the document",
    "turn off the computer",
    "نوت‌پد را باز کن",
    "فایل گزارش را پاک کن",
    "سند را چاپ کن",
    "پرینت بگیر",
    "کامپیوتر را خاموش کن",
    "تلگرام را باز کن و پیام بفرست",
    "look at my screen",
    "search the web for weather",
)


class FullAssistant:
    """
    Stands in for the PC's whole assistant: router, orchestrator, tools, queue.

    Any call counts as an action taken, because that is what reaching it from
    a remote session would make possible.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, question, emit):
        with self.lock:
            self.calls.append(question)

        return {"answer": "done", "performed_actions": ["something"]}


class Chat:
    def __init__(self, reply: str = "پاسخ") -> None:
        self.reply = reply
        self.messages: list[list[dict[str, str]]] = []

    def __call__(self, messages):
        self.messages.append(messages)
        return self.reply


class RemoteAskTestCase(unittest.TestCase):
    def build(self, remote_ask=None, chat: Chat | None = None) -> None:
        self.assistant = FullAssistant()
        self.chat = chat if chat is not None else Chat()
        self.remote_ask = remote_ask if remote_ask is not None else ToolFreeAsk(self.chat)
        # A burst large enough for every hostile request in one session; the
        # request budget has its own tests.
        self.rig = RemoteRig(
            handlers=default_handlers(ask=self.assistant, remote_ask=self.remote_ask),
            settings=LinkSettings(remote_burst=50),
        )
        self.addCleanup(self.rig.close)


class TestHostileRemoteSession(RemoteAskTestCase):
    def setUp(self) -> None:
        self.build()

    def test_action_requests_never_reach_the_assistant_or_the_model(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record, timeout=10) as phone:
            for text in ACTION_REQUESTS:
                with self.subTest(text=text):
                    response = phone.call("ask", question=text)

                    self.assertTrue(response.ok)
                    self.assertEqual(response.result["answer"], REMOTE_ACTION_REFUSAL)
                    self.assertTrue(response.result["refused_action"])
                    self.assertEqual(response.result["performed_actions"], [])

        self.assertEqual(self.assistant.calls, [])
        self.assertEqual(self.chat.messages, [])

    def test_an_ordinary_question_is_answered_by_the_chat_alone(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            response = phone.call("ask", question="پایتخت فرانسه کجاست")

        self.assertEqual(response.result["answer"], "پاسخ")
        self.assertEqual(response.result["performed_actions"], [])
        self.assertEqual(self.assistant.calls, [])
        self.assertEqual(self.chat.messages[0][0], {"role": "system", "content": REMOTE_SYSTEM_PROMPT})
        self.assertEqual(self.chat.messages[0][1]["content"], "پایتخت فرانسه کجاست")

    def test_the_same_device_on_the_local_network_reaches_the_assistant(self) -> None:
        record = self.rig.remote_device()

        with self.rig.local_phone(record) as phone:
            phone.call("ask", question="open notepad")

        self.assertEqual(self.assistant.calls, ["open notepad"])


class TestModelThatClaimsToAct(RemoteAskTestCase):
    def test_a_false_claim_is_contradicted_by_the_result(self) -> None:
        self.build(chat=Chat("I have opened Notepad for you."))
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            response = phone.call("ask", question="what is two plus two")

        # The model's words are its own; the fact the phone shows is not.
        self.assertEqual(response.result["performed_actions"], [])
        self.assertEqual(self.assistant.calls, [])


class TestWiring(unittest.TestCase):
    def test_without_a_remote_path_remote_questions_are_refused(self) -> None:
        assistant = FullAssistant()
        rig = RemoteRig(handlers=default_handlers(ask=assistant))
        self.addCleanup(rig.close)
        record = rig.remote_device()

        with rig.phone(record) as phone:
            response = phone.call("ask", question="hello")

        self.assertEqual(response.code, "not_configured")
        self.assertEqual(assistant.calls, [])

    def test_a_plain_function_cannot_be_the_remote_path(self) -> None:
        # A wiring mistake fails when the handler is built, not when a phone
        # outside the house asks for something.
        with self.assertRaises(TypeError):
            ask_handler(None, remote_ask=lambda question, emit, origin: {})

        with self.assertRaises(TypeError):
            default_handlers(remote_ask=FullAssistant())

    def test_tool_free_ask_needs_a_callable(self) -> None:
        with self.assertRaises(TypeError):
            ToolFreeAsk(None)  # type: ignore[arg-type]

    def test_the_origin_carries_the_ceiling(self) -> None:
        captured: list[LinkOrigin] = []

        class Recording(ToolFreeAsk):
            def __call__(self, question, emit, origin):
                captured.append(origin)
                return super().__call__(question, emit, origin)

        rig = RemoteRig(handlers={LinkOp.ASK: ask_handler(None, Recording(Chat()))})
        self.addCleanup(rig.close)
        record = rig.remote_device()

        with rig.phone(record) as phone:
            phone.call("ask", question="hello")

        self.assertTrue(captured[0].remote)
        self.assertEqual(captured[0].device_id, record.device_id)
        self.assertEqual(
            sorted(c.value for c in captured[0].capabilities),
            ["ask", "read_status", "search_web"],
        )


if __name__ == "__main__":
    unittest.main()
