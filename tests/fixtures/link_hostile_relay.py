"""
A relay that misbehaves on purpose, on loopback, for the dialler's abuse tests.

Plain TCP, one behaviour per instance, and a count of every connection the PC
makes to it - which is the number a hostile relay is trying to drive up.

    splice_hangup     answer every parking with SPLICED, send junk, hang up
                      (the pattern from review finding L3)
    expire_at_once    answer PARKED, then PARK_EXPIRED straight away
    keepalive_flood   answer PARKED, then stream KEEPALIVE bytes
    splice_and_hold   answer SPLICED and keep the connection open, silent,
                      so the PC's handshake waits for its timeout
"""

from __future__ import annotations

import socket
import threading
import time

from core.link_relay_wire import HELLO_BYTES, RelayStatus
from core.link_tunnel import RelayEndpoint


class HostileRelay:
    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.connections = 0
        self._held: list[socket.socket] = []
        self._stopping = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def endpoint(self) -> RelayEndpoint:
        return RelayEndpoint(host="127.0.0.1", port=self.port)

    @staticmethod
    def connector(endpoint: RelayEndpoint, timeout: float) -> socket.socket:
        return socket.create_connection((endpoint.host, endpoint.port), timeout=timeout)

    def parked_routes(self) -> dict[bytes, int]:
        return {}

    def _accept(self) -> None:
        while not self._stopping.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return

            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.recv(HELLO_BYTES)

            if self.behaviour == "splice_hangup":
                conn.sendall(bytes([RelayStatus.SPLICED.value]) + b"\x16\x03\x01junk")
                conn.close()
            elif self.behaviour == "expire_at_once":
                conn.sendall(bytes([RelayStatus.PARKED.value, RelayStatus.PARK_EXPIRED.value]))
                conn.close()
            elif self.behaviour == "keepalive_flood":
                conn.sendall(bytes([RelayStatus.PARKED.value]))

                while not self._stopping.is_set():
                    conn.sendall(bytes([RelayStatus.KEEPALIVE.value]) * 64)
                    time.sleep(0.001)
            elif self.behaviour == "splice_and_hold":
                conn.sendall(bytes([RelayStatus.SPLICED.value]))
                self._held.append(conn)
        except OSError:
            pass

    def close(self) -> None:
        self._stopping.set()

        try:
            self._listener.close()
        except OSError:
            pass

        for conn in self._held:
            try:
                conn.close()
            except OSError:
                pass
