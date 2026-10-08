"""
The Security Monitor's verdicts, enforced by the real link on loopback.

The monitor's own rules are tested in test_security_monitor.py; this file
checks that the server, the relay dialler and LinkRuntime act on them:

    remote access cannot start without an admitting monitor;
    containment ends remote sessions and parking and leaves the LAN alone;
    quarantine stops the link, and only a reset at the PC brings it back;
    unproved identities are never held against the device they name, and
    the relay's failures are never held against a LAN address;
    an abusive device loses remote access, not its LAN use, and none of it
    reaches a file, application or system handler.

Fresh test-only keys, in-memory or temporary files, loopback only.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from core.link_audit import AuditEvent
from core.link_capability import LinkOp, LinkScope
from core.link_client import LinkClient, LinkClientError
from core.link_handlers import ping_handler
from core.link_runtime import LinkRuntime, LinkRuntimeSettings, LinkServices, save_relay_endpoint
from core.link_server import LinkServer, LinkSettings, RejectReason, SessionRule
from core.link_devices import DeviceRegistry
from core.link_audit import AuditLog
from core.link_tunnel import RelayDialler, RelayEndpoint
from core.security_monitor import (
    DEFAULT_POLICIES,
    LAN_RETRY_INTERVAL_SECONDS,
    Principal,
    SecurityMonitor,
    SignalKind,
    verify_chain,
)
from tests.fixtures.link_fake_relay import FakeRelay
from tests.fixtures.link_harness import CLIENT_TIMEOUT, ok_handler, wait_for
from tests.fixtures.link_remote_rig import FAST_TUNNEL, RemoteRig


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Counted:
    """A handler that must never run for the requests these tests make."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, session, request):
        self.calls += 1
        return {"done": True}


def ended(rig, rule: SessionRule) -> bool:
    return any(
        r.event is AuditEvent.SESSION_ENDED and r.reason == rule.value for r in rig.audit.records()
    )


class TestWanAdmission(unittest.TestCase):
    def test_a_server_will_not_offer_remote_access_without_a_monitor(self) -> None:
        server = LinkServer(
            registry=DeviceRegistry(path=None),
            audit=AuditLog(path=None),
            settings=LinkSettings(
                host="127.0.0.1", port=0,
                allowed_scopes=frozenset({LinkScope.LOCAL_NETWORK, LinkScope.REMOTE_TUNNEL}),
            ),
        )

        with self.assertRaises(RuntimeError):
            server.start()

        self.assertEqual(server.remote_admission().reason, "security_monitor_missing")

    def test_the_dialler_refuses_to_start_while_contained(self) -> None:
        rig = RemoteRig(handlers={LinkOp.PING: ping_handler})
        self.addCleanup(rig.close)
        rig.monitor.observe(SignalKind.TAMPERING, Principal.relay())

        dialler = RelayDialler(rig.server, rig.endpoint, connector=FakeRelay.connector)

        with self.assertRaises(RuntimeError) as caught:
            dialler.start()

        self.assertIn("security_contained", str(caught.exception))

    def test_a_relayed_connection_is_refused_while_contained(self) -> None:
        rig = RemoteRig(handlers={LinkOp.PING: ping_handler})
        self.addCleanup(rig.close)
        rig.monitor.observe(SignalKind.TAMPERING, Principal.relay())
        verdicts: list[bool] = []

        pc_end, phone_end = socket.socketpair()
        self.addCleanup(phone_end.close)
        rig.server.serve_relayed(pc_end, "127.0.0.1", verdicts.append)

        self.assertTrue(wait_for(lambda: verdicts == [False]))
        self.assertTrue(
            any(r.reason == RejectReason.REMOTE_NOT_ADMITTED.value for r in rig.audit.records())
        )


class TestContainment(unittest.TestCase):
    def test_tampering_contains_remote_access_and_spares_the_lan(self) -> None:
        armed = threading.Event()

        def flip(data: bytes) -> bytes:
            return data[:-1] + bytes([data[-1] ^ 0x01]) if armed.is_set() else data

        rig = RemoteRig(handlers={LinkOp.PING: ping_handler, LinkOp.STATUS: ok_handler}, tamper=flip)
        self.addCleanup(rig.close)
        record = rig.remote_device()

        local = rig.local_phone(record)
        local.connect()
        self.addCleanup(local.close)
        other_remote = rig.phone(record)
        other_remote.connect()
        self.addCleanup(other_remote.close)

        with rig.phone(record) as remote:
            # One answered request first, so the handshake has finished on the
            # PC too. A byte altered during the handshake is indistinguishable
            # from a wrong key and is, correctly, an unauthenticated failure;
            # containment needs an authenticated session to be tampered with.
            self.assertTrue(remote.call("status").ok)
            armed.set()

            with self.assertRaises(LinkClientError):
                remote.call("status")

        armed.clear()

        self.assertTrue(wait_for(lambda: rig.monitor.contained))
        # Every remote session ends, the one that was not tampered with too...
        self.assertTrue(wait_for(lambda: ended(rig, SessionRule.CONTAINED)))

        with self.assertRaises(LinkClientError):
            other_remote.call("status")

        # ...nothing is parked at the relay any more...
        self.assertTrue(rig.wait_unparked(record))

        # ...and the phone on the sofa carries on.
        self.assertTrue(local.call("status").ok)

        lines = rig.monitor.incidents.records()
        self.assertEqual([line["response"] for line in lines], ["contain"])
        self.assertTrue(verify_chain(lines))

        # A reset at the PC lets parking resume without restarting anything.
        # The relay may still hold a parking the PC closed during containment
        # until its next keepalive notices; a phone joined to it retries, as
        # the reference client does.
        rig.monitor.reset()
        self.assertTrue(rig.wait_parked(record))

        remote = rig.phone(record)
        self.addCleanup(remote.close)
        response = remote.call_with_retry("status", sleep=lambda seconds: rig.wait_parked(record))

        self.assertTrue(response.ok)


class TestAttributionOnTheWire(unittest.TestCase):
    def test_failing_in_a_devices_name_does_not_lock_that_device_out(self) -> None:
        clock = Clock()
        rig = RemoteRig(handlers={LinkOp.PING: ping_handler}, monitor=SecurityMonitor(clock=clock))
        self.addCleanup(rig.close)
        victim = rig.remote_device()
        threshold = DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED].threshold

        # An attacker on the LAN claims the victim's identity with a wrong key.
        for _ in range(threshold):
            impostor = LinkClient("127.0.0.1", rig.port, victim.device_id, b"x" * 32, timeout=CLIENT_TIMEOUT)

            with self.assertRaises(LinkClientError):
                impostor.connect()

        self.assertTrue(wait_for(lambda: rig.monitor.throttled(Principal.lan("127.0.0.1"))))

        # The claimed identity is no principal: the real device is untouched,
        # remotely and on the LAN.
        for scope in ("local_network", "remote_tunnel"):
            self.assertFalse(rig.monitor.throttled(Principal.device(victim.device_id, scope)))

        with rig.phone(victim) as remote:
            self.assertTrue(remote.call("ping").ok)

        # The shared LAN address is slowed, not banned: the real phone on it
        # gets its attempt once the retry interval has passed.
        clock.now += LAN_RETRY_INTERVAL_SECONDS

        with rig.local_phone(victim) as local:
            self.assertTrue(local.call("ping").ok)

    def test_relay_failures_are_never_held_against_a_lan_address(self) -> None:
        rig = RemoteRig(handlers={LinkOp.PING: ping_handler})
        self.addCleanup(rig.close)
        record = rig.remote_device()
        threshold = DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED].threshold

        # The relay delivers junk handshakes; it shares 127.0.0.1 with the LAN
        # phone in this fixture, which is the worst case for attribution.
        for _ in range(threshold):
            pc_end, phone_end = socket.socketpair()
            phone_end.sendall(b"\x16\x03\x01 not a client hello")
            phone_end.close()
            rig.server.serve_relayed(pc_end, "127.0.0.1")

        self.assertTrue(wait_for(lambda: rig.monitor.throttled(Principal.relay())))
        self.assertFalse(rig.monitor.throttled(Principal.lan("127.0.0.1")))

        with rig.local_phone(record) as local:
            self.assertTrue(local.call("ping").ok)


class TestAbusiveDevice(unittest.TestCase):
    def test_a_device_that_keeps_asking_for_what_it_may_not_do_loses_remote_access(self) -> None:
        forbidden = Counted()
        rig = RemoteRig(
            handlers={LinkOp.PING: ping_handler, LinkOp.WRITE_FILE: forbidden, LinkOp.RUN_APP: forbidden, LinkOp.SYSTEM_CONTROL: forbidden},
            settings=LinkSettings(remote_burst=100),
        )
        self.addCleanup(rig.close)
        record = rig.remote_device()
        threshold = DEFAULT_POLICIES[SignalKind.REQUEST_REFUSED].threshold

        with rig.phone(record) as remote:
            with self.assertRaises(LinkClientError):
                for n in range(threshold + 1):
                    remote.call(("write_file", "run_app", "system_control", "list_devices")[n % 4])

        principal = Principal.device(record.device_id, "remote_tunnel")
        self.assertTrue(rig.monitor.throttled(principal))
        self.assertTrue(wait_for(lambda: ended(rig, SessionRule.THROTTLED)))

        # Nothing it asked for ran.
        self.assertEqual(forbidden.calls, 0)

        # It cannot come straight back over the relay...
        self.assertTrue(rig.wait_parked(record))

        with self.assertRaises(LinkClientError):
            with rig.phone(record) as remote:
                remote.call("ping")

        # ...but its LAN use is untouched.
        with rig.local_phone(record) as local:
            self.assertTrue(local.call("ping").ok)


class RuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.relay = FakeRelay()
        self.addCleanup(self.relay.close)
        save_relay_endpoint(self.folder / "link_relay.json", self.relay.endpoint)

    def runtime(self) -> LinkRuntime:
        runtime = LinkRuntime(
            LinkRuntimeSettings(
                link_enabled=True,
                remote_access_enabled=True,
                port=0,
                bind_host="127.0.0.1",
                device_path=self.folder / "link_devices.json",
                audit_path=self.folder / "link_audit.jsonl",
                relay_path=self.folder / "link_relay.json",
                security_path=self.folder / "link_security.json",
                incident_path=self.folder / "link_incidents.jsonl",
                pairing_host="127.0.0.1",
            ),
            services=LinkServices(remote_chat=lambda messages: "پاسخ"),
            relay_connector=FakeRelay.connector,
            tunnel_settings=FAST_TUNNEL,
        )
        self.addCleanup(runtime.stop)

        return runtime

    def latch(self, **state) -> None:
        payload = {"version": 1, "contained": False, "contained_reason": "", "quarantined": False,
                   "quarantine_reason": "", "contain_history": []}
        payload.update(state)
        (self.folder / "link_security.json").write_text(json.dumps(payload), encoding="utf-8")


class TestRuntimeLatches(RuntimeTestCase):
    def test_a_contained_link_starts_for_the_lan_only_until_reset(self) -> None:
        self.latch(contained=True, contained_reason="tampering")
        runtime = self.runtime()

        self.assertTrue(runtime.start())
        self.assertFalse(runtime.remote_active)
        self.assertEqual(runtime.status()["remote_unavailable"], "security_contained")

        status = runtime.reset_security()

        self.assertTrue(runtime.remote_active)
        self.assertIsNone(status["remote_unavailable"])
        self.assertFalse(status["security"]["contained"])

    def test_a_quarantined_link_does_not_start_at_all_until_reset(self) -> None:
        self.latch(quarantined=True, quarantine_reason="repeated_containment")
        runtime = self.runtime()

        self.assertFalse(runtime.start())
        self.assertFalse(runtime.running)

        runtime.reset_security()

        self.assertTrue(runtime.running)

    def test_an_unreadable_security_state_keeps_remote_off(self) -> None:
        (self.folder / "link_security.json").write_text("{ nope", encoding="utf-8")
        runtime = self.runtime()

        self.assertTrue(runtime.start())
        self.assertFalse(runtime.remote_active)
        self.assertEqual(runtime.status()["security"]["contained_reason"], "state_unreadable")

    def test_quarantine_stops_the_running_link(self) -> None:
        runtime = self.runtime()
        runtime.start()
        self.assertTrue(runtime.running)

        for _ in range(3):
            runtime.monitor.observe(SignalKind.TAMPERING, Principal.relay())
            runtime.monitor.contained = False  # each a separate incident

        self.assertTrue(wait_for(lambda: not runtime.running, timeout=5))
        self.assertFalse(runtime.start())

    def test_safe_shutdown_is_asked_for_only_when_configured(self) -> None:
        asked: list[str] = []
        runtime = LinkRuntime(
            LinkRuntimeSettings(
                link_enabled=True,
                port=0,
                bind_host="127.0.0.1",
                device_path=self.folder / "link_devices.json",
                audit_path=self.folder / "link_audit.jsonl",
                relay_path=self.folder / "link_relay.json",
                security_path=self.folder / "link_security.json",
                incident_path=self.folder / "link_incidents.jsonl",
                pairing_host="127.0.0.1",
                shutdown_on_quarantine=True,
            ),
            services=LinkServices(request_safe_shutdown=asked.append),
        )
        self.addCleanup(runtime.stop)
        runtime.start()

        for _ in range(3):
            runtime.monitor.observe(SignalKind.TAMPERING, Principal.relay())
            runtime.monitor.contained = False

        self.assertEqual(asked, ["repeated_containment"])
        self.assertTrue(wait_for(lambda: not runtime.running, timeout=5))

        # And by default it never is.
        quiet: list[str] = []
        default = self.runtime()
        default.monitor.on_safe_shutdown(quiet.append)

        for _ in range(3):
            default.monitor.observe(SignalKind.TAMPERING, Principal.relay())
            default.monitor.contained = False

        self.assertEqual(quiet, [])

    def test_a_corrupt_registry_under_an_open_session_contains_remote_access(self) -> None:
        runtime = self.runtime()
        runtime.start()
        uri = runtime.open_pairing("phone")

        with LinkClient.from_uri(uri, timeout=CLIENT_TIMEOUT) as local:
            self.assertTrue(local.call("ping").ok)
            (self.folder / "link_devices.json").write_text("{ corrupt", encoding="utf-8")

            with self.assertRaises(LinkClientError):
                local.call("ping")

        self.assertTrue(runtime.monitor.contained)
        self.assertEqual(runtime.monitor.contained_reason, "registry_unreadable")



class TestGateOnEveryPath(unittest.TestCase):
    def test_a_remote_scope_from_any_path_must_pass_the_wan_gate(self) -> None:
        # Production never classifies a direct peer as remote; if anything
        # ever did, the connection would still meet the same gate.
        monitor = SecurityMonitor()
        registry = DeviceRegistry(path=None)
        record = registry.activate(registry.create("phone").device_id)
        registry.set_remote_enabled(record.device_id, True)
        audit = AuditLog(path=None)
        server = LinkServer(
            registry=registry,
            audit=audit,
            handlers={LinkOp.PING: ping_handler},
            settings=LinkSettings(
                host="127.0.0.1", port=0, failure_pause=0.0,
                allowed_scopes=frozenset({LinkScope.LOCAL_NETWORK, LinkScope.REMOTE_TUNNEL}),
            ),
            scope_resolver=lambda peer: LinkScope.REMOTE_TUNNEL,
            monitor=monitor,
        )
        server.start()
        self.addCleanup(server.stop, 2.0)
        monitor.observe(SignalKind.TAMPERING, Principal.relay())

        client = LinkClient("127.0.0.1", server.address[1], record.device_id, record.secret, timeout=CLIENT_TIMEOUT)

        with self.assertRaises(LinkClientError):
            client.connect()
            client.call("ping")

        self.assertTrue(
            wait_for(lambda: any(r.reason == RejectReason.REMOTE_NOT_ADMITTED.value for r in audit.records()))
        )


class TestDeviceRates(unittest.TestCase):
    def test_malformed_frames_from_an_authenticated_device_throttle_its_remote_access(self) -> None:
        rig = RemoteRig(handlers={LinkOp.PING: ping_handler})
        self.addCleanup(rig.close)
        record = rig.remote_device()
        threshold = DEFAULT_POLICIES[SignalKind.PROTOCOL_FAULT].threshold
        principal = Principal.device(record.device_id, "remote_tunnel")

        for _ in range(threshold):
            self.assertTrue(rig.wait_parked(record))

            with rig.phone(record) as remote:
                remote._connection.sendall(b"\x00\x00\x00\x05{nope")

                with self.assertRaises(LinkClientError):
                    remote.call("ping")

        self.assertTrue(wait_for(lambda: rig.monitor.throttled(principal)))
        self.assertFalse(rig.monitor.throttled(Principal.device(record.device_id, "local_network")))

        with rig.local_phone(record) as local:
            self.assertTrue(local.call("ping").ok)

    def test_a_device_that_keeps_exhausting_its_budget_is_throttled(self) -> None:
        rig = RemoteRig(
            handlers={LinkOp.PING: ping_handler, LinkOp.STATUS: ok_handler},
            settings=LinkSettings(remote_burst=1, remote_refill_per_second=0.0001),
        )
        self.addCleanup(rig.close)
        record = rig.remote_device()
        threshold = DEFAULT_POLICIES[SignalKind.RATE_LIMITED].threshold
        codes: list[str] = []

        with rig.phone(record) as remote:
            with self.assertRaises(LinkClientError):
                for _ in range(threshold + 2):
                    codes.append(remote.call("status").code)

        # One allowed, then threshold - 1 refusals answered; the refusal that
        # reaches the threshold throttles the device and ends its session
        # before that answer is sent.
        self.assertEqual(codes, [""] + [SessionRule.RATE_LIMITED.value] * (threshold - 1))
        self.assertTrue(rig.monitor.throttled(Principal.device(record.device_id, "remote_tunnel")))
        self.assertTrue(wait_for(lambda: ended(rig, SessionRule.THROTTLED)))

if __name__ == "__main__":
    unittest.main()
