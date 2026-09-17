from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.file_action_parser import (
    FileActionParseError,
    parse_file_action,
)
from core.file_executor import FileOperation


class FileActionParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "test.txt"
        self.destination = self.root / "Archive"
        self.source.write_text("qronos", encoding="utf-8")
        self.destination.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_persian_move(self) -> None:
        action = parse_file_action(
            f'فایل "{self.source}" را به پوشه "{self.destination}" منتقل کن'
        )
        self.assertIs(action.operation, FileOperation.MOVE_FILE)
        self.assertEqual(Path(action.source), self.source)
        self.assertEqual(Path(action.destination), self.destination)

    def test_persian_copy(self) -> None:
        action = parse_file_action(
            f'فایل "{self.source}" را در پوشه "{self.destination}" کپی کن'
        )
        self.assertIs(action.operation, FileOperation.COPY_FILE)

    def test_persian_rename(self) -> None:
        action = parse_file_action(
            f'اسم فایل "{self.source}" را به "renamed.txt" تغییر بده'
        )
        self.assertIs(action.operation, FileOperation.RENAME_FILE)
        self.assertEqual(action.new_name, "renamed.txt")

    def test_persian_create_folder(self) -> None:
        target = self.root / "Photos"
        action = parse_file_action(
            f'پوشه "{target}" را بساز'
        )
        self.assertIs(action.operation, FileOperation.CREATE_FOLDER)
        self.assertEqual(Path(action.destination), target)

    def test_english_move(self) -> None:
        action = parse_file_action(
            f'move file "{self.source}" to folder "{self.destination}"'
        )
        self.assertIs(action.operation, FileOperation.MOVE_FILE)

    def test_guillemets_are_supported(self) -> None:
        action = parse_file_action(
            f'فایل «{self.source}» را در پوشه «{self.destination}» کپی کن'
        )
        self.assertIs(action.operation, FileOperation.COPY_FILE)

    def test_unquoted_paths_are_never_guessed(self) -> None:
        with self.assertRaises(FileActionParseError):
            parse_file_action(
                f"فایل {self.source} را منتقل کن"
            )

    def test_multiple_operations_are_refused(self) -> None:
        with self.assertRaises(FileActionParseError):
            parse_file_action(
                f'فایل "{self.source}" را کپی و به "{self.destination}" منتقل کن'
            )

    def test_open_is_not_part_of_text_gate_1b(self) -> None:
        with self.assertRaises(FileActionParseError):
            parse_file_action(
                f'فایل "{self.source}" را باز کن'
            )


if __name__ == "__main__":
    unittest.main()
