"""
The Security Monitor's policies, state and recovery, with injected clocks.

No sockets here: the link's enforcement of these decisions is tested in
test_link_security_integration.py.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.security_monitor import (
    DEFAULT_POLICIES,
    LAN_RETRY_INTERVAL_SECONDS,
    MAX_LAN_TRACKED,
    MAX_OTHER_TRACKED,
    MAX_TRACKED,
    QUARANTINE_AFTER_CONTAINMENTS,
    Principal,
    PrincipalKind,
    Response,
    SecurityMonitor,
    SecurityMonitorError,
    SignalKind,
    verify_chain,
)


DEVICE = "0123456789abcdef"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class MonitorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.wall = Clock(1_800_000_000.0)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.monitor = self.make()

    def make(self, **kwargs) -> SecurityMonitor:
        return SecurityMonitor(
            state_path=self.folder / "link_security.json",
            incident_path=self.folder / "link_incidents.jsonl",
            clock=self.clock,
            wall=self.wall,
            **kwargs,
        )

    def repeat(self, signal: SignalKind, principal: Principal, times: int):
        decision = None

        for _ in range(times):
            decision = self.monitor.observe(signal, principal)

        return decision


class TestThresholds(MonitorTestCase):
    def test_below_the_threshold_it_only_rejects(self) -> None:
        policy = DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED]
        decision = self.repeat(SignalKind.HANDSHAKE_FAILED, Principal.lan("192.168.1.9"), policy.threshold - 1)

        self.assertIs(decision.response, Response.REJECT)
        self.assertTrue(self.monitor.admit(Principal.lan("192.168.1.9")))

    def test_at_the_threshold_it_throttles(self) -> None:
        policy = DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED]
        decision = self.repeat(SignalKind.HANDSHAKE_FAILED, Principal.lan("192.168.1.9"), policy.threshold)

        self.assertIs(decision.response, Response.THROTTLE)
        self.assertEqual(decision.until, self.clock.now + policy.duration)

    def test_signals_outside_the_window_do_not_count(self) -> None:
        policy = DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED]
        peer = Principal.lan("192.168.1.9")

        for _ in range(policy.threshold - 1):
            self.monitor.observe(SignalKind.HANDSHAKE_FAILED, peer)

        self.clock.now += policy.window + 1

        self.assertIs(self.monitor.observe(SignalKind.HANDSHAKE_FAILED, peer).response, Response.REJECT)

    def test_a_single_tampering_signal_contains_but_does_not_quarantine(self) -> None:
        decision = self.monitor.observe(SignalKind.TAMPERING, Principal.relay())

        self.assertIs(decision.response, Response.CONTAIN)
        self.assertFalse(self.monitor.quarantined)
        self.assertEqual(self.monitor.remote_admission().reason, "security_contained")
        self.assertTrue(self.monitor.link_admission().allowed)


class TestAttribution(MonitorTestCase):
    def test_signals_cannot_be_held_against_the_wrong_kind_of_principal(self) -> None:
        cases = (
            (SignalKind.TAMPERING, Principal.device(DEVICE, "remote_tunnel")),
            (SignalKind.UNAUTHENTICATED_SPLICE, Principal.lan("192.168.1.9")),
            (SignalKind.HANDSHAKE_FAILED, Principal.device(DEVICE, "remote_tunnel")),
            (SignalKind.REQUEST_REFUSED, Principal.relay()),
        )

        for signal, principal in cases:
            with self.subTest(signal=signal, principal=principal.kind):
                with self.assertRaises(SecurityMonitorError):
                    self.monitor.observe(signal, principal)

    def test_an_unproved_device_id_is_not_a_principal(self) -> None:
        for bad in ("../../etc", "ABCDEF0123456789", "short", ""):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    Principal.device(bad, "remote_tunnel")

    def test_a_throttled_relay_does_not_touch_any_lan_address_or_device(self) -> None:
        policy = DEFAULT_POLICIES[SignalKind.UNAUTHENTICATED_SPLICE]
        self.repeat(SignalKind.UNAUTHENTICATED_SPLICE, Principal.relay(), policy.threshold)

        self.assertFalse(self.monitor.admit(Principal.relay()))
        self.assertTrue(self.monitor.admit(Principal.lan("127.0.0.1")))
        self.assertTrue(self.monitor.admit(Principal.device(DEVICE, "local_network")))
        self.assertTrue(self.monitor.admit(Principal.device(DEVICE, "remote_tunnel")))
        self.assertTrue(self.monitor.remote_admission().allowed)

    def test_device_abuse_over_the_relay_does_not_limit_its_lan_use(self) -> None:
        policy = DEFAULT_POLICIES[SignalKind.REQUEST_REFUSED]
        self.repeat(SignalKind.REQUEST_REFUSED, Principal.device(DEVICE, "remote_tunnel"), policy.threshold)

        self.assertFalse(self.monitor.admit(Principal.device(DEVICE, "remote_tunnel")))
        self.assertTrue(self.monitor.admit(Principal.device(DEVICE, "local_network")))

    def test_ipv4_mapped_addresses_are_one_principal(self) -> None:
        self.assertEqual(Principal.lan("::ffff:192.168.1.9"), Principal.lan("192.168.1.9"))
        self.assertEqual(Principal.lan("not an address").ident, "unparsed")


class TestLanSlowDownNotBan(MonitorTestCase):
    def test_a_throttled_lan_address_still_gets_one_attempt_per_interval(self) -> None:
        peer = Principal.lan("192.168.1.9")
        self.repeat(SignalKind.HANDSHAKE_FAILED, peer, DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED].threshold)

        self.assertFalse(self.monitor.admit(peer))

        self.clock.now += LAN_RETRY_INTERVAL_SECONDS
        self.assertTrue(self.monitor.admit(peer))
        self.assertFalse(self.monitor.admit(peer))

    def test_a_throttle_ends_by_itself_and_counting_starts_afresh(self) -> None:
        peer = Principal.lan("192.168.1.9")
        policy = DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED]
        self.repeat(SignalKind.HANDSHAKE_FAILED, peer, policy.threshold)

        self.clock.now += policy.duration

        self.assertTrue(self.monitor.admit(peer))
        self.assertTrue(self.monitor.admit(peer))
        self.assertIs(self.monitor.observe(SignalKind.HANDSHAKE_FAILED, peer).response, Response.REJECT)


class TestLatchesAndRecovery(MonitorTestCase):
    def test_containment_survives_a_restart(self) -> None:
        self.monitor.observe(SignalKind.TAMPERING, Principal.relay())

        again = self.make()

        self.assertTrue(again.contained)
        self.assertFalse(again.remote_admission().allowed)

    def test_an_unreadable_state_file_reads_as_contained(self) -> None:
        (self.folder / "link_security.json").write_text("{ broken", encoding="utf-8")

        again = self.make()

        self.assertEqual(again.remote_admission().reason, "security_contained")
        self.assertEqual(again.contained_reason, "state_unreadable")

    def test_repeated_containment_quarantines_the_link(self) -> None:
        quarantined: list[str] = []
        self.monitor.on_quarantine(quarantined.append)

        for n in range(QUARANTINE_AFTER_CONTAINMENTS):
            decision = self.monitor.observe(SignalKind.TAMPERING, Principal.relay())

            if n < QUARANTINE_AFTER_CONTAINMENTS - 1:
                self.monitor.reset()
                self.wall.now += 60

        self.assertIs(decision.response, Response.QUARANTINE)
        self.assertEqual(quarantined, ["repeated_containment"])
        self.assertFalse(self.monitor.link_admission().allowed)

    def test_containments_far_apart_do_not_escalate(self) -> None:
        for _ in range(QUARANTINE_AFTER_CONTAINMENTS + 2):
            self.monitor.observe(SignalKind.TAMPERING, Principal.relay())
            self.monitor.reset()
            self.wall.now += 25 * 3600

        self.assertFalse(self.monitor.quarantined)

    def test_an_ongoing_incident_counts_once(self) -> None:
        contained: list[str] = []
        self.monitor.on_contain(contained.append)

        for _ in range(10):
            self.monitor.observe(SignalKind.TAMPERING, Principal.relay())

        self.assertEqual(contained, ["tampering"])
        self.assertFalse(self.monitor.quarantined)

    def test_safe_shutdown_is_asked_for_only_when_configured(self) -> None:
        asked: list[str] = []
        monitor = self.make(shutdown_on_quarantine=True)
        monitor.on_safe_shutdown(asked.append)

        for _ in range(QUARANTINE_AFTER_CONTAINMENTS):
            decision = monitor.observe(SignalKind.TAMPERING, Principal.relay())
            monitor.contained = False  # simulate a reset without clearing history

        self.assertIs(decision.response, Response.SAFE_SHUTDOWN)
        self.assertEqual(asked, ["repeated_containment"])

    def test_only_the_console_resets_and_reset_clears_everything(self) -> None:
        resets: list[bool] = []
        self.monitor.on_reset(lambda: resets.append(True))
        self.monitor.observe(SignalKind.TAMPERING, Principal.relay())
        self.repeat(SignalKind.UNAUTHENTICATED_SPLICE, Principal.relay(), 10)

        with self.assertRaises(SecurityMonitorError):
            self.monitor.reset(by="phone")

        self.monitor.reset()

        self.assertTrue(self.monitor.remote_admission().allowed)
        self.assertTrue(self.monitor.admit(Principal.relay()))
        self.assertEqual(resets, [True])
        self.assertFalse(self.make().contained)

    def test_a_failing_enforcer_does_not_stop_the_others(self) -> None:
        reached: list[str] = []

        def broken(reason: str) -> None:
            raise RuntimeError("enforcer failed")

        self.monitor.on_contain(broken)
        self.monitor.on_contain(reached.append)
        self.monitor.observe(SignalKind.TAMPERING, Principal.relay())

        self.assertEqual(reached, ["tampering"])


class TestBoundsAndRecords(MonitorTestCase):
    def test_tracked_principals_are_bounded(self) -> None:
        for n in range(MAX_TRACKED + 300):
            self.monitor.observe(SignalKind.HANDSHAKE_FAILED, Principal.lan(f"10.{n // 65536}.{n // 256 % 256}.{n % 256}"))

        self.assertLessEqual(self.monitor.status()["tracked"], MAX_TRACKED)

    def test_throttles_are_bounded_too(self) -> None:
        threshold = DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED].threshold

        for n in range(MAX_TRACKED + 50):
            peer = Principal.lan(f"10.{n // 65536}.{n // 256 % 256}.{n % 256}")

            for _ in range(threshold):
                self.monitor.observe(SignalKind.HANDSHAKE_FAILED, peer)

        self.assertLessEqual(len(self.monitor._throttles["lan"]), MAX_LAN_TRACKED)
        self.assertLessEqual(len(self.monitor._windows["lan"]), MAX_LAN_TRACKED)
        self.assertLessEqual(self.monitor.status()["tracked"], MAX_TRACKED)

    def test_the_incident_log_is_one_line_per_decision_and_chained(self) -> None:
        peer = Principal.lan("192.168.1.9")

        for _ in range(500):
            self.monitor.observe(SignalKind.HANDSHAKE_FAILED, peer)

        lines = self.monitor.incidents.file_records()

        self.assertEqual(len(lines), 500 // DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED].threshold)
        self.assertTrue(verify_chain(lines))

    def test_an_edited_incident_line_breaks_the_chain(self) -> None:
        self.monitor.observe(SignalKind.TAMPERING, Principal.relay())
        self.monitor.reset()
        lines = [dict(line) for line in self.monitor.incidents.file_records()]
        lines[0]["response"] = "reset"

        self.assertFalse(verify_chain(lines))

    def test_the_chain_continues_across_a_restart(self) -> None:
        self.monitor.observe(SignalKind.TAMPERING, Principal.relay())
        again = self.make()
        again.reset()

        self.assertTrue(verify_chain(again.incidents.file_records()))

    def test_records_hold_no_keys_or_route_tokens(self) -> None:
        self.monitor.observe(SignalKind.TAMPERING, Principal.relay())
        text = (self.folder / "link_incidents.jsonl").read_text(encoding="utf-8")
        state = json.loads((self.folder / "link_security.json").read_text(encoding="utf-8"))

        self.assertNotIn("secret", text)
        self.assertNotIn("route", text)
        self.assertEqual(set(state), {"version", "contained", "contained_reason", "quarantined", "quarantine_reason", "contain_history"})

    def test_every_policy_names_its_principals_and_a_reason(self) -> None:
        for signal in SignalKind:
            with self.subTest(signal=signal):
                policy = DEFAULT_POLICIES[signal]
                self.assertTrue(policy.principals)
                self.assertTrue(policy.why)
                self.assertGreaterEqual(policy.threshold, 1)
                self.assertIn(policy.response, {Response.THROTTLE, Response.CONTAIN})

    def test_only_deterministic_evidence_contains(self) -> None:
        containing = {s for s, p in DEFAULT_POLICIES.items() if p.response is Response.CONTAIN}

        self.assertEqual(containing, {SignalKind.TAMPERING, SignalKind.REGISTRY_UNREADABLE})
        self.assertEqual(
            {k for s in containing for k in DEFAULT_POLICIES[s].principals},
            {PrincipalKind.RELAY, PrincipalKind.SYSTEM},
        )



def lan_address(n: int) -> Principal:
    return Principal.lan(f"10.{n // 65536 % 256}.{n // 256 % 256}.{n % 256}")


class TestLanChurnCannotLiftOtherRestrictions(MonitorTestCase):
    """
    Review finding D9-1: one shared LRU of 1024 let a churn of ~1,030 LAN
    addresses evict a device's and the relay's active throttles early.
    """

    def churn(self, addresses: int) -> None:
        threshold = DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED].threshold

        for n in range(addresses):
            for _ in range(threshold):
                self.monitor.observe(SignalKind.HANDSHAKE_FAILED, lan_address(n))

    def throttle(self, signal: SignalKind, principal: Principal) -> None:
        for _ in range(DEFAULT_POLICIES[signal].threshold):
            self.monitor.observe(signal, principal)

        self.assertTrue(self.monitor.throttled(principal))

    def test_active_device_and_relay_throttles_survive_lan_churn(self) -> None:
        remote = Principal.device(DEVICE, "remote_tunnel")
        local = Principal.device(DEVICE, "local_network")
        relay = Principal.relay()
        self.throttle(SignalKind.REQUEST_REFUSED, remote)
        self.throttle(SignalKind.PROTOCOL_FAULT, local)
        self.throttle(SignalKind.UNAUTHENTICATED_SPLICE, relay)

        self.churn(3 * MAX_LAN_TRACKED)

        for victim in (remote, local, relay):
            with self.subTest(victim=victim.describe()):
                self.assertTrue(self.monitor.throttled(victim))
                self.assertFalse(self.monitor.admit(victim))

        self.assertEqual(len(self.monitor._throttles["other"]), 3)
        self.assertLessEqual(len(self.monitor._throttles["lan"]), MAX_LAN_TRACKED)

    def test_they_still_expire_on_their_own_monotonic_time(self) -> None:
        remote = Principal.device(DEVICE, "remote_tunnel")
        relay = Principal.relay()
        self.throttle(SignalKind.REQUEST_REFUSED, remote)
        self.throttle(SignalKind.UNAUTHENTICATED_SPLICE, relay)
        self.churn(MAX_LAN_TRACKED + 50)

        duration = DEFAULT_POLICIES[SignalKind.REQUEST_REFUSED].duration
        self.assertEqual(duration, DEFAULT_POLICIES[SignalKind.UNAUTHENTICATED_SPLICE].duration)

        self.clock.now += duration - 1
        self.assertFalse(self.monitor.admit(remote))
        self.assertFalse(self.monitor.admit(relay))

        self.clock.now += 1
        self.assertTrue(self.monitor.admit(remote))
        self.assertTrue(self.monitor.admit(relay))

    def test_a_devices_partial_count_survives_lan_churn(self) -> None:
        remote = Principal.device(DEVICE, "remote_tunnel")
        threshold = DEFAULT_POLICIES[SignalKind.REQUEST_REFUSED].threshold

        for _ in range(threshold - 1):
            self.monitor.observe(SignalKind.REQUEST_REFUSED, remote)

        self.churn(MAX_LAN_TRACKED + 50)

        self.assertIs(self.monitor.observe(SignalKind.REQUEST_REFUSED, remote).response, Response.THROTTLE)

    def test_expired_lan_throttles_are_evicted_before_an_active_one(self) -> None:
        self.churn(MAX_LAN_TRACKED - 1)
        self.clock.now += DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED].duration

        fresh = Principal.lan("192.168.1.77")
        self.throttle(SignalKind.HANDSHAKE_FAILED, fresh)
        self.churn_range(MAX_LAN_TRACKED, MAX_LAN_TRACKED + 40)

        self.assertTrue(self.monitor.throttled(fresh))

    def churn_range(self, start: int, stop: int) -> None:
        threshold = DEFAULT_POLICIES[SignalKind.HANDSHAKE_FAILED].threshold

        for n in range(start, stop):
            for _ in range(threshold):
                self.monitor.observe(SignalKind.HANDSHAKE_FAILED, lan_address(n))

    def test_a_throttled_lan_address_keeps_its_retry_slot_under_churn(self) -> None:
        peer = Principal.lan("192.168.1.9")
        self.throttle(SignalKind.HANDSHAKE_FAILED, peer)
        self.churn_range(0, 200)

        self.assertFalse(self.monitor.admit(peer))
        self.clock.now += LAN_RETRY_INTERVAL_SECONDS
        self.assertTrue(self.monitor.admit(peer))

    def test_the_bounds_hold_in_both_tables(self) -> None:
        for n in range(MAX_OTHER_TRACKED + 40):
            device = f"{n:016x}"
            self.monitor.observe(SignalKind.REQUEST_REFUSED, Principal.device(device, "remote_tunnel"))

        self.churn(MAX_LAN_TRACKED + 40)

        self.assertLessEqual(len(self.monitor._windows["other"]), MAX_OTHER_TRACKED)
        self.assertLessEqual(len(self.monitor._windows["lan"]), MAX_LAN_TRACKED)
        self.assertLessEqual(self.monitor.status()["tracked"], MAX_TRACKED)
        self.assertEqual(MAX_TRACKED, MAX_LAN_TRACKED + MAX_OTHER_TRACKED)

    def test_reset_clears_both_tables(self) -> None:
        self.throttle(SignalKind.UNAUTHENTICATED_SPLICE, Principal.relay())
        self.churn(20)
        self.monitor.reset()

        self.assertEqual(self.monitor.status()["tracked"], 0)
        self.assertEqual(self.monitor.status()["throttled"], [])

if __name__ == "__main__":
    unittest.main()
