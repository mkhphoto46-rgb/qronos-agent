"""
What the Smart Queue remembers across a restart, and how honestly it says so.

The queue itself (:mod:`core.safe_queue`, :mod:`core.queue_scheduler`) is in
memory and stays that way: its invariants are proved in isolation and a file
does not belong in them. This is the record beside it — every accepted
reasoning request, the conversation request it answers, where it got to, and
whether the user put the queue on hold — written atomically through
:mod:`core.json_store` each time any of that changes.

Three rules decide everything here.

    **Nothing running survives as running.** A request that was running when
    Qronos stopped has an unknown outcome. Reading it back as "queued" would
    replay it unasked; it is read back as INTERRUPTED and runs again only when
    the user resumes it. (If its answer did reach the conversation before the
    stop, the caller can tell, and records it as done instead.)

    **A file this version cannot trust is never written over.** Corrupt, from
    a newer Qronos, or with no migration path: the journal stops writing,
    keeps the queue in memory, and says so. Starting empty and saving would
    look like "nothing was queued" and destroy whatever was recoverable.

    **Never claim a write that did not happen.** If the disk refuses, the
    journal reports itself not durable until a write succeeds again — and
    because it always writes its whole state, the first write that does
    succeed makes everything durable at once.

Finished requests keep only what the interface needs to show the outcome. The
request text and its conversation context are dropped the moment a request is
done, failed or cancelled: they are already in the conversation history, and
keeping a second copy is not needed for anything.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from core.json_store import (
    JsonStore,
    MigrationMissing,
    StoreCorrupt,
    StoreTooNew,
)


QUEUE_STATE_VERSION = 1

#: Points the journal somewhere else. Tests use it; so can a portable install.
QUEUE_STATE_PATH_ENV = "QRONOS_QUEUE_STATE_PATH"

QUEUE_STATE_FILE_NAME = "queue_state.json"

#: Finished outcomes kept for the interface. Older ones are forgotten; the
#: conversation history is the record, not this.
MAX_FINISHED_RECORDS = 50


def default_queue_state_path() -> Path:
    """The per-user location for queue state, under Qronos's own data root."""
    override = os.environ.get(QUEUE_STATE_PATH_ENV, "").strip()

    if override:
        return Path(override)

    # Imported here so that merely importing this module touches nothing on
    # disk: load_storage_locations creates directories.
    from core.storage_locations import load_storage_locations

    locations = load_storage_locations(create=True)

    return Path(locations.data_root) / "Queue" / QUEUE_STATE_FILE_NAME


class RecordState(Enum):
    """Where a request is, as far as anything on disk is concerned."""

    QUEUED = "queued"
    PAUSED = "paused"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in _FINISHED


_FINISHED = frozenset(
    {RecordState.DONE, RecordState.FAILED, RecordState.CANCELLED}
)


class StorageProblem(Enum):
    """Why the queue is not durable right now. Codes, not sentences."""

    CORRUPT = "corrupt"
    UNREADABLE = "unreadable"
    TOO_NEW = "too_new"
    NO_MIGRATION = "no_migration"
    UNWRITABLE = "unwritable"
    UNAVAILABLE = "unavailable"


#: Problems found in the file itself. While one of these stands, the journal
#: does not write at all: the file may hold something worth recovering.
_PROTECTED = frozenset(
    {
        StorageProblem.CORRUPT,
        StorageProblem.UNREADABLE,
        StorageProblem.TOO_NEW,
        StorageProblem.NO_MIGRATION,
    }
)


@dataclass(frozen=True)
class StorageStatus:
    durable: bool
    problem: StorageProblem | None = None
    detail: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "durable": self.durable,
            "problem": None if self.problem is None else self.problem.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class QueueRecord:
    """One accepted request, as written to disk."""

    task_id: str
    request_id: str
    summary: str
    route: str
    weight: str
    required_vram_mb: int
    state: RecordState
    submitted_at: float
    updated_at: float
    attempts: int = 0
    detail: str = ""

    # Present only while the request may still run. Cleared when it finishes.
    text: str = ""
    context: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def to_payload(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "requestId": self.request_id,
            "summary": self.summary,
            "route": self.route,
            "weight": self.weight,
            "requiredVramMb": self.required_vram_mb,
            "state": self.state.value,
            "submittedAt": self.submitted_at,
            "updatedAt": self.updated_at,
            "attempts": self.attempts,
            "detail": self.detail,
            "text": self.text,
            "context": [
                {"role": role, "content": content}
                for role, content in self.context
            ],
        }

    @classmethod
    def from_payload(cls, raw: Any) -> "QueueRecord":
        """
        Strictly. A record that does not look exactly right makes the whole
        file untrusted, rather than being skipped: a silently dropped request
        is the failure this module exists to prevent.
        """
        if not isinstance(raw, dict):
            raise StoreCorrupt("A queue record is not an object.")

        def text(key: str, *, required: bool = True) -> str:
            value = raw.get(key, None if required else "")

            if not isinstance(value, str) or (required and not value.strip()):
                raise StoreCorrupt(f"A queue record has no valid {key}.")

            return value

        def number(key: str) -> float:
            value = raw.get(key)

            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise StoreCorrupt(f"A queue record has no valid {key}.")

            return float(value)

        def whole(key: str) -> int:
            value = raw.get(key)

            if isinstance(value, bool) or not isinstance(value, int):
                raise StoreCorrupt(f"A queue record has no valid {key}.")

            return value

        try:
            state = RecordState(raw.get("state"))
        except ValueError:
            raise StoreCorrupt("A queue record has an unknown state.") from None

        context_raw = raw.get("context", [])

        if not isinstance(context_raw, list):
            raise StoreCorrupt("A queue record has an invalid context.")

        context: list[tuple[str, str]] = []

        for item in context_raw:
            if (
                not isinstance(item, dict)
                or item.get("role") not in ("user", "assistant")
                or not isinstance(item.get("content"), str)
            ):
                raise StoreCorrupt("A queue record has an invalid context.")

            context.append((item["role"], item["content"]))

        record = cls(
            task_id=text("taskId"),
            request_id=text("requestId"),
            summary=text("summary"),
            route=text("route"),
            weight=text("weight"),
            required_vram_mb=whole("requiredVramMb"),
            state=state,
            submitted_at=number("submittedAt"),
            updated_at=number("updatedAt"),
            attempts=whole("attempts"),
            detail=text("detail", required=False),
            text=text("text", required=False),
            context=tuple(context),
        )

        if not record.state.finished and not record.text.strip():
            raise StoreCorrupt(
                "A queue record that may still run has lost its request."
            )

        return record


@dataclass(frozen=True)
class Recovered:
    """What was read back at startup, already made safe to act on."""

    records: tuple[QueueRecord, ...]
    paused: bool

    #: Requests that were running when Qronos stopped and are now marked
    #: interrupted (or done, when their answer had already been recorded).
    interrupted: tuple[str, ...] = ()
    answered: tuple[str, ...] = ()


class QueueJournal:
    """
    The queue's durable record. Thread-safe; every change is one atomic save.

    ``path=None`` keeps it in memory, which is not a stub: it goes through
    the same validation and bookkeeping and simply reports itself as not
    durable.
    """

    def __init__(
        self,
        path: str | Path | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = JsonStore(path, version=QUEUE_STATE_VERSION)
        self._persistent = path is not None
        self._clock = clock
        self._lock = threading.RLock()

        self._records: dict[str, QueueRecord] = {}
        self._paused = False
        self._status = StorageStatus(
            durable=False,
            problem=None if self._persistent else StorageProblem.UNAVAILABLE,
            detail=(
                ""
                if self._persistent
                else "No place to save the queue was configured."
            ),
        )
        self._opened = False

    # ----------------------------------------------------------- startup

    def open(
        self,
        answered: Callable[[str], bool] = lambda request_id: False,
    ) -> Recovered:
        """
        Read the saved queue once, and make it safe.

        ``answered`` says whether a request's answer already reached the
        conversation; such a request was finished in all but bookkeeping when
        Qronos stopped, and is recorded as done rather than interrupted.
        """
        with self._lock:
            if self._opened:
                return Recovered(self.records(), self._paused)

            self._opened = True

            if not self._persistent:
                return Recovered((), False)

            try:
                document = self._store.load()
            except StoreTooNew:
                return self._refuse(
                    StorageProblem.TOO_NEW,
                    "The saved queue was written by a newer Qronos. It has "
                    "been left untouched; this queue is not being saved.",
                )
            except MigrationMissing:
                return self._refuse(
                    StorageProblem.NO_MIGRATION,
                    "The saved queue is from a version this Qronos cannot "
                    "read. It has been left untouched; this queue is not "
                    "being saved.",
                )
            except StoreCorrupt as error:
                if isinstance(error.__cause__, OSError):
                    # The disk would not hand it over. Not proof that it is
                    # damaged, so even more reason not to write over it.
                    return self._refuse(
                        StorageProblem.UNREADABLE,
                        "The saved queue could not be opened. It has been "
                        "left untouched; this queue is not being saved.",
                    )

                return self._refuse(
                    StorageProblem.CORRUPT,
                    "The saved queue could not be read. It has been left "
                    "untouched for recovery; this queue is not being saved.",
                )

            if document is None:
                # Nothing saved yet. Write the empty state now, so a location
                # that cannot be written is reported before anything is
                # queued rather than after the user trusted it with a
                # request.
                self._save()
                return Recovered((), False)

            try:
                records, paused = self._parse(document.payload)
            except StoreCorrupt:
                return self._refuse(
                    StorageProblem.CORRUPT,
                    "The saved queue is damaged. It has been left untouched "
                    "for recovery; this queue is not being saved.",
                )

            interrupted: list[str] = []
            finished_early: list[str] = []
            now = self._clock()

            for record in records:
                if not record.state.finished and answered(record.request_id):
                    # Its answer is already in the conversation. Whatever the
                    # file says - running, or a stale "queued" left by a save
                    # that failed after it started - it must not run again.
                    record = _finished(
                        record,
                        RecordState.DONE,
                        "Answered before Qronos closed; not run again.",
                        now,
                    )
                    finished_early.append(record.task_id)
                elif record.state is RecordState.RUNNING:
                    record = replace(
                        record,
                        state=RecordState.INTERRUPTED,
                        updated_at=now,
                        detail=(
                            "Qronos closed while this was running. It was "
                            "not run again automatically; resume it to try "
                            "again."
                        ),
                    )
                    interrupted.append(record.task_id)

                self._records[record.task_id] = record

            self._paused = paused
            self._status = StorageStatus(durable=True)

            if interrupted or finished_early or document.migrated:
                # The interrupted marks are written before anything can act
                # on them, so a second crash cannot turn them back into
                # "running".
                self._save()

            return Recovered(
                self.records(),
                paused,
                interrupted=tuple(interrupted),
                answered=tuple(finished_early),
            )

    def _refuse(self, problem: StorageProblem, detail: str) -> Recovered:
        self._status = StorageStatus(
            durable=False,
            problem=problem,
            detail=detail,
        )

        return Recovered((), False)

    @staticmethod
    def _parse(
        payload: dict[str, Any],
    ) -> tuple[list[QueueRecord], bool]:
        raw_records = payload.get("records", [])
        paused = payload.get("paused", False)

        if not isinstance(raw_records, list) or not isinstance(paused, bool):
            raise StoreCorrupt("The saved queue has the wrong shape.")

        records = [QueueRecord.from_payload(item) for item in raw_records]
        seen: set[str] = set()

        for record in records:
            if record.task_id in seen:
                raise StoreCorrupt("The saved queue repeats a task.")

            seen.add(record.task_id)

        return sorted(records, key=lambda item: item.submitted_at), paused

    # ----------------------------------------------------------- changes

    def add(self, record: QueueRecord) -> bool:
        """Record a newly accepted request. True when it is on disk."""
        with self._lock:
            if record.task_id in self._records:
                raise ValueError(f"{record.task_id} is already recorded.")

            self._records[record.task_id] = record

            return self._save()

    def update(
        self,
        task_id: str,
        state: RecordState,
        *,
        attempts: int | None = None,
        detail: str | None = None,
    ) -> bool:
        """
        Move a request. Writes only when something actually changed.

        A finished request loses its text and context in the same write.
        """
        with self._lock:
            record = self._records.get(task_id)

            if record is None:
                return self._status.durable

            if record.state.finished:
                # Final. A late worker, a stale view or a repeated event can
                # never turn a cancelled, failed or answered request back into
                # one that runs, or rewrite how it ended.
                return self._status.durable

            next_attempts = record.attempts if attempts is None else attempts
            next_detail = record.detail if detail is None else detail

            if (
                record.state is state
                and record.attempts == next_attempts
                and record.detail == next_detail
            ):
                return self._status.durable

            now = self._clock()

            if state.finished:
                updated = _finished(record, state, next_detail, now)
                updated = replace(updated, attempts=next_attempts)
            else:
                updated = replace(
                    record,
                    state=state,
                    attempts=next_attempts,
                    detail=next_detail,
                    updated_at=now,
                )

            self._records[task_id] = updated
            self._prune()

            return self._save()

    def set_paused(self, paused: bool) -> bool:
        with self._lock:
            if self._paused == paused:
                return self._status.durable

            self._paused = paused

            return self._save()

    # ----------------------------------------------------------- reading

    def records(self) -> tuple[QueueRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._records.values(),
                    key=lambda item: item.submitted_at,
                )
            )

    def get(self, task_id: str) -> QueueRecord | None:
        with self._lock:
            return self._records.get(task_id)

    def holds_request(self, request_id: str) -> bool:
        """
        Whether this request id is already spoken for.

        Anything still able to run, or already answered, owns its id: a
        second submission would attach a second answer to the same request.
        A failed or cancelled request does not; asking again is legitimate.
        """
        with self._lock:
            return any(
                record.request_id == request_id
                and record.state
                not in (RecordState.FAILED, RecordState.CANCELLED)
                for record in self._records.values()
            )

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def start_blocker(self) -> str | None:
        """
        Why queued work must not start right now, or None.

        Only one case: the queue is saved to a file, but the last save
        failed. The file then still says "queued" for anything that starts
        now, and a crash would replay it unasked. The save is retried here,
        so work resumes as soon as the disk does. A queue that was never
        saved (no location, or a file it refuses to touch) has nothing on
        disk that could replay, so it does not block.
        """
        with self._lock:
            if self._status.problem is not StorageProblem.UNWRITABLE:
                return None

            if self._save():
                return None

            return (
                "Qronos cannot save the queue right now, so it is not "
                "starting queued work: if Qronos stopped mid-run it could not "
                "record that honestly. It will try again."
            )

    def status(self) -> StorageStatus:
        with self._lock:
            return self._status

    # ----------------------------------------------------------- internals

    def _prune(self) -> None:
        finished = sorted(
            (
                record
                for record in self._records.values()
                if record.state.finished
            ),
            key=lambda item: item.updated_at,
        )

        for record in finished[: max(0, len(finished) - MAX_FINISHED_RECORDS)]:
            self._records.pop(record.task_id, None)

    def _save(self) -> bool:
        """The whole state, atomically. Lock held."""
        if not self._persistent:
            return False

        if self._status.problem in _PROTECTED:
            return False

        try:
            self._store.save(
                {
                    "paused": self._paused,
                    "records": [
                        record.to_payload() for record in self.records()
                    ],
                }
            )
        except OSError as error:
            self._status = StorageStatus(
                durable=False,
                problem=StorageProblem.UNWRITABLE,
                detail=(
                    "The queue could not be saved "
                    f"({error.__class__.__name__}). It keeps working, but "
                    "will be lost if Qronos closes before a save succeeds."
                ),
            )
            return False

        self._status = StorageStatus(durable=True)

        return True


def _finished(
    record: QueueRecord,
    state: RecordState,
    detail: str,
    now: float,
) -> QueueRecord:
    """A finished record, without the request text it no longer needs."""
    return replace(
        record,
        state=state,
        detail=detail,
        updated_at=now,
        text="",
        context=(),
    )
