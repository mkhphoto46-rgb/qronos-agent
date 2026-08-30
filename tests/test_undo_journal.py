from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.actions import ActionRequest, InvalidAction
from core.undo_journal import (
    EntryState,
    NotUndoable,
    Reversal,
    UndoEntry,
    UndoJournal,
)
from security.permissions import ActionCategory


class FakeClock:
    def __init__(self, initial_time: float = 1000.0) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


def a_request(summary: str = "Rename the file") -> ActionRequest:
    return ActionRequest(
        category=ActionCategory.CREATE_OR_EDIT_FILE,
        target="C:/notes.txt",
        summary=summary,
    )


class TestRecording(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.journal = UndoJournal(path=None, clock=self.clock)

    def test_a_reversible_action_is_recorded_as_undoable(self) -> None:
        entry = self.journal.record(
            a_request(),
            Reversal.RESTORE_CONTENT,
            {"backup": "C:/backups/notes.txt"},
        )

        self.assertTrue(entry.undoable)
        self.assertIs(entry.state, EntryState.PENDING)

    def test_an_irreversible_action_is_recorded_not_omitted(self) -> None:
        # "We deleted this and cannot bring it back" is exactly what somebody
        # asking for undo needs to hear. A silent gap tells them nothing.
        entry = self.journal.record(a_request(), Reversal.IRREVERSIBLE)

        self.assertFalse(entry.undoable)
        self.assertIn(entry, self.journal.entries())

    def test_an_action_that_changed_nothing_is_not_undoable(self) -> None:
        entry = self.journal.record(a_request(), Reversal.NOTHING_TO_UNDO)

        self.assertFalse(entry.undoable)


class TestRestorationDataIsRequired(unittest.TestCase):
    def test_a_reversal_that_needs_data_cannot_be_recorded_without_it(
        self,
    ) -> None:
        # The failure this prevents is the worst kind: the entry appears in the
        # undo list, the user picks it, and only then does it turn out there
        # was never anything to restore from.
        for reversal in (
            Reversal.RESTORE_CONTENT,
            Reversal.MOVE_BACK,
            Reversal.DELETE_CREATED,
            Reversal.STOP_STARTED,
        ):
            with self.subTest(reversal=reversal):
                with self.assertRaises(InvalidAction):
                    UndoJournal(path=None).record(a_request(), reversal)

    def test_irreversible_needs_no_data(self) -> None:
        UndoJournal(path=None).record(a_request(), Reversal.IRREVERSIBLE)

    def test_an_entry_must_name_an_action(self) -> None:
        with self.assertRaises(InvalidAction):
            UndoEntry(
                action_id="  ",
                category="x",
                summary="x",
                reversal=Reversal.IRREVERSIBLE,
                at=0.0,
            )


class TestUndoIsAStack(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = UndoJournal(path=None, clock=FakeClock())

        self.first = self.journal.record(
            a_request("first"), Reversal.DELETE_CREATED, {"path": "a"}
        )
        self.second = self.journal.record(
            a_request("second"), Reversal.DELETE_CREATED, {"path": "b"}
        )

    def test_the_most_recent_change_is_undone_first(self) -> None:
        # Reversing an older change while a newer one still sits on top of it
        # produces a state the user never had.
        self.assertEqual(
            self.journal.next_undoable().summary,
            "second",
        )

    def test_undoable_lists_newest_first(self) -> None:
        self.assertEqual(
            [entry.summary for entry in self.journal.undoable()],
            ["second", "first"],
        )

    def test_irreversible_entries_are_not_offered(self) -> None:
        self.journal.record(a_request("third"), Reversal.IRREVERSIBLE)

        self.assertEqual(
            [entry.summary for entry in self.journal.undoable()],
            ["second", "first"],
        )

    def test_an_undone_entry_drops_out_of_the_list(self) -> None:
        self.journal.mark(self.second.action_id, EntryState.UNDONE)

        self.assertEqual(
            [entry.summary for entry in self.journal.undoable()],
            ["first"],
        )


class TestClaimingAndMarking(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = UndoJournal(path=None, clock=FakeClock())

    def test_claiming_does_not_mark(self) -> None:
        # An entry marked before the work is attempted is a lie whichever way
        # the work goes. Claim checks; mark records what happened.
        entry = self.journal.record(
            a_request(), Reversal.DELETE_CREATED, {"path": "a"}
        )

        claimed = self.journal.claim(entry.action_id)

        self.assertIs(claimed.state, EntryState.PENDING)

    def test_an_irreversible_entry_cannot_be_claimed(self) -> None:
        entry = self.journal.record(a_request(), Reversal.IRREVERSIBLE)

        with self.assertRaises(NotUndoable):
            self.journal.claim(entry.action_id)

    def test_an_entry_that_changed_nothing_cannot_be_claimed(self) -> None:
        entry = self.journal.record(a_request(), Reversal.NOTHING_TO_UNDO)

        with self.assertRaises(NotUndoable):
            self.journal.claim(entry.action_id)

    def test_an_already_undone_entry_cannot_be_claimed_again(self) -> None:
        entry = self.journal.record(
            a_request(), Reversal.DELETE_CREATED, {"path": "a"}
        )
        self.journal.mark(entry.action_id, EntryState.UNDONE)

        with self.assertRaises(NotUndoable):
            self.journal.claim(entry.action_id)

    def test_a_failed_reversal_is_terminal(self) -> None:
        # Retrying a failed undo blind is how one failed reversal becomes two
        # conflicting states.
        entry = self.journal.record(
            a_request(), Reversal.DELETE_CREATED, {"path": "a"}
        )
        self.journal.mark(entry.action_id, EntryState.FAILED)

        with self.assertRaises(NotUndoable):
            self.journal.claim(entry.action_id)

        self.assertEqual(self.journal.undoable(), ())

    def test_an_unknown_action_cannot_be_claimed(self) -> None:
        with self.assertRaises(NotUndoable):
            self.journal.claim("no-such-action")

    def test_marking_an_unknown_action_raises(self) -> None:
        with self.assertRaises(NotUndoable):
            self.journal.mark("no-such-action", EntryState.UNDONE)


class TestSerialisation(unittest.TestCase):
    def test_an_entry_survives_a_round_trip(self) -> None:
        entry = UndoJournal(path=None, clock=FakeClock()).record(
            a_request(), Reversal.MOVE_BACK, {"from": "a", "to": "b"}
        )

        self.assertEqual(UndoEntry.from_json(entry.to_json()), entry)

    def test_a_wrong_schema_version_is_refused(self) -> None:
        entry = UndoJournal(path=None).record(
            a_request(), Reversal.IRREVERSIBLE
        )
        data = entry.to_json()
        data["schemaVersion"] = 99

        with self.assertRaises(InvalidAction):
            UndoEntry.from_json(data)

    def test_an_unknown_reversal_is_refused(self) -> None:
        entry = UndoJournal(path=None).record(
            a_request(), Reversal.IRREVERSIBLE
        )
        data = entry.to_json()
        data["reversal"] = "wave_a_wand"

        with self.assertRaises(InvalidAction):
            UndoEntry.from_json(data)


class TestTheFile(unittest.TestCase):
    def test_entries_and_their_updates_are_appended(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "undo.jsonl"
            journal = UndoJournal(path=path, clock=FakeClock())

            entry = journal.record(
                a_request(), Reversal.DELETE_CREATED, {"path": "a"}
            )
            journal.mark(entry.action_id, EntryState.UNDONE)

            lines = path.read_text(encoding="utf-8").splitlines()

            self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
