from __future__ import annotations

import hashlib
import hmac
import unittest

from core.link_relay_wire import (
    HELLO_BYTES,
    MAGIC,
    ROUTE_ID_LABEL,
    ROUTE_KEY_LABEL,
    RelayHello,
    RelayRole,
    RelayStatus,
    RelayWireError,
    decode_hello,
    decode_status,
    host_hello,
    phone_hello,
    route_id_for_key,
    route_id_for_secret,
    route_key_for_secret,
)


SECRET = bytes(range(32))
OTHER_SECRET = bytes(range(1, 33))


class TestRoutes(unittest.TestCase):
    def test_both_ends_of_one_device_meet_on_one_route(self) -> None:
        self.assertEqual(host_hello(SECRET).route_id, phone_hello(SECRET).route_id)

    def test_different_devices_get_different_routes(self) -> None:
        self.assertNotEqual(route_id_for_secret(SECRET), route_id_for_secret(OTHER_SECRET))

    def test_the_route_key_is_an_hmac_of_the_pairing_key(self) -> None:
        # Stated as the computation itself because the phone app must make the
        # same one; a relabelled or reordered HMAC would strand every phone.
        expected = hmac.new(SECRET, ROUTE_KEY_LABEL, hashlib.sha256).digest()

        self.assertEqual(route_key_for_secret(SECRET), expected)
        self.assertEqual(
            route_id_for_key(expected),
            hashlib.sha256(ROUTE_ID_LABEL + expected).digest(),
        )

    def test_the_relay_never_sees_the_pairing_key(self) -> None:
        for hello in (host_hello(SECRET), phone_hello(SECRET)):
            encoded = hello.encode()

            self.assertNotIn(SECRET, encoded)

    def test_the_phone_never_sends_the_host_credential(self) -> None:
        # Knowing a route id must not be enough to park as the PC, so the
        # phone's hello carries the id and never the key it was hashed from.
        self.assertNotIn(route_key_for_secret(SECRET), phone_hello(SECRET).encode())

    def test_a_host_is_filed_under_the_hash_of_what_it_presented(self) -> None:
        # So presenting a route id as a host key files the host somewhere the
        # phone will never look: a phone's hello cannot be replayed as a host.
        stolen_id = phone_hello(SECRET).token
        squatter = RelayHello(RelayRole.HOST, stolen_id)

        self.assertNotEqual(squatter.route_id, phone_hello(SECRET).route_id)

    def test_an_empty_key_has_no_route(self) -> None:
        with self.assertRaises(ValueError):
            route_key_for_secret(b"")


class TestHello(unittest.TestCase):
    def test_a_hello_is_thirty_seven_bytes(self) -> None:
        self.assertEqual(HELLO_BYTES, 37)
        self.assertEqual(len(host_hello(SECRET).encode()), HELLO_BYTES)
        self.assertEqual(len(phone_hello(SECRET).encode()), HELLO_BYTES)

    def test_round_trip(self) -> None:
        for hello in (host_hello(SECRET), phone_hello(SECRET)):
            with self.subTest(role=hello.role):
                self.assertEqual(decode_hello(hello.encode()), hello)

    def test_layout(self) -> None:
        encoded = phone_hello(SECRET).encode()

        self.assertEqual(encoded[:4], MAGIC)
        self.assertEqual(encoded[4], RelayRole.PHONE.value)
        self.assertEqual(encoded[5:], route_id_for_secret(SECRET))

    def test_wrong_magic_is_refused(self) -> None:
        encoded = bytearray(phone_hello(SECRET).encode())
        encoded[0] ^= 0xFF

        with self.assertRaises(RelayWireError):
            decode_hello(bytes(encoded))

    def test_unknown_role_is_refused(self) -> None:
        encoded = bytearray(phone_hello(SECRET).encode())
        encoded[4] = 9

        with self.assertRaises(RelayWireError):
            decode_hello(bytes(encoded))

    def test_wrong_length_is_refused_not_truncated(self) -> None:
        encoded = phone_hello(SECRET).encode()

        for raw in (encoded[:-1], encoded + b"\x00", b""):
            with self.subTest(length=len(raw)):
                with self.assertRaises(RelayWireError):
                    decode_hello(raw)

    def test_a_short_token_is_refused(self) -> None:
        with self.assertRaises(RelayWireError):
            RelayHello(RelayRole.PHONE, b"\x00" * 31)

    def test_repr_hides_the_token(self) -> None:
        hello = host_hello(SECRET)

        self.assertNotIn(hello.token.hex(), repr(hello))
        self.assertIn("redacted", repr(hello))


class TestStatus(unittest.TestCase):
    def test_progress_and_endings_are_separated_by_value(self) -> None:
        progress = {RelayStatus.PARKED, RelayStatus.SPLICED, RelayStatus.KEEPALIVE}

        for status in RelayStatus:
            with self.subTest(status=status):
                self.assertEqual(status.is_terminal, status not in progress)

    def test_unknown_bytes_decode_to_none(self) -> None:
        # The client treats None as the end, so a newer relay cannot keep an
        # older client waiting on a byte it does not understand.
        self.assertIsNone(decode_status(0x7F))
        self.assertIs(decode_status(0x01), RelayStatus.SPLICED)


if __name__ == "__main__":
    unittest.main()
