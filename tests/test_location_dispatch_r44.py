from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.computer_action_parser import (
    ComputerActionParseError,
    parse_computer_action,
)
from core.computer_executor import (
    ComputerOperation,
    _explorer_location_command,
)
from core.file_resolver import (
    KnownFolderFileResolver,
    NamedFileParseError,
    NamedLocationNotFound,
    NamedLocationParseError,
    parse_location_request,
    parse_named_file_request,
)


class ContextualExplorerDispatchR43Tests(unittest.TestCase):
    def test_plain_file_explorer_command_still_opens_the_app(self) -> None:
        action = parse_computer_action("فایل اکسپلورر رو باز کن")
        self.assertIs(action.operation, ComputerOperation.OPEN_APP)
        self.assertEqual(action.app_id, "file_explorer")

    def test_explorer_plus_downloads_is_not_collapsed_to_open_app(self) -> None:
        with self.assertRaises(ComputerActionParseError):
            parse_computer_action("فایل اکسپلورر دانلودز رو باز کن")

    def test_explorer_plus_drive_is_not_collapsed_to_open_app(self) -> None:
        with self.assertRaises(ComputerActionParseError):
            parse_computer_action("فایل اکسپلورر درایو اچ رو باز کن")


class ExactExplorerLocationR43Tests(unittest.TestCase):
    def test_exact_resolved_path_is_passed_as_an_explorer_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp).resolve())
            self.assertEqual(
                _explorer_location_command(path),
                ("explorer.exe", path),
            )


class NamedFolderResolverR43Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.desktop = base / "Desktop"
        self.documents = base / "Documents"
        self.downloads = base / "Downloads"
        for path in (self.desktop, self.documents, self.downloads):
            path.mkdir()

        self.resolver = KnownFolderFileResolver(
            roots={
                "desktop": (self.desktop,),
                "documents": (self.documents,),
                "downloads": (self.downloads,),
            },
            max_entries=500,
            max_depth=5,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_partial_named_folder_on_desktop_resolves_exact_directory(self) -> None:
        target = self.desktop / "Project Qronos Agent"
        target.mkdir()

        request = parse_location_request(
            "فولدر Project Qronos روی دسکتاپ رو باز کن"
        )
        self.assertEqual(request.folder_hint, "desktop")
        self.assertEqual(request.name_hint, "project qronos")

        resolved = self.resolver.resolve_location(request)
        self.assertEqual(resolved.path, target)
        self.assertEqual(resolved.display_name, "Project Qronos Agent")

    def test_named_folder_without_parent_searches_reviewed_known_roots(self) -> None:
        target = self.downloads / "Level Up Audio"
        target.mkdir()

        request = parse_location_request(
            "فولدر Level Up رو باز کن"
        )
        self.assertIsNone(request.folder_hint)
        self.assertEqual(request.name_hint, "level up")

        resolved = self.resolver.resolve_location(request)
        self.assertEqual(resolved.path, target)

    def test_known_folder_root_still_resolves_without_a_child_name(self) -> None:
        request = parse_location_request("برو توی فولدر دانلودز")
        self.assertEqual(request.folder_hint, "downloads")
        self.assertIsNone(request.name_hint)

        resolved = self.resolver.resolve_location(request)
        self.assertEqual(resolved.path, self.downloads)

    def test_explicit_file_command_is_not_stolen_by_location_parser(self) -> None:
        with self.assertRaises(NamedLocationParseError):
            parse_location_request(
                "برو درایو اچ فایل level up.mp3 رو باز کن"
            )

    def test_explicit_folder_command_is_not_stolen_by_file_parser(self) -> None:
        with self.assertRaises(NamedFileParseError):
            parse_named_file_request(
                "فولدر Project Qronos روی دسکتاپ رو باز کن"
            )

    def test_ambiguous_folder_names_fail_closed(self) -> None:
        (self.desktop / "Game Assets").mkdir()
        (self.downloads / "Game Assets").mkdir()

        request = parse_location_request("فولدر Game Assets رو باز کن")

        with self.assertRaises(NamedLocationNotFound):
            self.resolver.resolve_location(request)


class OwnershipBoundaryR44Tests(unittest.TestCase):
    def test_drive_plus_unlabelled_name_belongs_to_file_resolver(self) -> None:
        text = "برو درایو اچ جرمگیری دیژیتال رو برام باز کن"

        file_request = parse_named_file_request(text)
        self.assertEqual(file_request.folder_hint, "drive:H")
        self.assertEqual(file_request.name_hint, "جرمگیری دیژیتال")

        with self.assertRaises(NamedLocationParseError):
            parse_location_request(text)

    def test_unsupported_app_name_is_not_reinterpreted_as_a_folder(self) -> None:
        text = "Premiere رو باز کن"

        with self.assertRaises(NamedFileParseError):
            parse_named_file_request(text)

        with self.assertRaises(NamedLocationParseError):
            parse_location_request(text)

    def test_file_explorer_plus_known_folder_belongs_to_location_resolver(self) -> None:
        text = "File Explorer Downloads رو باز کن"

        with self.assertRaises(NamedFileParseError):
            parse_named_file_request(text)

        request = parse_location_request(text)
        self.assertEqual(request.folder_hint, "downloads")
        self.assertIsNone(request.name_hint)

    def test_stt_file_folder_downloads_has_no_fake_child_name(self) -> None:
        request = parse_location_request("ببین برو توی فایل فولدر دانلودز")
        self.assertEqual(request.folder_hint, "downloads")
        self.assertIsNone(request.name_hint)


if __name__ == "__main__":
    unittest.main()
