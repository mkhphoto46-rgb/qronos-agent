"""
A running link server on loopback, for tests that need the whole stack.

Real sockets and a real TLS handshake rather than mocks, because most of what
these tests check belongs to OpenSSL and to the threading, not to the pure
decision functions — those are tested directly elsewhere and need nothing.

Everything binds port 0 and lives for one test.
"""

from __future__ import annotations

import threading
from typing import Callable

from core.link_audit import AuditLog
from core.link_capability import LinkOp, LinkScope
from core.link_client import LinkClient
from core.link_devices import DeviceRegistry
from core.link_pairing import PairingService
from core.link_server import Handler, LinkServer, LinkSettings


CLIENT_TIMEOUT = 5.0


def ok_handler(session, request):
    """A handler that succeeds and reports which operation reached it."""
    return {"seen": request.op}


class FakeServerClock:
    """A clock the server's own threads can read safely."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class ServerHarness:
    """A started server plus everything needed to talk to it."""

    def __init__(
        self,
        settings: LinkSettings | None = None,
        handlers: dict[LinkOp, Handler] | None = None,
        clock: Callable[[], float] | None = None,
        scope_resolver: Callable[[str], LinkScope | None] | None = None,
        with_pairing: bool = True,
    ) -> None:
        self.registry = DeviceRegistry(path=None)
        self.audit = AuditLog(path=None)
        self.pairing = (
            PairingService(self.registry, host="127.0.0.1", port=0)
            if with_pairing
            else None
        )

        base = settings if settings is not None else LinkSettings()

        self.server = LinkServer(
            registry=self.registry,
            pairing=self.pairing,
            audit=self.audit,
            handlers=handlers,
            settings=LinkSettings(
                host="127.0.0.1",
                port=0,
                allowed_scopes=base.allowed_scopes,
                max_connections=base.max_connections,
                handshake_timeout=base.handshake_timeout,
                idle_timeout=base.idle_timeout,
                failure_pause=base.failure_pause,
            ),
            clock=clock,
            scope_resolver=scope_resolver,
        )
        self.server.start()

        self.port = self.server.address[1]

        if self.pairing is not None:
            self.pairing.port = self.port

    def pair(self, name: str = "phone", **kwargs) -> LinkClient:
        """
        Open a pairing window and return a client holding its key.

        The client is not connected yet, so callers can use it with ``with``.
        """

        assert self.pairing is not None

        payload = self.pairing.open(name)

        return LinkClient.from_payload(
            payload,
            host="127.0.0.1",
            port=self.port,
            timeout=CLIENT_TIMEOUT,
            **kwargs,
        )

    def enrolled(self, name: str = "phone", remote: bool = False):
        """An already-active device, skipping the pairing window."""

        record = self.registry.create(name)
        self.registry.activate(record.device_id)

        if remote:
            self.registry.set_remote_enabled(record.device_id, True)

        return self.registry.get(record.device_id)

    def client_for(self, device_id: str, secret: bytes, **kwargs) -> LinkClient:
        return LinkClient(
            host="127.0.0.1",
            port=self.port,
            device_id=device_id,
            secret=secret,
            timeout=CLIENT_TIMEOUT,
            **kwargs,
        )

    def close(self) -> None:
        self.server.stop(timeout=2.0)
