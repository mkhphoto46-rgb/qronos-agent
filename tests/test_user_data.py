from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.conversation_store import ConversationStore
from core.user_data import (
    ARCHIVE_KIND,
    ARCHIVE_VERSION,
    EXCLUDED_FROM_ARCHIVE,
    ArchiveInvalid,
    Section,
    UserDataArchive,
    readable_export,
)


class FakeClock:
    def __init__(self, initial_time: float = 1000.0) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time


def populated_conversations() -> ConversationStore:
    conversations = ConversationStore(path=None, clock=FakeClock())
    conversations.append("c1", "user", "سلام")
    conversations.append("c1", "assistant", "بله؟")

    return conversations


class TestBuilding(unittest.TestCase):
    def setUp(self) -> None:
        self.conversations = populated_conversations()
        self.archive = UserDataArchive(clock=FakeClock())
        self.archive.register(
            Section.CONVERSATIONS,
            exporter=lambda: self.conversations.export(),
            importer=lambda data: self.conversations.import_conversations(
                data.get("conversations", [])
            ),
        )

    def test_an_archive_identifies_itself(self) -> None:
        # A file picked out of a downloads folder should be identifiable
        # before it is trusted.
        built = self.archive.build()

        self.assertEqual(built["kind"], ARCHIVE_KIND)
        self.assertEqual(built["archiveVersion"], ARCHIVE_VERSION)

    def test_a_registered_section_is_included(self) -> None:
        built = self.archive.build()

        self.assertIn(
            Section.CONVERSATIONS.value,
            built["sections"],
        )

    def test_the_header_counts_what_is_inside(self) -> None:
        built = self.archive.build()

        self.assertEqual(built["counts"]["conversations"], 1)

    def test_an_empty_archive_is_valid(self) -> None:
        summary = UserDataArchive.summarise(
            UserDataArchive(clock=FakeClock()).build()
        )

        self.assertEqual(summary.sections, ())


class TestScopeIsStated(unittest.TestCase):
    """
    An archive holds user data and only user data.

    The list is a control rather than a convention, because "we would not do
    that" is not a control. A ten-gigabyte model in a backup makes the backup
    useless; runtime files restored onto another machine are wrong there; and
    logs contain fragments of everything, which matters the moment somebody
    hands an archive to a support engineer.
    """

    def test_the_exclusions_are_recorded_in_the_archive(self) -> None:
        built = UserDataArchive(clock=FakeClock()).build()

        self.assertEqual(built["excluded"], dict(EXCLUDED_FROM_ARCHIVE))

    def test_every_exclusion_carries_a_reason(self) -> None:
        for name, reason in EXCLUDED_FROM_ARCHIVE.items():
            with self.subTest(excluded=name):
                self.assertTrue(reason.strip())

    def test_models_and_runtime_are_excluded(self) -> None:
        for name in ("models", "runtime", "cache", "logs"):
            with self.subTest(excluded=name):
                self.assertIn(name, EXCLUDED_FROM_ARCHIVE)

    def test_pairing_secrets_are_excluded(self) -> None:
        # Restoring them onto another machine would clone a trusted device
        # rather than pair a new one.
        self.assertIn("device_secrets", EXCLUDED_FROM_ARCHIVE)

    def test_only_registered_sections_appear(self) -> None:
        archive = UserDataArchive(clock=FakeClock())
        archive.register(Section.SETTINGS, exporter=lambda: {"a": 1})

        self.assertEqual(
            list(archive.build()["sections"]),
            [Section.SETTINGS.value],
        )


class TestSummarising(unittest.TestCase):
    def test_something_that_is_not_an_archive_is_refused(self) -> None:
        with self.assertRaises(ArchiveInvalid):
            UserDataArchive.summarise({"hello": "world"})

    def test_an_archive_without_a_version_is_refused(self) -> None:
        with self.assertRaises(ArchiveInvalid):
            UserDataArchive.summarise(
                {"kind": ARCHIVE_KIND, "sections": {}}
            )

    def test_a_newer_archive_is_refused(self) -> None:
        # Restoring it would drop whatever the newer version added, then
        # present the result as a complete restore.
        with self.assertRaises(ArchiveInvalid):
            UserDataArchive.summarise(
                {
                    "kind": ARCHIVE_KIND,
                    "archiveVersion": ARCHIVE_VERSION + 5,
                    "sections": {},
                }
            )

    def test_an_unknown_section_does_not_invalidate_the_archive(self) -> None:
        # An archive containing something extra is still usable for everything
        # else in it.
        summary = UserDataArchive.summarise(
            {
                "kind": ARCHIVE_KIND,
                "archiveVersion": ARCHIVE_VERSION,
                "sections": {"conversations": {}, "quantum_state": {}},
            }
        )

        self.assertEqual(summary.sections, (Section.CONVERSATIONS,))


class TestRoundTrip(unittest.TestCase):
    def test_an_archive_restores_what_it_carried(self) -> None:
        source = populated_conversations()
        out = UserDataArchive(clock=FakeClock())
        out.register(
            Section.CONVERSATIONS, exporter=lambda: source.export()
        )

        destination = ConversationStore(path=None, clock=FakeClock())
        back = UserDataArchive(clock=FakeClock())
        back.register(
            Section.CONVERSATIONS,
            importer=lambda data: destination.import_conversations(
                data.get("conversations", [])
            ),
        )

        restored = back.restore(out.build())

        self.assertEqual(restored["conversations"], 1)
        self.assertEqual(destination.get("c1").messages[0].text, "سلام")

    def test_a_section_with_no_importer_is_skipped(self) -> None:
        out = UserDataArchive(clock=FakeClock())
        out.register(Section.SETTINGS, exporter=lambda: {"a": 1})

        self.assertEqual(
            UserDataArchive(clock=FakeClock()).restore(out.build()),
            {},
        )

    def test_a_bad_archive_is_refused_before_anything_is_written(
        self,
    ) -> None:
        # A restore that half-succeeds is worse than one that does not start.
        taken: list[object] = []
        archive = UserDataArchive(clock=FakeClock())
        archive.register(
            Section.CONVERSATIONS,
            importer=lambda data: (taken.append(data), 1)[1],
        )

        with self.assertRaises(ArchiveInvalid):
            archive.restore({"sections": {"conversations": {}}})

        self.assertEqual(taken, [])


class TestFiles(unittest.TestCase):
    def test_an_archive_survives_a_write_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "backup.qronos.json"
            source = populated_conversations()

            archive = UserDataArchive(clock=FakeClock())
            archive.register(
                Section.CONVERSATIONS, exporter=lambda: source.export()
            )
            archive.write(path)

            summary = UserDataArchive.summarise(
                UserDataArchive.read(path)
            )

            self.assertEqual(summary.sections, (Section.CONVERSATIONS,))

    def test_reading_a_missing_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(ArchiveInvalid):
                UserDataArchive.read(Path(name) / "nothing.json")

    def test_reading_something_corrupt_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "backup.json"
            path.write_text("{ not json", encoding="utf-8")

            with self.assertRaises(ArchiveInvalid):
                UserDataArchive.read(path)


class TestReadability(unittest.TestCase):
    def test_an_export_is_readable_without_qronos(self) -> None:
        # An export that needs the software that produced it is not much of an
        # export. This is the mechanism behind a data-subject request.
        source = populated_conversations()
        archive = UserDataArchive(clock=FakeClock())
        archive.register(
            Section.CONVERSATIONS, exporter=lambda: source.export()
        )

        text = readable_export(archive.build())

        self.assertIn("سلام", text)
        self.assertIn("\n", text)


if __name__ == "__main__":
    unittest.main()
