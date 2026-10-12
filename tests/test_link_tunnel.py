"""
The PC's outbound relay dialler and the relay endpoint it is configured with.

Loopback only, against the disposable ``FakeRelay``, with fresh keys. Nothing
here resolves or reaches a public host.
"""

from __future__ import annotations

import random
import socket
import tempfile
import unittest
from pathlib import Path

from core.link_audit import AuditEvent, AuditLog
from core.link_capability import LinkOp, LinkScope
from core.link_devices import DeviceRegistry
from core.link_relay_wire import (
    RelayRole,
    RelayStatus,
    decode_hello,
    route_id_for_secret,
    route_key_for_secret,
)
from core.link_server import LinkServer, LinkSettings
from core.link_tunnel import (
    Backoff,
    RelayConfigError,
    RelayDialler,
    RelayEndpoint,
    RelayFailure,
    RelayRefused,
    dial_as_phone,
)
from tests.fixtures.link_fake_relay import FakeRelay
from tests.fixtures.link_harness import FakeServerClock, ok_handler, wait_for
from tests.fixtures.link_remote_rig import FAST_TUNNEL, RemoteRig


def events(audit: AuditLog, event: AuditEvent) -> list:
    return [r for r in audit.records() if r.event is event]


class TestRelayEndpoint(unittest.TestCase):
    def test_a_host_name_or_address_is_accepted(self) -> None:
        for host in ("relay.example.org", "RELAY.Example.org", "203.0.113.9", "2001:db8::1"):
            with self.subTest(host=host):
                RelayEndpoint(host=host)

    def test_the_host_is_normalised(self) -> None:
        self.assertEqual(RelayEndpoint(host=" Relay.Example.ORG ").host, "relay.example.org")

    def test_urls_ports_and_junk_are_refused(self) -> None:
        for host in ("https://relay.example.org", "relay.example.org:443", "relay/x", "", "a b", "-bad.example"):
            with self.subTest(host=host):
                with self.assertRaises(RelayConfigError):
                    RelayEndpoint(host=host)

    def test_ports_are_range_checked(self) -> None:
        for port in (0, 65_536, -1):
            with self.subTest(port=port):
                with self.assertRaises(RelayConfigError):
                    RelayEndpoint(host="relay.example.org", port=port)

    def test_the_default_port_is_the_https_port(self) -> None:
        self.assertEqual(RelayEndpoint(host="relay.example.org").port, 443)

    def test_what_the_phone_is_told_contains_no_secret(self) -> None:
        info = RelayEndpoint(host="relay.example.org", port=8443).describe_for_phone()

        self.assertEqual(info, {"host": "relay.example.org", "port": 8443})

    def test_a_pinned_ca_is_handed_to_the_phone(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            ca = Path(folder) / "ca.pem"
            ca.write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", encoding="ascii")

            info = RelayEndpoint(
                host="203.0.113.9", server_name="relay.example.org", ca_file=str(ca)
            ).describe_for_phone()

        self.assertEqual(info["server_name"], "relay.example.org")
        self.assertIn("BEGIN CERTIFICATE", info["ca_pem"])

    def test_a_file_that_is_not_a_certificate_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            bogus = Path(folder) / "notes.pem"
            bogus.write_text("this is not a certificate\n", encoding="ascii")

            with self.assertRaises(RelayConfigError):
                RelayEndpoint(host="relay.example.org", ca_file=str(bogus)).ca_pem()


class TestBackoff(unittest.TestCase):
    def test_delays_grow_with_jitter_and_are_capped(self) -> None:
        backoff = Backoff(initial=1.0, maximum=8.0, rng=random.Random(7))
        delays = [backoff.next() for _ in range(8)]

        for attempt, delay in enumerate(delays):
            ceiling = min(8.0, 2 ** attempt)

            with self.subTest(attempt=attempt):
                self.assertGreaterEqual(delay, ceiling / 2)
                self.assertLessEqual(delay, ceiling)

    def test_reset_starts_again(self) -> None:
        backoff = Backoff(initial=1.0, maximum=60.0, rng=random.Random(1))

        for _ in range(5):
            backoff.next()

        backoff.reset()

        self.assertLessEqual(backoff.next(), 1.0)


class TestDiallerGate(unittest.TestCase):
    def test_it_will_not_start_unless_the_remote_scope_is_on(self) -> None:
        server = LinkServer(
            registry=DeviceRegistry(path=None),
            audit=AuditLog(path=None),
            settings=LinkSettings(host="127.0.0.1", port=0),
        )
        dialler = RelayDialler(server, RelayEndpoint(host="127.0.0.1", port=9))

        with self.assertRaises(RuntimeError):
            dialler.start()


class DiallerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = RemoteRig(handlers={LinkOp.STATUS: ok_handler})
        self.addCleanup(self.rig.close)


class TestWhoIsParked(DiallerTestCase):
    def test_only_active_opted_in_unexpired_devices_are_parked(self) -> None:
        registry = self.rig.registry
        clock = self.rig.clock

        yes = self.rig.remote_device("yes")

        pending = registry.create("pending")
        registry.set_remote_enabled(pending.device_id, True)

        not_opted = registry.create("not opted in")
        registry.activate(not_opted.device_id)

        expired = registry.create("expired")
        registry.activate(expired.device_id)
        registry.set_remote_enabled(expired.device_id, True, expires_at=clock() + 1)
        clock.advance(2)

        self.rig.dialler.refresh()

        self.assertTrue(wait_for(lambda: self.rig.dialler.parked_devices() == {yes.device_id}))
        self.assertEqual(set(self.rig.relay.parked_routes()), {route_id_for_secret(yes.secret)})

    def test_the_host_hello_carries_the_route_key_and_never_the_pairing_key(self) -> None:
        record = self.rig.remote_device()
        raw = self.rig.relay.host_hellos[0]
        hello = decode_hello(raw)

        self.assertIs(hello.role, RelayRole.HOST)
        self.assertEqual(hello.token, route_key_for_secret(record.secret))
        self.assertNotIn(record.secret, raw)

    def test_revocation_withdraws_the_parked_connection(self) -> None:
        record = self.rig.remote_device()

        self.rig.server.revoke(record.device_id)
        self.rig.dialler.refresh()

        self.assertTrue(self.rig.wait_unparked(record))
        self.assertEqual(self.rig.dialler.parked_devices(), frozenset())

    def test_the_timer_alone_withdraws_an_expired_device(self) -> None:
        record = self.rig.remote_device(seconds=10)
        self.rig.clock.advance(11)

        # No explicit refresh: the supervisor's own timer must notice.
        self.assertTrue(self.rig.wait_unparked(record))

    def test_the_number_of_routes_is_capped(self) -> None:
        self.rig.dialler.stop()
        rig = self.rig

        for n in range(3):
            record = rig.registry.create(f"phone {n}")
            rig.registry.activate(record.device_id)
            rig.registry.set_remote_enabled(record.device_id, True)

        capped = RelayDialler(
            rig.server,
            rig.endpoint,
            settings=type(FAST_TUNNEL)(**{**FAST_TUNNEL.__dict__, "max_routes": 2}),
            connector=rig.connector,
            clock=rig.clock,
        )
        capped.start()
        self.addCleanup(capped.stop)

        self.assertTrue(wait_for(lambda: len(capped.parked_devices()) == 2))
        self.assertTrue(wait_for(lambda: rig.relay.parked_count() == 2))

    def test_stop_closes_parked_connections_promptly(self) -> None:
        record = self.rig.remote_device()
        self.rig.dialler.stop(timeout=2.0)

        self.assertTrue(self.rig.wait_unparked(record))
        self.assertEqual(self.rig.dialler.parked_devices(), frozenset())


class TestRelayTrouble(unittest.TestCase):
    def test_a_relay_that_is_down_is_recorded_once_and_retried(self) -> None:
        # A port with nothing listening: bound, then closed.
        probe = socket.create_server(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()

        rig = RemoteRig(
            handlers={LinkOp.STATUS: ok_handler},
            endpoint=RelayEndpoint(host="127.0.0.1", port=dead_port),
        )
        self.addCleanup(rig.close)

        attempts: list[int] = []
        original = rig.dialler.connector

        def counting(endpoint, timeout):
            attempts.append(1)
            return original(endpoint, timeout)

        rig.dialler.connector = counting

        record = rig.registry.create("phone")
        rig.registry.activate(record.device_id)
        rig.registry.set_remote_enabled(record.device_id, True)
        rig.dialler.refresh()

        # Windows takes about two seconds to report a refused loopback connect
        # (it retries the SYN), so two attempts is the bound, not four.
        self.assertTrue(wait_for(lambda: len(attempts) >= 2, timeout=10))

        failures = events(rig.audit, AuditEvent.RELAY_FAILED)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].reason, RelayFailure.UNREACHABLE.value)
        self.assertEqual(failures[0].scope, LinkScope.REMOTE_TUNNEL)

    def test_a_parking_that_expires_is_renewed_without_a_failure(self) -> None:
        rig = RemoteRig(handlers={LinkOp.STATUS: ok_handler}, park_limit=0.15)
        self.addCleanup(rig.close)
        record = rig.remote_device()

        self.assertTrue(wait_for(lambda: rig.relay.expired_parks >= 2, timeout=5))
        self.assertTrue(rig.wait_parked(record))
        self.assertEqual(events(rig.audit, AuditEvent.RELAY_FAILED), [])
        self.assertEqual(len(events(rig.audit, AuditEvent.RELAY_PARKED)), 1)

        with rig.phone(record) as phone:
            self.assertTrue(phone.call("status").ok)

    def test_a_relay_that_goes_silent_is_left_and_redialled(self) -> None:
        # Keepalives far apart: the dialler's silence limit fires first.
        rig = RemoteRig(
            handlers={LinkOp.STATUS: ok_handler},
            tunnel=type(FAST_TUNNEL)(**{**FAST_TUNNEL.__dict__, "parked_silence": 0.2}),
            keepalive=5.0,
        )
        self.addCleanup(rig.close)
        rig.remote_device()

        self.assertTrue(
            wait_for(lambda: len(events(rig.audit, AuditEvent.RELAY_FAILED)) == 1, timeout=5)
        )
        self.assertEqual(
            events(rig.audit, AuditEvent.RELAY_FAILED)[0].reason, RelayFailure.SILENT.value
        )
        self.assertTrue(wait_for(lambda: len(rig.relay.host_hellos) >= 2, timeout=5))


class TestPhoneSide(unittest.TestCase):
    def test_no_parked_pc_is_a_clear_refusal(self) -> None:
        relay = FakeRelay(phone_wait=0.1)
        self.addCleanup(relay.close)

        with self.assertRaises(RelayRefused) as caught:
            dial_as_phone(relay.endpoint, b"k" * 32, timeout=3, connector=FakeRelay.connector)

        self.assertIs(caught.exception.status, RelayStatus.NO_HOST)

    def test_the_phone_hello_names_the_route_and_nothing_else(self) -> None:
        relay = FakeRelay(phone_wait=0.1)
        self.addCleanup(relay.close)
        secret = bytes(range(32))

        with self.assertRaises(RelayRefused):
            dial_as_phone(relay.endpoint, secret, timeout=3, connector=FakeRelay.connector)

        hello = decode_hello(relay.phone_hellos[0])

        self.assertIs(hello.role, RelayRole.PHONE)
        self.assertEqual(hello.token, route_id_for_secret(secret))


class TestNoDirectRemoteScope(unittest.TestCase):
    def test_the_listener_never_classifies_an_internet_address_as_remote(self) -> None:
        # Layer 2 arrives only through the relay path. A public address at the
        # listening port is still refused, as in Layer 1.
        from core.link_capability import scope_for_peer

        for address in ("203.0.113.7", "8.8.8.8", "2001:db8::1", "100.64.0.1"):
            with self.subTest(address=address):
                self.assertIsNone(scope_for_peer(address))


if __name__ == "__main__":
    unittest.main()
