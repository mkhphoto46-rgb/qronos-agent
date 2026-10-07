"""
Single-use identities for prompts that wait on a person.

A Paint drawing, a Paint undo or a screen read can wait for an on-screen
"yes". The answer arrives later, from another process, possibly after Stop,
Pause or shutdown and possibly after a new prompt with identical wording was
shown. Matching answers by their text cannot tell those apart, so every prompt
gets an unguessable identity from this ledger and an answer is honoured only
by presenting that identity:

    - an identity is bound to the exact prepared action (and, for a screen
      read, the window that was in front when the question was asked);
    - it can be taken once; a second answer is refused;
    - Stop/Pause/shutdown revoke every open identity, and a revoked identity
      stays refused even if the same question is asked again later (the new
      prompt gets a new identity);
    - identities expire, so an answer days later is refused;
    - after ``close`` no identity can be issued or taken.

The ledger grants nothing by itself. An accepted answer still runs through
the action's own permission gate re-evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import secrets
import threading
import time
from typing import Any, Callable


APPROVAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22,64}$")

DEFAULT_APPROVAL_TTL_SECONDS = 10 * 60.0
MAX_REMEMBERED_SETTLED = 256

# Why an identity is no longer usable. Fixed vocabulary; reported to the UI.
STALE_MALFORMED = "malformed"
STALE_UNKNOWN = "unknown"
STALE_USED = "already_answered"
STALE_EXPIRED = "expired"
STALE_SUPERSEDED = "superseded"
STALE_CLOSED = "runtime_shutdown"
STALE_WRONG_KIND = "wrong_kind"


class ApprovalKind(Enum):
    PAINT_DRAW = "paint_draw"
    PAINT_UNDO = "paint_undo"
    SCREEN_READ = "screen_read"
    FILE_ACTION = "file_action"


class StaleApproval(LookupError):
    """An answer named an identity that cannot be honoured."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Approval is not usable ({reason}).")


@dataclass(frozen=True)
class ApprovalTicket:
    approval_id: str
    kind: ApprovalKind
    question: str
    payload: Any = None
    window: int | None = None
    issued_at: float = 0.0
    expires_at: float = 0.0


class ApprovalLedger:
    """Thread-safe store of open prompt identities."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
        token_fn: Callable[[], str] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._ttl = float(ttl_seconds)
        self._token = token_fn if token_fn is not None else (
            lambda: secrets.token_urlsafe(18)
        )
        self._open: dict[str, ApprovalTicket] = {}
        # approval_id -> reason, for identities that were used or revoked.
        self._settled: dict[str, str] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def issue(
        self,
        kind: ApprovalKind,
        question: str,
        *,
        payload: Any = None,
        payload_fn: Callable[[str], Any] | None = None,
        window: int | None = None,
    ) -> ApprovalTicket:
        """
        Open one prompt identity. ``payload_fn`` builds the payload from the
        new identity when the payload must carry it.
        """
        with self._lock:
            if self._closed:
                raise StaleApproval(STALE_CLOSED)
            approval_id = self._token()
            if not APPROVAL_ID_PATTERN.match(approval_id) or (
                approval_id in self._open or approval_id in self._settled
            ):
                raise RuntimeError("Approval identity generator returned an unusable value.")
            now = self._clock()
            if payload_fn is not None:
                payload = payload_fn(approval_id)
            ticket = ApprovalTicket(
                approval_id=approval_id,
                kind=kind,
                question=str(question or ""),
                payload=payload,
                window=window,
                issued_at=now,
                expires_at=now + self._ttl,
            )
            self._open[approval_id] = ticket
            return ticket

    def open_ticket(self, *kinds: ApprovalKind) -> ApprovalTicket | None:
        """The most recently issued open ticket of any of ``kinds``."""
        with self._lock:
            self._expire_locked()
            for ticket in reversed(tuple(self._open.values())):
                if ticket.kind in kinds:
                    return ticket
            return None

    def open_tickets(self) -> tuple[ApprovalTicket, ...]:
        with self._lock:
            self._expire_locked()
            return tuple(self._open.values())

    def take(
        self,
        approval_id: str,
        *,
        kinds: tuple[ApprovalKind, ...] | None = None,
    ) -> ApprovalTicket:
        """
        Consume one identity exactly once, or raise StaleApproval.

        With ``kinds``, an identity of another kind is refused and left
        untouched: a file decision can never consume a screen prompt, nor
        the reverse.
        """
        value = str(approval_id or "")
        if not APPROVAL_ID_PATTERN.match(value):
            raise StaleApproval(STALE_MALFORMED)
        with self._lock:
            if self._closed:
                raise StaleApproval(STALE_CLOSED)
            self._expire_locked()
            ticket = self._open.get(value)
            if ticket is None:
                raise StaleApproval(self._settled.get(value, STALE_UNKNOWN))
            if kinds is not None and ticket.kind not in kinds:
                raise StaleApproval(STALE_WRONG_KIND)
            del self._open[value]
            self._settle_locked(value, STALE_USED)
            return ticket

    def revoke(self, approval_id: str, reason: str) -> ApprovalTicket | None:
        with self._lock:
            ticket = self._open.pop(str(approval_id or ""), None)
            if ticket is not None:
                self._settle_locked(ticket.approval_id, reason)
            return ticket

    def revoke_kinds(
        self,
        kinds: tuple[ApprovalKind, ...],
        reason: str,
    ) -> tuple[ApprovalTicket, ...]:
        with self._lock:
            revoked = tuple(t for t in self._open.values() if t.kind in kinds)
            for ticket in revoked:
                del self._open[ticket.approval_id]
                self._settle_locked(ticket.approval_id, reason)
            return revoked

    def revoke_all(self, reason: str) -> tuple[ApprovalTicket, ...]:
        return self.revoke_kinds(tuple(ApprovalKind), reason)

    def close(self) -> tuple[ApprovalTicket, ...]:
        """Revoke everything and refuse all later issue/take. Idempotent."""
        with self._lock:
            # Closing and revocation share one critical section: an issuer
            # cannot slip a new ticket between revocation and the closed flag.
            self._closed = True
            revoked = tuple(self._open.values())
            self._open.clear()
            for ticket in revoked:
                self._settle_locked(ticket.approval_id, STALE_CLOSED)
            return revoked

    def _expire_locked(self) -> None:
        now = self._clock()
        for approval_id, ticket in tuple(self._open.items()):
            if now >= ticket.expires_at:
                del self._open[approval_id]
                self._settle_locked(approval_id, STALE_EXPIRED)

    def _settle_locked(self, approval_id: str, reason: str) -> None:
        self._settled.pop(approval_id, None)
        self._settled[approval_id] = reason
        while len(self._settled) > MAX_REMEMBERED_SETTLED:
            self._settled.pop(next(iter(self._settled)))
