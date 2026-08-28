from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.json_store import (
    JsonStore,
    MigrationMissing,
    StoreCorrupt,
    StoreTooNew,
)


class TestInMemory(unittest.TestCase):
    def test_nothing_stored_reads_as_nothing(self) -> None:
        # Distinct from an empty document, and callers need to tell them apart.
        self.assertIsNone(JsonStore(None, version=1).load())

    def test_a_saved_payload_comes_back(self) -> None:
        store = JsonStore(None, version=1)
        store.save({"a": 1})

        self.assertEqual(store.load().payload, {"a": 1})

    def test_a_default_is_used_when_nothing_is_stored(self) -> None:
        self.assertEqual(
            JsonStore(None, version=1).load_payload({"a": 1}),
            {"a": 1},
        )


class TestOnDisk(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "doc.json"
        self.store = JsonStore(self.path, version=1)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_payload_survives_a_restart(self) -> None:
        self.store.save({"a": 1, "b": "two"})

        self.assertEqual(
            JsonStore(self.path, version=1).load().payload,
            {"a": 1, "b": "two"},
        )

    def test_the_version_is_written_alongside(self) -> None:
        self.store.save({"a": 1})

        raw = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(raw["schemaVersion"], 1)

    def test_persian_is_stored_readably(self) -> None:
        self.store.save({"summary": "پریمیر را باز کن"})

        self.assertIn("پریمیر", self.path.read_text(encoding="utf-8"))

    def test_a_missing_parent_directory_is_created(self) -> None:
        nested = Path(self.directory.name) / "deep" / "doc.json"
        JsonStore(nested, version=1).save({"a": 1})

        self.assertTrue(nested.exists())

    def test_no_temporary_file_is_left_behind(self) -> None:
        self.store.save({"a": 1})

        self.assertEqual(
            list(Path(self.directory.name).glob("*.tmp")),
            [],
        )

    def test_the_previous_file_survives_a_failed_write(self) -> None:
        # What the rename buys. A crash mid-write leaves either the previous
        # complete file or the new complete file, never a half of one.
        self.store.save({"a": 1})

        class Unserialisable:
            pass

        with self.assertRaises(TypeError):
            self.store.save({"bad": Unserialisable()})

        self.assertEqual(self.store.load().payload, {"a": 1})


class TestCorruptionIsNotSilent(unittest.TestCase):
    def test_invalid_json_raises_rather_than_starting_empty(self) -> None:
        # Starting empty looks exactly like "nothing saved yet" and invites the
        # user to write over whatever the corruption was. The device registry
        # made this choice already; same choice, same reason.
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "doc.json"
            path.write_text("{ not json", encoding="utf-8")

            with self.assertRaises(StoreCorrupt):
                JsonStore(path, version=1).load()

    def test_a_document_without_a_version_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "doc.json"
            path.write_text(json.dumps({"payload": {}}), encoding="utf-8")

            with self.assertRaises(StoreCorrupt):
                JsonStore(path, version=1).load()

    def test_a_document_without_a_payload_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "doc.json"
            path.write_text(
                json.dumps({"schemaVersion": 1}), encoding="utf-8"
            )

            with self.assertRaises(StoreCorrupt):
                JsonStore(path, version=1).load()

    def test_a_bare_value_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "doc.json"
            path.write_text('"hello"', encoding="utf-8")

            with self.assertRaises(StoreCorrupt):
                JsonStore(path, version=1).load()


class TestVersioning(unittest.TestCase):
    def test_a_newer_file_is_refused(self) -> None:
        # Loading it would drop fields this version does not know about, then
        # write the truncated result back over the user's data.
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "doc.json"
            JsonStore(path, version=3).save({"a": 1})

            with self.assertRaises(StoreTooNew):
                JsonStore(path, version=1).load()

    def test_an_older_file_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "doc.json"
            JsonStore(path, version=1).save({"name": "amin"})

            store = JsonStore(
                path,
                version=2,
                migrations={
                    1: lambda payload: {
                        **payload,
                        "displayName": payload["name"],
                    }
                },
            )
            document = store.load()

            self.assertTrue(document.migrated)
            self.assertEqual(document.payload["displayName"], "amin")

    def test_migrations_run_in_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "doc.json"
            JsonStore(path, version=1).save({"count": 1})

            store = JsonStore(
                path,
                version=3,
                migrations={
                    1: lambda p: {"count": p["count"] + 10},
                    2: lambda p: {"count": p["count"] + 100},
                },
            )

            self.assertEqual(store.load().payload["count"], 111)

    def test_a_missing_migration_refuses_rather_than_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "doc.json"
            JsonStore(path, version=1).save({"a": 1})

            with self.assertRaises(MigrationMissing):
                JsonStore(path, version=2).load()

    def test_a_matching_version_is_not_marked_migrated(self) -> None:
        store = JsonStore(None, version=1)
        store.save({"a": 1})

        self.assertFalse(store.load().migrated)

    def test_a_version_below_one_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            JsonStore(None, version=0)


if __name__ == "__main__":
    unittest.main()
