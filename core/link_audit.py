"""
An append-only record of what each device did.

The log answers one question: which device asked for what, from where, and what
was decided. It is written so it cannot answer any more than that.

Data minimisation is a property of the interface here, not a rule someone has
to remember. ``record`` takes enums and shape-checked identifiers. There is no
free-text field, so a caller cannot pass an utterance, an answer, a file path or
a key into it even by accident. The traffic on this link is a person's voice
commands in their own home; the log needs the shape of the activity to be
useful after an intrusion or a mistake, and does not need the contents to do
that.

Two smaller decisions worth naming:

    A device id that is not the right shape is logged as ``invalid`` rather
    than verbatim. The identity in a handshake comes from the peer, so it is
    untrusted input, and an unbounded attacker-chosen string does not belong in
    a log file.

    Size is capped with a single rollover. The file lives inside the existing
    ``LOGS_AND_TEMP`` storage budget component, and an audit log that can grow
    without limit is a storage problem wearing a security hat.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from core.config import CONFIG
from core.link_capability import LinkOp, LinkScope
from core.link_devices import is_valid_device_id


DEFAULT_AUDIT_PATH = CONFIG.paths.logs / "link_audit.jsonl"

# Two mebibytes, then one rollover, so the log costs at most four.
MAX_AUDIT_BYTES = 2 * 1024 * 1024

INVALID_DEVICE = "invalid"
UNPARSED_PEER = "unparsed"


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


class AuditEvent(Enum):
    """The things worth recording."""

    LINK_STARTED = "link_started"
    LINK_STOPPED = "link_stopped"

    HANDSHAKE_OK = "handshake_ok"
    HANDSHAKE_REFUSED = "handshake_refused"

    # A connection that authenticated but came from somewhere the current layer
    # does not accept. This is the line that would appear if Layer 2 were
    # switched on by mistake.
    SCOPE_REFUSED = "scope_refused"

    PAIRING_OPENED = "pairing_opened"
    PAIRING_COMPLETED = "pairing_completed"
    PAIRING_REFUSED = "pairing_refused"
    PAIRING_CANCELLED = "pairing_cancelled"

    REQUEST_ALLOWED = "request_allowed"
    REQUEST_REFUSED = "request_refused"
    REQUEST_NEEDS_APPROVAL = "request_needs_approval"
    REQUEST_FAILED = "request_failed"

    PROTOCOL_FAULT = "protocol_fault"

    DEVICE_REVOKED = "device_revoked"
    SESSION_OPENED = "session_opened"
    SESSION_CLOSED = "session_closed"

    CONNECTION_REJECTED = "connection_rejected"


@dataclass(frozen=True)
class AuditRecord:
    """One line of the log."""

    at: float
    event: AuditEvent
    device_id: str = ""
    peer: str = ""
    scope: LinkScope | None = None
    op: LinkOp | None = None
    reason: str = ""

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "at": round(self.at, 3),
            "event": self.event.value,
        }

        if self.device_id:
            payload["device"] = self.device_id

        if self.peer:
            payload["peer"] = self.peer

        if self.scope is not None:
            payload["scope"] = self.scope.value

        if self.op is not None:
            payload["op"] = self.op.value

        if self.reason:
            payload["reason"] = self.reason

        return payload

    def describe(self) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.at))
        parts = [stamp, self.event.value]

        if self.device_id:
            parts.append(self.device_id)

        if self.op is not None:
            parts.append(self.op.value)

        if self.reason:
            parts.append(f"({self.reason})")

        return " ".join(parts)


def _safe_device_id(device_id: str) -> str:
    """Untrusted identities do not enter the log verbatim."""

    if not device_id:
        return ""

    return device_id if is_valid_device_id(device_id) else INVALID_DEVICE


def _safe_peer(peer: str) -> str:
    """Only something that parses as an address is recorded as one."""

    if not peer:
        return ""

    import ipaddress

    try:
        return str(ipaddress.ip_address(peer.strip()))
    except ValueError:
        return UNPARSED_PEER


class AuditLog:
    """
    The audit log.

    ``path=None`` keeps records in memory, which is what the tests use and what
    the demo below uses.
    """

    def __init__(
        self,
        path: str | Path | None = DEFAULT_AUDIT_PATH,
        clock: Clock | None = None,
        max_bytes: int = MAX_AUDIT_BYTES,
    ) -> None:
        self.path = None if path is None else Path(path)
        self.clock: Clock = clock if clock is not None else time.time
        self.max_bytes = max_bytes
        self._memory: list[AuditRecord] = []

    def record(
        self,
        event: AuditEvent,
        device_id: str = "",
        peer: str = "",
        scope: LinkScope | None = None,
        op: LinkOp | None = None,
        reason: Enum | None = None,
    ) -> AuditRecord:
        """
        Append one record.

        ``reason`` must be an enum member. A plain string is refused, which is
        what stops arbitrary text — a file path, a question, an answer —
        reaching the log through the one field that looks like it might take
        some.
        """

        if reason is not None and not isinstance(reason, Enum):
            raise TypeError(
                "reason must be an enum member, not "
                f"{type(reason).__name__}; the audit log does not take "
                "free text"
            )

        record = AuditRecord(
            at=self.clock(),
            event=event,
            device_id=_safe_device_id(device_id),
            peer=_safe_peer(peer),
            scope=scope,
            op=op,
            reason="" if reason is None else str(reason.value),
        )

        self._memory.append(record)
        self._append(record)

        return record

    # ------------------------------------------------------------- the file

    def _append(self, record: AuditRecord) -> None:
        if self.path is None:
            return

        line = json.dumps(record.to_json(), ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed(len(encoded))

            with self.path.open("ab") as handle:
                handle.write(encoded)
        except OSError:
            # A link that cannot write its log should still work. The record is
            # already in memory, and losing an audit line is preferable to
            # dropping the user's connection over a full disk.
            return

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            current = self.path.stat().st_size  # type: ignore[union-attr]
        except OSError:
            return

        if current + incoming <= self.max_bytes:
            return

        previous = self.path.with_suffix(  # type: ignore[union-attr]
            self.path.suffix + ".1"  # type: ignore[union-attr]
        )

        try:
            os.replace(self.path, previous)  # type: ignore[arg-type]
        except OSError:
            return

    # ------------------------------------------------------------- reading

    def records(self) -> tuple[AuditRecord, ...]:
        """Everything recorded by this instance, in order."""
        return tuple(self._memory)

    def count(self, event: AuditEvent) -> int:
        return sum(1 for record in self._memory if record.event is event)

    def describe(self, limit: int = 20) -> str:
        return "\n".join(
            record.describe() for record in self._memory[-limit:]
        )


def main() -> None:
    """Record a few things, then try to smuggle text into the log."""

    from core.link_capability import AuthReason
    from core.link_pairing import PairingRefusal

    log = AuditLog(path=None)

    log.record(AuditEvent.LINK_STARTED)
    log.record(
        AuditEvent.HANDSHAKE_OK,
        device_id="00112233445566aa",
        peer="192.168.1.42",
        scope=LinkScope.LOCAL_NETWORK,
    )
    log.record(
        AuditEvent.REQUEST_REFUSED,
        device_id="00112233445566aa",
        op=LinkOp.RUN_APP,
        reason=AuthReason.OUT_OF_SCOPE,
    )
    log.record(
        AuditEvent.PAIRING_REFUSED,
        peer="203.0.113.7",
        reason=PairingRefusal.NOT_LOCAL_NETWORK,
    )
    log.record(
        AuditEvent.HANDSHAKE_REFUSED,
        device_id="../../etc/passwd",
        peer="not an address",
    )

    print(log.describe())
    print()

    try:
        log.record(AuditEvent.REQUEST_ALLOWED, reason="user asked about X")  # type: ignore[arg-type]
    except TypeError as exc:
        print("free text refused:", exc)


if __name__ == "__main__":
    main()
