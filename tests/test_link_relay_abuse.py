"""
What a hostile relay can make the PC do, and the limits on it.

Review finding L3: a relay answering every parking with SPLICED and a hang-up
made the PC redial 374 times in three seconds, with one audit line each, and
those connections competed with the phone on the local network. These tests
pin the repairs, on loopback with a deliberately misbehaving relay and fresh
test-only keys:

    a splice is a success only once a phone authenticated; otherwise backoff;
    each device has a splice quota;
    an "expiry" straight after parking, or a stream of keepalives, is
    misbehaviour, answered with backoff;
    relayed connections have their own slots and cannot crowd out the LAN;
    and the audit log stays small however long the abuse goes on.

Where a test says "production settings" it uses the shipped TunnelSettings
defaults, because that is what the finding was measured with.
"""

from __future__ import annotations

import socket
import time
import unittest

from core.link_audit import AuditEvent
from core.link_capability import LinkOp, LinkScope
from core.link_handlers import ping_handler
from core.link_server import LinkSettings, RejectReason
from core.link_tunnel import RelayFailure, TunnelSettings
from core.security_monitor import (
    DEFAULT_POLICIES,
    Policy,
    Principal,
    SecurityMonitor,
    SignalKind,
)
from tests.fixtures.link_harness import ok_handler, wait_for
from tests.fixtures.link_hostile_relay import HostileRelay
from tests.fixtures.link_remote_rig import RemoteRig


def failures(rig: RemoteRig) -> list[str]:
    return [r.reason for r in rig.audit.records() if r.event is AuditEvent.RELAY_FAILED]


class AbuseTestCase(unittest.TestCase):
    def rig(
        self,
        behaviour: str,
        tunnel: TunnelSettings,
        settings: LinkSettings | None = None,
        monitor: SecurityMonitor | None = None,
    ) -> tuple[RemoteRig, HostileRelay]:
        relay = HostileRelay(behaviour)
        rig = RemoteRig(
            handlers={LinkOp.PING: ping_handler, LinkOp.STATUS: ok_handler},
            relay=relay,
            endpoint=relay.endpoint,
            connector=HostileRelay.connector,
            tunnel=tunnel,
            settings=settings,
            monitor=monitor,
        )
        self.addCleanup(relay.close)
        self.addCleanup(rig.close)

        record = rig.registry.create("phone")
        rig.registry.activate(record.device_id)
        rig.registry.set_remote_enabled(record.device_id, True, expires_at=rig.clock() + 3600)
        rig.dialler.refresh()
        self.record = rig.registry.get(record.device_id)

        return rig, relay


class TestUnauthenticatedSplices(AbuseTestCase):
    def test_the_l3_flood_is_throttled_with_production_settings(self) -> None:
        rig, relay = self.rig("splice_hangup", TunnelSettings())

        time.sleep(3.0)

        # Backoff starts at about a second and doubles. Before the repair this
        # was 312-374 connections and as many audit lines.
        self.assertLessEqual(relay.connections, 6)
        self.assertLessEqual(len(rig.audit.records()), 10)
        self.assertEqual(failures(rig), [RelayFailure.UNAUTHENTICATED_SPLICE.value])

    def test_the_monitor_throttles_the_relay_and_the_lan_phone_is_still_served(self) -> None:
        rig, relay = self.rig("splice_hangup", TunnelSettings(backoff_initial=0.01, backoff_max=0.02))
        threshold = DEFAULT_POLICIES[SignalKind.UNAUTHENTICATED_SPLICE].threshold

        self.assertTrue(wait_for(lambda: rig.monitor.throttled(Principal.relay()), timeout=10))
        settled = relay.connections
        time.sleep(0.5)

        # Throttled: the dialler stopped dialling the relay.
        self.assertGreaterEqual(settled, threshold)
        self.assertLessEqual(relay.connections, settled + 1)

        # The relay is not the phone: neither its LAN address nor the device
        # it was parked for is held to account for what the relay did.
        self.assertFalse(rig.monitor.throttled(Principal.device(self.record.device_id, "remote_tunnel")))
        self.assertTrue(rig.monitor.remote_admission().allowed)

        with rig.local_phone(self.record) as phone:
            self.assertTrue(phone.call("ping").ok)

    def test_a_splice_held_open_without_a_handshake_is_waited_for_then_failed(self) -> None:
        rig, relay = self.rig(
            "splice_and_hold",
            TunnelSettings(handshake_wait=2.0, backoff_initial=5.0, backoff_max=10.0),
            settings=LinkSettings(handshake_timeout=0.5),
        )

        self.assertTrue(wait_for(lambda: failures(rig) != [], timeout=5))

        # One splice, one verdict, then a backoff of seconds - not a redial.
        self.assertEqual(relay.connections, 1)
        self.assertEqual(failures(rig), [RelayFailure.UNAUTHENTICATED_SPLICE.value])


class TestSpliceQuota(AbuseTestCase):
    def test_a_device_is_handed_only_its_quota_of_splices(self) -> None:
        # The monitor's relay thresholds are raised here so this test sees the
        # dialler's own quota and nothing else.
        lenient = {
            signal: Policy(policy.signal, policy.principals, 10_000, policy.window, policy.response, policy.duration, policy.why)
            for signal, policy in DEFAULT_POLICIES.items()
        }
        rig, relay = self.rig(
            "splice_hangup",
            TunnelSettings(
                backoff_initial=0.01,
                backoff_max=0.02,
                splice_burst=3,
                splice_refill_per_second=0.0001,
            ),
            monitor=SecurityMonitor(policies=lenient),
        )

        self.assertTrue(wait_for(lambda: relay.connections >= 15, timeout=10))

        # The relay kept splicing; the server was handed three of them. The
        # handshake refusals are coalesced in the file, but counted.
        self.assertTrue(wait_for(lambda: rig.audit.count(AuditEvent.HANDSHAKE_REFUSED) == 3))
        time.sleep(0.3)
        self.assertEqual(rig.audit.count(AuditEvent.HANDSHAKE_REFUSED), 3)


class TestRelayMisbehaviour(AbuseTestCase):
    def test_an_expiry_straight_after_parking_is_not_a_renewal(self) -> None:
        rig, relay = self.rig("expire_at_once", TunnelSettings())

        time.sleep(3.0)

        self.assertLessEqual(relay.connections, 6)
        self.assertEqual(failures(rig), [RelayFailure.MISBEHAVED.value])

    def test_a_stream_of_keepalives_is_left(self) -> None:
        rig, relay = self.rig("keepalive_flood", TunnelSettings())

        self.assertTrue(wait_for(lambda: failures(rig) != [], timeout=5))
        time.sleep(1.0)

        self.assertEqual(failures(rig), [RelayFailure.MISBEHAVED.value])
        self.assertLessEqual(relay.connections, 3)


class TestSeparateSlots(unittest.TestCase):
    def test_relayed_connections_cannot_take_the_lan_slots(self) -> None:
        rig = RemoteRig(
            handlers={LinkOp.PING: ping_handler},
            settings=LinkSettings(handshake_timeout=5.0, max_connections=2, max_relay_connections=2),
        )
        self.addCleanup(rig.close)
        record = rig.remote_device()
        stalled: list[socket.socket] = []

        # Two relayed connections that never start a handshake fill the relay
        # slots; a third is refused.
        for _ in range(3):
            pc_end, phone_end = socket.socketpair()
            stalled.append(phone_end)
            self.addCleanup(phone_end.close)
            rig.server.serve_relayed(pc_end, "127.0.0.1")

        self.assertTrue(
            wait_for(
                lambda: any(
                    r.event is AuditEvent.CONNECTION_REJECTED
                    and r.scope is LinkScope.REMOTE_TUNNEL
                    and r.reason == RejectReason.TOO_MANY_CONNECTIONS.value
                    for r in rig.audit.records()
                )
            )
        )

        # The phone on the sofa still gets in, on both LAN slots.
        with rig.local_phone(record) as first, rig.local_phone(record) as second:
            self.assertTrue(first.call("ping").ok)
            self.assertTrue(second.call("ping").ok)

    def test_a_relayed_connection_is_told_whether_it_authenticated(self) -> None:
        rig = RemoteRig(handlers={LinkOp.PING: ping_handler}, settings=LinkSettings(handshake_timeout=1.0))
        self.addCleanup(rig.close)
        verdicts: list[bool] = []

        pc_end, phone_end = socket.socketpair()
        self.addCleanup(phone_end.close)
        rig.server.serve_relayed(pc_end, "127.0.0.1", verdicts.append)
        phone_end.sendall(b"\x16\x03\x01not a client hello")

        self.assertTrue(wait_for(lambda: verdicts == [False], timeout=5))


if __name__ == "__main__":
    unittest.main()
