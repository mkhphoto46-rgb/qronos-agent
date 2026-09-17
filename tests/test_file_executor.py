from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.action_audit import (
    ActionAuditLog,
    AuditEvent,
)
from core.actions import ActionOutcome, InvalidAction
from core.file_executor import (
    FileAction,
    FileExecutor,
    FileOperation,
)
from core.undo_journal import (
    EntryState,
    Reversal,
    UndoJournal,
)
from security.gate import Verdict
from security.permissions import (
    ActionCategory,
    PermissionDecision,
    PermissionLevel,
    PermissionPolicy,
)


def evaluator_for(
    decision: PermissionDecision,
):
    outcomes = {
        PermissionDecision.ALLOW:
            ActionOutcome.SUCCEEDED,
        PermissionDecision.REQUIRE_VOICE_CONFIRMATION:
            ActionOutcome.AWAITING_APPROVAL,
        PermissionDecision.REQUIRE_UI_CONFIRMATION:
            ActionOutcome.AWAITING_APPROVAL,
        PermissionDecision.REQUIRE_TYPED_SECRET:
            ActionOutcome.AWAITING_APPROVAL,
        PermissionDecision.DENY:
            ActionOutcome.REFUSED,
    }

    levels = {
        PermissionDecision.ALLOW:
            PermissionLevel.AUTO_ALLOW,
        PermissionDecision.REQUIRE_VOICE_CONFIRMATION:
            PermissionLevel.VOICE_CONFIRMATION,
        PermissionDecision.REQUIRE_UI_CONFIRMATION:
            PermissionLevel.UI_CONFIRMATION,
        PermissionDecision.REQUIRE_TYPED_SECRET:
            PermissionLevel.TYPED_SECRET,
        PermissionDecision.DENY:
            PermissionLevel.FORBIDDEN,
    }

    def evaluate(request, audit=None):
        verdict = Verdict(
            request=request,
            decision=decision,
            outcome=outcomes[decision],
            reason="test verdict",
            policy=PermissionPolicy(
                category=request.category,
                level=levels[decision],
                reversible=True,
                description="test policy",
            ),
        )
        if audit is not None:
            audit(verdict)
        return verdict

    return evaluate


class FileExecutorFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source_dir = self.root / "source"
        self.destination_dir = self.root / "destination"
        self.source_dir.mkdir()
        self.destination_dir.mkdir()
        self.source = self.source_dir / "notes.txt"
        self.source.write_text(
            "qronos-file-control",
            encoding="utf-8",
        )
        self.audit = ActionAuditLog(path=None)
        self.journal = UndoJournal(path=None)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def executor(
        self,
        decision=PermissionDecision.REQUIRE_UI_CONFIRMATION,
        *,
        opener=None,
    ) -> FileExecutor:
        return FileExecutor(
            audit=self.audit,
            journal=self.journal,
            evaluate_fn=evaluator_for(decision),
            opener=opener,
        )


class TestTypedShape(FileExecutorFixture):
    def test_write_operations_use_create_or_edit_file_policy(self) -> None:
        for action in (
            FileAction(
                FileOperation.COPY_FILE,
                source=str(self.source),
                destination=str(self.destination_dir),
            ),
            FileAction(
                FileOperation.MOVE_FILE,
                source=str(self.source),
                destination=str(self.destination_dir),
            ),
            FileAction(
                FileOperation.RENAME_FILE,
                source=str(self.source),
                new_name="renamed.txt",
            ),
            FileAction(
                FileOperation.CREATE_FOLDER,
                destination=str(self.root / "new-folder"),
            ),
        ):
            with self.subTest(operation=action.operation):
                self.assertIs(
                    action.to_request().category,
                    ActionCategory.CREATE_OR_EDIT_FILE,
                )

    def test_open_uses_open_application_policy(self) -> None:
        action = FileAction(
            FileOperation.OPEN_FILE,
            source=str(self.source),
        )
        self.assertIs(
            action.to_request().category,
            ActionCategory.OPEN_APPLICATION,
        )

    def test_relative_paths_are_refused_at_construction(self) -> None:
        with self.assertRaises(InvalidAction):
            FileAction(
                FileOperation.COPY_FILE,
                source="notes.txt",
                destination=str(self.destination_dir),
            )

    def test_rename_accepts_only_a_basename(self) -> None:
        with self.assertRaises(InvalidAction):
            FileAction(
                FileOperation.RENAME_FILE,
                source=str(self.source),
                new_name="other/name.txt",
            )


class TestApprovalIsAnExecutionBoundary(FileExecutorFixture):
    def test_ui_gated_copy_does_not_run_without_approval(self) -> None:
        execution = self.executor().execute(
            FileAction(
                FileOperation.COPY_FILE,
                source=str(self.source),
                destination=str(self.destination_dir),
            )
        )

        self.assertIs(
            execution.result.outcome,
            ActionOutcome.AWAITING_APPROVAL,
        )
        self.assertFalse(
            (self.destination_dir / self.source.name).exists()
        )

    def test_wrong_approval_mechanism_does_not_run(self) -> None:
        execution = self.executor().execute(
            FileAction(
                FileOperation.COPY_FILE,
                source=str(self.source),
                destination=str(self.destination_dir),
            ),
            approval=PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
        )

        self.assertIs(
            execution.result.outcome,
            ActionOutcome.AWAITING_APPROVAL,
        )
        self.assertFalse(
            (self.destination_dir / self.source.name).exists()
        )

    def test_a_refusal_cannot_be_overridden_by_an_approval_argument(self) -> None:
        execution = self.executor(
            PermissionDecision.DENY
        ).execute(
            FileAction(
                FileOperation.COPY_FILE,
                source=str(self.source),
                destination=str(self.destination_dir),
            ),
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

        self.assertIs(
            execution.result.outcome,
            ActionOutcome.REFUSED,
        )
        self.assertFalse(
            (self.destination_dir / self.source.name).exists()
        )


class TestCopyMoveRenameCreateAndUndo(FileExecutorFixture):
    def test_copy_verifies_records_and_undoes(self) -> None:
        executor = self.executor()
        execution = executor.execute(
            FileAction(
                FileOperation.COPY_FILE,
                source=str(self.source),
                destination=str(self.destination_dir),
            ),
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

        copied = self.destination_dir / self.source.name
        self.assertIs(
            execution.result.outcome,
            ActionOutcome.SUCCEEDED,
        )
        self.assertEqual(
            copied.read_text(encoding="utf-8"),
            "qronos-file-control",
        )
        self.assertIs(
            execution.undo_entry.reversal,
            Reversal.DELETE_CREATED,
        )

        updated = executor.undo(
            execution.request.action_id
        )
        self.assertIs(updated.state, EntryState.UNDONE)
        self.assertFalse(copied.exists())

    def test_move_and_undo(self) -> None:
        executor = self.executor()
        execution = executor.execute(
            FileAction(
                FileOperation.MOVE_FILE,
                source=str(self.source),
                destination=str(self.destination_dir),
            ),
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

        moved = self.destination_dir / self.source.name
        self.assertFalse(self.source.exists())
        self.assertTrue(moved.is_file())
        self.assertIs(
            execution.undo_entry.reversal,
            Reversal.MOVE_BACK,
        )

        executor.undo(execution.request.action_id)
        self.assertTrue(self.source.is_file())
        self.assertFalse(moved.exists())

    def test_rename_and_undo(self) -> None:
        executor = self.executor()
        execution = executor.execute(
            FileAction(
                FileOperation.RENAME_FILE,
                source=str(self.source),
                new_name="renamed.txt",
            ),
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

        renamed = self.source.with_name("renamed.txt")
        self.assertFalse(self.source.exists())
        self.assertTrue(renamed.is_file())

        executor.undo(execution.request.action_id)
        self.assertTrue(self.source.is_file())
        self.assertFalse(renamed.exists())

    def test_create_folder_and_undo(self) -> None:
        executor = self.executor()
        folder = self.root / "created-by-qronos"
        execution = executor.execute(
            FileAction(
                FileOperation.CREATE_FOLDER,
                destination=str(folder),
            ),
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

        self.assertTrue(folder.is_dir())
        executor.undo(execution.request.action_id)
        self.assertFalse(folder.exists())

    def test_existing_copy_target_is_not_overwritten(self) -> None:
        target = self.destination_dir / self.source.name
        target.write_text("keep-me", encoding="utf-8")

        execution = self.executor().execute(
            FileAction(
                FileOperation.COPY_FILE,
                source=str(self.source),
                destination=str(self.destination_dir),
            ),
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

        self.assertIs(
            execution.result.outcome,
            ActionOutcome.FAILED,
        )
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "keep-me",
        )


class TestOpen(FileExecutorFixture):
    def test_open_requires_the_exact_voice_confirmation(self) -> None:
        opened: list[str] = []
        executor = self.executor(
            PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
            opener=opened.append,
        )
        action = FileAction(
            FileOperation.OPEN_FILE,
            source=str(self.source),
        )

        pending = executor.execute(action)
        self.assertIs(
            pending.result.outcome,
            ActionOutcome.AWAITING_APPROVAL,
        )
        self.assertEqual(opened, [])

        done = executor.execute(
            action,
            approval=PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
        )
        self.assertIs(
            done.result.outcome,
            ActionOutcome.SUCCEEDED,
        )
        self.assertEqual(opened, [str(self.source)])
        self.assertIs(
            done.undo_entry.reversal,
            Reversal.NOTHING_TO_UNDO,
        )


class TestAudit(FileExecutorFixture):
    def test_success_records_decided_approved_completed(self) -> None:
        execution = self.executor().execute(
            FileAction(
                FileOperation.COPY_FILE,
                source=str(self.source),
                destination=str(self.destination_dir),
            ),
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

        self.assertIs(
            execution.result.outcome,
            ActionOutcome.SUCCEEDED,
        )
        self.assertEqual(
            [record.event for record in self.audit.records()],
            [
                AuditEvent.DECIDED,
                AuditEvent.APPROVED,
                AuditEvent.COMPLETED,
            ],
        )

    def test_undo_is_audited(self) -> None:
        executor = self.executor()
        execution = executor.execute(
            FileAction(
                FileOperation.RENAME_FILE,
                source=str(self.source),
                new_name="renamed.txt",
            ),
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

        executor.undo(execution.request.action_id)

        self.assertEqual(
            self.audit.records()[-1].event,
            AuditEvent.UNDONE,
        )


class TestPreparedApprovalIdentity(FileExecutorFixture):
    def test_prepare_and_resume_keep_one_action_id(self) -> None:
        executor = self.executor()
        action = FileAction(
            FileOperation.COPY_FILE,
            source=str(self.source),
            destination=str(self.destination_dir),
        )

        prepared = executor.prepare(action)
        action_id = prepared.request.action_id

        self.assertTrue(prepared.verdict.needs_approval)
        self.assertFalse(
            (self.destination_dir / self.source.name).exists()
        )

        execution = executor.execute_prepared(
            prepared,
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
            reevaluate=False,
        )

        self.assertEqual(
            execution.request.action_id,
            action_id,
        )
        self.assertEqual(
            execution.result.action_id,
            action_id,
        )
        self.assertTrue(
            (self.destination_dir / self.source.name).is_file()
        )
        self.assertEqual(
            {
                record.action_id
                for record in self.audit.records()
            },
            {action_id},
        )

    def test_resume_rechecks_policy_on_the_same_request(self) -> None:
        decisions = iter(
            [
                PermissionDecision.REQUIRE_UI_CONFIRMATION,
                PermissionDecision.DENY,
            ]
        )

        def changing_policy(request, audit=None):
            decision = next(decisions)
            return evaluator_for(decision)(
                request,
                audit=audit,
            )

        executor = FileExecutor(
            audit=self.audit,
            journal=self.journal,
            evaluate_fn=changing_policy,
        )
        action = FileAction(
            FileOperation.COPY_FILE,
            source=str(self.source),
            destination=str(self.destination_dir),
        )
        prepared = executor.prepare(action)

        execution = executor.execute_prepared(
            prepared,
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
            reevaluate=True,
        )

        self.assertIs(
            execution.result.outcome,
            ActionOutcome.REFUSED,
        )
        self.assertEqual(
            execution.request.action_id,
            prepared.request.action_id,
        )
        self.assertFalse(
            (self.destination_dir / self.source.name).exists()
        )

    def test_decline_records_human_decision_and_changes_nothing(self) -> None:
        executor = self.executor()
        action = FileAction(
            FileOperation.MOVE_FILE,
            source=str(self.source),
            destination=str(self.destination_dir),
        )
        prepared = executor.prepare(action)

        execution = executor.decline_prepared(
            prepared
        )

        self.assertIs(
            execution.result.outcome,
            ActionOutcome.REFUSED,
        )
        self.assertTrue(self.source.is_file())
        self.assertFalse(
            (self.destination_dir / self.source.name).exists()
        )
        self.assertEqual(
            [record.event for record in self.audit.records()],
            [
                AuditEvent.DECIDED,
                AuditEvent.DENIED_BY_USER,
            ],
        )
        self.assertEqual(
            {
                record.action_id
                for record in self.audit.records()
            },
            {prepared.request.action_id},
        )



if __name__ == "__main__":
    unittest.main()
