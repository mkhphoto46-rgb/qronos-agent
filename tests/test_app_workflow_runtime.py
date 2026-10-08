from __future__ import annotations

import json

import pytest

from core.action_audit import ActionAuditLog, AuditEvent
from core.action_cancellation import ActiveActionRegistry, CANCEL_REASON_PAUSE
from core.app_workflow import SideEffectJournal
from core.app_workflow_runtime import AppWorkflowCoordinator
from core.approval_ledger import ApprovalKind, ApprovalLedger
from core.print_workflow import PrintWorkflow
from core.telegram_workflow import LocalDownloadFolder, TelegramDownloadWorkflow
from tests.test_print_workflow import FakeSpooler, FakeSubmitter
from tests.test_telegram_workflow import FakeTelegram, window

SECRET = "correct horse battery"


class Harness:
    def __init__(self, tmp_path) -> None:
        self.events: list[tuple[str, dict]] = []
        self.audit = ActionAuditLog(tmp_path / "audit.jsonl")
        self.ledger = ApprovalLedger()
        self.actions = ActiveActionRegistry()
        self.paused = False
        folder = tmp_path / "Telegram downloads"
        folder.mkdir()
        self.window_state = {"window": window()}
        self.telegram_ui = FakeTelegram(self.window_state, folder)
        journal = SideEffectJournal(tmp_path / "side-effects.json")
        self.telegram = TelegramDownloadWorkflow(
            self.telegram_ui, binder=self.telegram_ui.binder, journal=journal,
            folder=LocalDownloadFolder(folder), verify_timeout=2.0, poll_seconds=0.01,
        )
        self.spooler = FakeSpooler()
        self.submitter = FakeSubmitter(self.spooler)
        self.printer = PrintWorkflow(spooler=self.spooler, submitter=self.submitter, journal=journal,
                                     appear_timeout=1.0, monitor_timeout=1.0, poll_seconds=0.01)
        self.document = tmp_path / "report.pdf"
        self.document.write_bytes(b"%PDF-1.7 runtime test")
        self.coordinator = AppWorkflowCoordinator(
            ledger=self.ledger,
            notify=lambda event, status, message: self.events.append((event, json.loads(message))),
            actions=self.actions,
            telegram_workflow=lambda: self.telegram,
            print_workflow=lambda: self.printer,
            resolve_file=self._resolve,
            default_printer=lambda: "Office Laser",
            verify_secret=lambda secret: secret == SECRET,
            paused=lambda: self.paused,
            run_async=lambda work: work(),
            audit=self.audit,
        )

    def _resolve(self, reference):
        if reference != "report.pdf":
            raise LookupError("no such file")
        return self.document

    def last(self, event):
        return [payload for name, payload in self.events if name == event][-1]

    def names(self):
        return [name for name, _ in self.events]


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


def test_telegram_request_runs_two_approved_phases_and_reports_the_path(harness):
    assert harness.coordinator.try_begin("آخرین فایل «Sara Ahmadi» را در تلگرام دانلود کن", "r1")
    inspect = harness.last("app_workflow_approval_required")
    assert inspect["phase"] == "telegram_inspect" and inspect["permission"] == "require_voice_confirmation"
    assert harness.telegram_ui.calls == [], "nothing touches Telegram before the first approval"

    harness.coordinator.resolve(inspect["actionToken"], approved=True)
    download = harness.last("app_workflow_approval_required")
    assert download["phase"] == "telegram_download"
    assert download["permission"] == "require_ui_confirmation"
    assert download["details"]["fileName"] == "report.pdf"
    assert not any(call.startswith("download") for call in harness.telegram_ui.calls)

    harness.coordinator.resolve(download["actionToken"], approved=True)
    finished = harness.last("app_workflow_finished")
    assert finished["outcome"] == "succeeded"
    assert finished["result"]["fileName"] == "report.pdf"
    assert "دانلود شد" in finished["message"]


def test_declining_runs_nothing(harness):
    harness.coordinator.try_begin("download the latest file from \"Sara Ahmadi\" on Telegram", "r1")
    harness.coordinator.resolve(harness.last("app_workflow_approval_required")["actionToken"], approved=False)
    assert harness.last("app_workflow_finished")["outcome"] == "refused"
    assert harness.telegram_ui.calls == []


def test_stop_or_pause_revokes_open_prompts_and_late_answers_are_refused(harness):
    harness.coordinator.try_begin("آخرین فایل «Sara Ahmadi» را در تلگرام دانلود کن", "r1")
    token = harness.last("app_workflow_approval_required")["actionToken"]
    assert harness.coordinator.discard(CANCEL_REASON_PAUSE) == 1
    harness.coordinator.resolve(token, approved=True)
    assert harness.last("app_workflow_stale")["actionToken"] == token
    assert harness.telegram_ui.calls == []


def test_an_answer_cannot_be_used_twice_or_for_another_prompt_kind(harness):
    harness.coordinator.try_begin("آخرین فایل «Sara Ahmadi» را در تلگرام دانلود کن", "r1")
    token = harness.last("app_workflow_approval_required")["actionToken"]
    harness.coordinator.resolve(token, approved=False)
    harness.coordinator.resolve(token, approved=True)
    assert harness.last("app_workflow_stale")["actionToken"] == token
    file_ticket = harness.ledger.issue(ApprovalKind.FILE_ACTION, "other")
    harness.coordinator.resolve(file_ticket.approval_id, approved=True)
    assert harness.last("app_workflow_stale")["actionToken"] == file_ticket.approval_id
    assert harness.ledger.open_ticket(ApprovalKind.FILE_ACTION) is not None, "another kind is never consumed"


def test_paused_runtime_starts_nothing(harness):
    harness.paused = True
    harness.coordinator.try_begin("آخرین فایل «Sara Ahmadi» را در تلگرام دانلود کن", "r1")
    assert harness.last("app_workflow_finished")["outcome"] == "stopped"
    assert "app_workflow_approval_required" not in harness.names()


def test_stop_during_a_running_step_reports_stopped(harness):
    harness.coordinator.try_begin("آخرین فایل «Sara Ahmadi» را در تلگرام دانلود کن", "r1")
    original = harness.telegram_ui.search_contacts

    def search_then_stop(*args, **kwargs):
        harness.actions.cancel_all()
        return original(*args, **kwargs)

    harness.telegram_ui.search_contacts = search_then_stop
    harness.coordinator.resolve(harness.last("app_workflow_approval_required")["actionToken"], approved=True)
    assert harness.last("app_workflow_finished")["outcome"] == "stopped"
    assert not any(call.startswith("open") for call in harness.telegram_ui.calls)


def test_clarifications_are_reported_without_prompts(harness):
    assert harness.coordinator.try_begin("آخرین فایل را در تلگرام دانلود کن", "r1")
    finished = harness.last("app_workflow_finished")
    assert finished["workflow"] == "clarification" and "مخاطب" in finished["message"]
    assert "app_workflow_approval_required" not in harness.names()
    assert harness.coordinator.try_begin("سلام", "r2") is None


def test_print_needs_the_typed_secret_and_never_echoes_it(harness):
    harness.coordinator.try_begin("print report.pdf on printer Office Laser", "p1")
    prompt = harness.last("app_workflow_approval_required")
    assert prompt["phase"] == "print" and prompt["permission"] == "require_typed_secret"
    assert prompt["details"]["copies"] == 1
    harness.coordinator.resolve(prompt["actionToken"], approved=True, secret="wrong guess")
    assert harness.names()[-1] == "app_workflow_secret_rejected"
    assert harness.submitter.submissions == []
    harness.coordinator.resolve(prompt["actionToken"], approved=True, secret=SECRET)
    assert harness.submitter.submissions == [("report.pdf", "Office Laser")]
    assert harness.last("app_workflow_finished")["outcome"] == "succeeded"
    every_message = json.dumps(harness.events, ensure_ascii=False)
    assert SECRET not in every_message and "wrong guess" not in every_message


def test_print_without_a_named_printer_shows_the_default_for_confirmation(harness):
    harness.coordinator.try_begin("فایل report.pdf را چاپ کن", "p1")
    prompt = harness.last("app_workflow_approval_required")
    assert prompt["details"]["printer"] == "Office Laser (چاپگر پیش‌فرض)"
    assert "چاپگر پیش‌فرض" in prompt["message"]


def test_print_with_unsupported_copies_or_unknown_file_is_refused_before_any_prompt(harness):
    harness.coordinator.try_begin("print report.pdf 2 copies on Office Laser", "p1")
    assert harness.last("app_workflow_finished")["outcome"] == "refused"
    harness.coordinator.try_begin("print missing.pdf on Office Laser", "p2")
    assert harness.last("app_workflow_finished")["outcome"] == "refused"
    assert "app_workflow_approval_required" not in harness.names()
    assert harness.submitter.submissions == []


def test_a_repeat_after_an_uncertain_print_is_refused_not_reprinted(harness):
    harness.submitter.effect = "raise"
    harness.coordinator.try_begin("print report.pdf on Office Laser", "p1")
    harness.coordinator.resolve(harness.last("app_workflow_approval_required")["actionToken"], approved=True, secret=SECRET)
    assert harness.last("app_workflow_finished")["outcome"] == "uncertain"
    harness.submitter.effect = "job"
    harness.coordinator.try_begin("print report.pdf on Office Laser", "p2")
    harness.coordinator.resolve(harness.last("app_workflow_approval_required")["actionToken"], approved=True, secret=SECRET)
    assert harness.last("app_workflow_finished")["outcome"] == "refused"
    assert len(harness.submitter.submissions) == 1


def test_answers_and_outcomes_are_audited_with_their_typed_category(harness):
    harness.coordinator.try_begin("print report.pdf on Office Laser", "p1")
    harness.coordinator.resolve(harness.last("app_workflow_approval_required")["actionToken"], approved=True, secret=SECRET)
    workflow_records = [r for r in harness.audit.records() if r.category == "device_control"]
    events = [(r.event, r.decision or r.outcome) for r in workflow_records]
    assert (AuditEvent.APPROVED, "require_typed_secret") in events
    assert (AuditEvent.COMPLETED, "succeeded") in events
    assert all(r.target == "printer:Office Laser" for r in workflow_records)
    harness.coordinator.try_begin("آخرین فایل «Sara Ahmadi» را در تلگرام دانلود کن", "r1")
    harness.coordinator.resolve(harness.last("app_workflow_approval_required")["actionToken"], approved=False)
    gui = [(r.event, r.decision or r.outcome) for r in harness.audit.records() if r.category == "gui_automation"]
    assert (AuditEvent.DENIED_BY_USER, "require_voice_confirmation") in gui
    assert (AuditEvent.COMPLETED, "refused") in gui


def test_uncertain_attempts_are_listed_and_released_only_by_a_person(tmp_path):
    from core.app_workflow import SideEffectJournal, SideEffectState

    events = []
    journal = SideEffectJournal(tmp_path / "j.json")
    journal.begin("op-1", "content")
    journal.settle("op-1", SideEffectState.UNCERTAIN, "no job seen")
    coordinator = AppWorkflowCoordinator(
        ledger=ApprovalLedger(), notify=lambda e, s, m: events.append((e, json.loads(m))),
        actions=ActiveActionRegistry(), telegram_workflow=None, print_workflow=None,
        resolve_file=None, default_printer=None, verify_secret=None, paused=lambda: False,
        journal=journal,
    )
    coordinator.report_uncertain()
    listed = events[-1][1]
    assert listed["available"] and [a["operationKey"] for a in listed["attempts"]] == ["op-1"]
    coordinator.report_uncertain(acknowledge="op-1")
    assert events[-1][1]["attempts"] == []
    assert journal.get("op-1").state is SideEffectState.ACKNOWLEDGED
