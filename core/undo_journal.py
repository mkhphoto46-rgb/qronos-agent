"""
What would have to happen to put something back.

No rollback engine here, and that is the point. Undo is the kind of feature
that cannot be retrofitted: by the time an executor exists, the information
needed to reverse what it did has usually already been thrown away. Writing the
journal first forces the question — *what exactly would you need to undo this?*
— while the action schema is still soft enough to answer it.

So this module records intent to reverse, and refuses to pretend about the rest:

    An action is only undoable if the journal was given enough to reverse it.
    A recorded entry carries the restoration data explicitly. There is no
    "probably fine" state.

    An irreversible action is recorded as irreversible rather than omitted.
    "We deleted this and cannot bring it back" is exactly what a user asking
    for undo needs to be told, and a silent gap tells them nothing.

    Entries are consumed in reverse order, and consuming one is a separate step
    from performing it. Undo that half-succeeds must not leave the journal
    claiming the work is still pending or already done; the caller marks the
    entry only once it knows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol

from core.actions import ActionRequest, InvalidAction
from core.append_only_log import DEFAULT_MAX_BYTES, AppendOnlyLog
from core.config import CONFIG


DEFAULT_JOURNAL_PATH = CONFIG.paths.data / "undo_journal.jsonl"

SCHEMA_VERSION = 1


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


class Reversal(Enum):
    """How an action would be put back, if it can be."""

    # Nothing was changed, so there is nothing to reverse.
    NOTHING_TO_UNDO = "nothing_to_undo"

    # The previous content is held somewhere the journal names.
    RESTORE_CONTENT = "restore_content"

    # Something was moved and can be moved back.
    MOVE_BACK = "move_back"

    # Something was created and can be removed again.
    DELETE_CREATED = "delete_created"

    # Something was started and can be stopped.
    STOP_STARTED = "stop_started"

    # The change cannot be reversed by Qronos. Recorded, never hidden.
    IRREVERSIBLE = "irreversible"


class EntryState(Enum):
    """Where a journal entry is in its life."""

    # Recorded, and the action it describes has not been reversed.
    PENDING = "pending"

    # Reversed successfully.
    UNDONE = "undone"

    # Reversal was attempted and failed. Terminal: retrying blind is how one
    # failed undo becomes two conflicting states.
    FAILED = "failed"

    # Too old, superseded, or explicitly discarded by the user.
    EXPIRED = "expired"


class NotUndoable(Exception):
    """An entry cannot be reversed, and the caller asked to reverse it."""


# Which reversals need restoration data to be meaningful. Recording
# RESTORE_CONTENT with nothing to restore from is the failure this catches: it
# would look undoable in a list and fail at the moment the user asked.
_REQUIRES_RESTORATION = frozenset(
    {
        Reversal.RESTORE_CONTENT,
        Reversal.MOVE_BACK,
        Reversal.DELETE_CREATED,
        Reversal.STOP_STARTED,
    }
)


@dataclass(frozen=True)
class UndoEntry:
    """One reversible — or explicitly irreversible — step."""

    action_id: str
    category: str
    summary: str
    reversal: Reversal
    at: float

    # What the reversal needs, flat and JSON-safe: a backup path, an original
    # location, a process id. Never the content itself. A journal that held the
    # user's file contents would be a second uncontrolled copy of their data.
    restoration: Mapping[str, Any] = field(default_factory=dict)

    state: EntryState = EntryState.PENDING

    def __post_init__(self) -> None:
        if not self.action_id.strip():
            raise InvalidAction("An undo entry must name an action.")

        if (
            self.reversal in _REQUIRES_RESTORATION
            and not self.restoration
        ):
            raise InvalidAction(
                f"{self.reversal.value} needs restoration data; "
                "an entry that cannot actually be reversed must be "
                "recorded as irreversible instead."
            )

        object.__setattr__(self, "restoration", dict(self.restoration))

    @property
    def undoable(self) -> bool:
        """True when reversing this is both possible and still outstanding."""
        return (
            self.state is EntryState.PENDING
            and self.reversal
            not in {Reversal.IRREVERSIBLE, Reversal.NOTHING_TO_UNDO}
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "actionId": self.action_id,
            "category": self.category,
            "summary": self.summary,
            "reversal": self.reversal.value,
            "at": round(self.at, 3),
            "restoration": dict(self.restoration),
            "state": self.state.value,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "UndoEntry":
        if data.get("schemaVersion") != SCHEMA_VERSION:
            raise InvalidAction(
                f"Unsupported undo schema version: "
                f"{data.get('schemaVersion')!r}."
            )

        try:
            return cls(
                action_id=str(data["actionId"]),
                category=str(data.get("category", "")),
                summary=str(data.get("summary", "")),
                reversal=Reversal(str(data["reversal"])),
                at=float(data.get("at", 0.0)),
                restoration=data.get("restoration", {}),
                state=EntryState(str(data.get("state", "pending"))),
            )
        except (KeyError, ValueError) as error:
            raise InvalidAction(f"Malformed undo entry: {error}.") from error

    def describe(self) -> str:
        return f"{self.summary} ({self.reversal.value}, {self.state.value})"


class UndoJournal:
    """
    The stack of things that could be put back.

    ``path=None`` keeps entries in memory, which is what the tests use.
    """

    def __init__(
        self,
        path: str | Path | None = DEFAULT_JOURNAL_PATH,
        clock: Clock | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._file = AppendOnlyLog(path, max_bytes=max_bytes)
        self.clock: Clock = clock if clock is not None else time.time
        self._entries: list[UndoEntry] = []

    @property
    def path(self) -> Path | None:
        return self._file.path

    def record(
        self,
        request: ActionRequest,
        reversal: Reversal,
        restoration: Mapping[str, Any] | None = None,
    ) -> UndoEntry:
        """Add one entry for an action that has already happened."""
        entry = UndoEntry(
            action_id=request.action_id,
            category=request.category.value,
            summary=request.summary,
            reversal=reversal,
            at=self.clock(),
            restoration=restoration or {},
        )

        self._entries.append(entry)
        self._file.append(entry.to_json())

        return entry

    def entries(self) -> tuple[UndoEntry, ...]:
        """Everything recorded, oldest first."""
        return tuple(self._entries)

    def undoable(self) -> tuple[UndoEntry, ...]:
        """
        What could be reversed, most recent first.

        Reverse order because undo is a stack. Reversing an older change while
        a newer one still stands on top of it produces a state the user never
        had.
        """
        return tuple(
            entry for entry in reversed(self._entries) if entry.undoable
        )

    def next_undoable(self) -> UndoEntry | None:
        """The entry a plain "undo that" would reverse."""
        candidates = self.undoable()

        return candidates[0] if candidates else None

    def claim(self, action_id: str) -> UndoEntry:
        """
        Take an entry, checking it can actually be reversed.

        Claiming does not mark it. The caller marks it afterwards with
        :meth:`mark`, once it knows what happened, because an entry marked
        before the work is attempted is a lie either way it goes.
        """
        for entry in self._entries:
            if entry.action_id != action_id:
                continue

            if entry.reversal is Reversal.IRREVERSIBLE:
                raise NotUndoable(
                    f"{entry.summary} cannot be undone by Qronos."
                )

            if entry.reversal is Reversal.NOTHING_TO_UNDO:
                raise NotUndoable(f"{entry.summary} changed nothing.")

            if entry.state is not EntryState.PENDING:
                raise NotUndoable(
                    f"{entry.summary} is already {entry.state.value}."
                )

            return entry

        raise NotUndoable(f"No undo entry for action {action_id}.")

    def mark(self, action_id: str, state: EntryState) -> UndoEntry:
        """Record how a reversal turned out."""
        for index, entry in enumerate(self._entries):
            if entry.action_id != action_id:
                continue

            updated = UndoEntry(
                action_id=entry.action_id,
                category=entry.category,
                summary=entry.summary,
                reversal=entry.reversal,
                at=entry.at,
                restoration=entry.restoration,
                state=state,
            )

            self._entries[index] = updated
            self._file.append(updated.to_json())

            return updated

        raise NotUndoable(f"No undo entry for action {action_id}.")

    def describe(self, limit: int = 10) -> str:
        return "\n".join(
            entry.describe() for entry in self.undoable()[:limit]
        )
