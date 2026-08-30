from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.append_only_log import AppendOnlyLog


class TestDisabledLog(unittest.TestCase):
    def test_no_path_means_no_file(self) -> None:
        log = AppendOnlyLog(None)

        self.assertFalse(log.enabled)
        self.assertFalse(log.append({"a": 1}))
        self.assertEqual(log.read_records(), ())


class TestAppending(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "log.jsonl"
        self.log = AppendOnlyLog(self.path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_one_record_is_one_line(self) -> None:
        self.log.append({"a": 1})
        self.log.append({"a": 2})

        self.assertEqual(
            len(self.path.read_text(encoding="utf-8").splitlines()),
            2,
        )

    def test_records_read_back_in_order(self) -> None:
        for index in range(3):
            self.log.append({"index": index})

        self.assertEqual(
            [record["index"] for record in self.log.read_records()],
            [0, 1, 2],
        )

    def test_a_missing_parent_directory_is_created(self) -> None:
        nested = Path(self.directory.name) / "deep" / "deeper" / "log.jsonl"

        self.assertTrue(AppendOnlyLog(nested).append({"a": 1}))

    def test_persian_is_written_readably(self) -> None:
        # ensure_ascii is off here on purpose: this file is read by a person
        # diagnosing a problem, and escape sequences are not readable.
        self.log.append({"summary": "پریمیر را باز کن"})

        self.assertIn("پریمیر", self.path.read_text(encoding="utf-8"))


class TestFailureIsSurvivable(unittest.TestCase):
    def test_an_unwritable_path_does_not_raise(self) -> None:
        # A log that cannot be written must not take down the thing it is
        # logging. The link layer's rule: losing an audit line beats dropping
        # the user's connection over a full disk.
        with tempfile.TemporaryDirectory() as name:
            blocker = Path(name) / "blocked"
            blocker.write_text("i am a file", encoding="utf-8")

            log = AppendOnlyLog(blocker / "log.jsonl")

            self.assertFalse(log.append({"a": 1}))

    def test_a_truncated_final_line_does_not_lose_the_rest(self) -> None:
        # What a crash mid-write leaves behind. Refusing to read the file at
        # all would throw away the history that survived.
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "log.jsonl"
            path.write_text(
                json.dumps({"a": 1}) + "\n" + '{"a": 2',
                encoding="utf-8",
            )

            self.assertEqual(len(AppendOnlyLog(path).read_records()), 1)

    def test_a_bare_value_line_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "log.jsonl"
            path.write_text(
                '"just a string"\n' + json.dumps({"a": 1}) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(len(AppendOnlyLog(path).read_records()), 1)


class TestRotation(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "log.jsonl"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_the_file_is_rotated_once_it_passes_the_cap(self) -> None:
        log = AppendOnlyLog(self.path, max_bytes=120)

        for index in range(40):
            log.append({"index": index, "padding": "x" * 20})

        self.assertTrue(log.previous_path.exists())

    def test_only_one_previous_file_is_kept(self) -> None:
        # Qronos manages the user's disk. A log that keeps every generation is
        # a bug in the same product.
        log = AppendOnlyLog(self.path, max_bytes=120)

        for index in range(200):
            log.append({"index": index, "padding": "x" * 20})

        siblings = list(Path(self.directory.name).glob("log.jsonl*"))

        self.assertEqual(len(siblings), 2)

    def test_the_live_file_stays_under_control(self) -> None:
        log = AppendOnlyLog(self.path, max_bytes=200)

        for index in range(100):
            log.append({"index": index, "padding": "x" * 20})

        self.assertLessEqual(self.path.stat().st_size, 400)

    def test_nothing_rotates_below_the_cap(self) -> None:
        log = AppendOnlyLog(self.path, max_bytes=10_000)

        log.append({"a": 1})

        self.assertFalse(log.previous_path.exists())


if __name__ == "__main__":
    unittest.main()
