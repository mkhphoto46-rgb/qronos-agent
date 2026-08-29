from __future__ import annotations

import unittest

from core.link_devices import SECRET_BYTES, DeviceRegistry, DeviceStatus
from core.link_pairing import (
    PAIRING_WINDOW_SECONDS,
    PairingPayload,
    PairingPayloadError,
    PairingRefusal,
    PairingService,
    local_address,
    choose_local_address,
)


START = 1_800_000_000.0
LAN = "192.168.1.42"
PUBLIC = "8.8.8.8"


class FakeClock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def payload(**overrides: object) -> PairingPayload:
    fields: dict[str, object] = {
        "host": "192.168.1.10",
        "port": 47_711,
        "device_id": "0123456789abcdef",
        "secret": b"k" * SECRET_BYTES,
        "expires_at": START + 120,
    }
    fields.update(overrides)

    return PairingPayload(**fields)  # type: ignore[arg-type]


class TestPayload(unittest.TestCase):
    def test_a_payload_survives_the_qr_round_trip(self) -> None:
        original = payload()

        restored = PairingPayload.from_uri(original.to_uri())

        self.assertEqual(restored.host, original.host)
        self.assertEqual(restored.port, original.port)
        self.assertEqual(restored.device_id, original.device_id)
        self.assertEqual(restored.secret, original.secret)

    def test_the_uri_uses_the_qronos_scheme(self) -> None:
        self.assertTrue(payload().to_uri().startswith("qronos://pair?"))

    def test_the_uri_stays_small_enough_for_a_qr_code(self) -> None:
        # Version 4 QR at medium correction holds around 200 characters; this
        # needs to stay comfortably inside a code a phone can read quickly.
        self.assertLess(len(payload().to_uri()), 200)

    def test_the_key_never_appears_in_the_repr(self) -> None:
        text = repr(payload())

        self.assertNotIn(("k" * SECRET_BYTES).encode().hex(), text)
        self.assertIn("redacted", text)

    def test_a_key_with_url_unsafe_bytes_survives(self) -> None:
        # Base64url, so + and / cannot appear and no escaping is needed.
        original = payload(secret=bytes(range(SECRET_BYTES)))

        self.assertEqual(
            PairingPayload.from_uri(original.to_uri()).secret,
            original.secret,
        )


class TestPayloadRefusals(unittest.TestCase):
    def refuses(self, uri: str) -> None:
        with self.assertRaises(PairingPayloadError):
            PairingPayload.from_uri(uri)

    def test_the_wrong_scheme_is_refused(self) -> None:
        self.refuses(payload().to_uri().replace("qronos://", "https://"))

    def test_the_wrong_action_is_refused(self) -> None:
        self.refuses(payload().to_uri().replace("//pair?", "//connect?"))

    def test_a_future_payload_version_is_refused(self) -> None:
        # Better to say "update the app" than to guess at fields that moved.
        self.refuses(payload().to_uri().replace("v=1", "v=2"))

    def test_a_missing_field_is_refused(self) -> None:
        uri = payload().to_uri()

        self.refuses(uri.split("&k=")[0])

    def test_a_short_key_is_refused(self) -> None:
        self.refuses(payload().to_uri().split("&k=")[0] + "&k=AAAA&e=0")

    def test_a_malformed_key_is_refused(self) -> None:
        uri = payload().to_uri()
        head, _, tail = uri.partition("&k=")
        rest = tail.split("&", 1)[1]

        self.refuses(f"{head}&k=!!!not-base64!!!&{rest}")

    def test_a_bad_device_id_is_refused(self) -> None:
        self.refuses(payload().to_uri().replace("d=0123456789abcdef", "d=nope"))

    def test_a_non_numeric_port_is_refused(self) -> None:
        self.refuses(payload().to_uri().replace("p=47711", "p=http"))

    def test_an_out_of_range_port_is_refused(self) -> None:
        self.refuses(payload().to_uri().replace("p=47711", "p=70000"))

    def test_an_empty_host_is_refused(self) -> None:
        self.refuses(payload().to_uri().replace("h=192.168.1.10", "h="))

    def test_rubbish_is_refused(self) -> None:
        for text in ("", "hello", "qronos://", "qronos://pair", "://pair?v=1"):
            with self.subTest(text=text):
                self.refuses(text)

    def test_a_duplicated_field_is_refused(self) -> None:
        # Two values for one field is ambiguous; ambiguity in a pairing payload
        # is not something to resolve by picking one.
        self.refuses(payload().to_uri() + "&d=ffffffffffffffff")


class PairingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.registry = DeviceRegistry(path=None, clock=self.clock)
        self.service = PairingService(
            self.registry,
            host="192.168.1.10",
            port=47_711,
            clock=self.clock,
        )


class TestWindow(PairingTestCase):
    def test_no_window_is_open_to_begin_with(self) -> None:
        self.assertFalse(self.service.is_open)

    def test_opening_a_window_creates_a_pending_device(self) -> None:
        issued = self.service.open("phone")

        record = self.registry.get(issued.device_id)

        assert record is not None
        self.assertIs(record.status, DeviceStatus.PENDING)
        self.assertTrue(self.service.is_open)

    def test_the_payload_carries_the_device_key(self) -> None:
        issued = self.service.open("phone")

        record = self.registry.get(issued.device_id)

        assert record is not None
        self.assertEqual(issued.secret, record.secret)

    def test_the_window_expires(self) -> None:
        self.service.open("phone")

        self.clock.advance(PAIRING_WINDOW_SECONDS + 1)

        self.assertFalse(self.service.is_open)

    def test_seconds_remaining_counts_down(self) -> None:
        self.service.open("phone")

        self.clock.advance(20)

        self.assertAlmostEqual(
            self.service.seconds_remaining(), PAIRING_WINDOW_SECONDS - 20
        )

    def test_seconds_remaining_never_goes_negative(self) -> None:
        self.service.open("phone")
        self.clock.advance(PAIRING_WINDOW_SECONDS * 2)

        self.assertEqual(self.service.seconds_remaining(), 0.0)

    def test_cancelling_discards_the_pending_device(self) -> None:
        # A device that was never paired should not linger in the registry.
        issued = self.service.open("phone")

        self.service.cancel()

        self.assertIsNone(self.registry.get(issued.device_id))
        self.assertFalse(self.service.is_open)

    def test_opening_a_second_window_cancels_the_first(self) -> None:
        # So a window forgotten on screen cannot be completed later by
        # whoever finds it.
        first = self.service.open("first phone")

        second = self.service.open("second phone")

        self.assertIsNone(self.registry.get(first.device_id))
        self.assertIsNotNone(self.registry.get(second.device_id))
        self.assertEqual(len(self.registry), 1)

    def test_expire_if_due_closes_an_expired_window(self) -> None:
        issued = self.service.open("phone")
        self.clock.advance(PAIRING_WINDOW_SECONDS + 1)

        self.assertTrue(self.service.expire_if_due())
        self.assertIsNone(self.registry.get(issued.device_id))

    def test_expire_if_due_leaves_a_live_window_alone(self) -> None:
        self.service.open("phone")

        self.assertFalse(self.service.expire_if_due())
        self.assertTrue(self.service.is_open)

    def test_cancelling_does_not_undo_a_completed_pairing(self) -> None:
        issued = self.service.open("phone")
        self.service.complete(issued.device_id, LAN)

        self.service.cancel()

        record = self.registry.get(issued.device_id)

        assert record is not None
        self.assertIs(record.status, DeviceStatus.ACTIVE)


class TestCompletion(PairingTestCase):
    def test_a_local_device_pairs(self) -> None:
        issued = self.service.open("phone")

        outcome = self.service.complete(issued.device_id, LAN)

        self.assertTrue(outcome.accepted)
        assert outcome.record is not None
        self.assertIs(outcome.record.status, DeviceStatus.ACTIVE)

    def test_pairing_from_the_internet_is_refused(self) -> None:
        # The rule that makes Layer 2's untrusted relay acceptable: a hostile
        # relay cannot enrol a phone, only carry traffic for one paired in the
        # room.
        issued = self.service.open("phone")

        outcome = self.service.complete(issued.device_id, PUBLIC)

        self.assertFalse(outcome.accepted)
        self.assertIs(outcome.refusal, PairingRefusal.NOT_LOCAL_NETWORK)

    def test_pairing_from_an_unparseable_address_is_refused(self) -> None:
        issued = self.service.open("phone")

        outcome = self.service.complete(issued.device_id, "somewhere")

        self.assertIs(outcome.refusal, PairingRefusal.NOT_LOCAL_NETWORK)

    def test_a_window_cannot_be_reused(self) -> None:
        issued = self.service.open("phone")
        self.service.complete(issued.device_id, LAN)

        outcome = self.service.complete(issued.device_id, LAN)

        self.assertIs(outcome.refusal, PairingRefusal.ALREADY_USED)

    def test_completing_with_no_window_is_refused(self) -> None:
        outcome = self.service.complete("0123456789abcdef", LAN)

        self.assertIs(outcome.refusal, PairingRefusal.NO_WINDOW)

    def test_an_expired_window_is_refused(self) -> None:
        issued = self.service.open("phone")
        self.clock.advance(PAIRING_WINDOW_SECONDS + 1)

        outcome = self.service.complete(issued.device_id, LAN)

        self.assertIs(outcome.refusal, PairingRefusal.EXPIRED)

    def test_an_expired_window_is_also_closed(self) -> None:
        issued = self.service.open("phone")
        self.clock.advance(PAIRING_WINDOW_SECONDS + 1)

        self.service.complete(issued.device_id, LAN)

        self.assertIsNone(self.registry.get(issued.device_id))

    def test_a_different_device_cannot_use_the_window(self) -> None:
        self.service.open("phone")

        outcome = self.service.complete("ffffffffffffffff", LAN)

        self.assertIs(outcome.refusal, PairingRefusal.WRONG_DEVICE)

    def test_a_device_already_active_is_refused(self) -> None:
        issued = self.service.open("phone")
        self.registry.activate(issued.device_id)

        outcome = self.service.complete(issued.device_id, LAN)

        self.assertIs(outcome.refusal, PairingRefusal.NOT_PENDING)

    def test_a_device_removed_under_the_window_is_refused(self) -> None:
        issued = self.service.open("phone")
        self.registry.remove(issued.device_id)

        outcome = self.service.complete(issued.device_id, LAN)

        self.assertIs(outcome.refusal, PairingRefusal.UNKNOWN_DEVICE)

    def test_the_no_window_check_comes_before_anything_about_the_device(
        self,
    ) -> None:
        # Checks run in the order that reveals least.
        outcome = self.service.complete("not-a-device-id", PUBLIC)

        self.assertIs(outcome.refusal, PairingRefusal.NO_WINDOW)

    def test_an_outcome_describes_itself(self) -> None:
        issued = self.service.open("Amin's phone")

        accepted = self.service.complete(issued.device_id, LAN)
        self.assertIn("Amin's phone", accepted.describe())

        refused = self.service.complete(issued.device_id, LAN)
        self.assertIn("already_used", refused.describe())


class TestLocalAddress(unittest.TestCase):
    def test_physical_lan_wins_over_the_vpn_default_route(self) -> None:
        selected = choose_local_address(
            {
                "Ethernet": ("192.168.0.3",),
                "HssStore 127": ("10.235.60.60",),
                "WeOnlyDo": ("169.254.14.134",),
            },
            routed_address="10.235.60.60",
        )

        self.assertEqual(selected, "192.168.0.3")

    def test_a_down_or_missing_lan_fails_visibly_to_loopback(self) -> None:
        selected = choose_local_address(
            {"VPN": ("10.1.2.3",), "Link local": ("169.254.1.2",)},
            routed_address="10.1.2.3",
        )

        self.assertEqual(selected, "127.0.0.1")

    def test_it_returns_something_that_looks_like_an_address(self) -> None:
        import ipaddress

        # No assertion about which address: it depends on the machine. Only
        # that it is one, so the QR code cannot carry rubbish.
        ipaddress.ip_address(local_address())


if __name__ == "__main__":
    unittest.main()
