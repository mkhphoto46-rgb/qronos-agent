"""
File approvals under Stop, Pause and shutdown.

A text file action that needs on-screen approval waits with a single-use
identity (actionToken). Stop, Pause and close revoke every waiting identity
and end the reversible-write lease, so a late "approve" from the desktop can
never write. Real files in a temporary directory are the evidence: the
destination must not exist after a refused answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.runtime_bridge as runtime_bridge
from core.action_audit import ActionAuditLog, AuditEvent
from core.action_cancellation import (
    CANCEL_REASON_PAUSE,
    CANCEL_REASON_SHUTDOWN,
    CANCEL_REASON_USER_STOP,
)
from core.actions import ActionOutcome
from core.file_executor import FileExecutor
from core.runtime_bridge import FILE_ACTION_DECISION_ACTION, QronosRuntime, handle_action
from core.task_router import TaskRouter
from core.undo_journal import UndoJournal
from security.gate import set_default_audit_sink

from test_file_control_text_bridge import _FakeHistory
from test_runtime_bridge import captured_events


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.source = root / "source" / "test.txt"
        self.backup = root / "Backup"
        self.source.parent.mkdir()
        self.backup.mkdir()
        self.source.write_text("qronos-live", encoding="utf-8")
        self.events: list[tuple[str, str, str]] = []
        self.now = 1_000.0
        runtime = QronosRuntime(
            notify=lambda event, status, message: self.events.append((event, status, message))
        )
        runtime.task_router = TaskRouter()
        runtime._file_approval_clock = lambda: self.now
        runtime.orchestrator = object()  # COMPUTER turns never need a Brain
        runtime.conversation_history = _FakeHistory()
        self.audit = ActionAuditLog(path=None)
        runtime.action_audit = self.audit
        runtime.undo_journal = UndoJournal(path=None)
        runtime.file_executor = FileExecutor(audit=self.audit, journal=runtime.undo_journal)
        self.runtime = runtime

    @property
    def target(self) -> Path:
        return self.backup / self.source.name

    def copy_command(self) -> str:
        return f'فایل "{self.source}" را در پوشه "{self.backup}" کپی کن'

    def request_copy(self, request_id: str = "req-copy") -> str:
        self.runtime.run_text_turn(self.copy_command(), request_id)
        return self.payload("file_action_approval_required")["actionToken"]

    def payload(self, event_type: str) -> dict:
        matching = [m for e, _, m in self.events if e == event_type]
        assert matching, f"no {event_type}; events: {[e for e, _, _ in self.events]}"
        return json.loads(matching[-1])

    def count(self, event_type: str) -> int:
        return sum(1 for e, _, _ in self.events if e == event_type)

    def decide(self, token: str, approved: bool = True) -> None:
        self.runtime.resolve_file_action_decision(token, approved=approved)


@pytest.fixture()
def h(tmp_path, monkeypatch):
    monkeypatch.setenv("QRONOS_PERMISSION_POLICY_PATH", str(tmp_path / "permissions_v2.json"))
    monkeypatch.setenv("QRONOS_PERMISSION_AUDIT_PATH", str(tmp_path / "permission_changes.jsonl"))
    harness = Harness(tmp_path)
    yield harness
    set_default_audit_sink(None)
    harness.runtime.close()


def test_stop_while_file_approval_waits_means_a_late_yes_cannot_write(h):
    token = h.request_copy()

    result = h.runtime.request_stop_response()
    h.decide(token, approved=True)

    assert result["pendingFileApprovalsDiscarded"] == 1
    assert not h.target.exists()
    cancelled = h.payload("file_action_cancelled")
    assert cancelled["actionToken"] == token and cancelled["reason"] == CANCEL_REASON_USER_STOP
    assert h.payload("file_action_stale") == {"actionToken": token, "reason": CANCEL_REASON_USER_STOP}
    completed = [r for r in h.audit.records() if r.event is AuditEvent.COMPLETED]
    assert [r.outcome for r in completed] == [ActionOutcome.CANCELLED.value]
    # The text request was answered rather than left hanging.
    responses = [json.loads(m) for e, _, m in h.events if e == "text_response"]
    assert responses and responses[-1]["requestId"] == "req-copy"


def test_pause_revokes_waiting_file_approval_and_resume_does_not_revive_it(h):
    token = h.request_copy()

    h.runtime.set_runtime_paused(True)
    # The decision action is pause-safe at the bridge; it still cannot write.
    captured_events(
        lambda: handle_action(
            h.runtime,
            {"actionId": FILE_ACTION_DECISION_ACTION, "data": {"actionToken": token, "approved": True}},
        )
    )
    h.decide(token)
    h.runtime.set_runtime_paused(False)
    h.decide(token)

    assert not h.target.exists()
    assert h.payload("file_action_cancelled")["reason"] == CANCEL_REASON_PAUSE
    assert h.payload("file_action_stale")["reason"] == CANCEL_REASON_PAUSE


def test_close_revokes_waiting_file_approval(h):
    token = h.request_copy()

    h.runtime.close()
    h.runtime.close()
    h.decide(token)

    assert not h.target.exists()
    assert h.payload("file_action_cancelled")["reason"] == CANCEL_REASON_SHUTDOWN
    assert h.payload("file_action_stale")["reason"] == CANCEL_REASON_SHUTDOWN


def test_stop_ends_the_write_lease(h):
    first = h.request_copy("req-first")
    h.decide(first)
    assert h.target.is_file()
    assert h.runtime._file_write_approval_lease_active()

    h.runtime.request_stop_response()

    assert not h.runtime._file_write_approval_lease_active()
    folder = h.root / "AfterStop"
    h.runtime.run_text_turn(f'پوشه "{folder}" را بساز', "req-after-stop")
    assert not folder.exists()
    assert h.count("file_action_approval_required") == 2


def test_close_ends_the_write_lease(h):
    h.decide(h.request_copy())
    h.runtime.close()
    assert not h.runtime._file_write_approval_lease_active()


def test_old_token_cannot_approve_a_new_identical_request(h):
    old = h.request_copy("req-a")
    h.runtime.request_stop_response()
    new = h.request_copy("req-b")
    assert old != new

    h.decide(old)
    assert not h.target.exists()

    h.decide(new)
    assert h.target.is_file()


def test_token_is_single_use(h):
    token = h.request_copy()
    h.decide(token)
    h.target.unlink()

    h.decide(token)

    assert not h.target.exists()
    assert h.payload("file_action_stale")["reason"] == "already_answered"


def test_file_decision_cannot_consume_a_screen_prompt_and_vice_versa(h):
    screen = h.runtime._approvals.issue(runtime_bridge.ApprovalKind.SCREEN_READ, "what is this?")
    file_token = h.request_copy()

    h.decide(screen.approval_id)
    assert h.payload("file_action_stale")["reason"] == "wrong_kind"
    assert h.runtime.pending_look == "what is this?"

    h.runtime.look_at_screen(approved=True, approval_id=file_token)
    assert h.payload("screen_approval_stale")["reason"] == "wrong_kind"
    assert file_token in h.runtime.pending_file_actions
    assert not h.target.exists()


def test_malformed_token_is_refused(h):
    h.request_copy()
    h.decide("not a token")
    assert h.payload("file_action_stale")["reason"] == "malformed"
    assert not h.target.exists()


def test_stop_while_the_text_turn_is_routing_never_prompts_or_writes(h, monkeypatch):
    real_parse = runtime_bridge.parse_file_action

    def parse_then_stop(text):
        h.runtime.request_stop_response()  # Stop lands mid-turn
        return real_parse(text)

    monkeypatch.setattr(runtime_bridge, "parse_file_action", parse_then_stop)
    # A lease would otherwise auto-approve the write.
    h.runtime._grant_file_write_approval_lease()
    h.runtime.run_text_turn(h.copy_command(), "req-routing")

    assert not h.target.exists()
    assert h.count("file_action_approval_required") == 0
    assert h.runtime.pending_file_actions == {}


def test_desktop_closes_the_file_dialog_on_cancel_or_stale():
    app = (Path(__file__).resolve().parents[1] / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
    start = app.index('case "file_action_completed":')
    block = app[start : app.index("setPendingFileApproval(", start)]
    assert 'case "file_action_cancelled":' in block
    assert 'case "file_action_stale":' in block


def test_no_write_while_paused_even_for_a_ticket_that_raced_pause(h):
    token = h.request_copy()
    pending = h.runtime.pending_file_actions[token]
    h.runtime.set_runtime_paused(True)
    # Simulate a worker that issued its prompt just as Pause landed.
    raced = h.runtime._approvals.issue(
        runtime_bridge.ApprovalKind.FILE_ACTION, "req-copy", payload=pending
    )

    h.decide(raced.approval_id)

    assert not h.target.exists()
    assert h.payload("file_action_stale") == {
        "actionToken": raced.approval_id,
        "reason": CANCEL_REASON_PAUSE,
    }
