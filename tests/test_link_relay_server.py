"""
The real relay in ``relay/qronos_relay``, on loopback, with real outer TLS.

Three groups: the relay's copy of the wire format matches the definition in
``core``; the whole Layer 2 path works through it with the outer TLS verified
like a deployed relay's; and the relay holds its own limits against clients
that misbehave. Throwaway certificates and fresh device keys throughout.
"""

from __future__ import annotations

import socket
import ssl
import tempfile
import time
import unittest
from pathlib import Path

from core import link_relay_wire as core_wire
from core.link_audit import AuditEvent
from core.link_capability import LinkOp
from core.link_client import LinkClientError
from core.link_handlers import ping_handler
from core.link_server import SessionRule
from core.link_tunnel import RelayEndpoint, RelayFailure, connect_relay, relay_context
from relay.qronos_relay import wire as relay_wire
from relay.qronos_relay.server import RelayLimits, RelayServer
from tests.fixtures.link_harness import ok_handler, wait_for
from tests.fixtures.link_real_relay import RunningRelay
from tests.fixtures.link_remote_rig import RemoteRig


SECRET = bytes(range(32))


class TestWireContract(unittest.TestCase):
    """The relay is deployed alone, so it carries a copy. The copy must match."""

    def test_framing_constants_match(self) -> None:
        self.assertEqual(relay_wire.MAGIC, core_wire.MAGIC)
        self.assertEqual(relay_wire.TOKEN_BYTES, core_wire.TOKEN_BYTES)
        self.assertEqual(relay_wire.HELLO_BYTES, core_wire.HELLO_BYTES)
        self.assertEqual(relay_wire.ROUTE_ID_LABEL, core_wire.ROUTE_ID_LABEL)
        self.assertEqual(relay_wire.ROLE_HOST, core_wire.RelayRole.HOST.value)
        self.assertEqual(relay_wire.ROLE_PHONE, core_wire.RelayRole.PHONE.value)

    def test_status_bytes_match(self) -> None:
        self.assertEqual(
            {s.name: s.value for s in relay_wire.Status},
            {s.name: s.value for s in core_wire.RelayStatus},
        )

    def test_both_files_a_host_and_a_phone_under_the_same_route(self) -> None:
        for hello in (core_wire.host_hello(SECRET), core_wire.phone_hello(SECRET)):
            with self.subTest(role=hello.role):
                role, route = relay_wire.parse_hello(hello.encode())

                self.assertEqual(role, hello.role.value)
                self.assertEqual(route, hello.route_id)


class RelayTestCase(unittest.TestCase):
    limits = RelayLimits()

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.relay = RunningRelay(Path(self.folder.name), self.limits)
        self.addCleanup(self.relay.close)

    def connect(self) -> ssl.SSLSocket:
        conn = connect_relay(self.relay.endpoint, timeout=5)
        conn.settimeout(5)
        self.addCleanup(conn.close)

        return conn

    def status(self, conn: ssl.SSLSocket) -> int | None:
        try:
            byte = conn.recv(1)
        except (OSError, ssl.SSLError):
            return None

        return byte[0] if byte else None


class TestEndToEnd(RelayTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.rig = RemoteRig(
            handlers={LinkOp.PING: ping_handler, LinkOp.STATUS: ok_handler, LinkOp.ASK: ok_handler},
            relay=self.relay,
            endpoint=self.relay.endpoint,
            connector=connect_relay,
        )
        self.addCleanup(self.rig.close)

    def test_a_phone_reaches_the_pc_through_the_relay(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            ping = phone.call("ping")
            status = phone.call("status")
            summary = phone.tls_summary()

        self.assertTrue(ping.ok)
        self.assertEqual(ping.result["scope"], "remote_tunnel")
        self.assertTrue(status.ok)
        self.assertTrue(summary.startswith("TLSv1.3"))
        self.assertEqual(self.relay.stats()["joins"], 1)

    def test_revocation_through_the_relay(self) -> None:
        record = self.rig.remote_device()
        phone = self.rig.phone(record)
        phone.connect()
        self.addCleanup(phone.close)
        self.assertTrue(phone.call("status").ok)

        self.rig.server.revoke(record.device_id)
        self.rig.dialler.refresh()

        with self.assertRaises(LinkClientError):
            phone.call("status")

        self.assertTrue(self.rig.wait_unparked(record))

        with self.assertRaises(LinkClientError):
            self.rig.phone(record).connect()

        self.assertEqual(
            [e.reason for e in self.rig.audit.records() if e.event is AuditEvent.SESSION_ENDED],
            [SessionRule.REVOKED.value],
        )

    def test_the_pc_parks_again_for_the_next_session(self) -> None:
        record = self.rig.remote_device()

        for _ in range(3):
            with self.rig.phone(record) as phone:
                self.assertTrue(phone.call("status").ok)

        self.assertEqual(self.relay.stats()["joins"], 3)


class TestOuterTlsIsVerified(RelayTestCase):
    def test_a_relay_the_system_does_not_trust_is_refused(self) -> None:
        # The throwaway CA is in no trust store, so without the pin the outer
        # handshake must fail, exactly as for an impostor relay.
        untrusted = RelayEndpoint(
            host="127.0.0.1", port=self.relay.port, server_name=self.relay.certs.server_name
        )

        with self.assertRaises(ssl.SSLCertVerificationError):
            connect_relay(untrusted, timeout=5)

    def test_a_relay_with_the_wrong_name_is_refused(self) -> None:
        wrong = RelayEndpoint(
            host="127.0.0.1",
            port=self.relay.port,
            server_name="other.test",
            ca_file=str(self.relay.certs.ca_file),
        )

        with self.assertRaises(ssl.SSLCertVerificationError):
            connect_relay(wrong, timeout=5)

    def test_the_dialler_records_an_untrusted_relay_once(self) -> None:
        untrusted = RelayEndpoint(
            host="127.0.0.1", port=self.relay.port, server_name=self.relay.certs.server_name
        )
        rig = RemoteRig(
            handlers={LinkOp.STATUS: ok_handler},
            relay=self.relay,
            endpoint=untrusted,
            connector=connect_relay,
        )
        self.addCleanup(rig.close)

        record = rig.registry.create("phone")
        rig.registry.activate(record.device_id)
        rig.registry.set_remote_enabled(record.device_id, True)
        rig.dialler.refresh()

        self.assertTrue(
            wait_for(
                lambda: any(e.event is AuditEvent.RELAY_FAILED for e in rig.audit.records()),
                timeout=5,
            )
        )
        failures = [e for e in rig.audit.records() if e.event is AuditEvent.RELAY_FAILED]

        self.assertEqual([e.reason for e in failures], [RelayFailure.TLS_FAILED.value])
        self.assertEqual(self.relay.stats()["parked"], 0)

    def test_the_relay_will_not_run_without_tls(self) -> None:
        with self.assertRaises(TypeError):
            RelayServer(None)  # type: ignore[arg-type]

    def test_a_plaintext_client_is_not_served(self) -> None:
        with socket.create_connection(("127.0.0.1", self.relay.port), timeout=5) as raw:
            raw.sendall(core_wire.phone_hello(SECRET).encode())
            raw.settimeout(5)

            try:
                reply = raw.recv(64)
            except OSError:
                reply = b""

        # Whatever comes back is a TLS alert, never a relay status byte.
        self.assertNotIn(reply, {bytes([s.value]) for s in core_wire.RelayStatus})


class TestRelayProtocol(RelayTestCase):
    limits = RelayLimits(
        hello_timeout=0.5,
        phone_wait=0.3,
        keepalive=0.1,
        park_lifetime=0.6,
        pair_idle=0.4,
        max_parked_per_route=2,
        max_per_address=6,
    )

    def park(self, secret: bytes = SECRET) -> ssl.SSLSocket:
        host = self.connect()
        host.sendall(core_wire.host_hello(secret).encode())
        self.assertEqual(self.status(host), core_wire.RelayStatus.PARKED.value)

        return host

    def test_a_phone_with_no_pc_is_told_so(self) -> None:
        phone = self.connect()
        phone.sendall(core_wire.phone_hello(SECRET).encode())

        self.assertEqual(self.status(phone), core_wire.RelayStatus.NO_HOST.value)

    def test_a_phone_and_a_pc_are_joined_and_bytes_flow_both_ways(self) -> None:
        host = self.park()
        phone = self.connect()
        phone.sendall(core_wire.phone_hello(SECRET).encode())

        statuses = []

        while True:
            value = self.status(host)
            statuses.append(value)

            if value != core_wire.RelayStatus.KEEPALIVE.value:
                break

        self.assertEqual(statuses[-1], core_wire.RelayStatus.SPLICED.value)
        self.assertEqual(self.status(phone), core_wire.RelayStatus.SPLICED.value)

        phone.sendall(b"from the phone")
        self.assertEqual(host.recv(64), b"from the phone")
        host.sendall(b"from the pc")
        self.assertEqual(phone.recv(64), b"from the pc")

    def test_a_phone_waits_briefly_for_a_pc_that_is_re_parking(self) -> None:
        phone = self.connect()
        phone.sendall(core_wire.phone_hello(SECRET).encode())
        time.sleep(0.1)
        host = self.park()

        self.assertEqual(self.status(phone), core_wire.RelayStatus.SPLICED.value)

        while self.status(host) == core_wire.RelayStatus.KEEPALIVE.value:
            pass

    def test_another_devices_route_does_not_join(self) -> None:
        self.park(SECRET)
        phone = self.connect()
        phone.sendall(core_wire.phone_hello(bytes(32)).encode())

        self.assertEqual(self.status(phone), core_wire.RelayStatus.NO_HOST.value)

    def test_a_phone_hello_cannot_park_as_the_pc(self) -> None:
        # Presenting the route id as a host key files it under a different
        # route, so the real phone never meets the impostor.
        impostor = self.connect()
        impostor.sendall(
            core_wire.RelayHello(core_wire.RelayRole.HOST, core_wire.route_id_for_secret(SECRET)).encode()
        )
        self.assertEqual(self.status(impostor), core_wire.RelayStatus.PARKED.value)

        phone = self.connect()
        phone.sendall(core_wire.phone_hello(SECRET).encode())

        self.assertEqual(self.status(phone), core_wire.RelayStatus.NO_HOST.value)

    def test_a_parked_pc_hears_keepalives(self) -> None:
        host = self.park()

        self.assertEqual(self.status(host), core_wire.RelayStatus.KEEPALIVE.value)

    def test_parking_has_a_lifetime(self) -> None:
        host = self.park()
        seen = []

        while True:
            value = self.status(host)

            if value is None:
                break

            seen.append(value)

        self.assertEqual(seen[-1], core_wire.RelayStatus.PARK_EXPIRED.value)

    def test_a_route_holds_a_bounded_number_of_parked_pcs(self) -> None:
        self.park()
        self.park()
        third = self.connect()
        third.sendall(core_wire.host_hello(SECRET).encode())

        self.assertEqual(self.status(third), core_wire.RelayStatus.ROUTE_FULL.value)

    def test_a_bad_hello_is_refused(self) -> None:
        conn = self.connect()
        conn.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n" + b"\x00" * 20)

        self.assertEqual(self.status(conn), core_wire.RelayStatus.BAD_HELLO.value)

    def test_a_silent_client_is_dropped(self) -> None:
        conn = self.connect()

        self.assertIsNone(self.status(conn))
        self.assertTrue(wait_for(lambda: self.relay.stats().get("refused_no_hello", 0) == 1))

    def test_one_address_cannot_take_every_connection(self) -> None:
        held = [self.connect() for _ in range(self.limits.max_per_address)]
        extra = self.connect()

        self.assertEqual(self.status(extra), core_wire.RelayStatus.OVER_CAPACITY.value)
        self.assertEqual(len(held), self.limits.max_per_address)

    def test_a_joined_pair_that_goes_idle_is_closed(self) -> None:
        host = self.park()
        phone = self.connect()
        phone.sendall(core_wire.phone_hello(SECRET).encode())
        self.assertEqual(self.status(phone), core_wire.RelayStatus.SPLICED.value)

        self.assertIsNone(self.status(phone))
        self.assertTrue(wait_for(lambda: self.relay.stats().get("pair_idle", 0) == 1))

    def test_a_pc_that_hangs_up_while_parked_frees_its_route(self) -> None:
        host = self.park()
        host.close()

        self.assertTrue(wait_for(lambda: self.relay.stats()["parked"] == 0))

        phone = self.connect()
        phone.sendall(core_wire.phone_hello(SECRET).encode())

        self.assertEqual(self.status(phone), core_wire.RelayStatus.NO_HOST.value)

    def test_a_pc_that_talks_before_being_joined_is_dropped(self) -> None:
        host = self.park()
        host.sendall(b"early")

        self.assertTrue(wait_for(lambda: self.relay.stats()["parked"] == 0))

    def test_the_counts_name_no_route_and_no_address(self) -> None:
        self.park()
        phone = self.connect()
        phone.sendall(core_wire.phone_hello(bytes(32)).encode())
        self.status(phone)

        stats = self.relay.stats()
        rendered = repr(stats)

        self.assertTrue(all(isinstance(v, int) for v in stats.values()))
        self.assertNotIn("127.0.0.1", rendered)
        self.assertNotIn(core_wire.route_id_for_secret(SECRET).hex(), rendered)


class TestStandaloneEntryPoint(unittest.TestCase):
    """The relay is deployed without Qronos; its entry point must run alone."""

    def test_it_runs_from_its_own_directory_and_parks_a_pc(self) -> None:
        import os
        import subprocess
        import sys

        from tests.fixtures.link_certs import relay_certificates

        root = Path(__file__).resolve().parent.parent
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        certs = relay_certificates(Path(folder.name))

        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        process = subprocess.Popen(
            [
                sys.executable, "-m", "qronos_relay",
                "--listen", "127.0.0.1", "--port", "0",
                "--cert", str(certs.cert_file), "--key", str(certs.key_file),
                "--stats-every", "0",
            ],
            cwd=root / "relay",
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.kill)

        line = process.stdout.readline()
        self.assertIn("qronos relay listening on 127.0.0.1:", line)
        port = int(line.rsplit(":", 1)[1])

        endpoint = RelayEndpoint(
            host="127.0.0.1", port=port, server_name=certs.server_name, ca_file=str(certs.ca_file)
        )

        with connect_relay(endpoint, timeout=5) as host:
            host.sendall(core_wire.host_hello(SECRET).encode())
            self.assertEqual(host.recv(1), bytes([core_wire.RelayStatus.PARKED.value]))

    def test_the_relay_package_imports_nothing_from_qronos(self) -> None:
        import ast

        root = Path(__file__).resolve().parent.parent / "relay" / "qronos_relay"

        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))

            for node in ast.walk(tree):
                names = []

                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]

                for name in names:
                    with self.subTest(file=path.name, name=name):
                        self.assertNotIn(name.split(".")[0], {"core", "security", "tools", "tests", "relay"})


class TestRelayContextHelper(unittest.TestCase):
    def test_the_outer_context_verifies_chain_and_name(self) -> None:
        context = relay_context(RelayEndpoint(host="relay.example.org"))

        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)


if __name__ == "__main__":
    unittest.main()
