"""The desktop activity history (core/activity_history.py) through the bridge's registered actions.

A real QronosRuntime with a real FileExecutor in tmp_path (the file-approval harness pattern): a real, approved
copy is made, then the history is asked for and an undo is requested and answered through handle_action, as the
desktop's send_runtime_action delivers them. Fixtures only; the permission policy and audit live in tmp_path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import security.gate as gate_module
from core.action_audit import ActionAuditLog, AuditEvent
from core.actions import ActionOutcome, ActionRequest
from core.approval_ledger import STALE_USED
from core import activity_history
from core.file_executor import FileAction, FileExecutor, FileOperation
from core.runtime_bridge import (
    HISTORY_REQUEST_ACTION, HISTORY_UNDO_DECISION_ACTION, HISTORY_UNDO_REQUEST_ACTION, QronosRuntime, handle_action,
)
from core.task_router import TaskRouter
from core.undo_journal import Reversal, UndoJournal
from security.gate import Verdict, set_default_audit_sink
from security.permissions import ActionCategory, PermissionDecision

from test_file_control_text_bridge import _FakeHistory


class Harness:
    def __init__(self, root: Path, *, audit_path=None, journal_path=None) -> None:
        self.root = root
        self.backup = root / "Backup"
        self.backup.mkdir(exist_ok=True)
        self.events: list[tuple[str, str, str]] = []
        runtime = QronosRuntime(notify=lambda event, status, message: self.events.append((event, status, message)))
        runtime.task_router = TaskRouter()
        runtime.orchestrator = object()
        runtime.conversation_history = _FakeHistory()
        self.audit = ActionAuditLog(path=audit_path)
        runtime.action_audit = self.audit
        runtime.undo_journal = UndoJournal(path=journal_path)
        runtime.file_executor = FileExecutor(audit=self.audit, journal=runtime.undo_journal)
        self.runtime = runtime

    def source(self, name: str = "notes.txt", text: str = "qronos-history") -> Path:
        folder = self.root / "source"
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def copy(self, source: Path, request_id: str = "req") -> str:
        self.runtime.run_text_turn(f'فایل "{source}" را در پوشه "{self.backup}" کپی کن', request_id)
        token = self.payload("file_action_approval_required")["actionToken"]
        self.runtime.resolve_file_action_decision(token, approved=True)
        assert (self.backup / source.name).is_file()
        return self.payload("file_action_completed")["actionId"]

    def act(self, action_id: str, **data) -> None:
        handle_action(self.runtime, {"command": "action", "actionId": action_id, **({"data": data} if data else {})})

    def payload(self, event_type: str) -> dict:
        matching = [m for e, _, m in self.events if e == event_type]
        assert matching, f"no {event_type}; events: {[e for e, _, _ in self.events]}"
        return json.loads(matching[-1])

    def history(self) -> dict:
        self.act(HISTORY_REQUEST_ACTION)
        return self.payload("history_snapshot")

    def entry(self, action_id: str) -> dict:
        [found] = [item for item in self.history()["actions"] if item["actionId"] == action_id]
        return found


@pytest.fixture()
def h(tmp_path, monkeypatch):
    monkeypatch.setenv("QRONOS_PERMISSION_POLICY_PATH", str(tmp_path / "permissions_v2.json"))
    monkeypatch.setenv("QRONOS_PERMISSION_AUDIT_PATH", str(tmp_path / "permission_changes.jsonl"))
    harness = Harness(tmp_path)
    yield harness
    set_default_audit_sink(None)
    harness.runtime.close()


def test_a_real_copy_appears_redacted_with_an_honest_undo_and_the_undo_removes_exactly_that_copy(h):
    source = h.source()
    action_id = h.copy(source)
    item = h.entry(action_id)
    raw = [m for e, _, m in h.events if e == "history_snapshot"][-1]
    assert str(h.root) not in raw and "\\\\" not in raw and "qronos-history" not in raw  # no path, no content
    assert item["name"] == "notes.txt" and item["category"] == "create_or_edit_file"
    assert item["outcome"] == "succeeded" and item["undo"] == {"state": "available", "reversal": "delete_created",
                                                               "reason": ""}
    assert {e["event"] for e in item["events"]} >= {"decided", "approved", "completed"}
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    prompt = h.payload("history_undo_approval_required")
    assert prompt["actionId"] == action_id and prompt["name"] == "notes.txt" and prompt["needsSecret"] is False
    assert str(h.root) not in json.dumps(prompt)
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=prompt["actionToken"], approved=True)
    assert h.payload("history_undo_result")["outcome"] == "undone"
    assert not (h.backup / "notes.txt").exists() and source.read_text(encoding="utf-8") == "qronos-history"
    assert h.audit.count(AuditEvent.UNDONE) == 1
    assert h.entry(action_id)["undo"]["state"] == "done"


def test_an_edited_copy_is_never_deleted(h):
    source = h.source()
    action_id = h.copy(source)
    (h.backup / "notes.txt").write_text("the person's own edit", encoding="utf-8")
    assert h.entry(action_id)["undo"]["state"] == "changed_since"
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    assert h.payload("history_undo_refused") == {"actionId": action_id, "reason": "changed_since"}
    assert (h.backup / "notes.txt").read_text(encoding="utf-8") == "the person's own edit"


def test_the_copy_changed_after_the_prompt_is_checked_again_at_the_answer(h):
    action_id = h.copy(h.source())
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    token = h.payload("history_undo_approval_required")["actionToken"]
    (h.backup / "notes.txt").write_text("edited while the prompt was open", encoding="utf-8")
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=token, approved=True)
    assert h.payload("history_undo_result")["outcome"] == "changed_since"
    assert (h.backup / "notes.txt").exists()


def test_a_token_works_once_and_unknown_late_or_declined_answers_do_nothing(h):
    action_id = h.copy(h.source())
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    token = h.payload("history_undo_approval_required")["actionToken"]
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=token, approved=False)
    assert h.payload("history_undo_declined")["actionId"] == action_id
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=token, approved=True)  # replay of a spent identity
    assert h.payload("history_undo_stale")["reason"] == STALE_USED
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken="A" * 24, approved=True)
    assert h.payload("history_undo_stale")["reason"] == "unknown"
    assert (h.backup / "notes.txt").exists() and h.entry(action_id)["undo"]["state"] == "available"


def test_pause_and_stop_revoke_an_open_prompt_and_its_late_yes_cannot_undo(h):
    action_id = h.copy(h.source())
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    token = h.payload("history_undo_approval_required")["actionToken"]
    h.runtime.request_stop_response()
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=token, approved=True)
    assert h.payload("history_undo_stale")["reason"] != STALE_USED
    assert (h.backup / "notes.txt").exists()
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    token = h.payload("history_undo_approval_required")["actionToken"]
    h.runtime.set_runtime_paused(True)
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=token, approved=True)
    assert h.payload("history_undo_stale")["reason"] == "paused"
    prompts = len([e for e, _, _ in h.events if e == "history_undo_approval_required"])
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)  # the bridge's pause gate refuses it first
    assert h.events[-1][0] == "runtime_paused_rejected"
    assert len([e for e, _, _ in h.events if e == "history_undo_approval_required"]) == prompts
    h.runtime.set_runtime_paused(False)
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=token, approved=True)  # revoked by the pause
    assert h.payload("history_undo_stale")["reason"] not in ("", STALE_USED)
    assert (h.backup / "notes.txt").exists()


def test_a_typed_secret_gate_needs_the_real_secret_and_a_wrong_one_spends_the_prompt(h, monkeypatch):
    action_id = h.copy(h.source())
    real = gate_module.evaluate

    def typed_secret(request, audit=None):
        verdict = real(request, audit=audit)
        if request.parameters.get("undoesActionId"):
            return Verdict(request=request, decision=PermissionDecision.REQUIRE_TYPED_SECRET,
                           outcome=ActionOutcome.AWAITING_APPROVAL, reason="fixture: typed secret")
        return verdict

    monkeypatch.setattr(gate_module, "evaluate", typed_secret)
    h.runtime._history_secret_verifier = lambda secret: secret == "correct horse"
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    prompt = h.payload("history_undo_approval_required")
    assert prompt["needsSecret"] is True
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=prompt["actionToken"], approved=True, secret="wrong")
    assert h.payload("history_undo_secret_rejected")["actionId"] == action_id
    assert (h.backup / "notes.txt").exists()
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=prompt["actionToken"], approved=True, secret="correct horse")
    assert h.payload("history_undo_stale")["reason"] == STALE_USED  # never retried on the same identity
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    token = h.payload("history_undo_approval_required")["actionToken"]
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=token, approved=True)  # no secret at all
    assert h.payload("history_undo_secret_rejected")["actionId"] == action_id
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    token = h.payload("history_undo_approval_required")["actionToken"]
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=token, approved=True, secret="correct horse")
    assert h.payload("history_undo_result")["outcome"] == "undone" and not (h.backup / "notes.txt").exists()
    assert "correct horse" not in "".join(m for _, _, m in h.events)


def test_only_the_latest_change_can_be_undone(h):
    first = h.copy(h.source("first.txt"), "r1")
    second = h.copy(h.source("second.txt"), "r2")
    assert h.entry(first)["undo"]["state"] == "not_latest"
    assert h.entry(second)["undo"]["state"] == "available"
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=first)
    assert h.payload("history_undo_refused")["reason"] == "not_latest"


def test_other_changes_are_shown_with_the_reason_they_cannot_be_undone_here(h):
    journal = h.runtime.undo_journal
    paint = ActionRequest(category=ActionCategory.GUI_AUTOMATION, target="Paint", summary="Draw in Paint.")
    click = ActionRequest(category=ActionCategory.GUI_AUTOMATION, target="Notepad", summary="Type in Notepad.")
    for request in (paint, click):
        h.audit.record(AuditEvent.COMPLETED, request, outcome=ActionOutcome.SUCCEEDED)
    journal.record(click, Reversal.IRREVERSIBLE, {"reason": "automatic_undo_not_supported"})
    journal.record(paint, Reversal.APP_UNDO_STEPS, {"steps": 3, "window": "fixture"})
    assert h.entry(paint.action_id)["undo"]["state"] == "paint_flow"
    assert h.entry(click.action_id)["undo"] == {"state": "irreversible", "reversal": "irreversible",
                                                "reason": "automatic_undo_not_supported"}
    for action_id in (paint.action_id, click.action_id):
        h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
        assert h.payload("history_undo_refused")["actionId"] == action_id


def test_earlier_sessions_are_shown_but_never_offered_for_undo_and_the_list_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("QRONOS_PERMISSION_POLICY_PATH", str(tmp_path / "permissions_v2.json"))
    audit_path, journal_path = tmp_path / "logs" / "action_audit.jsonl", tmp_path / "data" / "undo_journal.jsonl"
    earlier_audit, earlier_journal = ActionAuditLog(path=audit_path), UndoJournal(path=journal_path)
    old = ActionRequest(category=ActionCategory.CREATE_OR_EDIT_FILE, target=str(tmp_path / "old" / "a.txt"),
                        summary="Move one exact local file.")
    earlier_audit.record(AuditEvent.COMPLETED, old, outcome=ActionOutcome.SUCCEEDED)
    earlier_journal.record(old, Reversal.MOVE_BACK, {"from": str(tmp_path / "x"), "to": str(tmp_path / "y")})
    for index in range(230):
        earlier_audit.record(AuditEvent.DECIDED, ActionRequest(category=ActionCategory.READ_NON_SENSITIVE,
                                                               target=f"item{index}", summary="Read."),
                             outcome=ActionOutcome.SUCCEEDED, decision=PermissionDecision.ALLOW)
    harness = Harness(tmp_path, audit_path=audit_path, journal_path=journal_path)  # a NEW Qronos session
    try:
        snapshot = harness.history()
        assert len(snapshot["actions"]) == 200 and snapshot["truncated"] is True
        harness.audit.record(AuditEvent.COMPLETED, old, outcome=ActionOutcome.SUCCEEDED)  # bring it to the top
        assert harness.entry(old.action_id)["undo"] == {"state": "earlier_session", "reversal": "move_back",
                                                         "reason": ""}
        harness.act(HISTORY_UNDO_REQUEST_ACTION, actionId=old.action_id)
        assert harness.payload("history_undo_refused")["reason"] == "earlier_session"
    finally:
        set_default_audit_sink(None)
        harness.runtime.close()


def test_an_identical_replacement_of_the_copy_is_not_the_object_qronos_created(h):
    action_id = h.copy(h.source())
    copy = h.backup / "notes.txt"
    copy.unlink()
    copy.write_text("qronos-history", encoding="utf-8")  # same bytes, a different file
    assert h.entry(action_id)["undo"]["state"] == "changed_since"
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    assert h.payload("history_undo_refused")["reason"] == "changed_since"
    assert copy.read_text(encoding="utf-8") == "qronos-history"


def test_a_record_without_ownership_evidence_is_never_offered(h):
    folder = h.root / "older-folder"
    folder.mkdir()
    request = ActionRequest(category=ActionCategory.CREATE_OR_EDIT_FILE, target=str(folder),
                            summary="Create one exact local folder.")
    h.audit.record(AuditEvent.COMPLETED, request, outcome=ActionOutcome.SUCCEEDED)
    h.runtime.undo_journal.record(request, Reversal.DELETE_CREATED, {"path": str(folder)})  # the legacy shape
    assert h.entry(request.action_id)["undo"]["state"] == "unverifiable"
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=request.action_id)
    assert h.payload("history_undo_refused")["reason"] == "unverifiable"
    assert folder.is_dir()


def test_no_file_content_is_read_to_list_history_or_before_the_gate_allows_the_undo(h, monkeypatch):
    action_id = h.copy(h.source())
    reads = []
    real = activity_history.bounded_sha256
    monkeypatch.setattr(activity_history, "bounded_sha256", lambda path: reads.append(path) or real(path))
    assert h.entry(action_id)["undo"]["state"] == "available" and reads == []  # metadata only
    refusing = lambda request, audit=None: Verdict(request=request, decision=PermissionDecision.DENY,  # noqa: E731
                                                   outcome=ActionOutcome.REFUSED, reason="fixture: Files denied")
    monkeypatch.setattr(gate_module, "evaluate", refusing)
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    assert h.payload("history_undo_refused")["reason"] == "permission_denied" and reads == []
    monkeypatch.undo()
    monkeypatch.setattr(activity_history, "bounded_sha256", lambda path: reads.append(path) or real(path))
    h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
    token = h.payload("history_undo_approval_required")["actionToken"]
    assert len(reads) == 1  # after the gate allowed it
    h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=token, approved=True)
    assert len(reads) == 2 and h.payload("history_undo_result")["outcome"] == "undone"  # and again before it


def test_a_moved_file_goes_back_only_while_it_is_the_same_file(h):
    source = h.source("report.txt", "report body")
    for attempt in ("replaced", "kept"):
        action = FileAction(operation=FileOperation.MOVE_FILE, source=str(source), destination=str(h.backup))
        execution = h.runtime.file_executor.execute(action, approval=PermissionDecision.REQUIRE_UI_CONFIRMATION)
        assert execution.result.outcome is ActionOutcome.SUCCEEDED
        moved = h.backup / "report.txt"
        action_id = execution.request.action_id
        if attempt == "replaced":
            moved.unlink()
            moved.write_text("report body", encoding="utf-8")
            assert h.entry(action_id)["undo"]["state"] == "changed_since"
            moved.unlink()
            source.write_text("report body", encoding="utf-8")  # start the second attempt cleanly
            continue
        assert h.entry(action_id)["undo"]["state"] == "available"
        h.act(HISTORY_UNDO_REQUEST_ACTION, actionId=action_id)
        token = h.payload("history_undo_approval_required")["actionToken"]
        assert h.payload("history_undo_approval_required")["reversal"] == "move_back"
        h.act(HISTORY_UNDO_DECISION_ACTION, actionToken=token, approved=True)
        assert h.payload("history_undo_result")["outcome"] == "undone"
        assert source.read_text(encoding="utf-8") == "report body" and not moved.exists()
