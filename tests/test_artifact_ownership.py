from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.artifact_ownership import (
    ArtifactOwner,
    ArtifactOwnershipRegistry,
    ArtifactRecord,
    InMemoryOwnershipStore,
    JsonFileOwnershipStore,
    OwnershipTransferError,
)
from core.storage_budget import BudgetComponent


CREATED_AT = 1_800_000_000.0
TRANSFERRED_AT = 1_800_009_999.0


class OwnershipTestCase(unittest.TestCase):
    """Shared fixture: a temporary directory and a registry over memory."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

        counter = {"n": 0}

        def deterministic_id() -> str:
            counter["n"] += 1
            return f"artifact-{counter['n']}"

        self.store = InMemoryOwnershipStore()
        self.registry = ArtifactOwnershipRegistry(
            store=self.store,
            id_factory=deterministic_id,
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, name: str, content: bytes = b"x" * 10) -> Path:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target


class TestRegister(OwnershipTestCase):
    def test_new_artifact_is_owned_by_qronos(self) -> None:
        path = self.write("preview.png")

        record = self.registry.register(
            path=path,
            component=BudgetComponent.VISION_TEMP,
            size_bytes=10,
            created_at=CREATED_AT,
        )

        self.assertIs(record.owner, ArtifactOwner.QRONOS)
        self.assertTrue(record.is_temporary)
        self.assertTrue(record.auto_cleanup_allowed)
        self.assertTrue(record.counts_toward_quota)
        self.assertIsNone(record.transferred_at)

    def test_registering_the_same_path_twice_is_refused(self) -> None:
        path = self.write("preview.png")

        self.registry.register(
            path, BudgetComponent.VISION_TEMP, 10, CREATED_AT
        )

        with self.assertRaises(OwnershipTransferError):
            self.registry.register(
                path, BudgetComponent.VISION_TEMP, 10, CREATED_AT
            )

    def test_negative_size_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.register(
                self.write("a.png"),
                BudgetComponent.VISION_TEMP,
                -1,
                CREATED_AT,
            )

    def test_registration_persists_through_the_store(self) -> None:
        self.registry.register(
            self.write("a.png"),
            BudgetComponent.VISION_TEMP,
            10,
            CREATED_AT,
        )

        self.assertEqual(len(self.store.load()), 1)
        self.assertGreaterEqual(self.store.save_count, 1)

    def test_an_artifact_may_be_registered_before_it_is_written(self) -> None:
        # A worker registers its output path first, then writes to it.
        future = self.root / "not-written-yet.png"

        record = self.registry.register(
            future, BudgetComponent.VISION_TEMP, 0, CREATED_AT
        )

        self.assertEqual(record.path, future.resolve())


class TestTransferToUser(OwnershipTestCase):
    def setUp(self) -> None:
        super().setUp()

        self.source = self.write("generated.png", b"y" * 40)
        self.record = self.registry.register(
            self.source, BudgetComponent.VISION_TEMP, 40, CREATED_AT
        )

    def test_transfer_flips_every_derived_property(self) -> None:
        destination = self.write("mine/kept.png", b"y" * 40)

        transferred = self.registry.transfer_to_user(
            self.record.artifact_id, destination, TRANSFERRED_AT
        )

        self.assertIs(transferred.owner, ArtifactOwner.USER)
        self.assertFalse(transferred.is_temporary)
        self.assertFalse(transferred.auto_cleanup_allowed)
        self.assertFalse(transferred.counts_toward_quota)
        self.assertEqual(transferred.transferred_at, TRANSFERRED_AT)
        self.assertEqual(transferred.path, destination.resolve())

    def test_transfer_requires_a_verified_destination(self) -> None:
        # Recording a transfer for a file that was never written would leave
        # Qronos believing the user owns something that is not there.
        missing = self.root / "mine" / "never-written.png"

        with self.assertRaises(OwnershipTransferError):
            self.registry.transfer_to_user(
                self.record.artifact_id, missing, TRANSFERRED_AT
            )

    def test_destination_that_is_a_directory_is_refused(self) -> None:
        directory = self.root / "folder"
        directory.mkdir()

        with self.assertRaises(OwnershipTransferError):
            self.registry.transfer_to_user(
                self.record.artifact_id, directory, TRANSFERRED_AT
            )

    def test_unknown_artifact_id_is_refused(self) -> None:
        with self.assertRaises(OwnershipTransferError):
            self.registry.transfer_to_user(
                "no-such-id", self.source, TRANSFERRED_AT
            )

    def test_transferring_twice_is_refused(self) -> None:
        destination = self.write("mine/kept.png", b"y" * 40)

        self.registry.transfer_to_user(
            self.record.artifact_id, destination, TRANSFERRED_AT
        )

        with self.assertRaises(OwnershipTransferError):
            self.registry.transfer_to_user(
                self.record.artifact_id, destination, TRANSFERRED_AT
            )

    def test_size_is_re_measured_from_the_destination(self) -> None:
        destination = self.write("mine/kept.png", b"z" * 123)

        transferred = self.registry.transfer_to_user(
            self.record.artifact_id, destination, TRANSFERRED_AT
        )

        self.assertEqual(transferred.size_bytes, 123)

    def test_an_explicit_size_overrides_measurement(self) -> None:
        destination = self.write("mine/kept.png", b"z" * 123)

        transferred = self.registry.transfer_to_user(
            self.record.artifact_id,
            destination,
            TRANSFERRED_AT,
            size_bytes=999,
        )

        self.assertEqual(transferred.size_bytes, 999)

    def test_destination_already_registered_to_another_artifact_is_refused(
        self,
    ) -> None:
        other = self.write("other.png", b"q" * 5)
        self.registry.register(
            other, BudgetComponent.VISION_TEMP, 5, CREATED_AT
        )

        with self.assertRaises(OwnershipTransferError):
            self.registry.transfer_to_user(
                self.record.artifact_id, other, TRANSFERRED_AT
            )

    def test_there_is_no_way_to_reclaim_an_artifact(self) -> None:
        # Ownership is one-directional by design. A method that moved a file
        # back to Qronos would be a method that deletes the user's file.
        self.assertFalse(
            hasattr(self.registry, "transfer_to_qronos")
        )
        self.assertFalse(hasattr(self.registry, "reclaim"))


class TestQuotaAccounting(OwnershipTestCase):
    def test_user_owned_artifacts_leave_the_quota(self) -> None:
        first = self.write("a.png", b"a" * 100)
        second = self.write("b.png", b"b" * 250)

        record_a = self.registry.register(
            first, BudgetComponent.VISION_TEMP, 100, CREATED_AT
        )
        self.registry.register(
            second, BudgetComponent.VISION_TEMP, 250, CREATED_AT
        )

        self.assertEqual(
            self.registry.quota_bytes(BudgetComponent.VISION_TEMP), 350
        )

        kept = self.write("mine/a.png", b"a" * 100)
        self.registry.transfer_to_user(
            record_a.artifact_id, kept, TRANSFERRED_AT
        )

        self.assertEqual(
            self.registry.quota_bytes(BudgetComponent.VISION_TEMP), 250
        )

    def test_quota_can_be_totalled_across_components(self) -> None:
        self.registry.register(
            self.write("a.png"), BudgetComponent.VISION_TEMP, 10, CREATED_AT
        )
        self.registry.register(
            self.write("b.json"),
            BudgetComponent.FILESYSTEM_METADATA,
            20,
            CREATED_AT,
        )

        self.assertEqual(self.registry.quota_bytes(), 30)

    def test_is_user_owned_reflects_transfer(self) -> None:
        source = self.write("a.png", b"a" * 10)
        record = self.registry.register(
            source, BudgetComponent.VISION_TEMP, 10, CREATED_AT
        )

        self.assertFalse(self.registry.is_user_owned(source))

        kept = self.write("mine/a.png", b"a" * 10)
        self.registry.transfer_to_user(
            record.artifact_id, kept, TRANSFERRED_AT
        )

        self.assertTrue(self.registry.is_user_owned(kept))

    def test_unregistered_path_is_not_reported_as_user_owned(self) -> None:
        # False here means "the registry knows nothing", not "safe to delete".
        # The janitor applies its own orphan policy on top.
        self.assertFalse(self.registry.is_user_owned(self.root / "stray.tmp"))

    def test_qronos_owned_can_be_filtered_by_component(self) -> None:
        self.registry.register(
            self.write("a.png"), BudgetComponent.VISION_TEMP, 10, CREATED_AT
        )
        self.registry.register(
            self.write("b.json"),
            BudgetComponent.FILESYSTEM_METADATA,
            20,
            CREATED_AT,
        )

        vision = self.registry.qronos_owned(BudgetComponent.VISION_TEMP)

        self.assertEqual(len(vision), 1)


class TestForgetAndPrune(OwnershipTestCase):
    def test_forgetting_a_qronos_artifact_drops_the_record(self) -> None:
        record = self.registry.register(
            self.write("a.png"), BudgetComponent.VISION_TEMP, 10, CREATED_AT
        )

        self.registry.forget(record.artifact_id)

        self.assertIsNone(self.registry.get(record.artifact_id))

    def test_forgetting_a_user_artifact_is_refused(self) -> None:
        # Forgetting one would make the file look like an orphan on the next
        # sweep, which is exactly how a user's file gets deleted by accident.
        source = self.write("a.png", b"a" * 10)
        record = self.registry.register(
            source, BudgetComponent.VISION_TEMP, 10, CREATED_AT
        )

        kept = self.write("mine/a.png", b"a" * 10)
        self.registry.transfer_to_user(
            record.artifact_id, kept, TRANSFERRED_AT
        )

        with self.assertRaises(OwnershipTransferError):
            self.registry.forget(record.artifact_id)

    def test_forgetting_an_unknown_id_is_a_no_op(self) -> None:
        self.registry.forget("no-such-id")

    def test_prune_removes_qronos_records_for_missing_files(self) -> None:
        path = self.write("gone.png")
        self.registry.register(
            path, BudgetComponent.VISION_TEMP, 10, CREATED_AT
        )

        path.unlink()

        removed = self.registry.prune_missing_qronos_records()

        self.assertEqual(len(removed), 1)
        self.assertEqual(len(self.registry.records), 0)

    def test_prune_keeps_user_records_even_when_the_file_is_gone(self) -> None:
        # The user may have moved it. Qronos forgetting that it is theirs is
        # worse than a stale entry.
        source = self.write("a.png", b"a" * 10)
        record = self.registry.register(
            source, BudgetComponent.VISION_TEMP, 10, CREATED_AT
        )

        kept = self.write("mine/a.png", b"a" * 10)
        self.registry.transfer_to_user(
            record.artifact_id, kept, TRANSFERRED_AT
        )
        kept.unlink()

        removed = self.registry.prune_missing_qronos_records()

        self.assertEqual(removed, ())
        self.assertEqual(len(self.registry.user_owned()), 1)


class TestJsonFileOwnershipStore(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.path = self.root / "nested" / "ownership.json"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def make_record(self, artifact_id: str = "a1") -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=artifact_id,
            path=self.root / "artifact.png",
            owner=ArtifactOwner.QRONOS,
            component=BudgetComponent.VISION_TEMP,
            size_bytes=42,
            created_at=CREATED_AT,
        )

    def test_missing_file_loads_as_empty(self) -> None:
        self.assertEqual(JsonFileOwnershipStore(self.path).load(), ())

    def test_round_trip_preserves_every_field(self) -> None:
        store = JsonFileOwnershipStore(self.path)
        original = self.make_record()

        store.save((original,))
        loaded = JsonFileOwnershipStore(self.path).load()

        self.assertEqual(loaded, (original,))

    def test_round_trip_preserves_a_transferred_record(self) -> None:
        store = JsonFileOwnershipStore(self.path)

        transferred = ArtifactRecord(
            artifact_id="a2",
            path=self.root / "kept.png",
            owner=ArtifactOwner.USER,
            component=BudgetComponent.VISION_TEMP,
            size_bytes=7,
            created_at=CREATED_AT,
            transferred_at=TRANSFERRED_AT,
        )

        store.save((transferred,))
        loaded = JsonFileOwnershipStore(self.path).load()

        self.assertEqual(loaded, (transferred,))
        self.assertFalse(loaded[0].counts_toward_quota)

    def test_save_creates_the_parent_directory(self) -> None:
        JsonFileOwnershipStore(self.path).save((self.make_record(),))

        self.assertTrue(self.path.is_file())

    def test_save_leaves_no_temporary_file_behind(self) -> None:
        JsonFileOwnershipStore(self.path).save((self.make_record(),))

        leftovers = [
            entry.name
            for entry in self.path.parent.iterdir()
            if entry.name.endswith(".tmp")
        ]

        self.assertEqual(leftovers, [])

    def test_corrupt_registry_raises_rather_than_loading_empty(self) -> None:
        # Silently treating a corrupt registry as empty would turn every
        # user-owned artifact into a deletion candidate.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ not json", encoding="utf-8")

        with self.assertRaises(OwnershipTransferError):
            JsonFileOwnershipStore(self.path).load()

    def test_unexpected_shape_raises(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        with self.assertRaises(OwnershipTransferError):
            JsonFileOwnershipStore(self.path).load()

    def test_version_is_recorded(self) -> None:
        JsonFileOwnershipStore(self.path).save((self.make_record(),))

        payload = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertIn("version", payload)

    def test_registry_loads_existing_records_on_construction(self) -> None:
        store = JsonFileOwnershipStore(self.path)
        store.save((self.make_record(),))

        registry = ArtifactOwnershipRegistry(store=store)

        self.assertEqual(len(registry.records), 1)


if __name__ == "__main__":
    unittest.main()
