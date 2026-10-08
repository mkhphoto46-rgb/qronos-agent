"""
Layer 2: reaching the PC from outside the house, through a relay.

**The PC dials out. The phone dials in. A relay copies bytes between them.**

This module is the narrow network exception that sentence needs, and nothing
more. It opens outbound connections to one relay that the user configured, and
only to that relay. It does not fetch content, call an API or run inference;
what it carries is the Layer 1 link session, encrypted end to end with the
device's pairing key, which the relay cannot read.

Two connections are layered on every relayed session:

    outer   ordinary certificate TLS to the relay on an ordinary port. It
            looks like any HTTPS connection, it rides inside whatever VPN the
            phone already runs, and it hides the route from the network.
    inner   the Layer 1 session — TLS 1.3, external PSK with an ephemeral key
            exchange — between the phone and the PC, through the relay.

The PC side works like this:

    For every device that is active and inside a remote opt-in, one parker
    thread holds an outbound connection to the relay, filed under that
    device's route (``core/link_relay_wire.py``). When the phone arrives, the
    relay joins the two and says so; the parker hands the connection to the
    link server as a REMOTE_TUNNEL session and immediately parks again.

    Eligibility is re-read from the registry every few seconds. A device that
    is revoked, opted out or past its expiry loses its parker, so the relay
    stops having anything to join it to. That is a second, independent barrier
    behind the real one, which is that the device's key no longer resolves in
    the inner handshake.

Nothing starts by itself. There is no module-level instance and no default
relay address. The dialler refuses to start unless the server it feeds has the
remote scope switched on, which the desktop does only when
``CONFIG.security.remote_access_enabled`` is set.
"""

from __future__ import annotations

import ipaddress
import random
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from core.link_audit import AuditEvent
from core.link_capability import LinkScope
from core.link_devices import DeviceRecord
from core.link_relay_wire import (
    RelayHello,
    RelayStatus,
    decode_status,
    host_hello,
    phone_hello,
)
from core.link_server import LinkServer


# The port a relay listens on unless configured otherwise. 443 because that is
# the port an HTTPS connection uses, which is what the outer session looks like.
DEFAULT_RELAY_PORT = 443

CONNECT_TIMEOUT_SECONDS = 15.0

# How long a parked connection may stay silent. The relay sends a keepalive
# every 25 seconds while parked; missing two and a margin means it is gone.
PARKED_SILENCE_SECONDS = 60.0

# How often the set of devices to park for is re-read from the registry. This
# bounds how long a revoked or expired device keeps a parked connection.
REFRESH_SECONDS = 2.0

# More remote-enabled devices than this is not a household, and each one costs
# a thread and a connection held open on the relay.
MAX_REMOTE_ROUTES = 8

BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0

# A self-hosted relay's certificate authority, handed to the phone over the
# local link so it can verify the relay too. A CA certificate is a few
# kilobytes; anything far larger is not one.
MAX_CA_BYTES = 16 * 1024

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


class RelayConfigError(ValueError):
    """A relay endpoint that cannot be used."""


class RelayRefused(Exception):
    """The relay answered, and the answer was no."""

    def __init__(self, status: RelayStatus | None, raw: int | None = None) -> None:
        self.status = status
        self.raw = raw
        name = status.name if status is not None else f"unknown status {raw}"
        super().__init__(f"the relay refused: {name}")


class RelayFailure(Enum):
    """Why the dialler lost or could not reach the relay. For the audit log."""

    UNREACHABLE = "relay_unreachable"
    TLS_FAILED = "relay_tls_failed"
    REFUSED = "relay_refused"
    SILENT = "relay_silent"
    CLOSED = "relay_closed"


@dataclass(frozen=True)
class RelayEndpoint:
    """
    Where the relay is. A configuration value, never a hard-coded default.

    ``server_name`` is the name the relay's certificate is checked against,
    when it differs from ``host`` — a relay reached by address, for one.
    ``ca_file`` pins a self-hosted relay's own certificate authority; without
    it, the operating system's trust store decides, as it would for a browser.
    """

    host: str
    port: int = DEFAULT_RELAY_PORT
    server_name: str | None = None
    ca_file: str | None = None

    def __post_init__(self) -> None:
        host = self.host.strip().lower() if isinstance(self.host, str) else ""

        if not host or not _is_host(host):
            raise RelayConfigError(
                "The relay must be a host name or an IP address, with no "
                "scheme, path or port."
            )

        if not isinstance(self.port, int) or not 1 <= self.port <= 65_535:
            raise RelayConfigError("The relay port must be between 1 and 65535.")

        if self.server_name is not None and not _HOSTNAME.match(
            self.server_name.strip().lower()
        ):
            raise RelayConfigError("The relay's certificate name is not valid.")

        object.__setattr__(self, "host", host)

    @property
    def tls_name(self) -> str:
        return (self.server_name or self.host).strip().lower()

    def ca_pem(self) -> str | None:
        """The pinned CA, as text, bounded; ``None`` when the system decides."""

        if self.ca_file is None:
            return None

        data = Path(self.ca_file).read_bytes()

        if len(data) > MAX_CA_BYTES or b"-----BEGIN CERTIFICATE-----" not in data:
            raise RelayConfigError("The relay CA file is not a PEM certificate.")

        return data.decode("ascii")

    def describe_for_phone(self) -> dict[str, object]:
        """
        What the phone needs to reach this relay. Nothing in it is secret.

        Sent over the authenticated local link, so the relay address never has
        to be typed on the phone and never travels in the QR code.
        """

        info: dict[str, object] = {"host": self.host, "port": self.port}

        if self.server_name is not None:
            info["server_name"] = self.tls_name

        ca = self.ca_pem()

        if ca is not None:
            info["ca_pem"] = ca

        return info


def _is_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME.match(host))


def relay_context(endpoint: RelayEndpoint) -> ssl.SSLContext:
    """
    The outer TLS context: verify the relay like any HTTPS server.

    ``create_default_context`` checks the certificate chain and the host name.
    A pinned CA file replaces the system store rather than adding to it, so a
    self-hosted relay is trusted on its own terms and nothing else is.
    """

    context = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH,
        cafile=endpoint.ca_file,
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    return context


Connector = Callable[[RelayEndpoint, float], ssl.SSLSocket]


def connect_relay(
    endpoint: RelayEndpoint,
    timeout: float = CONNECT_TIMEOUT_SECONDS,
) -> ssl.SSLSocket:
    """Open the outer TLS connection to the relay. Raises on any failure."""

    raw = socket.create_connection((endpoint.host, endpoint.port), timeout=timeout)

    try:
        return relay_context(endpoint).wrap_socket(
            raw, server_hostname=endpoint.tls_name
        )
    except BaseException:
        raw.close()
        raise


def read_status(carrier: ssl.SSLSocket) -> RelayStatus:
    """
    One status byte from the relay.

    End of stream and unknown bytes both raise ``RelayRefused``; an unknown
    byte is a relay this build does not understand, and waiting on it would
    be guessing.
    """

    byte = carrier.recv(1)

    if not byte:
        raise RelayRefused(None)

    status = decode_status(byte[0])

    if status is None:
        raise RelayRefused(None, byte[0])

    return status


def dial_as_phone(
    endpoint: RelayEndpoint,
    secret: bytes,
    timeout: float = CONNECT_TIMEOUT_SECONDS,
    connector: Connector = connect_relay,
) -> ssl.SSLSocket:
    """
    The phone's half: reach the relay and be joined to the PC.

    Returns the outer connection once the relay has spliced it, ready for the
    inner handshake. This is the reference for the phone app.
    """

    carrier = connector(endpoint, timeout)

    try:
        carrier.sendall(phone_hello(secret).encode())
        status = read_status(carrier)

        if status is not RelayStatus.SPLICED:
            raise RelayRefused(status)

        return carrier
    except BaseException:
        _close_quietly(carrier)
        raise


# ------------------------------------------------------------------ backoff


class Backoff:
    """
    Exponential backoff with full jitter, for redialling the relay.

    Jitter so that a relay coming back after an outage is not met by every PC
    at the same second. Capped so a long outage still retries once a minute.
    """

    def __init__(
        self,
        initial: float = BACKOFF_INITIAL_SECONDS,
        maximum: float = BACKOFF_MAX_SECONDS,
        rng: random.Random | None = None,
    ) -> None:
        self.initial = initial
        self.maximum = maximum
        self._rng = rng if rng is not None else random.SystemRandom()
        self._attempt = 0

    def next(self) -> float:
        ceiling = min(self.maximum, self.initial * (2 ** self._attempt))
        self._attempt = min(self._attempt + 1, 32)

        return self._rng.uniform(ceiling / 2, ceiling)

    def reset(self) -> None:
        self._attempt = 0


# ------------------------------------------------------------------ dialler


@dataclass(frozen=True)
class TunnelSettings:
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS
    parked_silence: float = PARKED_SILENCE_SECONDS
    refresh: float = REFRESH_SECONDS
    max_routes: int = MAX_REMOTE_ROUTES
    backoff_initial: float = BACKOFF_INITIAL_SECONDS
    backoff_max: float = BACKOFF_MAX_SECONDS


class _Parker:
    """One device's standing connection to the relay."""

    def __init__(self, dialler: RelayDialler, device_id: str) -> None:
        self.dialler = dialler
        self.device_id = device_id
        self.stop = threading.Event()
        self._carrier: ssl.SSLSocket | None = None
        self._carrier_lock = threading.Lock()
        self.thread = threading.Thread(
            target=self._run,
            name=f"qronos-relay-{device_id}",
            daemon=True,
        )
        self.failing = False
        self.announced = False
        self.splices = 0

    def start(self) -> None:
        self.thread.start()

    def halt(self) -> None:
        """
        Stop parking. Closes the parked connection to wake the blocked read.

        Closed, not just shut down: on Windows a shutdown from another thread
        does not wake a ``recv`` that is waiting on the socket.
        """

        self.stop.set()

        with self._carrier_lock:
            carrier = self._carrier

        if carrier is not None:
            try:
                carrier.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

            _close_quietly(carrier)

    def _run(self) -> None:
        settings = self.dialler.settings
        backoff = Backoff(settings.backoff_initial, settings.backoff_max)

        while not self.stop.is_set() and not self.dialler.stopping:
            record = self.dialler.eligible_record(self.device_id)

            if record is None:
                return

            failure = self._park_once(record)

            if failure is None:
                # Spliced, and the session was handed over. Park again at once:
                # a phone that reconnects should find the PC waiting.
                backoff.reset()
                continue

            self._note_failure(failure)

            if self.stop.wait(backoff.next()):
                return

    def _park_once(self, record: DeviceRecord) -> RelayFailure | None:
        dialler = self.dialler
        hello = host_hello(record.secret)

        try:
            carrier = dialler.connector(dialler.endpoint, dialler.settings.connect_timeout)
        except ssl.SSLError:
            return RelayFailure.TLS_FAILED
        except OSError:
            return RelayFailure.UNREACHABLE

        with self._carrier_lock:
            self._carrier = carrier

        handed_over = False

        try:
            return self._wait_for_phone(carrier, hello, record)
        except (ssl.SSLError, OSError):
            return RelayFailure.CLOSED
        except RelayRefused as exc:
            if exc.status is RelayStatus.PARK_EXPIRED:
                # Not a failure: the relay bounds how long anything may park.
                return None

            if exc.status is None:
                return RelayFailure.CLOSED

            return RelayFailure.REFUSED
        finally:
            with self._carrier_lock:
                handed_over = self._carrier is None
                self._carrier = None

            if not handed_over:
                _close_quietly(carrier)

    def _wait_for_phone(
        self,
        carrier: ssl.SSLSocket,
        hello: RelayHello,
        record: DeviceRecord,
    ) -> RelayFailure | None:
        dialler = self.dialler
        carrier.settimeout(dialler.settings.parked_silence)
        carrier.sendall(hello.encode())

        while not self.stop.is_set():
            try:
                status = read_status(carrier)
            except (TimeoutError, socket.timeout):
                return RelayFailure.SILENT

            if status is RelayStatus.PARKED:
                # Recorded once, and again only after a failure, so routine
                # re-parking does not write a line every few minutes.
                if self.failing or not self.announced:
                    dialler.audit_relay(AuditEvent.RELAY_PARKED, record.device_id)

                self.failing = False
                self.announced = True
                continue

            if status is RelayStatus.KEEPALIVE:
                continue

            if status is RelayStatus.SPLICED:
                # Re-check at the moment of handing over: a device revoked a
                # millisecond ago does not get a session because its parker had
                # not yet been told. The server checks again after the inner
                # handshake, which is the check that cannot be raced.
                if dialler.eligible_record(record.device_id) is None:
                    return RelayFailure.CLOSED

                with self._carrier_lock:
                    self._carrier = None

                self.splices += 1
                dialler.hand_over(carrier)

                return None

            raise RelayRefused(status)

        return RelayFailure.CLOSED

    def _note_failure(self, failure: RelayFailure) -> None:
        # One line when the relay goes away, not one per retry. A relay that is
        # down for an hour should not fill the audit log.
        if not self.failing:
            self.failing = True
            self.dialler.audit_relay(
                AuditEvent.RELAY_FAILED, self.device_id, reason=failure
            )


class RelayDialler:
    """
    Keeps one parked relay connection per remote-enabled device.

    Constructed and started by the desktop when remote access is switched on;
    stopped when it is switched off. ``stop`` closes parked connections; sessions
    already handed to the server are the server's to end.
    """

    def __init__(
        self,
        server: LinkServer,
        endpoint: RelayEndpoint,
        settings: TunnelSettings | None = None,
        connector: Connector = connect_relay,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.server = server
        self.endpoint = endpoint
        self.settings = settings if settings is not None else TunnelSettings()
        self.connector = connector
        self.clock = clock if clock is not None else time.time

        self._stopping = threading.Event()
        self._parkers: dict[str, _Parker] = {}
        self._lock = threading.Lock()
        self._supervisor: threading.Thread | None = None

    # -------------------------------------------------------------- queries

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def parked_devices(self) -> frozenset[str]:
        with self._lock:
            return frozenset(
                device_id
                for device_id, parker in self._parkers.items()
                if parker.thread.is_alive()
            )

    def eligible_record(self, device_id: str) -> DeviceRecord | None:
        """The device's record if it may be served remotely right now."""

        record = self.server.registry.get(device_id)

        if record is None or not record.remote_allowed(self.clock()):
            return None

        return record

    def eligible_devices(self) -> tuple[str, ...]:
        now = self.clock()
        eligible = [
            record.device_id
            for record in self.server.registry.all()
            if record.remote_allowed(now)
        ]

        return tuple(eligible[: self.settings.max_routes])

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if LinkScope.REMOTE_TUNNEL not in self.server.settings.allowed_scopes:
            raise RuntimeError(
                "Remote access is not switched on for this link server; the "
                "relay dialler will not start without it."
            )

        if self._supervisor is not None:
            raise RuntimeError("the relay dialler is already running")

        self._stopping.clear()
        self._supervisor = threading.Thread(
            target=self._supervise,
            name="qronos-relay-supervisor",
            daemon=True,
        )
        self._supervisor.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()

        if self._supervisor is not None:
            self._supervisor.join(timeout=timeout)
            self._supervisor = None

        with self._lock:
            parkers = list(self._parkers.values())
            self._parkers.clear()

        for parker in parkers:
            parker.halt()

        for parker in parkers:
            parker.thread.join(timeout=timeout)

    def __enter__(self) -> RelayDialler:
        self.start()

        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def refresh(self) -> None:
        """
        Bring the parked set in line with the registry now.

        Called on a timer, and directly by the desktop after it revokes a
        device or changes an opt-in, so the change does not wait for the timer.
        """

        wanted = set(self.eligible_devices())

        with self._lock:
            stale = [
                parker
                for device_id, parker in self._parkers.items()
                if device_id not in wanted or not parker.thread.is_alive()
            ]

            for parker in stale:
                del self._parkers[parker.device_id]

            if self._stopping.is_set():
                fresh: list[_Parker] = []
            else:
                fresh = [
                    _Parker(self, device_id)
                    for device_id in wanted
                    if device_id not in self._parkers
                ]

                for parker in fresh:
                    self._parkers[parker.device_id] = parker

        for parker in stale:
            parker.halt()

        for parker in fresh:
            parker.start()

    def _supervise(self) -> None:
        while not self._stopping.is_set():
            self.refresh()

            if self._stopping.wait(self.settings.refresh):
                return

    # ----------------------------------------------------------- callbacks

    def hand_over(self, carrier: ssl.SSLSocket) -> None:
        """Give a spliced connection to the server as a remote session."""

        try:
            peer = str(carrier.getpeername()[0])
        except (OSError, IndexError):
            peer = ""

        self.server.serve_relayed(carrier, peer)

    def audit_relay(
        self,
        event: AuditEvent,
        device_id: str,
        reason: RelayFailure | None = None,
    ) -> None:
        self.server.audit.record(
            event,
            device_id=device_id,
            scope=LinkScope.REMOTE_TUNNEL,
            reason=reason,
        )


def _close_quietly(connection: socket.socket) -> None:
    try:
        connection.close()
    except OSError:
        pass
