"""
The real relay from ``relay/qronos_relay``, running on loopback in a thread.

With outer TLS from a throwaway CA generated for the test session
(``link_certs``), so the PC's dialler and the reference phone client verify it
exactly as they would verify a deployed relay — chain, name and all.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from core.link_tunnel import RelayEndpoint
from relay.qronos_relay.server import RelayLimits, RelayServer, server_context
from tests.fixtures.link_certs import RelayCertificates, relay_certificates


class RunningRelay:
    def __init__(self, directory: Path, limits: RelayLimits | None = None) -> None:
        self.certs: RelayCertificates = relay_certificates(directory)
        self.limits = limits if limits is not None else RelayLimits()
        self.server: RelayServer | None = None
        self.port = 0

        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        if not self._ready.wait(10):
            raise RuntimeError("the relay did not start")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def boot() -> None:
            self.server = RelayServer(
                server_context(str(self.certs.cert_file), str(self.certs.key_file)),
                self.limits,
            )
            _, self.port = await self.server.start("127.0.0.1", 0)
            self._ready.set()

        self._loop.run_until_complete(boot())
        self._loop.run_forever()

    @property
    def endpoint(self) -> RelayEndpoint:
        """How a PC or phone configured for this relay would name it."""

        return RelayEndpoint(
            host="127.0.0.1",
            port=self.port,
            server_name=self.certs.server_name,
            ca_file=str(self.certs.ca_file),
        )

    def stats(self) -> dict[str, int]:
        assert self.server is not None

        future = asyncio.run_coroutine_threadsafe(self._stats(), self._loop)

        return future.result(5)

    async def _stats(self) -> dict[str, int]:
        assert self.server is not None
        return self.server.stats()

    def parked_routes(self) -> dict[bytes, int]:
        """For the shared rig: live parked hosts per route."""

        future = asyncio.run_coroutine_threadsafe(self._parked_routes(), self._loop)

        return future.result(5)

    async def _parked_routes(self) -> dict[bytes, int]:
        assert self.server is not None

        return {
            route: sum(1 for entry in queue if not entry.joined.done())
            for route, queue in self.server._parked.items()
            if queue
        }

    def parked_count(self) -> int:
        return sum(self.parked_routes().values())

    def close(self) -> None:
        if self._loop.is_closed():
            return

        if self.server is not None:
            asyncio.run_coroutine_threadsafe(self.server.close(), self._loop).result(10)

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()
