"""
Runtime contract for application workflows through QronosRuntime.

The runtime, its approval ledger, its Stop/Pause handling and its action
dispatcher are real. Only the native boundaries (Telegram's UI, the print
spooler and printto handler) are test doubles, so nothing on this computer is
opened, clicked or printed.
"""

from __future__ import annotations

import json
import time

import pytest

from core.action_audit import ActionAuditLog
from core.app_workflow import SideEffectJournal
from core.app_workflow_runtime import APP_WORKFLOW_DECISION_ACTION, AppWorkflowCoordinator
from core.print_workflow import PrintWorkflow
from core.runtime_bridge import QronosRuntime, handle_action
from core.task_router import TaskRouter
from core.telegram_workflow import LocalDownloadFolder, TelegramDownloadWorkflow
from test_file_control_text_bridge import _FakeHistory
from tests.test_print_workflow import FakeSpooler, FakeSubmitter
from tests.test_telegram_workflow import FakeTelegram, window

SECRET = "correct horse battery"


class BridgeHarness:
    def __init__(self, tmp_path) -> None:
        self.events: list[tuple[str, str, str]] = []
        runtime = QronosRuntime(notify=lambda event, status, message: self.events.append((event, status, message)))
        runtime.task_router = TaskRouter()
        runtime.orchestrator = object()
        runtime.conversation_history = _FakeHistory()
        runtime.action_audit = ActionAuditLog(path=None)
        folder = tmp_path / "Telegram downloads"
        folder.mkdir()
        self.telegram_ui = FakeTelegram({"window": window()}, folder)
        journal = SideEffectJournal(tmp_path / "side-effects.json")
        telegram = TelegramDownloadWorkflow(self.telegram_ui, binder=self.telegram_ui.binder, journal=journal,
                                            folder=LocalDownloadFolder(folder), verify_timeout=2.0, poll_seconds=0.01)
        self.spooler = FakeSpooler()
        self.submitter = FakeSubmitter(self.spooler)
        printer = PrintWorkflow(spooler=self.spooler, submitter=self.submitter, journal=journal,
                                appear_timeout=1.0, monitor_timeout=1.0, poll_seconds=0.01)
        self.document = tmp_path / "report.pdf"
        self.document.write_bytes(b"%PDF-1.7 bridge test")
        runtime._app_workflows = AppWorkflowCoordinator(
            ledger=runtime._approvals,
            notify=runtime._notify,
            actions=runtime._computer_actions,
            telegram_workflow=lambda: telegram,
            print_workflow=lambda: printer,
            resolve_file=lambda reference: self.document,
            default_printer=lambda: "Office Laser",
            verify_secret=lambda secret: secret == SECRET,
            paused=lambda: runtime.runtime_paused,
            run_async=lambda work: work(),
            audit=runtime.action_audit,
        )
        self.runtime = runtime

    def payloads(self, event_type):
        return [json.loads(message) for event, _, message in self.events if event == event_type]

    def last(self, event_type):
        found = self.payloads(event_type)
        assert found, f"no {event_type}; events: {[event for event, _, _ in self.events]}"
        return found[-1]

    def wait_for(self, event_type, count):
        deadline = time.monotonic() + 5
        while len(self.payloads(event_type)) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.payloads(event_type)


@pytest.fixture
def h(tmp_path):
    return BridgeHarness(tmp_path)


def test_text_turn_prompts_and_answers_the_chat_without_touching_telegram(h):
    h.runtime.run_text_turn("آخرین فایل «Sara Ahmadi» را در تلگرام دانلود کن", "req-1")
    prompt = h.last("app_workflow_approval_required")
    assert prompt["requestId"] == "req-1" and prompt["phase"] == "telegram_inspect"
    responses = h.payloads("text_response")
    assert responses and responses[-1]["requestId"] == "req-1"
    assert "تأیید" in responses[-1]["text"]
    assert h.telegram_ui.calls == []


def test_desktop_decisions_run_through_the_action_dispatcher(h):
    h.runtime.run_text_turn("آخرین فایل «Sara Ahmadi» را در تلگرام دانلود کن", "req-1")
    inspect = h.last("app_workflow_approval_required")
    handle_action(h.runtime, {"actionId": APP_WORKFLOW_DECISION_ACTION,
                              "data": {"actionToken": inspect["actionToken"], "approved": True}})
    prompts = h.wait_for("app_workflow_approval_required", 2)
    download = prompts[-1]
    assert download["phase"] == "telegram_download"
    handle_action(h.runtime, {"actionId": APP_WORKFLOW_DECISION_ACTION,
                              "data": {"actionToken": download["actionToken"], "approved": True}})
    finished = h.wait_for("app_workflow_finished", 1)[-1]
    assert finished["outcome"] == "succeeded"
    assert [call for call in h.telegram_ui.calls if call.startswith("download")] == ["download:m7"]


@pytest.mark.parametrize("data, message", [
    ({"actionToken": "x", "approved": "yes"}, "boolean"),
    ({"actionToken": "x", "approved": True, "secret": 12345}, "short string"),
    ({"actionToken": "x", "approved": True, "secret": "s" * 300}, "short string"),
])
def test_malformed_decisions_are_rejected_without_echoing_secrets(h, data, message):
    with pytest.raises(ValueError, match=message) as raised:
        handle_action(h.runtime, {"actionId": APP_WORKFLOW_DECISION_ACTION, "data": data})
    assert "s" * 300 not in str(raised.value) and "12345" not in str(raised.value)


def test_pause_revokes_the_open_prompt_and_resume_does_not_revive_it(h):
    h.runtime.run_text_turn("آخرین فایل «Sara Ahmadi» را در تلگرام دانلود کن", "req-1")
    token = h.last("app_workflow_approval_required")["actionToken"]
    h.runtime.set_runtime_paused(True)
    assert h.last("app_workflow_stale")["actionToken"] == token
    h.runtime.set_runtime_paused(False)
    h.runtime.resolve_app_workflow_decision(token, approved=True)
    assert h.payloads("app_workflow_stale")[-1]["actionToken"] == token
    assert h.telegram_ui.calls == []


def test_stop_revokes_the_open_print_prompt(h):
    h.runtime.run_text_turn("print report.pdf on Office Laser", "req-p")
    token = h.last("app_workflow_approval_required")["actionToken"]
    h.runtime.request_stop_response()
    h.runtime.resolve_app_workflow_decision(token, approved=True, secret=SECRET)
    assert any(item.get("actionToken") == token for item in h.payloads("app_workflow_stale"))
    assert h.submitter.submissions == []


def test_print_secret_is_checked_and_never_reaches_events(h):
    h.runtime.run_text_turn("print report.pdf on Office Laser", "req-p")
    token = h.last("app_workflow_approval_required")["actionToken"]
    h.runtime.resolve_app_workflow_decision(token, approved=True, secret="nope")
    assert h.submitter.submissions == []
    h.runtime.resolve_app_workflow_decision(token, approved=True, secret=SECRET)
    assert h.submitter.submissions == [("report.pdf", "Office Laser")]
    every = json.dumps(h.events, ensure_ascii=False)
    assert SECRET not in every and "nope" not in every


def test_ordinary_text_is_not_captured_by_the_workflow_router(h):
    assert h.runtime._begin_text_app_workflow("سلام، حالت چطوره؟", "req-x", time.perf_counter()) is False
    assert h.runtime._begin_text_app_workflow("open Telegram", "req-y", time.perf_counter()) is False


def test_unknown_or_forged_tokens_are_stale(h):
    h.runtime.resolve_app_workflow_decision("forged-token-that-was-never-issued", approved=True)
    assert h.last("app_workflow_stale")["reason"] in {"unknown_or_used", "unknown"}


@pytest.fixture
def temporary_data_root(tmp_path, monkeypatch):
    """Production wiring under test writes its journal lock here, never the checkout."""
    from dataclasses import replace
    import core.runtime_bridge as runtime_bridge

    root = tmp_path / "qronos-data"
    monkeypatch.setattr(
        runtime_bridge, "CONFIG",
        replace(runtime_bridge.CONFIG, paths=replace(runtime_bridge.CONFIG.paths, data=root)),
    )
    return root


def test_production_wiring_refuses_printing_until_an_approval_secret_is_set(temporary_data_root, monkeypatch):
    """With no secret in this profile, a print request is refused before any
    prompt or spooler access (conftest gives every test its own secret path)."""
    events = []
    runtime = QronosRuntime(notify=lambda event, status, message: events.append((event, message)))
    runtime.task_router = TaskRouter()
    runtime.orchestrator = object()
    runtime.conversation_history = _FakeHistory()
    runtime.action_audit = ActionAuditLog(path=None)
    import core.print_workflow as print_workflow

    def no_spooler(*args, **kwargs):
        raise AssertionError("the spooler must not be touched")

    monkeypatch.setattr(print_workflow, "WinSpooler", no_spooler)
    runtime.run_text_turn("print report.pdf on Office Laser", "req-p")
    names = [event for event, _ in events]
    assert "app_workflow_approval_required" not in names
    finished = [json.loads(message) for event, message in events if event == "app_workflow_finished"][-1]
    assert finished["outcome"] == "refused" and "هنوز رمزی تعیین نشده" in finished["message"]


def test_production_wiring_checks_the_profile_secret_with_the_shared_lockout(temporary_data_root):
    from core.approval_secret import ApprovalSecretStore, approval_secret_path

    # Provisioned the way the Permissions view does, in this test's own profile path.
    ApprovalSecretStore().set_secret("correct horse battery")
    runtime = QronosRuntime(notify=lambda *args: None)
    runtime.action_audit = ActionAuditLog(path=None)
    coordinator = runtime._ensure_app_workflows()
    assert coordinator._secret_state() == "ready"
    assert coordinator._verify_secret("wrong guess") is False
    # The wrong guess counted in the same store the Library CLI reads.
    assert ApprovalSecretStore(approval_secret_path()).status().remaining_attempts == 4
    assert coordinator._verify_secret("correct horse battery") is True


def test_production_wiring_uses_one_durable_journal_under_the_qronos_state_root(temporary_data_root):
    runtime = QronosRuntime(notify=lambda *args: None)
    runtime.action_audit = ActionAuditLog(path=None)
    coordinator = runtime._ensure_app_workflows()
    assert runtime._ensure_app_workflows() is coordinator
    journal = runtime._app_workflow_journal
    assert journal.path == (temporary_data_root / "workflows" / "side-effects.json").absolute()
    assert not journal.path.exists(), "nothing is recorded until a side effect is begun"


def test_production_wiring_routes_web_navigation_to_an_approval_prompt(temporary_data_root):
    events = []
    runtime = QronosRuntime(notify=lambda event, status, message: events.append((event, message)))
    runtime.task_router = TaskRouter()
    runtime.orchestrator = object()
    runtime.conversation_history = _FakeHistory()
    runtime.action_audit = ActionAuditLog(path=None)
    runtime.run_text_turn("open https://example.org/docs", "req-w")
    prompt = [json.loads(message) for event, message in events if event == "app_workflow_approval_required"][-1]
    assert prompt["phase"] == "browser_navigate" and prompt["details"]["url"] == "https://example.org/docs"
    assert runtime._begin_text_app_workflow("search my files for the report", "req-x", time.perf_counter()) is False
