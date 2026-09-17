from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from core.computer_executor import (
    ComputerAction,
    ComputerExecutor,
    ComputerOperation,
)
from core.computer_action_parser import (
    ComputerActionParseError,
    parse_computer_action,
)
from core.file_resolver import (
    KnownFolderFileResolver,
    parse_location_request,
)
from core.runtime_bridge import _computer_action_success_response
from security.permissions import ActionCategory


class LocationSpeechRobustnessV22Tests(unittest.TestCase):
    def test_drive_h_ezafe_form_parses(self) -> None:
        request = parse_location_request("درایوه اچ رو برام باز کن")
        self.assertEqual(request.folder_hint, "drive:H")
        self.assertIsNone(request.name_hint)

    def test_urdu_dal_downloads_parses(self) -> None:
        request = parse_location_request("برو فولدر دانلوڈز")
        self.assertEqual(request.folder_hint, "downloads")
        self.assertIsNone(request.name_hint)

    def test_windows_explorer_persian_context_does_not_become_child_folder(self) -> None:
        request = parse_location_request(
            "کرونوس برو ویندوز اکسپلورر فولدر دانلودز"
        )
        self.assertEqual(request.folder_hint, "downloads")
        self.assertIsNone(request.name_hint)

    def test_common_spoken_drive_letters_parse(self) -> None:
        samples = {
            "برو درایو سی": "drive:C",
            "برو درایو دی": "drive:D",
            "برو درایو اف": "drive:F",
            "برو درایو اچ": "drive:H",
        }
        for text, expected in samples.items():
            with self.subTest(text=text):
                self.assertEqual(
                    parse_location_request(text).folder_hint,
                    expected,
                )

    def test_injected_downloads_root_resolves_without_child_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            downloads = Path(temp) / "Downloads"
            downloads.mkdir()
            resolver = KnownFolderFileResolver(
                roots={"downloads": (downloads,)},
            )
            request = parse_location_request("برو فولدر دانلوڈز")
            resolved = resolver.resolve_location(request)
            self.assertEqual(resolved.path, downloads)
            self.assertEqual(resolved.folder_key, "downloads")


    def test_qronos_inside_real_folder_name_is_preserved(self) -> None:
        request = parse_location_request(
            "فولدر Project Qronos روی دسکتاپ رو باز کن"
        )
        self.assertEqual(request.folder_hint, "desktop")
        self.assertEqual(request.name_hint, "project qronos")

    def test_leading_qronos_invocation_is_removed_without_touching_target(self) -> None:
        request = parse_location_request(
            "کرونوس فولدر Project Qronos روی دسکتاپ رو باز کن"
        )
        self.assertEqual(request.folder_hint, "desktop")
        self.assertEqual(request.name_hint, "project qronos")


class ActionAcknowledgementV22Tests(unittest.TestCase):
    def test_operation_specific_acknowledgements(self) -> None:
        self.assertEqual(
            _computer_action_success_response(
                ComputerOperation.OPEN_APP,
                "Paint",
            ),
            "انجام شد. Paint باز شد.",
        )
        self.assertEqual(
            _computer_action_success_response(
                ComputerOperation.CLOSE_APP,
                "Paint",
            ),
            "انجام شد. Paint بسته شد.",
        )
        self.assertEqual(
            _computer_action_success_response(
                ComputerOperation.MINIMIZE_WINDOW,
                "Telegram",
            ),
            "انجام شد. Telegram مینیمایز شد.",
        )
        self.assertEqual(
            _computer_action_success_response(
                ComputerOperation.MAXIMIZE_WINDOW,
                "Telegram",
            ),
            "انجام شد. Telegram ماکسیمایز شد.",
        )

    def test_paint_stt_variant_is_known(self) -> None:
        executor = ComputerExecutor(dynamic_apps=False)
        self.assertEqual(executor.resolve_app_id("پاینت"), "paint")
        self.assertEqual(
            executor.display_name_for_app_id("paint"),
            "Paint",
        )


class BoundedPaintAutomationTests(unittest.TestCase):
    def test_persian_house_command_is_a_typed_paint_action(self) -> None:
        action = parse_computer_action("توی پینت یه خونه ساده بکش")
        self.assertIs(action.operation, ComputerOperation.DRAW_BASIC_HOUSE)
        self.assertEqual(action.app_id, "paint")
        self.assertIs(action.category, ActionCategory.GUI_AUTOMATION)

    def test_english_house_command_is_a_typed_paint_action(self) -> None:
        action = parse_computer_action("draw a simple house in Paint")
        self.assertIs(action.operation, ComputerOperation.DRAW_BASIC_HOUSE)
        self.assertEqual(action.app_id, "paint")

    def test_house_template_without_paint_fails_closed(self) -> None:
        with self.assertRaises(ComputerActionParseError):
            parse_computer_action("یه خونه ساده بکش")

    def test_executor_restricts_house_template_to_paint(self) -> None:
        with self.assertRaisesRegex(Exception, "restricted to Paint"):
            ComputerAction(
                ComputerOperation.DRAW_BASIC_HOUSE,
                app_id="notepad",
            )

    def test_injected_gui_executor_receives_only_typed_template(self) -> None:
        seen: list[tuple[str, ComputerOperation]] = []

        def gui_action(spec, operation):
            seen.append((spec.app_id, operation))
            return "drawn"

        executor = ComputerExecutor(
            gui_action=gui_action,
            verification_delay_seconds=0,
        )
        detail = executor._perform(
            ComputerAction(
                ComputerOperation.DRAW_BASIC_HOUSE,
                app_id="paint",
            )
        )
        self.assertEqual(detail, "drawn")
        self.assertEqual(
            seen,
            [("paint", ComputerOperation.DRAW_BASIC_HOUSE)],
        )


if __name__ == "__main__":
    unittest.main()
