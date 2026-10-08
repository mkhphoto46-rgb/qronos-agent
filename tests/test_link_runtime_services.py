"""
The link's services built from the running Qronos, on a stand-in runtime.

The stand-in has the same attribute names the real runtime has
(``orchestrator``, ``speech_runtime``, ``voice_output``, ``is_busy``,
``runtime_paused``) and the same method names. Its plan executor and its queue
raise if they are touched at all: the remote path must reach the guarded
prompt-only brain call and nothing that plans, acts or queues. No model, no
microphone, no speaker.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.link_handlers import REMOTE_ACTION_REFUSAL, REMOTE_SYSTEM_PROMPT, default_handlers, ToolFreeAsk
from core.brain_runtime import BrainMessageRole
from core.link_runtime import brain_messages, runtime_services
from core.link_server import LinkSettings
from tests.fixtures.link_remote_rig import RemoteRig


class Forbidden(AssertionError):
    pass


class StandInOrchestrator:
    def __init__(self) -> None:
        self.prompts: list[list] = []
        self.refuse = False

    def answer_chat_only(self, messages):
        if self.refuse:
            raise RuntimeError("resource admission refused")

        self.prompts.append(list(messages))
        return "پاسخ کوتاه"

    def answer_web_prompt(self, prompt: str) -> str:
        # The web-research profile is not the Fast profile (review finding L4).
        raise Forbidden("the remote path used the web-research overrides")

    def execute_plan(self, *args, **kwargs):
        raise Forbidden("the remote path reached the planner")


class StandInScheduler:
    def submit(self, *args, **kwargs):
        raise Forbidden("the remote path reached the queue")


class StandInSpeech:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def transcribe_file(self, audio_path, language="auto"):
        self.calls.append((Path(audio_path).suffix, language))
        return "سلام"


class Utterance:
    def __init__(self, path: Path) -> None:
        self.audio_path = path


class StandInVoice:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def speak_to_file(self, text, destination=None):
        path = self.folder / "reply.wav"
        path.write_bytes(b"RIFF....WAVE")
        return Utterance(path)


class StandInRuntime:
    def __init__(self, folder: Path) -> None:
        self.orchestrator = StandInOrchestrator()
        self.scheduler = StandInScheduler()
        self.speech_runtime = StandInSpeech()
        self.voice_output = StandInVoice(folder)
        self.is_busy = False
        self.runtime_paused = False


class TestMessages(unittest.TestCase):
    def test_the_rules_come_before_the_question_as_brain_messages(self) -> None:
        messages = brain_messages(
            [
                {"role": "system", "content": REMOTE_SYSTEM_PROMPT},
                {"role": "user", "content": "پایتخت فرانسه کجاست"},
                {"role": "assistant", "content": "not passed on"},
                {"role": "user", "content": "   "},
            ]
        )

        self.assertEqual([m.role for m in messages], [BrainMessageRole.SYSTEM, BrainMessageRole.USER])
        self.assertEqual(messages[0].content, REMOTE_SYSTEM_PROMPT)
        self.assertEqual(messages[1].content, "پایتخت فرانسه کجاست")


class ServicesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runtime = StandInRuntime(Path(folder.name))
        services = runtime_services(self.runtime)
        remote_ask = ToolFreeAsk(services.remote_chat)
        self.rig = RemoteRig(
            handlers=default_handlers(
                ask=remote_ask,
                remote_ask=remote_ask,
                transcribe=services.transcribe,
                speak=services.speak,
            ),
            settings=LinkSettings(remote_burst=50),
        )
        self.addCleanup(self.rig.close)
        self.record = self.rig.remote_device()


class TestRemoteAskThroughTheRuntime(ServicesTestCase):
    def test_a_remote_question_reaches_only_the_guarded_prompt_call(self) -> None:
        with self.rig.phone(self.record) as phone:
            response = phone.call("ask", question="پایتخت فرانسه کجاست")

        self.assertEqual(response.result["answer"], "پاسخ کوتاه")
        self.assertEqual(response.result["performed_actions"], [])
        self.assertEqual(len(self.runtime.orchestrator.prompts), 1)
        self.assertEqual(self.runtime.orchestrator.prompts[0][0].content, REMOTE_SYSTEM_PROMPT)

    def test_action_requests_reach_nothing_in_the_runtime(self) -> None:
        with self.rig.phone(self.record) as phone:
            for text in ("open notepad", "فایل گزارش را پاک کن", "سند را چاپ کن"):
                with self.subTest(text=text):
                    self.assertEqual(phone.call("ask", question=text).result["answer"], REMOTE_ACTION_REFUSAL)

        self.assertEqual(self.runtime.orchestrator.prompts, [])

    def test_a_busy_runtime_refuses_rather_than_queues(self) -> None:
        self.runtime.is_busy = True

        with self.rig.phone(self.record) as phone:
            response = phone.call("ask", question="سلام")

        self.assertEqual(response.code, "busy")
        self.assertEqual(self.runtime.orchestrator.prompts, [])

    def test_a_paused_runtime_refuses_too(self) -> None:
        self.runtime.runtime_paused = True

        with self.rig.phone(self.record) as phone:
            self.assertEqual(phone.call("ask", question="سلام").code, "busy")

    def test_without_an_orchestrator_the_phone_is_told_to_wait(self) -> None:
        self.runtime.orchestrator = None

        with self.rig.phone(self.record) as phone:
            self.assertEqual(phone.call("ask", question="سلام").code, "busy")


class TestAdmission(ServicesTestCase):
    def test_a_refused_admission_is_busy_not_queued(self) -> None:
        self.runtime.orchestrator.refuse = True

        with self.rig.phone(self.record) as phone:
            self.assertEqual(phone.call("ask", question="سلام").code, "busy")

        self.assertEqual(self.runtime.orchestrator.prompts, [])

    def test_a_runtime_without_the_chat_only_call_is_not_served_another_profile(self) -> None:
        class Old:
            def answer_web_prompt(self, prompt):
                raise Forbidden("fell back to the web-research profile")

        self.runtime.orchestrator = Old()

        with self.rig.phone(self.record) as phone:
            self.assertEqual(phone.call("ask", question="سلام").code, "not_configured")


class TestVoiceThroughTheRuntime(ServicesTestCase):
    def test_push_to_talk_uses_the_runtimes_speech_and_voice(self) -> None:
        import base64

        pcm = b"\x01\x00" * 16_000

        with self.rig.phone(self.record) as phone:
            voice_id = phone.call("voice_begin", language="fa").result["voice_id"]
            phone.call("voice_chunk", voice_id=voice_id, seq=0, data=base64.b64encode(pcm).decode())
            response = phone.call("voice_end", voice_id=voice_id, reply="audio")
            replies = [e for e in phone.events if e.topic == "voice_reply"]

        self.assertTrue(response.ok)
        self.assertEqual(self.runtime.speech_runtime.calls, [(".wav", "fa")])
        self.assertEqual(response.result["answer"], "پاسخ کوتاه")
        self.assertEqual(len(replies), 1)
        self.assertFalse((self.runtime.voice_output.folder / "reply.wav").exists())


if __name__ == "__main__":
    unittest.main()
