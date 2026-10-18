from __future__ import annotations

from dataclasses import replace
import json

import pytest

from core.action_audit import ActionAuditLog, AuditEvent
from core.action_cancellation import ActiveActionRegistry, CANCEL_REASON_PAUSE
from core.app_workflow_runtime import AppWorkflowCoordinator
from core.approval_ledger import ApprovalKind, ApprovalLedger
from tests.test_browser_research import PAGE, make

OPEN = "open https://example.org/docs"


class Harness:
    def __init__(self, tmp_path, pages=(PAGE,), *, browser=True) -> None:
        self.events: list[tuple[str, dict]] = []
        self.audit = ActionAuditLog(tmp_path / "audit.jsonl")
        self.ledger = ApprovalLedger()
        self.actions = ActiveActionRegistry()
        self.paused = False
        self.workflow, self.launcher, self.reader, _ = make(list(pages))
        self.coordinator = AppWorkflowCoordinator(
            ledger=self.ledger,
            notify=lambda event, status, message: self.events.append((event, json.loads(message))),
            actions=self.actions,
            paused=lambda: self.paused,
            run_async=lambda work: work(),
            audit=self.audit,
            browser_workflow=(lambda: self.workflow) if browser else None,
        )

    def last(self, event):
        return [payload for name, payload in self.events if name == event][-1]

    def names(self):
        return [name for name, _ in self.events]


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


def test_declining_runs_nothing(harness):
    harness.coordinator.try_begin(OPEN, "w1")
    harness.coordinator.resolve(harness.last("app_workflow_approval_required")["actionToken"], approved=False)
    assert harness.last("app_workflow_finished")["outcome"] == "refused"
    assert harness.launcher.opened == []


def test_stop_or_pause_revokes_open_prompts_and_late_answers_are_refused(harness):
    harness.coordinator.try_begin(OPEN, "w1")
    token = harness.last("app_workflow_approval_required")["actionToken"]
    assert harness.coordinator.discard(CANCEL_REASON_PAUSE) == 1
    harness.coordinator.resolve(token, approved=True)
    assert harness.last("app_workflow_stale")["actionToken"] == token
    assert harness.launcher.opened == []


def test_an_answer_cannot_be_used_twice_or_for_another_prompt_kind(harness):
    harness.coordinator.try_begin(OPEN, "w1")
    token = harness.last("app_workflow_approval_required")["actionToken"]
    harness.coordinator.resolve(token, approved=False)
    harness.coordinator.resolve(token, approved=True)
    assert harness.last("app_workflow_stale")["actionToken"] == token
    file_ticket = harness.ledger.issue(ApprovalKind.FILE_ACTION, "other")
    harness.coordinator.resolve(file_ticket.approval_id, approved=True)
    assert harness.last("app_workflow_stale")["actionToken"] == file_ticket.approval_id
    assert harness.ledger.open_ticket(ApprovalKind.FILE_ACTION) is not None, "another kind is never consumed"
    assert harness.launcher.opened == []


def test_paused_runtime_starts_nothing(harness):
    harness.paused = True
    harness.coordinator.try_begin(OPEN, "w1")
    assert harness.last("app_workflow_finished")["outcome"] == "stopped"
    assert "app_workflow_approval_required" not in harness.names()


def test_stop_during_a_running_step_reports_stopped(harness):
    harness.coordinator.try_begin(OPEN, "w1")
    original = harness.launcher.launch

    def launch_then_stop(url):
        identity = original(url)
        harness.actions.cancel_all()
        return identity

    harness.launcher.launch = launch_then_stop
    harness.coordinator.resolve(harness.last("app_workflow_approval_required")["actionToken"], approved=True)
    assert harness.last("app_workflow_finished")["outcome"] == "stopped"
    assert harness.reader.reads == 0, "nothing is read after Stop"


def test_clarifications_are_reported_without_prompts(harness):
    assert harness.coordinator.try_begin("open http://localhost:8080/", "w1")
    finished = harness.last("app_workflow_finished")
    assert finished["outcome"] == "refused" and finished["detail"] == "clarification needed"
    assert "app_workflow_approval_required" not in harness.names()
    assert harness.coordinator.try_begin("سلام", "w2") is None


def test_answers_and_outcomes_are_audited_with_their_typed_category(harness):
    harness.coordinator.try_begin(OPEN, "w1")
    harness.coordinator.resolve(harness.last("app_workflow_approval_required")["actionToken"], approved=False)
    records = [r for r in harness.audit.records() if r.category == "browser_navigation"]
    events = [(r.event, r.decision or r.outcome) for r in records]
    assert (AuditEvent.DENIED_BY_USER, "require_voice_confirmation") in events
    assert (AuditEvent.COMPLETED, "refused") in events
    assert all(r.target == "https://example.org/docs" for r in records)


def test_uncertain_attempts_are_listed_and_released_only_by_a_person(tmp_path):
    from core.app_workflow import SideEffectJournal, SideEffectState

    events = []
    journal = SideEffectJournal(tmp_path / "j.json")
    journal.begin("op-1", "content")
    journal.settle("op-1", SideEffectState.UNCERTAIN, "no result seen")
    coordinator = AppWorkflowCoordinator(
        ledger=ApprovalLedger(), notify=lambda e, s, m: events.append((e, json.loads(m))),
        actions=ActiveActionRegistry(), paused=lambda: False, journal=journal,
    )
    coordinator.report_uncertain()
    listed = events[-1][1]
    assert listed["available"] and [a["operationKey"] for a in listed["attempts"]] == ["op-1"]
    coordinator.report_uncertain(acknowledge="op-1")
    assert events[-1][1]["attempts"] == []
    assert journal.get("op-1").state is SideEffectState.ACKNOWLEDGED


# ------------------------------------------------------------- browser phase


def test_web_search_opens_only_after_approval_and_returns_untrusted_page_text(tmp_path):
    page = replace(PAGE, url="https://duckduckgo.com/?q=solar", text="x" * 9000)
    h = Harness(tmp_path, [page])
    reply = h.coordinator.try_begin("search the web for solar panels", "w1")
    assert "تأیید" in reply
    prompt = h.last("app_workflow_approval_required")
    assert prompt["phase"] == "browser_navigate" and prompt["permission"] == "require_voice_confirmation"
    assert prompt["details"]["url"] == "https://duckduckgo.com/?q=solar+panels"
    assert h.launcher.opened == []
    h.coordinator.resolve(prompt["actionToken"], approved=True)
    finished = h.last("app_workflow_finished")
    assert finished["outcome"] == "succeeded" and finished["result"]["untrusted"] is True
    assert len(finished["result"]["text"]) == 6000 and finished["result"]["truncated"] is True
    assert h.launcher.opened == ["https://duckduckgo.com/?q=solar+panels"]
    events = [(r.event, r.decision or r.outcome) for r in h.audit.records() if r.category == "browser_navigation"]
    assert (AuditEvent.APPROVED, "require_voice_confirmation") in events
    assert (AuditEvent.COMPLETED, "succeeded") in events


def test_declined_or_revoked_navigation_opens_nothing(tmp_path):
    h = Harness(tmp_path)
    h.coordinator.try_begin(OPEN, "w1")
    h.coordinator.resolve(h.last("app_workflow_approval_required")["actionToken"], approved=False)
    h.coordinator.try_begin(OPEN, "w2")
    token = h.last("app_workflow_approval_required")["actionToken"]
    h.coordinator.discard(CANCEL_REASON_PAUSE)
    h.coordinator.resolve(token, approved=True)
    assert h.launcher.opened == []


def test_a_page_on_another_host_is_reported_uncertain_without_its_text(tmp_path):
    h = Harness(tmp_path, [replace(PAGE, url="https://other.example.net/")] * 20)
    h.coordinator.try_begin(OPEN, "w1")
    h.coordinator.resolve(h.last("app_workflow_approval_required")["actionToken"], approved=True)
    finished = h.last("app_workflow_finished")
    assert finished["outcome"] == "uncertain" and "text" not in finished["result"]


def test_without_a_browser_workflow_navigation_is_not_captured(tmp_path):
    h = Harness(tmp_path, browser=False)
    assert h.coordinator.try_begin(OPEN, "w1") is None
