from __future__ import annotations

import unittest

from core.computer_action_parser import (
    ComputerActionParseError,
    parse_computer_action,
)
from core.computer_executor import ComputerOperation


class ComputerActionParserTests(unittest.TestCase):
    def assert_open(
        self,
        text: str,
        app_id: str,
    ) -> None:
        action = parse_computer_action(text)
        self.assertIs(
            action.operation,
            ComputerOperation.OPEN_APP,
        )
        self.assertEqual(action.app_id, app_id)

    def test_persian_notepad(self) -> None:
        self.assert_open(
            "نوت پد رو باز کن",
            "notepad",
        )

    def test_english_notepad(self) -> None:
        self.assert_open(
            "Open Notepad",
            "notepad",
        )

    def test_persian_calculator(self) -> None:
        self.assert_open(
            "ماشین حساب رو باز کن",
            "calculator",
        )

    def test_file_explorer(self) -> None:
        self.assert_open(
            "فایل اکسپلورر رو باز کن",
            "file_explorer",
        )

    def test_paint(self) -> None:
        self.assert_open(
            "Launch Paint",
            "paint",
        )

    def test_unknown_application_fails_closed(self) -> None:
        with self.assertRaises(ComputerActionParseError):
            parse_computer_action(
                "Premiere رو باز کن"
            )

    def test_no_open_verb_fails_closed(self) -> None:
        with self.assertRaises(ComputerActionParseError):
            parse_computer_action("Notepad")

    def test_shell_syntax_fails_closed(self) -> None:
        with self.assertRaises(ComputerActionParseError):
            parse_computer_action(
                "open notepad && calc"
            )

    def test_executable_name_fails_closed(self) -> None:
        with self.assertRaises(ComputerActionParseError):
            parse_computer_action(
                "open notepad.exe"
            )

    def test_two_apps_fail_closed(self) -> None:
        with self.assertRaises(ComputerActionParseError):
            parse_computer_action(
                "open Notepad and Calculator"
            )


if __name__ == "__main__":
    unittest.main()
