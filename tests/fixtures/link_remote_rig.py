"""
The whole Layer 2 path on loopback: link server, relay dialler, a relay, and
the reference client dialling in through it.

Fresh keys every time, an in-memory registry and audit log, port 0 for
everything, and one clock shared by the registry, the server and the dialler,
so expiry can be tested by moving time rather than by waiting for it.

The relay is ``FakeRelay`` (plain TCP, hostile on request) unless a test passes
another; the real relay's own tests run this same rig over outer TLS.
"""

from __future__ import annotations

from typing import Any, Callable

from core.link_audit import AuditLog
from core.link_capability import LinkOp, LinkScope
from core.link_client import LinkClient
from core.link_devices import DeviceRecord, DeviceRegistry
from core.link_pairing import PairingService
from core.link_server import Handler, LinkServer, LinkSettings
from core.security_monitor import SecurityMonitor
from core.link_relay_wire import route_id_for_secret
from core.link_tunnel import RelayDialler, RelayEndpoint, TunnelSettings
from tests.fixtures.link_fake_relay import FakeRelay
from tests.fixtures.link_harness import CLIENT_TIMEOUT, FakeServerClock, wait_for


# Fast timings for loopback tests. The fake relay keeps-alive every 0.2 s and
# expires parkings after fractions of a second when asked to, so the
# misbehaviour guards are scaled to match; their production values have their
# own tests in test_link_relay_abuse.py.
FAST_TUNNEL = TunnelSettings(
    connect_timeout=3.0,
    parked_silence=3.0,
    refresh=0.05,
    backoff_initial=0.05,
    backoff_max=0.2,
    min_park_seconds=0.1,
    keepalive_per_second=20.0,
)


class RemoteRig:
    def __init__(
        self,
        handlers: dict[LinkOp, Handler] | None = None,
        settings: LinkSettings | None = None,
        relay: Any = None,
        endpoint: RelayEndpoint | None = None,
        connector: Callable[..., Any] | None = None,
        tunnel: TunnelSettings = FAST_TUNNEL,
        monitor: SecurityMonitor | None = None,
        **relay_options: Any,
    ) -> None:
        self.clock = FakeServerClock()
        self.registry = DeviceRegistry(path=None, clock=self.clock)
        self.audit = AuditLog(path=None, clock=self.clock)
        self.monitor = monitor if monitor is not None else SecurityMonitor()
        self.pairing = PairingService(self.registry, host="127.0.0.1", port=0, clock=self.clock)

        base = settings if settings is not None else LinkSettings()

        self.server = LinkServer(
            registry=self.registry,
            pairing=self.pairing,
            audit=self.audit,
            handlers=handlers,
            settings=LinkSettings(
                host="127.0.0.1",
                port=0,
                allowed_scopes=frozenset({LinkScope.LOCAL_NETWORK, LinkScope.REMOTE_TUNNEL}),
                max_connections=base.max_connections,
                max_relay_connections=base.max_relay_connections,
                handshake_timeout=base.handshake_timeout,
                idle_timeout=base.idle_timeout,
                failure_pause=0.0,
                remote_session_lifetime=base.remote_session_lifetime,
                remote_burst=base.remote_burst,
                remote_refill_per_second=base.remote_refill_per_second,
            ),
            clock=self.clock,
            monitor=self.monitor,
        )
        self.server.start()
        self.port = self.server.address[1]
        self.pairing.port = self.port

        self.relay = relay if relay is not None else FakeRelay(**relay_options)
        self.endpoint = endpoint if endpoint is not None else self.relay.endpoint
        self.connector = connector if connector is not None else FakeRelay.connector

        self.dialler = RelayDialler(
            self.server,
            self.endpoint,
            settings=tunnel,
            connector=self.connector,
            clock=self.clock,
        )
        self.dialler.start()

    # ------------------------------------------------------------- devices

    def remote_device(self, name: str = "phone", seconds: float = 3600.0) -> DeviceRecord:
        """An active device inside a remote opt-in, already parked for."""

        record = self.registry.create(name)
        self.registry.activate(record.device_id)
        record = self.registry.set_remote_enabled(
            record.device_id, True, expires_at=self.clock() + seconds
        )
        self.dialler.refresh()

        assert self.wait_parked(record), "the dialler never parked for the device"

        return record

    def wait_parked(self, record: DeviceRecord, timeout: float = 3.0) -> bool:
        route = route_id_for_secret(record.secret)

        return wait_for(lambda: self.relay.parked_routes().get(route, 0) >= 1, timeout=timeout)

    def wait_unparked(self, record: DeviceRecord, timeout: float = 3.0) -> bool:
        route = route_id_for_secret(record.secret)

        return wait_for(lambda: self.relay.parked_routes().get(route, 0) == 0, timeout=timeout)

    # -------------------------------------------------------------- phones

    def phone(self, record: DeviceRecord, secret: bytes | None = None, **kwargs: Any) -> LinkClient:
        """The reference client, set to go through the relay. Not connected."""

        return LinkClient(
            host="unused-for-relay",
            port=1,
            device_id=record.device_id,
            secret=record.secret if secret is None else secret,
            timeout=kwargs.pop("timeout", CLIENT_TIMEOUT),
            relay=self.endpoint,
            relay_connector=self.connector,
            **kwargs,
        )

    def local_phone(self, record: DeviceRecord) -> LinkClient:
        return LinkClient(
            host="127.0.0.1",
            port=self.port,
            device_id=record.device_id,
            secret=record.secret,
            timeout=CLIENT_TIMEOUT,
        )

    # ------------------------------------------------------------- teardown

    def close(self) -> None:
        self.dialler.stop(timeout=2.0)
        self.server.stop(timeout=2.0)
        self.relay.close()
