from __future__ import annotations

import getpass
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.link_capability import Capability
from core.link_devices import (
    SECRET_BYTES,
    _PERMITTED_ACL_PRINCIPALS,
    DeviceRecord,
    DeviceRegistry,
    DeviceStatus,
    DeviceStoreCorrupt,
    SecretFileNotProtected,
    UnknownDevice,
    _acl_principals,
    is_valid_device_id,
    new_device_id,
    new_secret,
)


START = 1_800_000_000.0


class FakeClock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.registry = DeviceRegistry(path=None, clock=self.clock)


class TestIdentities(unittest.TestCase):
    def test_a_generated_id_is_valid(self) -> None:
        self.assertTrue(is_valid_device_id(new_device_id()))

    def test_a_generated_id_is_sixteen_hex_characters(self) -> None:
        self.assertEqual(len(new_device_id()), 16)

    def test_a_generated_key_is_the_documented_length(self) -> None:
        self.assertEqual(len(new_secret()), SECRET_BYTES)

    def test_two_generated_keys_differ(self) -> None:
        self.assertNotEqual(new_secret(), new_secret())

    def test_malformed_identities_are_rejected(self) -> None:
        # The identity in a handshake comes from the peer, so its shape is
        # checked before it is used as a key or written to a log.
        for candidate in (
            "",
            "short",
            "ABCDEF0123456789",          # uppercase
            "0123456789abcdefg",         # too long
            "0123456789abcde",           # too short
            "../../etc/passwd",
            "0123456789abcde\n",
            "0123456789abcdez",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(is_valid_device_id(candidate))


class TestRecords(RegistryTestCase):
    def test_a_new_device_is_pending(self) -> None:
        record = self.registry.create("phone")

        self.assertIs(record.status, DeviceStatus.PENDING)

    def test_a_pending_device_may_authenticate(self) -> None:
        # Which is how pairing completes: the first handshake proves the key.
        record = self.registry.create("phone")

        self.assertTrue(record.can_authenticate)

    def test_a_revoked_device_may_not_authenticate(self) -> None:
        record = self.registry.create("phone")
        revoked = self.registry.revoke(record.device_id)

        self.assertFalse(revoked.can_authenticate)

    def test_the_key_never_appears_in_the_repr(self) -> None:
        # A stray debug print, a log line, or an exception traceback showing a
        # record must not leak the key.
        record = self.registry.create("phone")

        self.assertNotIn(record.secret.hex(), repr(record))
        self.assertNotIn(str(record.secret), repr(record))
        self.assertIn("redacted", repr(record))

    def test_the_key_never_appears_in_a_description(self) -> None:
        record = self.registry.create("phone")

        self.assertNotIn(record.secret.hex(), record.describe())

    def test_a_description_names_the_device_and_its_state(self) -> None:
        record = self.registry.create("Amin's phone")
        text = record.describe()

        self.assertIn("Amin's phone", text)
        self.assertIn(record.device_id, text)
        self.assertIn("pending", text)
        self.assertIn("never", text)


class TestRegistryQueries(RegistryTestCase):
    def test_an_unknown_device_resolves_to_nothing(self) -> None:
        self.assertIsNone(self.registry.get("0" * 16))
        self.assertIsNone(self.registry.secret_for("0" * 16))

    def test_a_pending_device_resolves_a_key(self) -> None:
        record = self.registry.create("phone")

        self.assertEqual(
            self.registry.secret_for(record.device_id), record.secret
        )

    def test_a_revoked_device_resolves_no_key(self) -> None:
        # This is the whole of revocation. The handshake fails because there is
        # no key to complete it with, so there is no window in which a revoked
        # phone holds an open session.
        record = self.registry.create("phone")
        self.registry.revoke(record.device_id)

        self.assertIsNone(self.registry.secret_for(record.device_id))

    def test_a_malformed_identity_resolves_no_key(self) -> None:
        self.assertIsNone(self.registry.secret_for("../../etc/passwd"))
        self.assertIsNone(self.registry.secret_for(""))

    def test_devices_are_listed_in_creation_order(self) -> None:
        first = self.registry.create("first")
        self.clock.advance(10)
        second = self.registry.create("second")

        self.assertEqual(
            [record.device_id for record in self.registry.all()],
            [first.device_id, second.device_id],
        )

    def test_devices_can_be_filtered_by_status(self) -> None:
        active = self.registry.create("active")
        self.registry.activate(active.device_id)
        self.registry.create("pending")

        self.assertEqual(
            len(self.registry.with_status(DeviceStatus.ACTIVE)), 1
        )
        self.assertEqual(
            len(self.registry.with_status(DeviceStatus.PENDING)), 1
        )


class TestRegistryChanges(RegistryTestCase):
    def test_activation_records_the_time(self) -> None:
        record = self.registry.create("phone")
        self.clock.advance(30)

        activated = self.registry.activate(record.device_id)

        self.assertIs(activated.status, DeviceStatus.ACTIVE)
        self.assertEqual(activated.last_seen_at, START + 30)

    def test_a_revoked_device_cannot_be_activated(self) -> None:
        # Coming back must go through pairing again, deliberately, at the PC.
        record = self.registry.create("phone")
        self.registry.revoke(record.device_id)

        with self.assertRaises(ValueError):
            self.registry.activate(record.device_id)

    def test_renaming_does_not_change_the_identity(self) -> None:
        record = self.registry.create("old name")

        renamed = self.registry.rename(record.device_id, "new name")

        self.assertEqual(renamed.device_id, record.device_id)
        self.assertEqual(renamed.name, "new name")

    def test_renaming_does_not_change_the_key(self) -> None:
        record = self.registry.create("old name")

        renamed = self.registry.rename(record.device_id, "new name")

        self.assertEqual(renamed.secret, record.secret)

    def test_touch_updates_only_the_last_seen_time(self) -> None:
        record = self.registry.create("phone")
        self.registry.activate(record.device_id)
        self.clock.advance(600)

        touched = self.registry.touch(record.device_id)

        self.assertEqual(touched.last_seen_at, START + 600)
        self.assertIs(touched.status, DeviceStatus.ACTIVE)

    def test_remote_access_is_off_for_a_new_device(self) -> None:
        # Layer 2 is opt-in per device, never a global switch.
        self.assertFalse(self.registry.create("phone").remote_enabled)

    def test_remote_access_can_be_enabled_for_one_device(self) -> None:
        record = self.registry.create("phone")

        updated = self.registry.set_remote_enabled(record.device_id, True)

        self.assertTrue(updated.remote_enabled)

    def test_grants_default_to_everything_the_scope_allows(self) -> None:
        self.assertIsNone(self.registry.create("phone").grants)

    def test_grants_can_be_narrowed_and_cleared(self) -> None:
        record = self.registry.create("phone")

        narrowed = self.registry.set_grants(
            record.device_id, {Capability.ASK}
        )
        self.assertEqual(narrowed.grants, frozenset({Capability.ASK}))

        cleared = self.registry.set_grants(record.device_id, None)
        self.assertIsNone(cleared.grants)

    def test_removing_a_device_forgets_it(self) -> None:
        record = self.registry.create("phone")

        self.registry.remove(record.device_id)

        self.assertIsNone(self.registry.get(record.device_id))
        self.assertEqual(len(self.registry), 0)

    def test_operations_on_an_unknown_device_raise(self) -> None:
        for action in (
            lambda: self.registry.revoke("0" * 16),
            lambda: self.registry.rename("0" * 16, "x"),
            lambda: self.registry.touch("0" * 16),
            lambda: self.registry.remove("0" * 16),
            lambda: self.registry.activate("0" * 16),
        ):
            with self.subTest(action=action):
                with self.assertRaises(UnknownDevice):
                    action()


class TestPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "link_devices.json"
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def registry(self) -> DeviceRegistry:
        return DeviceRegistry(path=self.path, clock=self.clock)

    def test_a_missing_file_is_an_empty_registry(self) -> None:
        self.assertEqual(len(self.registry()), 0)

    def test_devices_survive_a_restart(self) -> None:
        first = self.registry()
        record = first.create("phone")
        first.activate(record.device_id)

        second = self.registry()
        restored = second.get(record.device_id)

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.secret, record.secret)
        self.assertIs(restored.status, DeviceStatus.ACTIVE)
        self.assertEqual(restored.name, "phone")

    def test_grants_survive_a_restart(self) -> None:
        first = self.registry()
        record = first.create("phone")
        first.set_grants(record.device_id, {Capability.ASK, Capability.SEARCH_WEB})

        restored = self.registry().get(record.device_id)

        assert restored is not None
        self.assertEqual(
            restored.grants,
            frozenset({Capability.ASK, Capability.SEARCH_WEB}),
        )

    def test_revocation_survives_a_restart(self) -> None:
        first = self.registry()
        record = first.create("phone")
        first.revoke(record.device_id)

        self.assertIsNone(self.registry().secret_for(record.device_id))

    @unittest.skipIf(os.name == "nt", "POSIX modes; Windows is checked below")
    def test_the_file_is_not_world_readable(self) -> None:
        registry = self.registry()
        registry.create("phone")

        mode = self.path.stat().st_mode & 0o777

        self.assertEqual(mode & 0o077, 0, oct(mode))

    @unittest.skipUnless(os.name == "nt", "Windows access-control lists")
    def test_the_file_is_reachable_only_by_its_owner(self) -> None:
        # The Windows half of the rule above. Not a mode check: Windows keeps
        # reporting 0666 from stat no matter what the real permissions are,
        # which is exactly why asserting the mode here proved nothing.
        registry = self.registry()
        registry.create("phone")

        owner = getpass.getuser().upper()
        outsiders = {
            principal
            for principal in _acl_principals(self.path)
            if principal not in _PERMITTED_ACL_PRINCIPALS
            and principal != owner
            and not principal.endswith("\\" + owner)
        }

        self.assertEqual(outsiders, set())

    @unittest.skipUnless(os.name == "nt", "Windows access-control lists")
    def test_inheritance_is_removed_so_the_parent_cannot_widen_it(
        self,
    ) -> None:
        # Without /inheritance:r the file keeps whatever the parent grants, and
        # a later change to the parent would silently widen access to the keys.
        registry = self.registry()
        registry.create("phone")

        listing = subprocess.run(
            ["icacls", str(self.path)],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout

        self.assertNotIn("(I)", listing)

    def test_no_secret_is_written_when_the_file_cannot_be_protected(
        self,
    ) -> None:
        # The rule this module is built on: a pairing secret in a file that
        # could not be locked down is worse than no pairing at all, because
        # everything else in the link trusts that secret. Refusing to write is
        # the safe failure, and it must leave nothing behind.
        registry = self.registry()

        with mock.patch(
            "core.link_devices._restrict_to_owner",
            side_effect=SecretFileNotProtected("simulated failure"),
        ):
            with self.assertRaises(SecretFileNotProtected):
                registry.create("phone")

        self.assertFalse(self.path.exists())
        self.assertEqual(list(Path(self.directory.name).glob("*.tmp")), [])

    def test_no_temporary_file_is_left_behind(self) -> None:
        registry = self.registry()
        registry.create("phone")

        leftovers = list(Path(self.directory.name).glob("*.tmp"))

        self.assertEqual(leftovers, [])

    def test_a_corrupt_file_raises_rather_than_starting_empty(self) -> None:
        # Starting empty would look exactly like "nothing is paired yet" and
        # would invite re-pairing over whatever the corruption was.
        self.path.write_text("{ not json", encoding="utf-8")

        with self.assertRaises(DeviceStoreCorrupt):
            self.registry()

    def test_a_file_without_a_device_list_raises(self) -> None:
        self.path.write_text('{"version": 1}', encoding="utf-8")

        with self.assertRaises(DeviceStoreCorrupt):
            self.registry()

    def test_a_json_array_raises(self) -> None:
        self.path.write_text("[]", encoding="utf-8")

        with self.assertRaises(DeviceStoreCorrupt):
            self.registry()

    def test_a_record_with_a_bad_identity_raises(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "devices": [
                        {
                            "device_id": "NOT-HEX",
                            "name": "x",
                            "secret": "AAAA",
                            "status": "active",
                            "created_at": START,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(DeviceStoreCorrupt):
            self.registry()

    def test_a_record_with_a_short_key_raises(self) -> None:
        import base64

        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "devices": [
                        {
                            "device_id": "0" * 16,
                            "name": "x",
                            "secret": base64.b64encode(b"tooshort").decode(),
                            "status": "active",
                            "created_at": START,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(DeviceStoreCorrupt) as caught:
            self.registry()

        self.assertIn("byte key", str(caught.exception))

    def test_a_record_with_an_unknown_status_raises(self) -> None:
        import base64

        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "devices": [
                        {
                            "device_id": "0" * 16,
                            "name": "x",
                            "secret": base64.b64encode(b"k" * 32).decode(),
                            "status": "trusted",
                            "created_at": START,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(DeviceStoreCorrupt):
            self.registry()

    def test_a_record_with_an_unknown_capability_raises(self) -> None:
        import base64

        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "devices": [
                        {
                            "device_id": "0" * 16,
                            "name": "x",
                            "secret": base64.b64encode(b"k" * 32).decode(),
                            "status": "active",
                            "created_at": START,
                            "grants": ["become_root"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(DeviceStoreCorrupt):
            self.registry()

    def test_the_stored_key_is_not_plain_text(self) -> None:
        registry = self.registry()
        record = registry.create("phone")

        raw = self.path.read_text(encoding="utf-8")

        self.assertNotIn(record.secret.hex(), raw)


class TestRecordSerialisation(unittest.TestCase):
    def test_a_record_survives_a_json_round_trip(self) -> None:
        original = DeviceRecord(
            device_id="0123456789abcdef",
            name="phone",
            secret=b"k" * SECRET_BYTES,
            status=DeviceStatus.ACTIVE,
            created_at=START,
            last_seen_at=START + 5,
            paired_from="192.168.1.42",
            grants=frozenset({Capability.ASK}),
            remote_enabled=True,
        )

        restored = DeviceRecord.from_json(original.to_json())

        self.assertEqual(restored, original)

    def test_a_record_with_no_last_seen_time_survives(self) -> None:
        original = DeviceRecord(
            device_id="0123456789abcdef",
            name="phone",
            secret=b"k" * SECRET_BYTES,
            status=DeviceStatus.PENDING,
            created_at=START,
        )

        self.assertEqual(DeviceRecord.from_json(original.to_json()), original)


if __name__ == "__main__":
    unittest.main()
