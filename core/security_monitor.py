"""
The deterministic Security Monitor for the device link (requirement D9).

The product architecture fixes the ladder - Reject, Throttle, Contain, Safe
Shutdown - and two rules this module is built around:

    No model decides whether an intrusion exists. Every decision here is a
    typed policy applied to counted, typed signals in monotonic time windows.

    One malformed packet, or a message claiming an attack, must never be
    enough for the big responses, or the defence becomes the attack. Only
    deterministic evidence contains, and only repeated containment
    quarantines.

What it acts on is Qronos's own link and nothing else: it can refuse a
connection or a request, slow one source down, stop the relay dialler and end
remote sessions, or stop the whole link. It never touches Windows, other
programs or the user's files. Safe shutdown of the rest of Qronos is a hook
the runtime may provide; this module never shuts anything down by itself, and
the default policy does not ask for it.

Who a signal is held against is the part that is easy to get wrong:

    LAN_PEER    an address on the home network, before it has proved a key.
                A throttled peer is slowed - one attempt per interval - not
                banned, so a phone sharing that address still gets in.
    RELAY       everything the relay delivers before a phone has proved a key.
                All relayed traffic shares the relay's address, so it is never
                held against a LAN address or against a device it merely
                names: a hostile relay cannot get a phone on the sofa, or a
                device it has never authenticated as, refused.
    DEVICE      a device that has proved its key, in one scope. Abuse over
                the relay limits that device's remote access, not its LAN use.
    SYSTEM      the PC's own state, for integrity failures it detects itself.

A claimed identity that was not proved - the device id offered in a failed
handshake - is never anybody's principal.

State is bounded (a fixed number of tracked principals, each window no longer
than its threshold) and nothing in it is secret. The bound is segregated: LAN
addresses are untrusted and cheap to invent, so they live in a table of their
own, and filling that table can only ever push out other LAN entries - never
the relay's, a device's or the system's counters or throttles (review finding
D9-1: a shared table let a churn of ~1,030 LAN addresses lift a device's and
the relay's active throttles early). Nothing in either table is secret: device ids, addresses and
reasons, never keys, route tokens or request contents. Containment and
quarantine are latched and persisted, so a restart does not quietly undo
them; only the user, at the PC, resets them. An unreadable state file reads
as contained - the safe direction. Decisions are written to a hash-chained,
size-capped incident log, so a later edit or deletion of a line is evident.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from core.append_only_log import AppendOnlyLog


STATE_VERSION = 1

# Principals followed at once, in two tables that never evict each other.
#
# LAN addresses are untrusted and anyone on the network can invent more; when
# their table is full, an expired LAN throttle goes first, then the least
# recently seen LAN entry - which at worst forgives a LAN address early and
# never touches anything else.
MAX_LAN_TRACKED = 1024

# The relay, the system and authenticated devices (two scopes each). None of
# these can be multiplied by an outsider - a device exists only after pairing
# at the PC - so this table is not reachable by churn at all; its bound is a
# backstop, and even then expired throttles go first.
MAX_OTHER_TRACKED = 256

# The total hard bound on tracked windows, and separately on throttles.
MAX_TRACKED = MAX_LAN_TRACKED + MAX_OTHER_TRACKED

_LAN = "lan"
_OTHER = "other"
_CAPS = {_LAN: MAX_LAN_TRACKED, _OTHER: MAX_OTHER_TRACKED}


def _segment(principal: Principal) -> str:
    return _LAN if principal.kind is PrincipalKind.LAN_PEER else _OTHER

# A throttled LAN address may still try once per this long, so a phone that
# shares the address with whatever earned the throttle is slowed, not shut out.
LAN_RETRY_INTERVAL_SECONDS = 5.0

# Containments that, within this long, escalate to quarantine.
QUARANTINE_AFTER_CONTAINMENTS = 3
QUARANTINE_WINDOW_SECONDS = 24 * 3600.0

MAX_INCIDENT_BYTES = 512 * 1024
MAX_MEMORY_INCIDENTS = 512


class PrincipalKind(Enum):
    LAN_PEER = "lan_peer"
    RELAY = "relay"
    DEVICE = "device"
    SYSTEM = "system"


@dataclass(frozen=True)
class Principal:
    """Who a signal is held against. Built only through the constructors below."""

    kind: PrincipalKind
    ident: str = ""
    scope: str = ""

    @classmethod
    def lan(cls, address: str) -> Principal:
        try:
            parsed = ipaddress.ip_address(address.strip())
            mapped = getattr(parsed, "ipv4_mapped", None)
            ident = str(mapped or parsed)
        except (ValueError, AttributeError):
            ident = "unparsed"

        return cls(PrincipalKind.LAN_PEER, ident)

    @classmethod
    def relay(cls) -> Principal:
        return cls(PrincipalKind.RELAY, "relay")

    @classmethod
    def device(cls, device_id: str, scope: str) -> Principal:
        # Only ever built after a proved key; the shape is checked anyway so a
        # caller mistake cannot put arbitrary text into state or the log.
        if len(device_id) != 16 or any(c not in "0123456789abcdef" for c in device_id):
            raise ValueError("a device principal needs a proved device id")

        return cls(PrincipalKind.DEVICE, device_id, scope)

    @classmethod
    def system(cls) -> Principal:
        return cls(PrincipalKind.SYSTEM, "qronos")

    def describe(self) -> str:
        return f"{self.kind.value}:{self.ident}" + (f"@{self.scope}" if self.scope else "")


class SignalKind(Enum):
    """What happened. Each kind may only be held against certain principals."""

    HANDSHAKE_FAILED = "handshake_failed"
    PAIRING_OVER_RELAY = "pairing_over_relay"
    UNAUTHENTICATED_SPLICE = "unauthenticated_splice"
    RELAY_MISBEHAVED = "relay_misbehaved"
    TAMPERING = "tampering"
    REQUEST_REFUSED = "request_refused"
    RATE_LIMITED = "rate_limited"
    COMMAND_CONFLICT = "command_conflict"
    PROTOCOL_FAULT = "protocol_fault"
    REGISTRY_UNREADABLE = "registry_unreadable"


class Response(Enum):
    """The ladder, in order of severity."""

    ALLOW = 0
    REJECT = 1
    THROTTLE = 2
    CONTAIN = 3
    QUARANTINE = 4
    SAFE_SHUTDOWN = 5


@dataclass(frozen=True)
class Policy:
    """
    When enough of one signal arrives from one principal, respond.

    ``threshold`` signals within ``window`` seconds trigger ``response``. A
    throttle lasts ``duration`` seconds. Below the threshold the answer is
    REJECT: the action that produced the signal was already refused, and the
    monitor only counts it.
    """

    signal: SignalKind
    principals: frozenset[PrincipalKind]
    threshold: int
    window: float
    response: Response
    duration: float = 0.0
    why: str = ""


_UNAUTH = frozenset({PrincipalKind.LAN_PEER, PrincipalKind.RELAY})
_RELAY = frozenset({PrincipalKind.RELAY})
_DEVICE = frozenset({PrincipalKind.DEVICE})
_SYSTEM = frozenset({PrincipalKind.SYSTEM})

DEFAULT_POLICIES: dict[SignalKind, Policy] = {
    policy.signal: policy
    for policy in (
        Policy(SignalKind.HANDSHAKE_FAILED, _UNAUTH, 10, 60.0, Response.THROTTLE, 300.0,
               "Keys are 256 bits, so this is about CPU, not guessing: slow the source."),
        Policy(SignalKind.PAIRING_OVER_RELAY, _RELAY, 3, 3600.0, Response.THROTTLE, 3600.0,
               "Pairing is local-only; a relay delivering pending devices is misbehaving."),
        Policy(SignalKind.UNAUTHENTICATED_SPLICE, _RELAY, 10, 600.0, Response.THROTTLE, 900.0,
               "Splices that never authenticate: stop parking for a while."),
        Policy(SignalKind.RELAY_MISBEHAVED, _RELAY, 5, 600.0, Response.THROTTLE, 900.0,
               "Instant expiries or keepalive floods."),
        Policy(SignalKind.TAMPERING, _RELAY, 1, 60.0, Response.CONTAIN, 0.0,
               "The inner TLS failed its integrity check inside an authenticated relayed "
               "session. TCP and the outer TLS already guard the wire, so only something "
               "past the outer TLS - the relay - can have altered the bytes. Deterministic."),
        Policy(SignalKind.REQUEST_REFUSED, _DEVICE, 20, 600.0, Response.THROTTLE, 900.0,
               "An authenticated device asking again and again for what it may not do."),
        Policy(SignalKind.RATE_LIMITED, _DEVICE, 10, 600.0, Response.THROTTLE, 900.0,
               "A device that keeps exhausting its request budget."),
        Policy(SignalKind.COMMAND_CONFLICT, _DEVICE, 5, 600.0, Response.THROTTLE, 900.0,
               "Command ids reused for different requests."),
        Policy(SignalKind.PROTOCOL_FAULT, _DEVICE, 5, 600.0, Response.THROTTLE, 900.0,
               "Malformed frames from an authenticated session."),
        Policy(SignalKind.REGISTRY_UNREADABLE, _SYSTEM, 1, 60.0, Response.CONTAIN, 0.0,
               "Nobody can say which devices are trusted, so nothing remote is."),
    )
}


@dataclass(frozen=True)
class Decision:
    response: Response
    signal: SignalKind
    principal: Principal
    until: float | None = None


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str = ""


@dataclass
class _Throttle:
    until: float
    next_attempt: float = 0.0


class SecurityMonitorError(Exception):
    pass


# --------------------------------------------------------------- incident log


class IncidentLog:
    """
    Decisions, hash-chained so an edited or deleted line is evident.

    Each line carries the hash of the line before it and its own hash over its
    content plus that link. The chain continues across the single rollover.
    Only decisions and resets are written - one line per throttle, containment
    or quarantine, not one per signal - so a flood cannot rotate history away.
    """

    def __init__(self, path: Path | None, max_bytes: int = MAX_INCIDENT_BYTES) -> None:
        self._file = AppendOnlyLog(path, max_bytes=max_bytes)
        self._memory: deque[dict[str, Any]] = deque(maxlen=MAX_MEMORY_INCIDENTS)
        self._last = "0" * 64

        records = self._file.read_records()

        if records and isinstance(records[-1].get("hash"), str):
            self._last = records[-1]["hash"]

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        body = dict(entry, prev=self._last)
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        line = dict(body, hash=digest)
        self._last = digest
        self._memory.append(line)
        self._file.append(line)

        return line

    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._memory)

    def file_records(self) -> tuple[dict[str, Any], ...]:
        return self._file.read_records()


def verify_chain(lines: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> bool:
    """True when every line's hash matches its content and links to the last."""

    previous: str | None = None

    for line in lines:
        body = {key: value for key, value in line.items() if key != "hash"}
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        if digest != line.get("hash"):
            return False

        if previous is not None and line.get("prev") != previous:
            return False

        previous = line["hash"]

    return True


# ------------------------------------------------------------------- monitor


class SecurityMonitor:
    """One per link runtime. Thread-safe; callbacks run outside its lock."""

    def __init__(
        self,
        state_path: Path | None = None,
        incident_path: Path | None = None,
        policies: dict[SignalKind, Policy] | None = None,
        clock: Callable[[], float] | None = None,
        wall: Callable[[], float] | None = None,
        shutdown_on_quarantine: bool = False,
    ) -> None:
        self.state_path = None if state_path is None else Path(state_path)
        self.policies = dict(DEFAULT_POLICIES if policies is None else policies)
        self.clock = clock if clock is not None else time.monotonic
        self.wall = wall if wall is not None else time.time
        self.shutdown_on_quarantine = shutdown_on_quarantine
        self.incidents = IncidentLog(None if incident_path is None else Path(incident_path))

        self._lock = threading.RLock()
        # Segregated by _segment(): LAN churn can only evict LAN entries.
        self._windows: dict[str, OrderedDict[tuple[Principal, SignalKind], deque[float]]] = {
            _LAN: OrderedDict(), _OTHER: OrderedDict(),
        }
        self._throttles: dict[str, OrderedDict[Principal, _Throttle]] = {
            _LAN: OrderedDict(), _OTHER: OrderedDict(),
        }

        self.contained = False
        self.contained_reason = ""
        self.quarantined = False
        self.quarantine_reason = ""
        self._contain_history: list[float] = []

        self._on_contain: list[Callable[[str], None]] = []
        self._on_quarantine: list[Callable[[str], None]] = []
        self._on_safe_shutdown: list[Callable[[str], None]] = []
        self._on_reset: list[Callable[[], None]] = []

        self.ready = False
        self._load()
        self.ready = True

    # ---------------------------------------------------------------- state

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return

        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))

            if raw.get("version") != STATE_VERSION:
                raise ValueError("unknown version")

            self.contained = bool(raw.get("contained", False))
            self.contained_reason = str(raw.get("contained_reason", ""))
            self.quarantined = bool(raw.get("quarantined", False))
            self.quarantine_reason = str(raw.get("quarantine_reason", ""))
            self._contain_history = [float(t) for t in raw.get("contain_history", [])][-QUARANTINE_AFTER_CONTAINMENTS:]
        except (OSError, ValueError, TypeError, AttributeError):
            # Unreadable means unknown, and unknown is not safe for remote
            # access. Contained until the user resets it at the PC.
            self.contained = True
            self.contained_reason = "state_unreadable"

    def _save(self) -> None:
        if self.state_path is None:
            return

        payload = {
            "version": STATE_VERSION,
            "contained": self.contained,
            "contained_reason": self.contained_reason,
            "quarantined": self.quarantined,
            "quarantine_reason": self.quarantine_reason,
            "contain_history": self._contain_history[-QUARANTINE_AFTER_CONTAINMENTS:],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_path)

    # ------------------------------------------------------------- hooks

    def on_contain(self, callback: Callable[[str], None]) -> None:
        self._on_contain.append(callback)

    def on_quarantine(self, callback: Callable[[str], None]) -> None:
        self._on_quarantine.append(callback)

    def on_safe_shutdown(self, callback: Callable[[str], None]) -> None:
        self._on_safe_shutdown.append(callback)

    def on_reset(self, callback: Callable[[], None]) -> None:
        self._on_reset.append(callback)

    @staticmethod
    def _call(callbacks: list, *args: Any) -> None:
        for callback in list(callbacks):
            try:
                callback(*args)
            except Exception:
                # One enforcer failing must not stop the others enforcing.
                pass

    # --------------------------------------------------------- admission

    def remote_admission(self) -> Admission:
        """Whether remote access may run at all. The one gate for WAN."""

        with self._lock:
            if not self.ready:
                return Admission(False, "security_monitor_not_ready")

            if self.quarantined:
                return Admission(False, "security_quarantined")

            if self.contained:
                return Admission(False, "security_contained")

            return Admission(True)

    def link_admission(self) -> Admission:
        """Whether the link may run at all. Only quarantine stops the LAN."""

        with self._lock:
            if self.quarantined:
                return Admission(False, "security_quarantined")

            return Admission(True)

    def admit(self, principal: Principal) -> bool:
        """
        May this principal go ahead now?

        A throttled LAN address gets one attempt per interval; any other
        throttled principal waits for the throttle to end.
        """

        now = self.clock()

        with self._lock:
            table = self._throttles[_segment(principal)]
            throttle = table.get(principal)

            if throttle is None:
                return True

            if now >= throttle.until:
                del table[principal]

                return True

            if principal.kind is PrincipalKind.LAN_PEER and now >= throttle.next_attempt:
                throttle.next_attempt = now + LAN_RETRY_INTERVAL_SECONDS

                return True

            return False

    def throttled(self, principal: Principal) -> bool:
        now = self.clock()

        with self._lock:
            throttle = self._throttles[_segment(principal)].get(principal)

            return throttle is not None and now < throttle.until

    # ----------------------------------------------------------- signals

    def observe(self, signal: SignalKind, principal: Principal) -> Decision:
        """
        Count one signal and apply its policy. Returns what was decided.

        A signal held against a principal its policy does not name is refused
        outright: that would be a caller attributing, say, a relay's failure to
        a device, which is exactly the mistake this module exists to prevent.
        """

        policy = self.policies.get(signal)

        if policy is None or principal.kind not in policy.principals:
            raise SecurityMonitorError(
                f"{signal.value} cannot be held against a {principal.kind.value}"
            )

        now = self.clock()
        escalate: list[tuple[list, str]] = []

        with self._lock:
            segment = _segment(principal)
            windows = self._windows[segment]
            key = (principal, signal)
            window = windows.pop(key, None)

            if window is None:
                window = deque(maxlen=max(1, policy.threshold))

            windows[key] = window

            while len(windows) > _CAPS[segment]:
                windows.popitem(last=False)

            while window and now - window[0] > policy.window:
                window.popleft()

            window.append(now)

            if len(window) < policy.threshold:
                return Decision(Response.REJECT, signal, principal)

            # Threshold reached: respond, and start counting afresh.
            window.clear()

            if policy.response is Response.THROTTLE:
                until = now + policy.duration
                throttles = self._throttles[segment]
                current = throttles.pop(principal, None)
                throttles[principal] = _Throttle(
                    until=max(until, current.until if current else 0.0),
                    next_attempt=now + LAN_RETRY_INTERVAL_SECONDS,
                )
                self._bound_throttles(segment, now)

                self._record(Response.THROTTLE, signal, principal, policy.duration)

                return Decision(Response.THROTTLE, signal, principal, until)

            decision = self._contain_locked(signal, principal, escalate)

        for callbacks, reason in escalate:
            self._call(callbacks, reason)

        return decision

    def _bound_throttles(self, segment: str, now: float) -> None:
        """
        Keep one segment's throttles within its cap, touching no other segment.

        Expired throttles are dropped first. Only if every throttle in the
        segment is still running is the oldest of them forgiven - and for the
        LAN segment that is a LAN address, never a device or the relay.
        """

        throttles = self._throttles[segment]

        while len(throttles) > _CAPS[segment]:
            expired = next((p for p, t in throttles.items() if now >= t.until), None)

            if expired is not None:
                del throttles[expired]
            else:
                throttles.popitem(last=False)

    def _contain_locked(self, signal: SignalKind, principal: Principal, escalate: list) -> Decision:
        if self.contained or self.quarantined:
            # Already latched: one incident does not count twice.
            return Decision(Response.CONTAIN, signal, principal)

        wall_now = self.wall()
        self.contained = True
        self.contained_reason = signal.value
        self._contain_history = [
            t for t in self._contain_history if wall_now - t < QUARANTINE_WINDOW_SECONDS
        ] + [wall_now]
        response = Response.CONTAIN
        self._record(Response.CONTAIN, signal, principal)
        escalate.append((self._on_contain, signal.value))

        if len(self._contain_history) >= QUARANTINE_AFTER_CONTAINMENTS:
            self.quarantined = True
            self.quarantine_reason = "repeated_containment"
            response = Response.QUARANTINE
            self._record(Response.QUARANTINE, signal, principal)
            escalate.append((self._on_quarantine, self.quarantine_reason))

            if self.shutdown_on_quarantine:
                response = Response.SAFE_SHUTDOWN
                self._record(Response.SAFE_SHUTDOWN, signal, principal)
                escalate.append((self._on_safe_shutdown, self.quarantine_reason))

        self._save()

        return Decision(response, signal, principal)

    def _record(self, response: Response, signal: SignalKind, principal: Principal, duration: float = 0.0) -> None:
        entry: dict[str, Any] = {
            "at": round(self.wall(), 3),
            "response": response.name.lower(),
            "signal": signal.value,
            "principal": principal.describe(),
        }

        if duration:
            entry["seconds"] = duration

        self.incidents.append(entry)

    # ------------------------------------------------------------- reset

    def reset(self, by: str = "console") -> None:
        """
        The user, at the PC, says it is over. Clears every latch and throttle.

        Not reachable over the link: device management is console-only, and
        this is device management of the strongest kind.
        """

        if by != "console":
            raise SecurityMonitorError("only the console can reset the security monitor")

        with self._lock:
            self.contained = False
            self.contained_reason = ""
            self.quarantined = False
            self.quarantine_reason = ""
            for table in (*self._throttles.values(), *self._windows.values()):
                table.clear()
            self.incidents.append({"at": round(self.wall(), 3), "response": "reset", "by": "console"})
            self._save()

        self._call(self._on_reset)

    # ------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        now = self.clock()

        with self._lock:
            return {
                "ready": self.ready,
                "contained": self.contained,
                "contained_reason": self.contained_reason,
                "quarantined": self.quarantined,
                "quarantine_reason": self.quarantine_reason,
                "throttled": sorted(
                    p.describe()
                    for table in self._throttles.values()
                    for p, t in table.items()
                    if now < t.until
                ),
                "tracked": sum(len(table) for table in self._windows.values()),
            }
