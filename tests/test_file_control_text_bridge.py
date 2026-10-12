from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.action_audit import ActionAuditLog, AuditEvent
from core.file_executor import FileExecutor
from core.runtime_bridge import (
    FILE_ACTION_DECISION_ACTION,
    FILE_WRITE_APPROVAL_LEASE_SECONDS,
    QronosRuntime,
    handle_action,
)
from core.task_router import TaskRouter
from core.undo_journal import UndoJournal
from security.gate import set_default_audit_sink


class _FakeMessage:
    def __init__(
        self,
        *,
        role: str,
        text: str,
        source: str,
        request_id: str,
    ) -> None:
        self.role = role
        self.text = text
        self.source = source
        self.request_id = request_id

    def to_public_dict(self) -> dict:
        return {
            "id": f"{self.role}-{self.request_id}",
            "role": "qronos" if self.role == "assistant" else self.role,
            "text": self.text,
            "source": self.source,
            "timestamp": 1.0,
            "requestId": self.request_id,
        }


class _FakeHistory:
    def __init__(self) -> None:
        self.messages: list[_FakeMessage] = []

    def append(
        self,
        *,
        role: str,
        text: str,
        source: str,
        request_id: str = "",
    ) -> _FakeMessage:
        message = _FakeMessage(
            role=role,
            text=text,
            source=source,
            request_id=request_id,
        )
        self.messages.append(message)
        return message

    def brain_context(self):
        return ()


class TextFileControlFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source_dir = self.root / "source"
        self.archive = self.root / "Archive"
        self.backup = self.root / "Backup"
        self.source_dir.mkdir()
        self.archive.mkdir()
        self.backup.mkdir()

        self.source = self.source_dir / "test.txt"
        self.source.write_text("qronos-live", encoding="utf-8")

        self.old_policy = os.environ.get(
            "QRONOS_PERMISSION_POLICY_PATH"
        )
        self.old_permission_audit = os.environ.get(
            "QRONOS_PERMISSION_AUDIT_PATH"
        )
        os.environ["QRONOS_PERMISSION_POLICY_PATH"] = str(
            self.root / "permissions_v2.json"
        )
        os.environ["QRONOS_PERMISSION_AUDIT_PATH"] = str(
            self.root / "permission_changes.jsonl"
        )

        self.events: list[tuple[str, str, str]] = []
        self.runtime = QronosRuntime(
            notify=lambda event, status, message: self.events.append(
                (event, status, message)
            )
        )
        self.runtime.task_router = TaskRouter()
        self.now = 1_000.0
        self.runtime._file_approval_clock = lambda: self.now

        # COMPUTER turns never call the Brain. A sentinel here proves the path
        # does not need to initialize or invoke a model.
        self.runtime.orchestrator = object()  # type: ignore[assignment]
        self.runtime.conversation_history = _FakeHistory()  # type: ignore[assignment]

        audit = ActionAuditLog(path=None)
        journal = UndoJournal(path=None)
        self.runtime.action_audit = audit
        self.runtime.undo_journal = journal
        self.runtime.file_executor = FileExecutor(
            audit=audit,
            journal=journal,
        )

    def tearDown(self) -> None:
        set_default_audit_sink(None)

        if self.old_policy is None:
            os.environ.pop(
                "QRONOS_PERMISSION_POLICY_PATH",
                None,
            )
        else:
            os.environ[
                "QRONOS_PERMISSION_POLICY_PATH"
            ] = self.old_policy

        if self.old_permission_audit is None:
            os.environ.pop(
                "QRONOS_PERMISSION_AUDIT_PATH",
                None,
            )
        else:
            os.environ[
                "QRONOS_PERMISSION_AUDIT_PATH"
            ] = self.old_permission_audit

        self.runtime.close()
        self.temp.cleanup()

    def event_payload(self, event_type: str) -> dict:
        matching = [
            message
            for event, _, message in self.events
            if event == event_type
        ]
        self.assertTrue(
            matching,
            f"No {event_type} event was emitted. Events: {self.events}",
        )
        return json.loads(matching[-1])

    def test_copy_waits_for_ui_then_reuses_same_action_identity(self) -> None:
        command = (
            f'فایل "{self.source}" را در پوشه "{self.backup}" کپی کن'
        )

        self.runtime.run_text_turn(
            command,
            "req-copy",
        )

        target = self.backup / self.source.name
        self.assertFalse(target.exists())

        approval = self.event_payload(
            "file_action_approval_required"
        )
        self.assertEqual(
            approval["permission"],
            "require_ui_confirmation",
        )
        initial_action_id = approval["actionId"]

        self.runtime.resolve_file_action_decision(
            approval["actionToken"],
            approved=True,
        )

        self.assertTrue(target.is_file())
        completed = self.event_payload(
            "file_action_completed"
        )
        self.assertEqual(
            completed["actionId"],
            initial_action_id,
        )
        self.assertTrue(
            any(
                event == "text_response"
                for event, _, _ in self.events
            )
        )

        action_ids = {
            record.action_id
            for record in self.runtime.action_audit.records()
        }
        self.assertEqual(
            action_ids,
            {initial_action_id},
        )

    def test_decline_changes_nothing_and_resolves_text_turn(self) -> None:
        command = (
            f'فایل "{self.source}" را به پوشه "{self.archive}" منتقل کن'
        )

        self.runtime.run_text_turn(
            command,
            "req-decline",
        )

        approval = self.event_payload(
            "file_action_approval_required"
        )

        self.runtime.resolve_file_action_decision(
            approval["actionToken"],
            approved=False,
        )

        self.assertTrue(self.source.is_file())
        self.assertFalse(
            (self.archive / self.source.name).exists()
        )
        self.assertTrue(
            any(
                event == "file_action_declined"
                for event, _, _ in self.events
            )
        )
        self.assertTrue(
            any(
                record.event is AuditEvent.DENIED_BY_USER
                for record in self.runtime.action_audit.records()
            )
        )
        response = self.event_payload(
            "text_response"
        )
        self.assertIn(
            "هیچ تغییری",
            response["text"],
        )

    def test_rename_executes_only_after_approval(self) -> None:
        command = (
            f'اسم فایل "{self.source}" را به "renamed.txt" تغییر بده'
        )

        self.runtime.run_text_turn(
            command,
            "req-rename",
        )
        approval = self.event_payload(
            "file_action_approval_required"
        )

        renamed = self.source.with_name("renamed.txt")
        self.assertFalse(renamed.exists())

        self.runtime.resolve_file_action_decision(
            approval["actionToken"],
            approved=True,
        )

        self.assertFalse(self.source.exists())
        self.assertTrue(renamed.is_file())

    def test_create_folder_executes_only_after_approval(self) -> None:
        folder = self.root / "Photos"
        command = f'پوشه "{folder}" را بساز'

        self.runtime.run_text_turn(
            command,
            "req-folder",
        )
        approval = self.event_payload(
            "file_action_approval_required"
        )

        self.assertFalse(folder.exists())

        self.runtime.resolve_file_action_decision(
            approval["actionToken"],
            approved=True,
        )

        self.assertTrue(folder.is_dir())

    def test_unquoted_computer_request_fails_closed(self) -> None:
        self.runtime.run_text_turn(
            "این فایل را جابه جا کن",
            "req-ambiguous",
        )

        self.assertTrue(
            any(
                event == "text_error"
                for event, _, _ in self.events
            )
        )
        self.assertFalse(
            any(
                event == "file_action_approval_required"
                for event, _, _ in self.events
            )
        )


    def approval_event_count(self) -> int:
        return sum(
            1
            for event, _, _ in self.events
            if event == "file_action_approval_required"
        )

    def approve_first_copy(self) -> None:
        command = (
            f'فایل "{self.source}" را در پوشه "{self.backup}" کپی کن'
        )
        self.runtime.run_text_turn(command, "req-lease-first")
        approval = self.event_payload("file_action_approval_required")
        self.runtime.resolve_file_action_decision(
            approval["actionToken"],
            approved=True,
        )
        self.assertTrue((self.backup / self.source.name).is_file())

    def test_approval_starts_a_ten_minute_fixed_lease(self) -> None:
        self.assertEqual(
            FILE_WRITE_APPROVAL_LEASE_SECONDS,
            600.0,
        )
        self.approve_first_copy()
        self.assertTrue(
            self.runtime._file_write_approval_lease_active()
        )
        self.assertAlmostEqual(
            self.runtime._file_write_approval_lease_remaining(),
            600.0,
            places=6,
        )

    def test_second_action_inside_lease_executes_without_second_dialog(self) -> None:
        self.approve_first_copy()
        first_count = self.approval_event_count()

        self.now += 120.0
        folder = self.root / "LeaseFolder"
        self.runtime.run_text_turn(
            f'پوشه "{folder}" را بساز',
            "req-lease-second",
        )

        self.assertTrue(folder.is_dir())
        self.assertEqual(
            self.approval_event_count(),
            first_count,
        )
        self.assertTrue(
            any(
                event == "file_action_approval_lease_used"
                for event, _, _ in self.events
            )
        )

    def test_auto_actions_do_not_extend_the_lease(self) -> None:
        self.approve_first_copy()
        lease_until = self.runtime._file_write_approval_lease_until

        self.now += 540.0
        folder = self.root / "NearExpiry"
        self.runtime.run_text_turn(
            f'پوشه "{folder}" را بساز',
            "req-near-expiry",
        )
        self.assertTrue(folder.is_dir())
        self.assertEqual(
            self.runtime._file_write_approval_lease_until,
            lease_until,
        )

        self.now += 61.0
        next_folder = self.root / "AfterExpiry"
        self.runtime.run_text_turn(
            f'پوشه "{next_folder}" را بساز',
            "req-after-expiry",
        )
        self.assertFalse(next_folder.exists())
        self.assertEqual(self.approval_event_count(), 2)

    def test_pause_revokes_an_active_lease(self) -> None:
        self.approve_first_copy()
        self.assertTrue(
            self.runtime._file_write_approval_lease_active()
        )

        self.runtime.set_runtime_paused(True)
        self.assertFalse(
            self.runtime._file_write_approval_lease_active()
        )
        self.runtime.set_runtime_paused(False)

        folder = self.root / "AfterPause"
        self.runtime.run_text_turn(
            f'پوشه "{folder}" را بساز',
            "req-after-pause",
        )
        self.assertFalse(folder.exists())
        self.assertEqual(self.approval_event_count(), 2)

    def test_decline_does_not_start_a_lease(self) -> None:
        folder = self.root / "DeclinedLease"
        self.runtime.run_text_turn(
            f'پوشه "{folder}" را بساز',
            "req-decline-lease",
        )
        approval = self.event_payload("file_action_approval_required")
        self.runtime.resolve_file_action_decision(
            approval["actionToken"],
            approved=False,
        )
        self.assertFalse(
            self.runtime._file_write_approval_lease_active()
        )


class TestFileDecisionActionDispatch(unittest.TestCase):
    def test_nested_tauri_data_reaches_runtime_decision_method(self) -> None:
        done = threading.Event()
        calls: list[tuple[str, bool]] = []

        class FakeRuntime:
            runtime_paused = False

            def resolve_file_action_decision(
                self,
                action_token: str,
                *,
                approved: bool,
            ) -> None:
                calls.append(
                    (action_token, approved)
                )
                done.set()

            def _reject_if_runtime_paused(self, _: str) -> bool:
                return False

        with redirect_stdout(io.StringIO()):
            handle_action(
                FakeRuntime(),  # type: ignore[arg-type]
                {
                    "actionId": FILE_ACTION_DECISION_ACTION,
                    "data": {
                        "actionToken": "token-123",
                        "approved": True,
                    },
                },
            )

        self.assertTrue(done.wait(2.0))
        self.assertEqual(
            calls,
            [("token-123", True)],
        )


if __name__ == "__main__":
    unittest.main()
