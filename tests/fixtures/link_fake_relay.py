"""
A disposable relay on loopback, for testing the PC and phone sides of Layer 2.

It speaks the relay wire format from ``core/link_relay_wire.py`` over plain TCP
— no outer TLS, which the real relay's own tests cover — and it is hostile on
request, because the whole security argument of Layer 2 is about what a relay
can and cannot do:

    ``tap``      keeps a copy of every byte the phone sent through a splice, so
                 a test can replay a recorded session later.
    ``tamper``   rewrites bytes in flight from the phone to the PC.
    ``misroute_to``  joins every phone to the host parked on this route,
                 whatever route the phone asked for.

Threads and plain sockets: each direction of a splice is one thread, which is
safe on plain sockets. Everything binds port 0 and lives for one test.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable

from core.link_relay_wire import (
    HELLO_BYTES,
    RelayRole,
    RelayStatus,
    RelayWireError,
    decode_hello,
)
from core.link_tunnel import RelayEndpoint


def _read_exactly(conn: socket.socket, count: int) -> bytes:
    data = b""

    while len(data) < count:
        try:
            chunk = conn.recv(count - len(data))
        except OSError:
            return data

        if not chunk:
            return data

        data += chunk

    return data


class _Parked:
    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.joined = threading.Event()
        self.abandoned = threading.Event()

        # Held for every status byte written to the host and for the moment of
        # joining, so a keepalive can never land after SPLICED and inside the
        # inner TLS stream.
        self.lock = threading.Lock()

    def claim(self) -> bool:
        with self.lock:
            if self.abandoned.is_set() or self.joined.is_set():
                return False

            self.joined.set()

            return True


class FakeRelay:
    """A relay that parks hosts, joins phones to them, and copies bytes."""

    def __init__(
        self,
        keepalive: float = 0.2,
        phone_wait: float = 2.0,
        max_parked_per_route: int = 2,
        tamper: Callable[[bytes], bytes] | None = None,
        tap: bool = False,
        park_limit: float | None = None,
    ) -> None:
        self.keepalive = keepalive
        self.phone_wait = phone_wait
        self.max_parked_per_route = max_parked_per_route
        self.tamper = tamper
        self.tap = tap
        self.misroute_to: bytes | None = None
        self.park_limit = park_limit
        self.expired_parks = 0

        self.tapped: list[bytearray] = []
        self.host_hellos: list[bytes] = []
        self.phone_hellos: list[bytes] = []

        self._parked: dict[bytes, list[_Parked]] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._connections: list[socket.socket] = []

        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- endpoint

    @property
    def endpoint(self) -> RelayEndpoint:
        return RelayEndpoint(host="127.0.0.1", port=self.port)

    @staticmethod
    def connector(endpoint: RelayEndpoint, timeout: float) -> socket.socket:
        """Plain TCP in place of the outer TLS connection."""
        return socket.create_connection((endpoint.host, endpoint.port), timeout=timeout)

    def parked_routes(self) -> dict[bytes, int]:
        with self._lock:
            return {
                route: sum(
                    1 for p in parked
                    if not p.joined.is_set() and not p.abandoned.is_set()
                )
                for route, parked in self._parked.items()
                if parked
            }

    def parked_count(self) -> int:
        return sum(self.parked_routes().values())

    # --------------------------------------------------------------- serving

    def _accept(self) -> None:
        while not self._stopping.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return

            self._connections.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        raw = _read_exactly(conn, HELLO_BYTES)

        try:
            hello = decode_hello(raw)
        except RelayWireError:
            self._send(conn, RelayStatus.BAD_HELLO)
            conn.close()

            return

        if hello.role is RelayRole.HOST:
            self.host_hellos.append(raw)
            self._park(conn, hello.route_id)
        else:
            self.phone_hellos.append(raw)
            self._join(conn, hello.route_id)

    def _send(self, conn: socket.socket, status: RelayStatus) -> bool:
        try:
            conn.sendall(bytes([status.value]))
            return True
        except OSError:
            return False

    def _park(self, conn: socket.socket, route: bytes) -> None:
        parked = _Parked(conn)

        with self._lock:
            entries = [p for p in self._parked.get(route, []) if not p.abandoned.is_set()]

            if len(entries) >= self.max_parked_per_route:
                self._send(conn, RelayStatus.ROUTE_FULL)
                conn.close()

                return

            entries.append(parked)
            self._parked[route] = entries

        self._send(conn, RelayStatus.PARKED)
        parked_at = time.monotonic()

        # Keepalives until joined. The phone's thread owns the splice.
        while not parked.joined.wait(self.keepalive):
            with parked.lock:
                if parked.joined.is_set():
                    return

                if (
                    self.park_limit is not None
                    and time.monotonic() - parked_at >= self.park_limit
                ):
                    parked.abandoned.set()
                    self.expired_parks += 1
                    self._send(conn, RelayStatus.PARK_EXPIRED)
                    conn.close()

                    return

                if self._stopping.is_set() or not self._send(conn, RelayStatus.KEEPALIVE):
                    parked.abandoned.set()

                    return

    def _join(self, phone: socket.socket, route: bytes) -> None:
        if self.misroute_to is not None:
            route = self.misroute_to

        deadline = time.monotonic() + self.phone_wait
        host: _Parked | None = None

        while host is None and time.monotonic() < deadline:
            with self._lock:
                entries = self._parked.get(route, [])

                while entries:
                    candidate = entries.pop(0)

                    if candidate.claim():
                        host = candidate
                        break

            if host is None:
                time.sleep(0.01)

        if host is None:
            self._send(phone, RelayStatus.NO_HOST)
            phone.close()

            return

        self._send(host.conn, RelayStatus.SPLICED)
        self._send(phone, RelayStatus.SPLICED)

        record = bytearray()

        if self.tap:
            self.tapped.append(record)

        upstream = threading.Thread(
            target=self._pipe,
            args=(phone, host.conn, self.tamper, record if self.tap else None),
            daemon=True,
        )
        upstream.start()
        self._pipe(host.conn, phone, None, None)
        upstream.join(timeout=5)

    @staticmethod
    def _pipe(
        source: socket.socket,
        sink: socket.socket,
        tamper: Callable[[bytes], bytes] | None,
        record: bytearray | None,
    ) -> None:
        while True:
            try:
                data = source.recv(65536)
            except OSError:
                data = b""

            if not data:
                for end in (sink, source):
                    try:
                        end.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

                return

            if record is not None:
                record.extend(data)

            if tamper is not None:
                data = tamper(data)

            try:
                sink.sendall(data)
            except OSError:
                return

    # ------------------------------------------------------------- replaying

    def replay_to_parked_host(self, route: bytes, recorded: bytes, wait: float = 2.0) -> bytes:
        """
        Act as a phone that only has a recording: join a parked host and send
        it, byte for byte, what a real phone sent in an earlier session.

        Returns whatever the PC sent back before closing.
        """

        deadline = time.monotonic() + wait
        host: _Parked | None = None

        while host is None and time.monotonic() < deadline:
            with self._lock:
                entries = self._parked.get(route, [])

                while entries:
                    candidate = entries.pop(0)

                    if candidate.claim():
                        host = candidate
                        break

            if host is None:
                time.sleep(0.01)

        if host is None:
            raise AssertionError("no host was parked to replay into")

        self._send(host.conn, RelayStatus.SPLICED)

        try:
            host.conn.sendall(recorded)
        except OSError:
            pass

        host.conn.settimeout(3.0)
        received = b""

        while True:
            try:
                chunk = host.conn.recv(65536)
            except OSError:
                break

            if not chunk:
                break

            received += chunk

        return received

    # ------------------------------------------------------------- teardown

    def close(self) -> None:
        self._stopping.set()

        try:
            self._listener.close()
        except OSError:
            pass

        for conn in self._connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

            try:
                conn.close()
            except OSError:
                pass
