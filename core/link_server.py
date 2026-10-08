"""
The link service: listens, authenticates, and dispatches.

One thread accepts connections, one thread per connection serves it. Threads
rather than asyncio because the codebase has no asyncio and the load is one to
three phones — the wrong choice for hundreds of clients, the right one here.

The order of checks on a new connection is deliberate, cheapest and most
revealing first:

    1. Is there room for another connection?
    2. Is this peer somewhere this layer accepts connections from?
    3. Has a handshake failed too recently?
    4. Does the peer hold a key for a device that may authenticate?
    5. If the device is still pending, is there an open pairing window?
    6. Is this scope enabled for this particular device?

Steps 1 to 3 happen before any TLS work, so a peer that is not going to be
served costs almost nothing. Step 4 is the handshake itself: revocation is
enforced there, by the key failing to resolve, so a revoked phone never reaches
a session.

Two things this module deliberately does not do.

It does not rate-limit per address as an authentication control. The key is 256
bits, so repeated attempts are a way to waste CPU, not a way to guess a key, and
a token bucket sold as key protection would defend against nothing that matters.
The controls are sized for CPU: a connection cap, a pause after a failure, and an
idle timeout.

The pause is nonetheless kept *per peer*, and that is an availability decision
rather than an authentication one. A single global pause was measured letting any
machine that can reach the port lock the owner out of pairing their own phone:
one failed handshake every half second held the pause permanently in the future,
and five of five legitimate pairing attempts failed until the attacker stopped.
Scoping the pause to the address that earned it costs the same CPU protection and
removes that. The map of paused peers is capped, so an attacker cycling addresses
cannot grow it without bound.

It does not report internal errors to the phone. A traceback or an exception
message can carry a file path or a fragment of the user's data, so a handler
that fails returns a fixed code and the detail stays in the log on the machine.

Layer 2 sessions arrive through ``serve_relayed`` rather than the listener: the
relay dialler in ``core/link_tunnel.py`` hands over an outbound connection the
relay has joined to a phone, and the same handshake runs inside it, end to end.
Such a session is REMOTE_TUNNEL by construction — the scope comes from the path
the connection took, never from an address — and four rules apply to it on top
of everything above:

    It cannot pair. A pending device is refused over the relay whatever
    address the relay happens to have, so a hostile relay cannot enrol a phone.
    The device must be inside a remote opt-in, checked at the handshake and
    again before every request.
    It lives for a bounded time and has a request budget.
    Every request but ``ping`` carries a command id, so a retry after a dropped
    connection is answered rather than run twice (``core/link_commands.py``).

Revocation reaches open sessions as well as new handshakes, in both layers:
``revoke`` closes a device's live sessions at once, and every request re-reads
the device's record first, so a revocation made behind the server's back takes
effect at the next request at the latest.
"""

from __future__ import annotations

from collections import OrderedDict

import selectors
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from core.link_audit import AuditEvent, AuditLog
from core.security_monitor import (
    Admission,
    Principal,
    Response as Verdict,
    SecurityMonitor,
    SignalKind,
)
from core.link_commands import (
    CommandLedger,
    CommandState,
    RequestBudget,
    command_fingerprint,
)
from core.link_capability import (
    AuthReason,
    Capability,
    LinkOp,
    LinkScope,
    authorise,
    scope_for_peer,
)
from core.link_devices import (
    DeviceRecord,
    DeviceRegistry,
    DeviceStatus,
    DeviceStoreBusy,
    DeviceStoreCorrupt,
    is_valid_device_id,
)
from core.link_pairing import DEFAULT_PORT, PairingRefusal, PairingService
from core.link_protocol import (
    Event,
    ProtocolError,
    Request,
    Response,
    encode_frame,
    parse_frame,
    read_frame,
)
from core.link_transport import (
    HANDSHAKE_TIMEOUT_SECONDS,
    IDLE_TIMEOUT_SECONDS,
    TlsStream,
    reader_for,
    require_psk_support,
    server_context,
)


# Bound on all interfaces by default. The defence against a connection from
# somewhere it should not come from is the peer check in ``_serve``, not the
# bind address: binding to one interface breaks on a machine with several and
# on any address change, and would give a false sense of protection either way.
DEFAULT_BIND_HOST = "0.0.0.0"

# A phone, a tablet, and headroom.
MAX_CONNECTIONS = 4

# Connections arriving through the relay have slots of their own. A relay is
# untrusted and can open connections as fast as the PC will take them; if the
# two paths shared one budget, a hostile relay could keep the phone on the
# sofa from connecting at all.
MAX_RELAY_CONNECTIONS = 4

# How long a failed handshake makes the next one from that peer wait.
FAILURE_PAUSE_SECONDS = 1.0

# How many recently-failed peers are remembered. Bounded so an attacker cycling
# source addresses cannot grow the map; the oldest entry is dropped when full,
# which at worst forgives a peer early and costs one more handshake attempt.
MAX_PAUSED_PEERS = 256

# How long the accept loop waits before doing its housekeeping — closing an
# expired pairing window that nothing ever connected to. Stopping does not wait
# for this: ``stop`` writes to a wake socket that interrupts the wait at once.
HOUSEKEEPING_SECONDS = 1.0

# A remote session is short-lived: after this long it is closed and the phone
# reconnects, which runs a fresh handshake with fresh ephemeral keys and
# re-checks the device against the registry from scratch.
REMOTE_SESSION_LIFETIME_SECONDS = 30 * 60.0

# The request budget of one remote session: a burst, then one request every
# two seconds. Generous for a person; small for a stolen phone in a loop.
REMOTE_BURST = 10.0
REMOTE_REFILL_PER_SECOND = 0.5


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


class RejectReason(Enum):
    """Why a connection was turned away before or during authentication."""

    TOO_MANY_CONNECTIONS = "too_many_connections"
    SCOPE_NOT_ALLOWED = "scope_not_allowed"
    TOO_SOON_AFTER_FAILURE = "too_soon_after_failure"
    BAD_KEY_OR_UNKNOWN_DEVICE = "bad_key_or_unknown_device"
    BAD_IDENTITY = "bad_identity"
    REMOTE_NOT_ENABLED = "remote_not_enabled"
    UNEXPECTED_FRAME = "unexpected_frame"
    PAIRING_OVER_RELAY = "pairing_over_relay"

    # Security Monitor verdicts, enforced here.
    THROTTLED = "throttled"
    REMOTE_NOT_ADMITTED = "remote_not_admitted"
    LINK_QUARANTINED = "link_quarantined"


class SessionRule(Enum):
    """Why the PC ended a session or refused a request on session grounds."""

    REVOKED = "revoked"
    REMOTE_WITHDRAWN = "remote_withdrawn"
    LIFETIME_REACHED = "lifetime_reached"
    SERVER_STOPPING = "server_stopping"
    RATE_LIMITED = "rate_limited"
    COMMAND_ID_REQUIRED = "command_id_required"
    COMMAND_CONFLICT = "command_conflict"
    COMMAND_IN_PROGRESS = "command_in_progress"
    COMMAND_ALREADY_DONE = "command_already_done"
    DUPLICATE_ANSWERED = "duplicate_answered"
    THROTTLED = "throttled"
    CONTAINED = "contained"
    QUARANTINED = "quarantined"


class LinkOperationError(Exception):
    """
    A handler refused, in a way the phone should be told about.

    Anything else a handler raises becomes a generic internal error, because an
    arbitrary exception message is not safe to send.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class LinkSettings:
    """How the listener behaves."""

    host: str = DEFAULT_BIND_HOST
    port: int = DEFAULT_PORT

    # Layer 1. Adding LinkScope.REMOTE_TUNNEL here is most of what turns Layer
    # 2 on, and the per-device ``remote_enabled`` flag still has to agree.
    allowed_scopes: frozenset[LinkScope] = frozenset(
        {LinkScope.LOCAL_NETWORK}
    )

    max_connections: int = MAX_CONNECTIONS
    max_relay_connections: int = MAX_RELAY_CONNECTIONS
    handshake_timeout: float = HANDSHAKE_TIMEOUT_SECONDS
    idle_timeout: float = IDLE_TIMEOUT_SECONDS
    failure_pause: float = FAILURE_PAUSE_SECONDS

    remote_session_lifetime: float = REMOTE_SESSION_LIFETIME_SECONDS
    remote_burst: float = REMOTE_BURST
    remote_refill_per_second: float = REMOTE_REFILL_PER_SECOND


@dataclass
class LinkSession:
    """One authenticated connection."""

    device: DeviceRecord
    scope: LinkScope
    peer: str
    # A TLS socket for a direct connection, a ``TlsStream`` for a relayed one.
    # The session uses only the methods the two have in common.
    connection: ssl.SSLSocket | TlsStream
    started_at: float
    budget: RequestBudget | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    ended: SessionRule | None = None

    @property
    def grants(self) -> frozenset[Capability] | None:
        return self.device.grants

    @property
    def relayed(self) -> bool:
        return isinstance(self.connection, TlsStream)

    def end(self, rule: SessionRule) -> None:
        """
        Close the session from outside its own thread.

        The transport is closed, not only shut down, because only closing wakes
        the session thread out of its read. Measured on Windows 11 with Python
        3.13: ``shutdown(SHUT_RDWR)`` from another thread leaves a blocked
        ``recv`` waiting, with or without a timeout, until the peer reacts;
        ``close`` wakes it at once. A revoked phone that never reacts must not
        keep its session thread until the idle timeout. CPython's ``_ssl``
        reports a socket closed under a pending read as an error rather than
        reading a stale descriptor, so this is safe for the TLS socket too.
        """

        if self.ended is None:
            self.ended = rule

        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass

        abort = getattr(self.connection, "abort", None)

        try:
            if abort is not None:
                abort()
            else:
                self.connection.close()
        except (OSError, ValueError):
            pass

    def send(self, frame: Request | Response | Event) -> None:
        """
        Write one frame.

        Locked because an event pushed from a handler's own thread must not
        interleave with a response being written on the session thread. Half of
        one frame inside another would desynchronise the stream for good.
        """

        payload = encode_frame(frame)

        with self._lock:
            self.connection.sendall(payload)

    def emit(self, topic: str, **data: Any) -> None:
        """Push progress to the phone. Never raises into the caller."""

        try:
            self.send(Event(topic=topic, data=data))
        except (OSError, ssl.SSLError, ProtocolError):
            # The phone has gone. The handler should finish its work rather
            # than unwinding because a progress line could not be delivered.
            return

    def describe(self) -> str:
        return f"{self.device.name} ({self.device.device_id}) from {self.peer}"


Handler = Callable[[LinkSession, Request], Any]


class LinkServer:
    """
    The PC side of the device link.

    Nothing starts on its own. The desktop app constructs this and calls
    ``start`` when the user turns the link on, which is why there is no module
    level instance and no import side effect.
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        pairing: PairingService | None = None,
        audit: AuditLog | None = None,
        handlers: dict[LinkOp, Handler] | None = None,
        settings: LinkSettings | None = None,
        clock: Clock | None = None,
        scope_resolver: Callable[[str], LinkScope | None] | None = None,
        ledger: CommandLedger | None = None,
        monitor: SecurityMonitor | None = None,
    ) -> None:
        self.registry = registry
        self.pairing = pairing
        self.audit = audit if audit is not None else AuditLog(path=None)
        self.handlers: dict[LinkOp, Handler] = dict(handlers or {})
        self.settings = settings if settings is not None else LinkSettings()
        self.clock: Clock = clock if clock is not None else time.time

        # Injected so the remote-tunnel path can be exercised without an
        # internet-facing socket. Layer 2 does not replace it; it adds
        # LinkScope.REMOTE_TUNNEL to ``allowed_scopes`` and lets the real
        # classifier return it.
        self.scope_resolver: Callable[[str], LinkScope | None] = (
            scope_resolver if scope_resolver is not None else scope_for_peer
        )

        self._listener: socket.socket | None = None
        self._wake_reader: socket.socket | None = None
        self._wake_writer: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._stopping = threading.Event()

        self._state_lock = threading.Lock()
        self._active = 0
        self._active_relayed = 0

        # peer address -> the time it may try again. Ordered, so the oldest
        # entry is the one evicted when the map is full.
        self._paused_peers: OrderedDict[str, float] = OrderedDict()

        self._sessions_served = 0

        # Shared by every session, so a retry that arrives on a new connection
        # meets the record its first attempt left on the old one.
        self.ledger = ledger if ledger is not None else CommandLedger()

        # device id -> its open sessions, so revocation can reach them.
        self._live: dict[str, set[int]] = {}
        self._sessions: dict[int, LinkSession] = {}

        # The Security Monitor (core/security_monitor.py). Remote access does
        # not run without one; see start(). The server enforces its own part
        # of every verdict, so no caller can forget to.
        self.monitor = monitor

        if monitor is not None:
            monitor.on_contain(self._contain)
            monitor.on_quarantine(self._quarantine)

    # ---------------------------------------------------------------- registry

    def handle(self, op: LinkOp, handler: Handler) -> None:
        """Register a handler for one operation."""
        self.handlers[op] = handler

    # ------------------------------------------------------------ revocation

    def revoke(self, device_id: str) -> DeviceRecord:
        """
        Revoke a device and end its open sessions now.

        The registry change stops future handshakes; ending the sessions stops
        the present ones, so a phone reported lost loses the PC immediately
        rather than at its next request. Its command ledger goes too.
        """

        record = self.registry.revoke(device_id)

        self.audit.record(AuditEvent.DEVICE_REVOKED, device_id=device_id)
        self.end_sessions(device_id, SessionRule.REVOKED)
        self.ledger.forget_device(device_id)

        return record

    def set_remote_access(
        self,
        device_id: str,
        enabled: bool,
        expires_at: float | None = None,
    ) -> DeviceRecord:
        """
        Change one device's remote opt-in, ending its remote sessions if off.

        Local sessions are left alone: withdrawing remote access is not a
        statement about the phone on the sofa.
        """

        record = self.registry.set_remote_enabled(device_id, enabled, expires_at)

        if not enabled:
            self.end_sessions(
                device_id, SessionRule.REMOTE_WITHDRAWN, remote_only=True
            )

        return record

    def end_sessions(
        self,
        device_id: str,
        rule: SessionRule,
        remote_only: bool = False,
    ) -> int:
        """End a device's open sessions. Returns how many were ended."""

        with self._state_lock:
            sessions = [
                self._sessions[key]
                for key in self._live.get(device_id, ())
                if key in self._sessions
            ]

        ended = 0

        for session in sessions:
            if remote_only and session.scope is not LinkScope.REMOTE_TUNNEL:
                continue

            session.end(rule)
            ended += 1

        return ended

    # ------------------------------------------------------- security monitor

    def remote_admission(self) -> Admission:
        """Remote access runs only with a monitor that admits it."""

        if self.monitor is None:
            return Admission(False, "security_monitor_missing")

        return self.monitor.remote_admission()

    def _signal(self, kind: SignalKind, principal: Principal) -> Verdict:
        """Report one signal; enforce a device throttle on the spot."""

        if self.monitor is None:
            return Verdict.ALLOW

        decision = self.monitor.observe(kind, principal)

        if decision.response is Verdict.THROTTLE and principal.ident and principal.scope:
            self._end_device_sessions(principal.ident, principal.scope, SessionRule.THROTTLED)

        return decision.response

    def _admitted(self, principal: Principal) -> bool:
        return self.monitor is None or self.monitor.admit(principal)

    def _end_device_sessions(self, device_id: str, scope: str, rule: SessionRule) -> None:
        with self._state_lock:
            sessions = [
                self._sessions[key]
                for key in self._live.get(device_id, ())
                if key in self._sessions
            ]

        for session in sessions:
            if session.scope.value == scope:
                session.end(rule)

    def _contain(self, reason: str) -> None:
        """Containment: every remote session ends now; LAN sessions carry on."""

        with self._state_lock:
            sessions = list(self._sessions.values())

        for session in sessions:
            if session.scope is LinkScope.REMOTE_TUNNEL:
                session.end(SessionRule.CONTAINED)

    def _quarantine(self, reason: str) -> None:
        """Quarantine: every session ends; new connections are refused."""

        with self._state_lock:
            sessions = list(self._sessions.values())

        for session in sessions:
            session.end(SessionRule.QUARANTINED)

    def live_sessions(self, device_id: str) -> int:
        with self._state_lock:
            return len(self._live.get(device_id, ()))

    def _register(self, session: LinkSession) -> None:
        with self._state_lock:
            key = id(session)
            self._sessions[key] = session
            self._live.setdefault(session.device.device_id, set()).add(key)

    def _unregister(self, session: LinkSession) -> None:
        with self._state_lock:
            key = id(session)
            self._sessions.pop(key, None)
            keys = self._live.get(session.device.device_id)

            if keys is not None:
                keys.discard(key)

                if not keys:
                    del self._live[session.device.device_id]

    # --------------------------------------------------------------- lifecycle

    @property
    def address(self) -> tuple[str, int]:
        """The address actually bound, which matters when port is 0."""

        if self._listener is None:
            return (self.settings.host, self.settings.port)

        host, port = self._listener.getsockname()[:2]

        return (str(host), int(port))

    @property
    def sessions_served(self) -> int:
        return self._sessions_served

    def start(self) -> None:
        """
        Bind and begin accepting.

        The PSK check comes first so an unsupported build fails here, with a
        message naming the fallback, rather than as an unexplained handshake
        error once a phone tries to connect.
        """

        require_psk_support()

        if self._listener is not None:
            raise RuntimeError("the link server is already running")

        if LinkScope.REMOTE_TUNNEL in self.settings.allowed_scopes and self.monitor is None:
            # Not bypassable: without a monitor there is no remote access,
            # whoever constructs the server.
            raise RuntimeError(
                "remote access needs a Security Monitor; this server has none"
            )

        if self.monitor is not None and not self.monitor.link_admission().allowed:
            raise RuntimeError("the device link is quarantined; reset it at the computer")

        self._stopping.clear()

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            listener.bind((self.settings.host, self.settings.port))
            listener.listen(self.settings.max_connections)
        except OSError:
            listener.close()
            raise

        # A socket pair used only to interrupt the accept loop. Closing a
        # listening socket from another thread does not reliably wake a
        # blocked accept, so shutdown gets an explicit signal rather than
        # relying on a poll interval to notice.
        self._wake_reader, self._wake_writer = socket.socketpair()
        self._listener = listener

        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="qronos-link-accept",
            daemon=True,
        )
        self._accept_thread.start()

        self.audit.record(AuditEvent.LINK_STARTED)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop accepting and wait for in-flight sessions to finish."""

        self._stopping.set()

        # Wake the accept loop before closing anything under it.
        if self._wake_writer is not None:
            try:
                self._wake_writer.send(b"\x00")
            except OSError:
                pass

        # The accept thread is joined *before* the listener is closed. It
        # exits on its own via the wake socket, and closing a socket that a
        # selector is still watching leaves the selector holding a dead file
        # descriptor — which surfaced in CI as the accept thread dying with
        # "ValueError: Invalid file descriptor: -1" during shutdown.
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=timeout)
            self._accept_thread = None

        listener, self._listener = self._listener, None

        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

        # Open sessions are woken rather than waited out: one sitting in its
        # idle read would otherwise hold ``stop`` for the whole idle timeout.
        with self._state_lock:
            open_sessions = list(self._sessions.values())

        for session in open_sessions:
            session.end(SessionRule.SERVER_STOPPING)

        for worker in tuple(self._workers):
            worker.join(timeout=timeout)

        self._workers.clear()

        for end in (self._wake_reader, self._wake_writer):
            if end is not None:
                try:
                    end.close()
                except OSError:
                    pass

        self._wake_reader = self._wake_writer = None

        self.audit.record(AuditEvent.LINK_STOPPED)

    def __enter__(self) -> LinkServer:
        self.start()

        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------ accept loop

    def _accept_loop(self) -> None:
        listener = self._listener
        wake = self._wake_reader

        if listener is None or wake is None:  # pragma: no cover - guarded
            return

        selector = selectors.DefaultSelector()

        try:
            selector.register(listener, selectors.EVENT_READ)
            selector.register(wake, selectors.EVENT_READ)
        except (OSError, ValueError):  # pragma: no cover - stopped mid-start
            selector.close()

            return

        try:
            self._select_loop(selector, listener, wake)
        finally:
            selector.close()

    def _select_loop(
        self,
        selector: selectors.BaseSelector,
        listener: socket.socket,
        wake: socket.socket,
    ) -> None:
        while not self._stopping.is_set():
            # An abandoned pairing window is closed here, so it expires even if
            # nothing ever connects to it.
            if self.pairing is not None:
                self.pairing.expire_if_due()

            try:
                ready = selector.select(timeout=HOUSEKEEPING_SECONDS)
            except (OSError, ValueError):
                # ValueError, not just OSError: a selector whose socket has
                # been closed underneath it reports a file descriptor of -1
                # rather than an operating-system error.
                return

            if self._stopping.is_set():
                return

            if any(key.fileobj is wake for key, _ in ready):
                return

            if not ready:
                # Timed out; go round again and do the housekeeping.
                continue

            try:
                raw, address = listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except (OSError, ValueError):
                return

            peer = str(address[0])

            worker = threading.Thread(
                target=self._serve,
                args=(raw, peer),
                name=f"qronos-link-{peer}",
                daemon=True,
            )

            self._workers = [t for t in self._workers if t.is_alive()]
            self._workers.append(worker)
            worker.start()

    # ----------------------------------------------------------- relayed client

    def serve_relayed(
        self,
        carrier: socket.socket,
        peer: str,
        on_outcome: Callable[[bool], None] | None = None,
    ) -> None:
        """
        Serve one connection the relay has joined to a phone. Returns at once.

        ``carrier`` is the PC's outer connection to the relay, already spliced;
        the inner handshake runs inside it. ``peer`` is the relay's address,
        which is all the PC can know: the phone's own address is the relay's
        business, and that is part of what the relay is trusted not to need.

        ``on_outcome`` is told, exactly once, whether the splice became an
        authenticated session. The dialler needs that: a relay can say
        "joined" whenever it likes, so a splice is only a success once the
        phone on the other end has proved it holds a key.
        """

        outcome = _Outcome(on_outcome)

        if self._stopping.is_set() or self._listener is None:
            self._shutdown(carrier)
            outcome.report(False)

            return

        worker = threading.Thread(
            target=self._serve,
            args=(carrier, peer, True, outcome),
            name="qronos-link-relayed",
            daemon=True,
        )

        with self._state_lock:
            self._workers = [t for t in self._workers if t.is_alive()]
            self._workers.append(worker)

        worker.start()

    # -------------------------------------------------------------- one client

    def _serve(
        self,
        raw: socket.socket,
        peer: str,
        relayed: bool = False,
        outcome: _Outcome | None = None,
    ) -> None:
        outcome = outcome if outcome is not None else _Outcome(None)

        try:
            self._serve_counted(raw, peer, relayed, outcome)
        finally:
            # Anything that did not reach an authenticated session is a
            # failure, whichever way it ended.
            outcome.report(False)

    def _serve_counted(
        self,
        raw: socket.socket,
        peer: str,
        relayed: bool,
        outcome: _Outcome,
    ) -> None:
        if not self._claim_slot(relayed):
            self.audit.record(
                AuditEvent.CONNECTION_REJECTED,
                peer=peer,
                scope=LinkScope.REMOTE_TUNNEL if relayed else None,
                reason=RejectReason.TOO_MANY_CONNECTIONS,
            )
            self._shutdown(raw)

            return

        try:
            self._serve_claimed(raw, peer, relayed, outcome)
        finally:
            self._release_slot(relayed)
            self._shutdown(raw)

    def _serve_claimed(
        self,
        raw: socket.socket,
        peer: str,
        relayed: bool = False,
        outcome: _Outcome | None = None,
    ) -> None:
        # A relayed connection is remote because of the path it took, not
        # because of any address. The default resolver, ``scope_for_peer``,
        # never returns the remote scope for any address, so in production
        # only the relay path produces it and the listener stays Layer 1.
        scope = LinkScope.REMOTE_TUNNEL if relayed else self.scope_resolver(peer)

        if scope is None or scope not in self.settings.allowed_scopes:
            # Refused before any TLS work. In Layer 1 this is every connection
            # that did not come from the local network.
            self.audit.record(
                AuditEvent.SCOPE_REFUSED,
                peer=peer,
                scope=scope,
                reason=RejectReason.SCOPE_NOT_ALLOWED,
            )

            return

        if self.monitor is not None and not self.monitor.link_admission().allowed:
            self.audit.record(
                AuditEvent.CONNECTION_REJECTED,
                peer=peer,
                scope=scope,
                reason=RejectReason.LINK_QUARANTINED,
            )

            return

        # Who this connection answers to before it has proved a key. Anything
        # the relay delivers is the relay's, never a LAN address's - the relay
        # shares one address with every phone it carries.
        principal = Principal.relay() if relayed else Principal.lan(peer)

        # Every remote-scoped connection passes the WAN gate, whatever path
        # produced the scope - not only those the dialler hands over.
        if (relayed or scope is LinkScope.REMOTE_TUNNEL) and not self.remote_admission().allowed:
            self.audit.record(
                AuditEvent.SCOPE_REFUSED,
                peer=peer,
                scope=scope,
                reason=RejectReason.REMOTE_NOT_ADMITTED,
            )

            return

        if not self._admitted(principal):
            self.audit.record(
                AuditEvent.CONNECTION_REJECTED,
                peer=peer,
                scope=scope,
                reason=RejectReason.THROTTLED,
            )

            return

        # The relay's address is kept apart from LAN addresses here too, so a
        # failure through the relay never pauses a phone on the sofa.
        pause_key = f"relay {peer}" if relayed else peer

        if not self._handshake_permitted(pause_key):
            self.audit.record(
                AuditEvent.CONNECTION_REJECTED,
                peer=peer,
                reason=RejectReason.TOO_SOON_AFTER_FAILURE,
            )

            return

        raw.settimeout(self.settings.handshake_timeout)

        context, seen = server_context(self.registry.secret_for)

        try:
            if relayed:
                tls: ssl.SSLSocket | TlsStream = TlsStream.server(context, raw)
            else:
                tls = context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError, TimeoutError):
            self._penalise_failure(pause_key)
            self.audit.record(
                AuditEvent.HANDSHAKE_REFUSED,
                device_id=seen.identity or "",
                peer=peer,
                scope=scope,
                reason=RejectReason.BAD_KEY_OR_UNKNOWN_DEVICE,
            )
            # Held against the source, never the identity it claimed: that
            # identity was not proved, and holding it against the device would
            # let anyone get a real phone refused by failing in its name.
            self._signal(SignalKind.HANDSHAKE_FAILED, principal)

            return

        with tls:
            session = self._authorise_session(tls, seen.identity or "", peer, scope)

            if session is None:
                return

            if outcome is not None:
                outcome.report(True)

            self.audit.record(
                AuditEvent.HANDSHAKE_OK,
                device_id=session.device.device_id,
                peer=peer,
                scope=scope,
            )
            self.audit.record(
                AuditEvent.SESSION_OPENED,
                device_id=session.device.device_id,
                peer=peer,
                scope=scope,
            )

            self._sessions_served += 1
            self._register(session)

            try:
                self._session_loop(session)
            finally:
                self._unregister(session)

                if relayed and getattr(tls, "integrity_failed", False):
                    # The inner TLS rejected a record inside an authenticated
                    # relayed session. TCP and the outer TLS guard the wire up
                    # to the relay, so the bytes were altered past it.
                    self._signal(SignalKind.TAMPERING, Principal.relay())

                if session.ended is not None:
                    self.audit.record(
                        AuditEvent.SESSION_ENDED,
                        device_id=session.device.device_id,
                        peer=peer,
                        scope=scope,
                        reason=session.ended,
                    )

                self.audit.record(
                    AuditEvent.SESSION_CLOSED,
                    device_id=session.device.device_id,
                    peer=peer,
                    scope=scope,
                )

    def _authorise_session(
        self,
        tls: ssl.SSLSocket | TlsStream,
        identity: str,
        peer: str,
        scope: LinkScope,
    ) -> LinkSession | None:
        """
        Turn a completed handshake into a session, or refuse it.

        The handshake already proved the peer holds the key. What is left is
        whether the device is in a state to be served: a pending device needs
        an open pairing window, and a remote connection needs that device to
        have been opted in.
        """

        if not is_valid_device_id(identity):
            # Should be unreachable: an identity of the wrong shape has no key
            # and so cannot have completed a handshake. Refused rather than
            # trusted, in case that ever stops being true.
            self.audit.record(
                AuditEvent.HANDSHAKE_REFUSED,
                device_id=identity,
                peer=peer,
                scope=scope,
                reason=RejectReason.BAD_IDENTITY,
            )

            return None

        try:
            record = self.registry.get(identity)
        except DeviceStoreCorrupt:
            record = None
            self._signal(SignalKind.REGISTRY_UNREADABLE, Principal.system())
        except DeviceStoreBusy:
            record = None

        if record is None or not record.can_authenticate:
            self.audit.record(
                AuditEvent.HANDSHAKE_REFUSED,
                device_id=identity,
                peer=peer,
                scope=scope,
                reason=RejectReason.BAD_KEY_OR_UNKNOWN_DEVICE,
            )

            return None

        if record.status is DeviceStatus.PENDING:
            if scope is not LinkScope.LOCAL_NETWORK:
                # Pairing is local-only, and over the relay that is decided by
                # the path, not by the relay's address — a relay on the same
                # network would otherwise look local. This is the rule that
                # makes an untrusted relay acceptable: it cannot enrol a phone.
                self.audit.record(
                    AuditEvent.PAIRING_REFUSED,
                    device_id=record.device_id,
                    peer=peer,
                    scope=scope,
                    reason=RejectReason.PAIRING_OVER_RELAY,
                )
                self._signal(SignalKind.PAIRING_OVER_RELAY, Principal.relay())

                return None

            try:
                record = self._complete_pairing(record, peer, scope)
            except (DeviceStoreCorrupt, DeviceStoreBusy):
                record = None

            if record is None:
                return None
        elif scope is LinkScope.REMOTE_TUNNEL and not record.remote_allowed(
            self.clock()
        ):
            # Layer 2 is opt-in for one named device at a time, for a period,
            # never a global switch. Checked before the record is touched, so a
            # refused remote attempt does not show up as "last seen".
            self.audit.record(
                AuditEvent.SCOPE_REFUSED,
                device_id=record.device_id,
                peer=peer,
                scope=scope,
                reason=RejectReason.REMOTE_NOT_ENABLED,
            )
            self._signal(SignalKind.REQUEST_REFUSED, Principal.device(record.device_id, scope.value))

            return None
        else:
            try:
                record = self.registry.touch(record.device_id)
            except (DeviceStoreCorrupt, DeviceStoreBusy):
                # The record was read a moment ago but cannot be written now.
                # Not a state to start a session in.
                self.audit.record(
                    AuditEvent.HANDSHAKE_REFUSED,
                    device_id=identity,
                    peer=peer,
                    scope=scope,
                    reason=RejectReason.BAD_KEY_OR_UNKNOWN_DEVICE,
                )

                return None

        if not self._admitted(Principal.device(record.device_id, scope.value)):
            self.audit.record(
                AuditEvent.CONNECTION_REJECTED,
                device_id=record.device_id,
                peer=peer,
                scope=scope,
                reason=RejectReason.THROTTLED,
            )

            return None

        budget = (
            RequestBudget(
                self.settings.remote_burst,
                self.settings.remote_refill_per_second,
                clock=self.clock,
            )
            if scope is LinkScope.REMOTE_TUNNEL
            else None
        )

        return LinkSession(
            device=record,
            scope=scope,
            peer=peer,
            connection=tls,
            started_at=self.clock(),
            budget=budget,
        )

    def _complete_pairing(
        self,
        record: DeviceRecord,
        peer: str,
        scope: LinkScope,
    ) -> DeviceRecord | None:
        if self.pairing is None:
            self.audit.record(
                AuditEvent.PAIRING_REFUSED,
                device_id=record.device_id,
                peer=peer,
                reason=PairingRefusal.NO_WINDOW,
            )

            return None

        outcome = self.pairing.complete(record.device_id, peer)

        if not outcome.accepted or outcome.record is None:
            self.audit.record(
                AuditEvent.PAIRING_REFUSED,
                device_id=record.device_id,
                peer=peer,
                reason=outcome.refusal,
            )

            return None

        self.audit.record(
            AuditEvent.PAIRING_COMPLETED,
            device_id=record.device_id,
            peer=peer,
            scope=scope,
        )

        return outcome.record

    # ------------------------------------------------------------ the session

    def _session_timeout(self, session: LinkSession) -> float:
        """The read timeout now: the idle limit, or less if lifetime is near."""

        if session.scope is not LinkScope.REMOTE_TUNNEL:
            return self.settings.idle_timeout

        remaining = (
            session.started_at
            + self.settings.remote_session_lifetime
            - self.clock()
        )

        return max(0.001, min(self.settings.idle_timeout, remaining))

    def _still_valid(self, session: LinkSession) -> SessionRule | None:
        """
        Re-read the device before acting for it. ``None`` means carry on.

        This is what makes revocation, a withdrawn opt-in, an expired opt-in
        and a narrowed grant take effect inside an open session, including when
        the change was made by something that did not go through ``revoke``.
        The fresh record replaces the session's copy, so grants narrowed a
        moment ago apply to the very next request.
        """

        try:
            record = self.registry.get(session.device.device_id)
        except DeviceStoreCorrupt:
            # The registry is shared with other processes. If it cannot be
            # read now, nobody can say this device is still trusted, so the
            # session ends rather than carrying on from a stale copy - and
            # the monitor contains remote access until the user looks.
            self._signal(SignalKind.REGISTRY_UNREADABLE, Principal.system())

            return SessionRule.REVOKED
        except DeviceStoreBusy:
            return SessionRule.REVOKED

        if record is None or record.status is not DeviceStatus.ACTIVE:
            return SessionRule.REVOKED

        if self.monitor is not None:
            if not self.monitor.link_admission().allowed:
                return SessionRule.QUARANTINED

            if session.scope is LinkScope.REMOTE_TUNNEL and not self.monitor.remote_admission().allowed:
                return SessionRule.CONTAINED

            if self.monitor.throttled(Principal.device(record.device_id, session.scope.value)):
                return SessionRule.THROTTLED

        if session.scope is LinkScope.REMOTE_TUNNEL:
            now = self.clock()

            if not record.remote_allowed(now):
                return SessionRule.REMOTE_WITHDRAWN

            if now - session.started_at >= self.settings.remote_session_lifetime:
                return SessionRule.LIFETIME_REACHED

        session.device = record

        return None

    def _session_loop(self, session: LinkSession) -> None:
        read = reader_for(session.connection)

        while not self._stopping.is_set() and session.ended is None:
            session.connection.settimeout(self._session_timeout(session))

            try:
                payload = read_frame(read)
            except ProtocolError as exc:
                self.audit.record(
                    AuditEvent.PROTOCOL_FAULT,
                    device_id=session.device.device_id,
                    peer=session.peer,
                    reason=exc.fault,
                )
                self._signal(SignalKind.PROTOCOL_FAULT, _device_principal(session))

                return

            if payload is None:
                # A remote session that read nothing because its lifetime ran
                # out is ended by the PC, and is recorded as such.
                if (
                    session.ended is None
                    and session.scope is LinkScope.REMOTE_TUNNEL
                    and self.clock() - session.started_at
                    >= self.settings.remote_session_lifetime
                ):
                    session.ended = SessionRule.LIFETIME_REACHED

                return

            try:
                frame = parse_frame(payload)
            except ProtocolError as exc:
                self.audit.record(
                    AuditEvent.PROTOCOL_FAULT,
                    device_id=session.device.device_id,
                    peer=session.peer,
                    reason=exc.fault,
                )
                self._signal(SignalKind.PROTOCOL_FAULT, _device_principal(session))

                return

            if not isinstance(frame, Request):
                # The phone does not send responses or events. A peer that does
                # is confused or hostile; either way there is nothing to do
                # with it.
                self.audit.record(
                    AuditEvent.PROTOCOL_FAULT,
                    device_id=session.device.device_id,
                    peer=session.peer,
                    reason=RejectReason.UNEXPECTED_FRAME,
                )

                return

            rule = self._still_valid(session)

            if rule is not None:
                # Ended without an answer. The phone learns it from the closed
                # connection; a revoked phone is owed no explanation, and one
                # whose session merely aged out reconnects and retries.
                session.ended = rule

                return

            response = self._respond(session, frame)

            try:
                session.send(response)
            except (OSError, ssl.SSLError, ProtocolError):
                return

    def _respond(self, session: LinkSession, request: Request) -> Response:
        """
        The session-level rules, then the request itself.

        In order: the remote request budget, then the command id. Both come
        before authorisation, and neither can widen what authorisation allows:
        they only ever refuse, or answer from what an earlier, authorised run
        of the same command already produced.
        """

        remote = session.scope is LinkScope.REMOTE_TUNNEL
        is_ping = request.op == LinkOp.PING.value

        if session.budget is not None and not session.budget.take():
            return self._session_refusal(
                session, request, SessionRule.RATE_LIMITED,
                "Too many requests. Wait a moment and try again.",
            )

        if request.command_id is None:
            if remote and not is_ping:
                return self._session_refusal(
                    session, request, SessionRule.COMMAND_ID_REQUIRED,
                    "Requests over the internet must carry a command id.",
                )

            return self._dispatch(session, request)

        device_id = session.device.device_id
        decision = self.ledger.begin(
            device_id,
            request.command_id,
            command_fingerprint(request.op, request.params),
        )

        if decision.state is CommandState.CONFLICT:
            return self._session_refusal(
                session, request, SessionRule.COMMAND_CONFLICT,
                "That command id was already used for a different request.",
            )

        if decision.state is CommandState.IN_PROGRESS:
            return self._session_refusal(
                session, request, SessionRule.COMMAND_IN_PROGRESS,
                "That command is still running. Ask again shortly.",
            )

        if decision.state is CommandState.DONE:
            op = _known_op(request.op)

            if decision.outcome is None:
                return self._session_refusal(
                    session, request, SessionRule.COMMAND_ALREADY_DONE,
                    "That command was already done; its answer is no longer kept.",
                )

            self.audit.record(
                AuditEvent.REQUEST_DUPLICATE,
                device_id=device_id,
                peer=session.peer,
                scope=session.scope,
                op=op,
                reason=SessionRule.DUPLICATE_ANSWERED,
            )

            return _response_from_outcome(request.id, decision.outcome)

        response: Response | None = None

        try:
            response = self._dispatch(session, request)

            return response
        finally:
            if response is None:
                response = Response.failure(
                    request.id,
                    "internal_error",
                    "Something went wrong on the computer.",
                )

            self.ledger.finish(
                device_id, request.command_id, _outcome_of(response)
            )

    def _session_refusal(
        self,
        session: LinkSession,
        request: Request,
        rule: SessionRule,
        message: str,
    ) -> Response:
        self.audit.record(
            AuditEvent.REQUEST_REFUSED,
            device_id=session.device.device_id,
            peer=session.peer,
            scope=session.scope,
            op=_known_op(request.op),
            reason=rule,
        )
        signal = _SIGNAL_FOR_RULE.get(rule)

        if signal is not None:
            self._signal(signal, _device_principal(session))

        return Response.failure(request.id, rule.value, message)

    def _dispatch(self, session: LinkSession, request: Request) -> Response:
        """Authorise one request, then run it."""

        decision = authorise(request.op, session.scope, session.grants)

        if decision.needs_approval:
            self.audit.record(
                AuditEvent.REQUEST_NEEDS_APPROVAL,
                device_id=session.device.device_id,
                peer=session.peer,
                scope=session.scope,
                op=decision.op,
                reason=decision.reason,
            )

            return Response.failure(
                request.id,
                AuthReason.NEEDS_APPROVAL.value,
                "This needs approval on the computer first.",
            )

        if not decision.allowed:
            self.audit.record(
                AuditEvent.REQUEST_REFUSED,
                device_id=session.device.device_id,
                peer=session.peer,
                scope=session.scope,
                op=decision.op,
                reason=decision.reason,
            )
            self._signal(SignalKind.REQUEST_REFUSED, _device_principal(session))

            return Response.failure(
                request.id,
                decision.reason.value,
                _refusal_message(decision.reason),
            )

        assert decision.op is not None  # guaranteed once allowed

        handler = self.handlers.get(decision.op)

        if handler is None:
            return Response.failure(
                request.id,
                "not_implemented",
                f"{request.op} is not available on this build.",
            )

        self.audit.record(
            AuditEvent.REQUEST_ALLOWED,
            device_id=session.device.device_id,
            peer=session.peer,
            scope=session.scope,
            op=decision.op,
        )

        try:
            result = handler(session, request)
        except LinkOperationError as exc:
            return Response.failure(request.id, exc.code, exc.message)
        except Exception:
            # Deliberately generic. An exception message can carry a path or a
            # fragment of the user's data, and the phone does not need either.
            self.audit.record(
                AuditEvent.REQUEST_FAILED,
                device_id=session.device.device_id,
                peer=session.peer,
                scope=session.scope,
                op=decision.op,
            )

            return Response.failure(
                request.id,
                "internal_error",
                "Something went wrong on the computer.",
            )

        return Response.success(request.id, result)

    # ---------------------------------------------------------------- limits

    def _claim_slot(self, relayed: bool = False) -> bool:
        """Take a slot from the budget of the path the connection came by."""

        with self._state_lock:
            if relayed:
                if self._active_relayed >= self.settings.max_relay_connections:
                    return False

                self._active_relayed += 1
            else:
                if self._active >= self.settings.max_connections:
                    return False

                self._active += 1

            return True

    def _release_slot(self, relayed: bool = False) -> None:
        with self._state_lock:
            if relayed:
                self._active_relayed = max(0, self._active_relayed - 1)
            else:
                self._active = max(0, self._active - 1)

    def _handshake_permitted(self, peer: str) -> bool:
        """Whether this peer may attempt a handshake yet."""
        with self._state_lock:
            now = self.clock()
            until = self._paused_peers.get(peer)

            if until is None:
                return True

            if now >= until:
                # Served its pause. Forget it rather than letting the map
                # fill with peers that are no longer paused.
                del self._paused_peers[peer]

                return True

            return False

    def _penalise_failure(self, peer: str) -> None:
        """
        Make this peer wait before trying again.

        Per peer, not global. A global pause means one hostile machine on the
        network stops the owner pairing their own phone, which was measured
        rather than supposed.
        """
        with self._state_lock:
            self._paused_peers.pop(peer, None)
            self._paused_peers[peer] = (
                self.clock() + self.settings.failure_pause
            )

            while len(self._paused_peers) > MAX_PAUSED_PEERS:
                self._paused_peers.popitem(last=False)

    @staticmethod
    def _shutdown(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        try:
            connection.close()
        except OSError:
            pass


class _Outcome:
    """Tells a relayed connection's dialler once whether it authenticated."""

    def __init__(self, callback: Callable[[bool], None] | None) -> None:
        self._callback = callback
        self._lock = threading.Lock()
        self._reported = False

    def report(self, authenticated: bool) -> None:
        with self._lock:
            if self._reported:
                return

            self._reported = True

        if self._callback is not None:
            try:
                self._callback(authenticated)
            except Exception:
                pass


_REFUSAL_MESSAGES = {
    AuthReason.UNKNOWN_OP: "Qronos does not know that request.",
    AuthReason.CONSOLE_ONLY: "That can only be done at the computer.",
    AuthReason.OUT_OF_SCOPE: "That is not allowed over this connection.",
    AuthReason.NOT_GRANTED: "This device is not allowed to do that.",
    AuthReason.PERMISSION_DENIED: "That is not permitted.",
}


def _refusal_message(reason: AuthReason) -> str:
    return _REFUSAL_MESSAGES.get(reason, "Refused.")


# Which session-rule refusals count against a device. A command still running
# or already answered is a phone waiting or retrying correctly, never abuse.
_SIGNAL_FOR_RULE = {
    SessionRule.RATE_LIMITED: SignalKind.RATE_LIMITED,
    SessionRule.COMMAND_CONFLICT: SignalKind.COMMAND_CONFLICT,
    SessionRule.COMMAND_ID_REQUIRED: SignalKind.REQUEST_REFUSED,
}


def _device_principal(session: LinkSession) -> Principal:
    """An authenticated session's device, in the scope it is connected in."""
    return Principal.device(session.device.device_id, session.scope.value)


def _known_op(name: str) -> LinkOp | None:
    """The operation, if it is one; the audit log takes no free text."""

    try:
        return LinkOp(name)
    except ValueError:
        return None


def _outcome_of(response: Response) -> dict[str, Any]:
    """A response without its request id, as the ledger keeps it."""

    outcome = response.to_payload()
    outcome.pop("id", None)
    outcome.pop("kind", None)

    return outcome


def _response_from_outcome(request_id: int, outcome: dict[str, Any]) -> Response:
    """The kept answer, re-addressed to the request that asked again."""

    if outcome.get("ok"):
        return Response.success(request_id, outcome.get("result"))

    return Response.failure(
        request_id,
        str(outcome.get("code", "")),
        str(outcome.get("error", "")),
    )


def main() -> None:
    """Run a server on loopback and drive it with the reference client."""

    from core.link_client import LinkClient
    from core.link_handlers import default_handlers

    registry = DeviceRegistry(path=None)
    pairing = PairingService(registry, host="127.0.0.1", port=0)
    audit = AuditLog(path=None)

    server = LinkServer(
        registry=registry,
        pairing=pairing,
        audit=audit,
        handlers=default_handlers(),
        settings=LinkSettings(host="127.0.0.1", port=0),
    )

    with server:
        host, port = server.address
        pairing.port = port

        payload = pairing.open("demo phone")
        print("pairing URI is", len(payload.to_uri()), "characters")

        client = LinkClient.from_payload(payload, host="127.0.0.1", port=port)

        with client:
            print("ping   :", client.call("ping").result)
            print("status :", client.call("status").ok)
            print("run_app:", client.call("run_app").code)
            print("devices:", client.call("list_devices").code)
            print("bogus  :", client.call("nonsense").code)

    print()
    print(audit.describe())


if __name__ == "__main__":
    main()
