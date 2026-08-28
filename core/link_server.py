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

It does not throttle per address. The key is 256 bits, so repeated attempts are
a way to waste CPU, not a way to guess a key. The controls are sized for that:
a connection cap, a pause after a failure, and an idle timeout. A per-address
token bucket would look thorough and defend against nothing that matters.

It does not report internal errors to the phone. A traceback or an exception
message can carry a file path or a fragment of the user's data, so a handler
that fails returns a fixed code and the detail stays in the log on the machine.
"""

from __future__ import annotations

import selectors
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from core.link_audit import AuditEvent, AuditLog
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

# How long a failed handshake makes the next one wait.
FAILURE_PAUSE_SECONDS = 1.0

# How long the accept loop waits before doing its housekeeping — closing an
# expired pairing window that nothing ever connected to. Stopping does not wait
# for this: ``stop`` writes to a wake socket that interrupts the wait at once.
HOUSEKEEPING_SECONDS = 1.0


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
    handshake_timeout: float = HANDSHAKE_TIMEOUT_SECONDS
    idle_timeout: float = IDLE_TIMEOUT_SECONDS
    failure_pause: float = FAILURE_PAUSE_SECONDS


@dataclass
class LinkSession:
    """One authenticated connection."""

    device: DeviceRecord
    scope: LinkScope
    peer: str
    connection: ssl.SSLSocket
    started_at: float
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def grants(self) -> frozenset[Capability] | None:
        return self.device.grants

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
        self._next_handshake_at = 0.0

        self._sessions_served = 0

    # ---------------------------------------------------------------- registry

    def handle(self, op: LinkOp, handler: Handler) -> None:
        """Register a handler for one operation."""
        self.handlers[op] = handler

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

    # -------------------------------------------------------------- one client

    def _serve(self, raw: socket.socket, peer: str) -> None:
        if not self._claim_slot():
            self.audit.record(
                AuditEvent.CONNECTION_REJECTED,
                peer=peer,
                reason=RejectReason.TOO_MANY_CONNECTIONS,
            )
            self._shutdown(raw)

            return

        try:
            self._serve_claimed(raw, peer)
        finally:
            self._release_slot()
            self._shutdown(raw)

    def _serve_claimed(self, raw: socket.socket, peer: str) -> None:
        scope = self.scope_resolver(peer)

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

        if not self._handshake_permitted():
            self.audit.record(
                AuditEvent.CONNECTION_REJECTED,
                peer=peer,
                reason=RejectReason.TOO_SOON_AFTER_FAILURE,
            )

            return

        raw.settimeout(self.settings.handshake_timeout)

        context, seen = server_context(self.registry.secret_for)

        try:
            tls = context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError, TimeoutError):
            self._penalise_failure()
            self.audit.record(
                AuditEvent.HANDSHAKE_REFUSED,
                device_id=seen.identity or "",
                peer=peer,
                scope=scope,
                reason=RejectReason.BAD_KEY_OR_UNKNOWN_DEVICE,
            )

            return

        with tls:
            session = self._authorise_session(tls, seen.identity or "", peer, scope)

            if session is None:
                return

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

            try:
                self._session_loop(session)
            finally:
                self.audit.record(
                    AuditEvent.SESSION_CLOSED,
                    device_id=session.device.device_id,
                    peer=peer,
                    scope=scope,
                )

    def _authorise_session(
        self,
        tls: ssl.SSLSocket,
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

        record = self.registry.get(identity)

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
            record = self._complete_pairing(record, peer, scope)

            if record is None:
                return None
        else:
            record = self.registry.touch(record.device_id)

        if scope is LinkScope.REMOTE_TUNNEL and not record.remote_enabled:
            # Layer 2 is opt-in for one named device at a time, never a global
            # switch. Unreachable in Layer 1 because the scope is refused
            # earlier; present so turning the scope on does not also turn every
            # paired device on.
            self.audit.record(
                AuditEvent.SCOPE_REFUSED,
                device_id=record.device_id,
                peer=peer,
                scope=scope,
                reason=RejectReason.REMOTE_NOT_ENABLED,
            )

            return None

        return LinkSession(
            device=record,
            scope=scope,
            peer=peer,
            connection=tls,
            started_at=self.clock(),
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

    def _session_loop(self, session: LinkSession) -> None:
        session.connection.settimeout(self.settings.idle_timeout)
        read = reader_for(session.connection)

        while not self._stopping.is_set():
            try:
                payload = read_frame(read)
            except ProtocolError as exc:
                self.audit.record(
                    AuditEvent.PROTOCOL_FAULT,
                    device_id=session.device.device_id,
                    peer=session.peer,
                    reason=exc.fault,
                )

                return

            if payload is None:
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

            response = self._dispatch(session, frame)

            try:
                session.send(response)
            except (OSError, ssl.SSLError, ProtocolError):
                return

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

    def _claim_slot(self) -> bool:
        with self._state_lock:
            if self._active >= self.settings.max_connections:
                return False

            self._active += 1

            return True

    def _release_slot(self) -> None:
        with self._state_lock:
            self._active = max(0, self._active - 1)

    def _handshake_permitted(self) -> bool:
        with self._state_lock:
            return self.clock() >= self._next_handshake_at

    def _penalise_failure(self) -> None:
        with self._state_lock:
            self._next_handshake_at = (
                self.clock() + self.settings.failure_pause
            )

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


_REFUSAL_MESSAGES = {
    AuthReason.UNKNOWN_OP: "Qronos does not know that request.",
    AuthReason.CONSOLE_ONLY: "That can only be done at the computer.",
    AuthReason.OUT_OF_SCOPE: "That is not allowed over this connection.",
    AuthReason.NOT_GRANTED: "This device is not allowed to do that.",
    AuthReason.PERMISSION_DENIED: "That is not permitted.",
}


def _refusal_message(reason: AuthReason) -> str:
    return _REFUSAL_MESSAGES.get(reason, "Refused.")


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
