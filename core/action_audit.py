"""
What Qronos was asked to do, what it was allowed to do, and what happened.

The device link already has an audit log, and this is its sibling for actions.
Both write one JSON object per line through :mod:`core.append_only_log`, and
both follow the same rule about what may go in a record.

That rule is the reason this module exists rather than a logging call at each
call site: **an audit record must not become a place user content ends up.**
A file path, a spoken sentence or a model's answer written into a log turns a
diagnostic file into a copy of the user's data, sitting outside every control
that governs the real thing. So the fields here are the ones already fixed by
the action schema — a category, a target, a one-line summary the user was shown
anyway — and a free-text field the caller cannot reach.

Refusals are recorded as carefully as successes. An action that was denied is
the more interesting entry: it is the one that tells you something tried.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from core.actions import ActionOutcome, ActionRequest, ActionResult
from core.append_only_log import DEFAULT_MAX_BYTES, AppendOnlyLog
from core.config import CONFIG
from security.permissions import PermissionDecision


DEFAULT_ACTION_AUDIT_PATH = CONFIG.paths.logs / "action_audit.jsonl"

SCHEMA_VERSION = 1

# How much of a summary reaches the log. The summary is written by Qronos, not
# typed by the user, so it is not user content — but it describes user content,
# and an unbounded field is how a log quietly becomes a transcript.
MAX_SUMMARY_CHARS = 200

# Same reasoning for the target, which is often a path or a URL.
MAX_TARGET_CHARS = 200

REDACTED = "[redacted]"


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


class AuditEvent(Enum):
    """The moments in an action's life that are worth recording."""

    # The gate decided. Written for every action, including refusals.
    DECIDED = "decided"

    # A person answered a confirmation request.
    APPROVED = "approved"
    DENIED_BY_USER = "denied_by_user"

    # The action ran, or tried to.
    COMPLETED = "completed"

    # The action was rolled back.
    UNDONE = "undone"


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value).split())

    if len(text) <= limit:
        return text

    return text[: limit - 1] + "…"


@dataclass(frozen=True)
class ActionAuditRecord:
    """One line of the action audit trail."""

    at: float
    event: AuditEvent
    action_id: str
    category: str
    target: str
    summary: str

    # An enum value, never free text. Same rule the link audit log enforces.
    outcome: str = ""
    decision: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "at": round(self.at, 3),
            "event": self.event.value,
            "actionId": self.action_id,
            "category": self.category,
            "target": self.target,
            "summary": self.summary,
            "outcome": self.outcome,
            "decision": self.decision,
        }

    def describe(self) -> str:
        parts = [self.event.value, self.category, self.target]

        if self.outcome:
            parts.append(self.outcome)

        return " | ".join(parts)


class ActionAuditLog:
    """
    The action audit trail.

    ``path=None`` keeps records in memory only, which is what the tests use.
    """

    def __init__(
        self,
        path: str | Path | None = DEFAULT_ACTION_AUDIT_PATH,
        clock: Clock | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._file = AppendOnlyLog(path, max_bytes=max_bytes)
        self.clock: Clock = clock if clock is not None else time.time
        self._memory: list[ActionAuditRecord] = []

    @property
    def path(self) -> Path | None:
        return self._file.path

    def record(
        self,
        event: AuditEvent,
        request: ActionRequest,
        outcome: ActionOutcome | None = None,
        decision: PermissionDecision | None = None,
    ) -> ActionAuditRecord:
        """
        Append one record.

        ``outcome`` and ``decision`` must be enum members. A plain string is
        refused, which is what keeps arbitrary text out of the one field that
        looks like it might take some — the same guard the link audit log
        applies to its reason field, for the same reason.
        """
        for name, value, expected in (
            ("outcome", outcome, ActionOutcome),
            ("decision", decision, PermissionDecision),
        ):
            if value is not None and not isinstance(value, expected):
                raise TypeError(
                    f"{name} must be a {expected.__name__} member, not "
                    f"{type(value).__name__}; the audit log does not take "
                    "free text"
                )

        record = ActionAuditRecord(
            at=self.clock(),
            event=event,
            action_id=request.action_id,
            category=request.category.value,
            target=_clip(request.target, MAX_TARGET_CHARS),
            summary=_clip(request.summary, MAX_SUMMARY_CHARS),
            outcome="" if outcome is None else outcome.value,
            decision="" if decision is None else decision.value,
        )

        self._memory.append(record)
        self._file.append(record.to_json())

        return record

    def record_verdict(self, verdict: Any) -> ActionAuditRecord:
        """
        Record a gate verdict.

        Shaped to be passed straight to ``security.gate.evaluate`` as its audit
        sink, so wiring the trail in is one argument rather than a wrapper at
        every call site. Typed loosely to keep the audit trail free of an
        import back into the gate, which imports the action schema this module
        also uses.
        """
        return self.record(
            AuditEvent.DECIDED,
            request=verdict.request,
            outcome=verdict.outcome,
            decision=verdict.decision,
        )

    def record_result(
        self,
        request: ActionRequest,
        result: ActionResult,
    ) -> ActionAuditRecord:
        return self.record(
            AuditEvent.COMPLETED,
            request=request,
            outcome=result.outcome,
        )

    # ------------------------------------------------------------- reading

    def records(self) -> tuple[ActionAuditRecord, ...]:
        """Everything recorded by this instance, in order."""
        return tuple(self._memory)

    def count(self, event: AuditEvent) -> int:
        return sum(1 for record in self._memory if record.event is event)

    def for_action(self, action_id: str) -> tuple[ActionAuditRecord, ...]:
        """Every record about one action, in order."""
        return tuple(
            record
            for record in self._memory
            if record.action_id == action_id
        )

    def describe(self, limit: int = 20) -> str:
        return "\n".join(
            record.describe() for record in self._memory[-limit:]
        )
