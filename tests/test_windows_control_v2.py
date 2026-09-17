from __future__ import annotations

import unittest

from core.computer_action_parser import parse_computer_action
from core.computer_executor import (
    ComputerAction,
    ComputerExecutor,
    ComputerOperation,
)


class WindowsControlV2ParserTests(unittest.TestCase):
    def test_close_paint_parser(self) -> None:
        action = parse_computer_action("پینت رو ببند")
        self.assertIs(action.operation, ComputerOperation.CLOSE_APP)
        self.assertEqual(action.app_id, "paint")

    def test_minimize_paint_parser(self) -> None:
        action = parse_computer_action("پینت رو مینیمایز کن")
        self.assertIs(action.operation, ComputerOperation.MINIMIZE_WINDOW)

    def test_maximize_paint_parser(self) -> None:
        action = parse_computer_action("پینت رو ماکسیمایز کن")
        self.assertIs(action.operation, ComputerOperation.MAXIMIZE_WINDOW)

    def test_dynamic_app_resolver_is_explicitly_injected(self) -> None:
        action = parse_computer_action(
            "تلگرام رو باز کن",
            app_resolver=lambda query: "discovered:shortcut:0123456789abcdefabcd",
        )
        self.assertIs(action.operation, ComputerOperation.OPEN_APP)
        self.assertTrue(action.app_id.startswith("discovered:"))

    def test_notepad_is_not_stolen_by_note_file_cue(self) -> None:
        action = parse_computer_action("نوت پد رو باز کن")
        self.assertIs(action.operation, ComputerOperation.OPEN_APP)
        self.assertEqual(action.app_id, "notepad")

    def test_file_location_is_not_stolen_by_app_parser(self) -> None:
        with self.assertRaises(Exception):
            parse_computer_action(
                "برو درایو اچ فایل test رو باز کن",
                app_resolver=lambda query: "discovered:shortcut:0123456789abcdefabcd",
            )


class WindowsControlV2ExecutorTests(unittest.TestCase):
    def test_window_action_callback_is_used_for_close(self) -> None:
        calls = []
        executor = ComputerExecutor(
            window_action=lambda spec, op: calls.append((spec.app_id, op)) or "ok",
            dynamic_apps=False,
        )
        detail = executor._perform(
            ComputerAction(ComputerOperation.CLOSE_APP, app_id="paint")
        )
        self.assertEqual(detail, "ok")
        self.assertEqual(calls, [("paint", ComputerOperation.CLOSE_APP)])


if __name__ == "__main__":
    unittest.main()
