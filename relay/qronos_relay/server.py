"""
The Qronos relay: park PCs, join phones to them, copy bytes.

The PC dials out and parks under a route; the phone dials in and asks for that
route; the relay joins the two and copies bytes until either side stops. That
is the whole job. Everything after the join is a TLS session between the phone
and the PC, keyed by a secret the relay never sees, so the relay cannot read,
alter or inject into it, and the code below never tries to parse it.

The relay is untrusted by design, which shapes what it does keep:

    No contents, ever. Bytes are copied, not inspected or stored.
    No route ids or addresses in its log. It counts: connections, parkings,
    joins, refusals by reason. That is enough to operate it and not enough to
    say who talked to whom, which is the one thing a relay operator should
    find hard to hand over.

Everything is bounded, because a relay on the internet is attacked by default:

    connections in total and per source address, routes, hosts parked per
    route, the time to send a hello, the time a phone waits for its PC, the
    time a PC may stay parked, idle and total time for a joined pair, and the
    bytes in flight (one chunk per direction, with back-pressure).

Python's standard library only, so it can run on any small Linux machine with
Python 3.11 or newer and nothing installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import time
from collections import Counter, deque
from dataclasses import dataclass, field

from .wire import (
    HELLO_BYTES,
    ROLE_HOST,
    BadHello,
    Status,
    parse_hello,
)


@dataclass(frozen=True)
class RelayLimits:
    max_connections: int = 1024
    max_per_address: int = 32
    max_routes: int = 4096
    max_parked_per_route: int = 2

    # Seconds.
    hello_timeout: float = 10.0
    phone_wait: float = 10.0
    keepalive: float = 25.0
    park_lifetime: float = 15 * 60.0
    pair_idle: float = 10 * 60.0
    pair_lifetime: float = 4 * 3600.0

    # Bytes read from one side before they are written to the other.
    chunk: int = 16 * 1024


@dataclass
class _Parked:
    """A PC waiting for its phone."""

    route: bytes
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    joined: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    done: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class RelayServer:
    """
    One relay. ``start`` binds; ``close`` stops accepting and ends every pair.

    ``ssl_context`` is the relay's own certificate TLS — the outer session. It
    is required: a relay without it would show every route to the network.
    """

    def __init__(
        self,
        ssl_context: ssl.SSLContext,
        limits: RelayLimits | None = None,
    ) -> None:
        if not isinstance(ssl_context, ssl.SSLContext):
            raise TypeError("the relay needs a TLS context; it does not run in the clear")

        self.ssl_context = ssl_context
        self.limits = limits if limits is not None else RelayLimits()
        self.counts: Counter[str] = Counter()

        self._server: asyncio.base_events.Server | None = None
        self._parked: dict[bytes, deque[_Parked]] = {}
        # Phones waiting for a PC to park, per route. Bounded by the
        # connection limit, since each entry is a phone holding a connection.
        self._route_waiters: dict[bytes, asyncio.Event] = {}
        self._waiting: Counter[bytes] = Counter()
        self._per_address: Counter[str] = Counter()
        self._connections = 0
        self._writers: set[asyncio.StreamWriter] = set()

    # ------------------------------------------------------------- lifecycle

    async def start(self, host: str, port: int) -> tuple[str, int]:
        self._server = await asyncio.start_server(
            self._handle,
            host,
            port,
            ssl=self.ssl_context,
            ssl_handshake_timeout=self.limits.hello_timeout,
            limit=self.limits.chunk * 2,
        )
        address = self._server.sockets[0].getsockname()

        return str(address[0]), int(address[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()

        for writer in list(self._writers):
            writer.close()

        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)

    def stats(self) -> dict[str, int]:
        """Counts only. No route, no address."""

        parked = sum(
            1
            for queue in self._parked.values()
            for entry in queue
            if not entry.joined.done()
        )

        return {
            "connections": self._connections,
            "parked": parked,
            "routes": sum(1 for queue in self._parked.values() if queue),
            **dict(self.counts),
        }

    # ------------------------------------------------------------ connections

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        address = str(peer[0]) if peer else ""

        if (
            self._connections >= self.limits.max_connections
            or self._per_address[address] >= self.limits.max_per_address
        ):
            self.counts["refused_capacity"] += 1
            await self._finish(writer, Status.OVER_CAPACITY)

            return

        self._connections += 1
        self._per_address[address] += 1
        self._writers.add(writer)

        try:
            await self._serve(reader, writer)
        except (ConnectionError, OSError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            self._connections -= 1
            self._per_address[address] -= 1

            if self._per_address[address] <= 0:
                del self._per_address[address]

            self._writers.discard(writer)
            await self._close(writer)

    async def _serve(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(
                reader.readexactly(HELLO_BYTES), self.limits.hello_timeout
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            self.counts["refused_no_hello"] += 1

            return

        try:
            role, route = parse_hello(raw)
        except BadHello:
            self.counts["refused_bad_hello"] += 1
            await self._send(writer, Status.BAD_HELLO)

            return

        if role == ROLE_HOST:
            await self._park(route, reader, writer)
        else:
            await self._join(route, reader, writer)

    # ----------------------------------------------------------------- hosts

    async def _park(
        self,
        route: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        queue = self._parked.get(route)

        if queue is None:
            if len(self._parked) >= self.limits.max_routes:
                self.counts["refused_routes"] += 1
                await self._send(writer, Status.OVER_CAPACITY)

                return

            queue = self._parked[route] = deque()

        live = [entry for entry in queue if not entry.joined.done()]

        if len(live) >= self.limits.max_parked_per_route:
            self.counts["refused_route_full"] += 1
            await self._send(writer, Status.ROUTE_FULL)
            self._forget_if_empty(route)

            return

        parked = _Parked(route, reader, writer)
        queue.append(parked)
        self.counts["parkings"] += 1

        waiter = self._route_waiters.get(route)

        if waiter is not None:
            waiter.set()

        try:
            await self._send(writer, Status.PARKED)

            if await self._wait_for_phone(parked):
                await self._splice(parked)
        finally:
            if not parked.joined.done():
                parked.joined.cancel()

            if not parked.done.done():
                parked.done.set_result(None)

            with contextlib.suppress(ValueError):
                queue.remove(parked)

            self._forget_if_empty(route)

    async def _wait_for_phone(self, parked: _Parked) -> bool:
        """
        Keep a parked PC company until its phone arrives. True when joined.

        A parked PC sends nothing, so anything it does send — or the end of its
        stream — means it has gone. The watch on its reader is cancelled before
        the splice takes the reader over, so nothing it sends is lost.
        """

        deadline = time.monotonic() + self.limits.park_lifetime
        watch = asyncio.ensure_future(parked.reader.read(1))

        try:
            while True:
                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    self.counts["park_expired"] += 1
                    await self._send(parked.writer, Status.PARK_EXPIRED)

                    return False

                done, _ = await asyncio.wait(
                    {parked.joined, watch},
                    timeout=min(self.limits.keepalive, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if watch in done:
                    # Bytes before SPLICED, or a hang-up. Either way it is not
                    # a PC waiting for a phone any more.
                    return False

                if parked.joined in done:
                    return not parked.joined.cancelled()

                await self._send(parked.writer, Status.KEEPALIVE)
        finally:
            if not watch.done():
                watch.cancel()

                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watch

    # ---------------------------------------------------------------- phones

    async def _join(
        self,
        route: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        parked = await self._claim(route)

        if parked is None:
            self.counts["refused_no_host"] += 1
            await self._send(writer, Status.NO_HOST)

            return

        self.counts["joins"] += 1
        parked.joined.set_result((reader, writer))

        # The PC's coroutine runs the splice; this one keeps the phone's
        # connection open until it is over.
        await parked.done

    async def _claim(self, route: bytes) -> _Parked | None:
        """Take a parked PC for this route, waiting a little for one to park."""

        deadline = time.monotonic() + self.limits.phone_wait

        while True:
            queue = self._parked.get(route)

            while queue:
                candidate = queue.popleft()

                if not candidate.joined.done():
                    self._forget_if_empty(route)

                    return candidate

            self._forget_if_empty(route)
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                return None

            # Park-and-wait: a PC that has just served one session parks again
            # within a moment, and a phone reconnecting should find it.
            waiter = self._route_waiters.setdefault(route, asyncio.Event())
            waiter.clear()
            self._waiting[route] += 1

            try:
                await asyncio.wait_for(waiter.wait(), remaining)
            except asyncio.TimeoutError:
                return None
            finally:
                self._waiting[route] -= 1

                if self._waiting[route] <= 0:
                    del self._waiting[route]
                    self._route_waiters.pop(route, None)

    # ---------------------------------------------------------------- splice

    async def _splice(self, parked: _Parked) -> None:
        phone_reader, phone_writer = parked.joined.result()

        # The PC first: it must see SPLICED before any byte of the phone's.
        await self._send(parked.writer, Status.SPLICED)
        await self._send(phone_writer, Status.SPLICED)

        activity = [time.monotonic()]
        started = activity[0]

        upstream = asyncio.ensure_future(
            self._copy(phone_reader, parked.writer, activity)
        )
        downstream = asyncio.ensure_future(
            self._copy(parked.reader, phone_writer, activity)
        )

        try:
            while True:
                done, _ = await asyncio.wait(
                    {upstream, downstream},
                    timeout=min(30.0, self.limits.pair_idle / 4),
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if done:
                    return

                now = time.monotonic()

                if now - activity[0] > self.limits.pair_idle:
                    self.counts["pair_idle"] += 1

                    return

                if now - started > self.limits.pair_lifetime:
                    self.counts["pair_lifetime"] += 1

                    return
        finally:
            for task in (upstream, downstream):
                task.cancel()

            for task in (upstream, downstream):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

            await self._close(phone_writer)

    async def _copy(
        self,
        source: asyncio.StreamReader,
        sink: asyncio.StreamWriter,
        activity: list[float],
    ) -> None:
        while True:
            data = await source.read(self.limits.chunk)

            if not data:
                return

            activity[0] = time.monotonic()
            sink.write(data)

            # Back-pressure: never hold more than a chunk in flight per
            # direction, however fast one side sends.
            await sink.drain()

    # --------------------------------------------------------------- helpers

    def _forget_if_empty(self, route: bytes) -> None:
        queue = self._parked.get(route)

        if queue is not None and not queue:
            del self._parked[route]

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, status: Status) -> None:
        writer.write(status.byte)
        await writer.drain()

    async def _finish(self, writer: asyncio.StreamWriter, status: Status) -> None:
        with contextlib.suppress(Exception):
            await self._send(writer, status)

        await self._close(writer)

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        if writer.is_closing():
            return

        writer.close()

        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=2)


def server_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    """The outer TLS: the relay's certificate, TLS 1.2 or newer."""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)

    return context
