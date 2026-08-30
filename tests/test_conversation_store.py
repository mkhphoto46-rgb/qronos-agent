from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.conversation_store import ConversationStore


class FakeClock:
    def __init__(self, initial_time: float = 1000.0) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


def store(path=None, enabled: bool = True, **kwargs) -> ConversationStore:
    return ConversationStore(
        path=path,
        clock=kwargs.pop("clock", FakeClock()),
        enabled=enabled,
        **kwargs,
    )


class TestAppending(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = store(clock=self.clock)

    def test_a_turn_is_kept(self) -> None:
        self.store.append("c1", "user", "سلام")

        self.assertEqual(self.store.get("c1").message_count, 1)

    def test_turns_accumulate_in_order(self) -> None:
        self.store.append("c1", "user", "سلام")
        self.clock.advance(1)
        self.store.append("c1", "assistant", "سلام، چطور کمک کنم؟")

        self.assertEqual(
            [m.role for m in self.store.get("c1").messages],
            ["user", "assistant"],
        )

    def test_conversations_are_separate(self) -> None:
        self.store.append("c1", "user", "one")
        self.store.append("c2", "user", "two")

        self.assertEqual(self.store.count(), 2)

    def test_a_conversation_needs_an_id(self) -> None:
        with self.assertRaises(ValueError):
            self.store.append("  ", "user", "x")

    def test_the_preview_is_what_the_user_said(self) -> None:
        # The user's own words rather than a generated title. A summary would
        # mean sending the transcript somewhere to be summarised, which is the
        # one thing this product promises not to do.
        self.store.append("c1", "assistant", "hello there")
        self.store.append("c1", "user", "ساعت چند است؟")

        self.assertEqual(self.store.get("c1").preview, "ساعت چند است؟")


class TestHistoryCanBeTurnedOff(unittest.TestCase):
    """
    Off means nothing is written, not written-and-hidden.

    A design that stores everything and filters it in the interface leaves the
    transcripts on disk regardless of what the user was told. That is the
    difference between a privacy control and a privacy claim.
    """

    def test_nothing_is_kept_when_disabled(self) -> None:
        disabled = store(enabled=False)

        self.assertIsNone(disabled.append("c1", "user", "secret"))
        self.assertEqual(disabled.count(), 0)

    def test_nothing_reaches_the_file_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "conversations.json"
            store(path, enabled=False).append("c1", "user", "secret")

            self.assertFalse(path.exists())


class TestDeletion(unittest.TestCase):
    def setUp(self) -> None:
        self.store = store()
        self.store.append("c1", "user", "one")
        self.store.append("c2", "user", "two")

    def test_one_conversation_can_be_forgotten(self) -> None:
        self.assertTrue(self.store.forget("c1"))
        self.assertIsNone(self.store.get("c1"))

    def test_forgetting_something_absent_is_not_an_error(self) -> None:
        self.assertFalse(self.store.forget("nope"))

    def test_everything_can_be_forgotten(self) -> None:
        self.assertEqual(self.store.forget_all(), 2)
        self.assertEqual(self.store.count(), 0)

    def test_deletion_removes_the_text_from_the_file(self) -> None:
        # A real removal, not a flag. An export of a file full of tombstones
        # still contains everything the user asked to be rid of.
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "conversations.json"
            instance = store(path)

            instance.append("c1", "user", "the secret sentence")
            instance.forget("c1")

            self.assertNotIn(
                "the secret sentence",
                path.read_text(encoding="utf-8"),
            )

    def test_a_deleted_conversation_is_not_in_an_export(self) -> None:
        self.store.forget("c1")

        exported = self.store.export()

        self.assertEqual(len(exported["conversations"]), 1)


class TestCaps(unittest.TestCase):
    def test_a_long_conversation_drops_its_oldest_turns(self) -> None:
        instance = store(max_messages=3)

        for index in range(10):
            instance.append("c1", "user", f"turn {index}")

        messages = instance.get("c1").messages

        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[-1].text, "turn 9")

    def test_old_conversations_age_out(self) -> None:
        # A storage control and a privacy control at once: history that ages
        # out is history that cannot leak later.
        clock = FakeClock()
        instance = store(clock=clock, max_conversations=2)

        for index in range(5):
            instance.append(f"c{index}", "user", "x")
            clock.advance(10)

        self.assertEqual(instance.count(), 2)

    def test_the_newest_conversations_are_the_ones_kept(self) -> None:
        clock = FakeClock()
        instance = store(clock=clock, max_conversations=2)

        for index in range(4):
            instance.append(f"c{index}", "user", "x")
            clock.advance(10)

        self.assertIsNotNone(instance.get("c3"))
        self.assertIsNone(instance.get("c0"))


class TestPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "conversations.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_conversation_survives_a_restart(self) -> None:
        store(self.path).append("c1", "user", "سلام")

        restored = store(self.path).get("c1")

        self.assertEqual(restored.messages[0].text, "سلام")

    def test_recent_lists_newest_first(self) -> None:
        clock = FakeClock()
        instance = store(self.path, clock=clock)

        instance.append("old", "user", "x")
        clock.advance(100)
        instance.append("new", "user", "y")

        self.assertEqual(
            [c.conversation_id for c in instance.recent()],
            ["new", "old"],
        )


class TestExportAndRestore(unittest.TestCase):
    def test_an_export_contains_what_is_stored(self) -> None:
        instance = store()
        instance.append("c1", "user", "سلام")
        instance.append("c1", "assistant", "بله؟")

        exported = instance.export()

        self.assertEqual(len(exported["conversations"]), 1)
        self.assertEqual(
            len(exported["conversations"][0]["messages"]),
            2,
        )

    def test_an_export_round_trips_through_a_restore(self) -> None:
        original = store()
        original.append("c1", "user", "سلام")

        restored = store()
        taken = restored.import_conversations(
            original.export()["conversations"]
        )

        self.assertEqual(taken, 1)
        self.assertEqual(restored.get("c1").messages[0].text, "سلام")

    def test_a_restore_replaces_rather_than_merges(self) -> None:
        # A restore reproduces a previous state. Merging would produce a
        # conversation that never happened.
        original = store()
        original.append("c1", "user", "from the backup")

        current = store()
        current.append("c1", "user", "from today")
        current.import_conversations(original.export()["conversations"])

        self.assertEqual(
            current.get("c1").messages[0].text,
            "from the backup",
        )

    def test_malformed_entries_are_skipped(self) -> None:
        instance = store()

        taken = instance.import_conversations(
            [{"conversationId": ""}, {"conversationId": "good"}]
        )

        self.assertEqual(taken, 1)


if __name__ == "__main__":
    unittest.main()
