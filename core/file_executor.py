"""
Typed, permission-gated file actions for Qronos.

This module is deliberately narrower than a general computer agent. It accepts
only explicit local paths and a small whitelist of file operations. Model prose
must never become shell commands.

Gate 1B keeps the Gate 1A operation set:
- open one exact file;
- copy one exact file into an existing destination directory;
- move one exact file into an existing destination directory;
- rename one exact file in place;
- create one exact folder under an existing parent.

Delete is intentionally excluded from Gate 1A. Recoverable deletion already has
its own stronger policy and will be integrated only after the approval path is
live end-to-end.

Every action:
1. becomes a typed ActionRequest;
2. goes through security.gate.evaluate;
3. stops on REFUSED or AWAITING_APPROVAL unless the caller supplies the exact
   approval mechanism requested by the verdict;
4. performs one whitelisted filesystem operation;
5. verifies the post-condition;
6. records the result in the action audit when one is supplied;
7. records explicit reversal data in the UndoJournal when one is supplied.

The approval argument is not a UI by itself. Gate 1B resumes the exact
PreparedFileAction after a trusted runtime/UI confirmation, preserving one
action identity across decision, execution, audit, and undo.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import shutil
from typing import Callable

from core.action_audit import ActionAuditLog, AuditEvent
from core.actions import (
    ActionOutcome,
    ActionRequest,
    ActionResult,
    InvalidAction,
)
from core.undo_journal import (
    EntryState,
    NotUndoable,
    Reversal,
    UndoEntry,
    UndoJournal,
)
from security.gate import Verdict, evaluate
from security.permissions import ActionCategory, PermissionDecision


class FileOperation(Enum):
    OPEN_FILE = "open_file"
    COPY_FILE = "copy_file"
    MOVE_FILE = "move_file"
    RENAME_FILE = "rename_file"
    CREATE_FOLDER = "create_folder"


@dataclass(frozen=True)
class FileAction:
    operation: FileOperation
    source: str = ""
    destination: str = ""
    new_name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.operation, FileOperation):
            raise InvalidAction("FileAction requires a FileOperation.")

        if self.operation in {
            FileOperation.OPEN_FILE,
            FileOperation.COPY_FILE,
            FileOperation.MOVE_FILE,
            FileOperation.RENAME_FILE,
        }:
            _absolute_path(self.source, "source")

        if self.operation in {
            FileOperation.COPY_FILE,
            FileOperation.MOVE_FILE,
            FileOperation.CREATE_FOLDER,
        }:
            _absolute_path(self.destination, "destination")

        if self.operation is FileOperation.RENAME_FILE:
            cleaned = self.new_name.strip()
            if not cleaned:
                raise InvalidAction("Rename requires a non-empty new_name.")
            if (
                cleaned in {".", ".."}
                or "/" in cleaned
                or "\\" in cleaned
            ):
                raise InvalidAction(
                    "Rename new_name must be a basename, not a path."
                )

    @property
    def category(self) -> ActionCategory:
        if self.operation is FileOperation.OPEN_FILE:
            return ActionCategory.OPEN_APPLICATION
        return ActionCategory.CREATE_OR_EDIT_FILE

    def to_request(self) -> ActionRequest:
        target = (
            self.destination
            if self.operation is FileOperation.CREATE_FOLDER
            else self.source
        )

        summaries = {
            FileOperation.OPEN_FILE: "Open one exact local file.",
            FileOperation.COPY_FILE: "Copy one exact local file.",
            FileOperation.MOVE_FILE: "Move one exact local file.",
            FileOperation.RENAME_FILE: "Rename one exact local file.",
            FileOperation.CREATE_FOLDER: "Create one exact local folder.",
        }

        return ActionRequest(
            category=self.category,
            target=str(_absolute_path(target, "target")),
            summary=summaries[self.operation],
            parameters={
                "operation": self.operation.value,
                "source": self.source,
                "destination": self.destination,
                "newName": self.new_name,
            },
        )


@dataclass(frozen=True)
class PreparedFileAction:
    """One immutable typed action and the exact gate verdict that judged it."""

    action: FileAction
    request: ActionRequest
    verdict: Verdict


@dataclass(frozen=True)
class FileExecution:
    request: ActionRequest
    verdict: Verdict
    result: ActionResult
    undo_entry: UndoEntry | None = None


EvaluateFn = Callable[..., Verdict]
OpenFn = Callable[[str], None]


class FileExecutor:
    """Execute only the whitelisted FileAction vocabulary."""

    def __init__(
        self,
        *,
        audit: ActionAuditLog | None = None,
        journal: UndoJournal | None = None,
        evaluate_fn: EvaluateFn = evaluate,
        opener: OpenFn | None = None,
    ) -> None:
        self.audit = audit
        self.journal = journal
        self._evaluate = evaluate_fn
        self._opener = opener if opener is not None else _default_open
        self._requests: dict[str, ActionRequest] = {}

    def prepare(
        self,
        action: FileAction,
    ) -> PreparedFileAction:
        """
        Build and judge one action without changing the machine.

        The returned object is what a trusted UI may hold while it asks the
        person for approval. Resuming it later keeps the same ActionRequest and
        therefore the same action_id in the audit and undo chain.
        """
        request = action.to_request()
        verdict = self._evaluate_request(request)

        return PreparedFileAction(
            action=action,
            request=request,
            verdict=verdict,
        )

    def execute(
        self,
        action: FileAction,
        *,
        approval: PermissionDecision | None = None,
    ) -> FileExecution:
        prepared = self.prepare(action)
        return self.execute_prepared(
            prepared,
            approval=approval,
            reevaluate=False,
        )

    def execute_prepared(
        self,
        prepared: PreparedFileAction,
        *,
        approval: PermissionDecision | None = None,
        reevaluate: bool = True,
    ) -> FileExecution:
        """
        Resume one exact prepared action.

        ``reevaluate=True`` is the production-safe default: a person may change
        the permission policy while an approval dialog is open. Re-evaluating
        the SAME ActionRequest respects that change without creating a new
        action identity.
        """
        if not isinstance(prepared, PreparedFileAction):
            raise InvalidAction(
                "execute_prepared requires a PreparedFileAction."
            )

        verdict = (
            self._evaluate_request(prepared.request)
            if reevaluate
            else prepared.verdict
        )

        if verdict.refused:
            return FileExecution(
                request=prepared.request,
                verdict=verdict,
                result=verdict.as_result(),
            )

        if verdict.needs_approval:
            if approval is not verdict.decision:
                return FileExecution(
                    request=prepared.request,
                    verdict=verdict,
                    result=verdict.as_result(),
                )

            if self.audit is not None:
                self.audit.record(
                    AuditEvent.APPROVED,
                    prepared.request,
                    decision=verdict.decision,
                )

        return self._run(
            prepared.action,
            prepared.request,
            verdict,
        )

    def decline_prepared(
        self,
        prepared: PreparedFileAction,
    ) -> FileExecution:
        """Record an explicit human decline without changing the machine."""
        if not isinstance(prepared, PreparedFileAction):
            raise InvalidAction(
                "decline_prepared requires a PreparedFileAction."
            )

        if not prepared.verdict.needs_approval:
            raise InvalidAction(
                "Only an action awaiting approval can be declined."
            )

        result = ActionResult(
            action_id=prepared.request.action_id,
            outcome=ActionOutcome.REFUSED,
            detail="Denied by user.",
        )

        if self.audit is not None:
            self.audit.record(
                AuditEvent.DENIED_BY_USER,
                prepared.request,
                decision=prepared.verdict.decision,
                outcome=ActionOutcome.REFUSED,
            )

        return FileExecution(
            request=prepared.request,
            verdict=prepared.verdict,
            result=result,
        )

    def _evaluate_request(
        self,
        request: ActionRequest,
    ) -> Verdict:
        return self._evaluate(
            request,
            audit=(
                self.audit.record_verdict
                if self.audit is not None
                else None
            ),
        )

    def _run(
        self,
        action: FileAction,
        request: ActionRequest,
        verdict: Verdict,
    ) -> FileExecution:
        try:
            detail, reversal, restoration = self._perform(action)
            result = ActionResult(
                action_id=request.action_id,
                outcome=ActionOutcome.SUCCEEDED,
                detail=detail,
            )
        except Exception as exc:
            result = ActionResult(
                action_id=request.action_id,
                outcome=ActionOutcome.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )

        if self.audit is not None:
            self.audit.record_result(request, result)

        undo_entry: UndoEntry | None = None

        if result.outcome is ActionOutcome.SUCCEEDED:
            self._requests[request.action_id] = request

            if self.journal is not None:
                undo_entry = self.journal.record(
                    request,
                    reversal,
                    restoration,
                )

        return FileExecution(
            request=request,
            verdict=verdict,
            result=result,
            undo_entry=undo_entry,
        )

    def undo(self, action_id: str) -> UndoEntry:
        if self.journal is None:
            raise NotUndoable(
                "This FileExecutor has no UndoJournal."
            )

        entry = self.journal.claim(action_id)

        try:
            self._perform_undo(entry)
        except Exception:
            self.journal.mark(
                action_id,
                EntryState.FAILED,
            )
            raise

        updated = self.journal.mark(
            action_id,
            EntryState.UNDONE,
        )

        request = self._requests.get(action_id)
        if (
            request is not None
            and self.audit is not None
        ):
            self.audit.record(
                AuditEvent.UNDONE,
                request,
                outcome=ActionOutcome.SUCCEEDED,
            )

        return updated

    def _perform(
        self,
        action: FileAction,
    ) -> tuple[str, Reversal, dict[str, str]]:
        operation = action.operation

        if operation is FileOperation.OPEN_FILE:
            source = _existing_file(action.source)
            self._opener(str(source))
            return (
                f"Opened {source}",
                Reversal.NOTHING_TO_UNDO,
                {},
            )

        if operation is FileOperation.COPY_FILE:
            source = _existing_file(action.source)
            destination = _existing_directory(
                action.destination
            )
            target = destination / source.name

            if target.exists():
                raise FileExistsError(str(target))

            shutil.copy2(source, target)

            if (
                not target.is_file()
                or target.stat().st_size
                != source.stat().st_size
            ):
                raise RuntimeError(
                    "Copy verification failed."
                )

            return (
                f"Copied {source} to {target}",
                Reversal.DELETE_CREATED,
                {"path": str(target)},
            )

        if operation is FileOperation.MOVE_FILE:
            source = _existing_file(action.source)
            destination = _existing_directory(
                action.destination
            )
            target = destination / source.name

            if target.exists():
                raise FileExistsError(str(target))

            original = str(source)
            shutil.move(str(source), str(target))

            if (
                source.exists()
                or not target.is_file()
            ):
                raise RuntimeError(
                    "Move verification failed."
                )

            return (
                f"Moved {original} to {target}",
                Reversal.MOVE_BACK,
                {
                    "from": str(target),
                    "to": original,
                },
            )

        if operation is FileOperation.RENAME_FILE:
            source = _existing_file(action.source)
            target = source.with_name(
                action.new_name.strip()
            )

            if target.exists():
                raise FileExistsError(str(target))

            original = str(source)
            source.rename(target)

            if (
                source.exists()
                or not target.is_file()
            ):
                raise RuntimeError(
                    "Rename verification failed."
                )

            return (
                f"Renamed {original} to {target}",
                Reversal.MOVE_BACK,
                {
                    "from": str(target),
                    "to": original,
                },
            )

        if operation is FileOperation.CREATE_FOLDER:
            target = _absolute_path(
                action.destination,
                "destination",
            )

            if target.exists():
                raise FileExistsError(str(target))

            parent = target.parent
            if not parent.is_dir():
                raise FileNotFoundError(
                    f"Parent folder does not exist: {parent}"
                )

            target.mkdir()

            if not target.is_dir():
                raise RuntimeError(
                    "Create-folder verification failed."
                )

            return (
                f"Created folder {target}",
                Reversal.DELETE_CREATED,
                {"path": str(target)},
            )

        raise InvalidAction(
            f"Unsupported file operation: {operation!r}"
        )

    def _perform_undo(
        self,
        entry: UndoEntry,
    ) -> None:
        if entry.reversal is Reversal.MOVE_BACK:
            source = _absolute_path(
                str(entry.restoration["from"]),
                "undo source",
            )
            target = _absolute_path(
                str(entry.restoration["to"]),
                "undo target",
            )

            if not source.exists():
                raise FileNotFoundError(str(source))

            if target.exists():
                raise FileExistsError(str(target))

            if not target.parent.is_dir():
                raise FileNotFoundError(
                    f"Undo parent does not exist: {target.parent}"
                )

            shutil.move(str(source), str(target))

            if source.exists() or not target.exists():
                raise RuntimeError(
                    "Move-back verification failed."
                )
            return

        if entry.reversal is Reversal.DELETE_CREATED:
            target = _absolute_path(
                str(entry.restoration["path"]),
                "undo target",
            )

            if target.is_file():
                target.unlink()
            elif target.is_dir():
                target.rmdir()
            elif target.exists():
                raise RuntimeError(
                    f"Unsupported created target: {target}"
                )
            else:
                raise FileNotFoundError(str(target))

            if target.exists():
                raise RuntimeError(
                    "Delete-created verification failed."
                )
            return

        raise NotUndoable(
            f"FileExecutor cannot perform reversal "
            f"{entry.reversal.value}."
        )


def _absolute_path(
    value: str,
    field: str,
) -> Path:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise InvalidAction(
            f"File action {field} must not be empty."
        )

    path = Path(cleaned).expanduser()

    if not path.is_absolute():
        raise InvalidAction(
            f"File action {field} must be an absolute path: "
            f"{cleaned}"
        )

    return path.resolve(strict=False)


def _existing_file(value: str) -> Path:
    path = _absolute_path(value, "source")
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path


def _existing_directory(value: str) -> Path:
    path = _absolute_path(value, "destination")
    if not path.is_dir():
        raise FileNotFoundError(
            f"Destination directory does not exist: {path}"
        )
    return path


def _default_open(path: str) -> None:
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise RuntimeError(
            "Opening files is supported by the Windows runtime only."
        )
    startfile(path)
