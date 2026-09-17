from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.file_resolver import (
    KnownFolderFileResolver,
    NamedFileParseError,
    NamedLocationParseError,
    is_computer_capability_query,
    parse_location_request,
    parse_named_file_request,
)


class FileResolverR4Tests(unittest.TestCase):
    def test_spoken_persian_new_text_document_matches_latin_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "New Text Document (2).txt"
            target.write_text("x", encoding="utf-8")
            resolver = KnownFolderFileResolver(
                roots={"desktop": (root,), "documents": (), "downloads": ()},
                max_entries=100,
                max_depth=2,
            )
            request = parse_named_file_request(
                "فایل روی دسکتاپ به اسم نیو تکس دایکیومنت دو رو باز کن"
            )
            self.assertEqual(request.name_hint, "new text document 2")
            self.assertEqual(resolver.resolve(request).path, target)


    def test_filename_leading_dar_is_preserved(self) -> None:
        request = parse_named_file_request(
            "فایل در سال 1714 روی دسکتاپ رو باز کن"
        )
        self.assertEqual(request.name_hint, "در سال 1714")

    def test_filename_leading_dar_keeps_prefix_strength(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "در سال 1714 آغاز دوران.txt"
            target.write_text("x", encoding="utf-8")
            resolver = KnownFolderFileResolver(
                roots={"desktop": (root,), "documents": (), "downloads": ()},
                max_entries=100,
                max_depth=2,
            )
            request = parse_named_file_request(
                "فایل در سال 1714 روی دسکتاپ رو باز کن"
            )
            resolved = resolver.resolve(request)
            self.assertEqual(resolved.path, target)
            self.assertGreaterEqual(resolved.score, 950)

    def test_document_inside_filename_does_not_mean_documents_folder(self) -> None:
        request = parse_named_file_request(
            "توی desktop کامپیوتر new text document بازش کن"
        )
        self.assertEqual(request.folder_hint, "desktop")
        self.assertEqual(request.name_hint, "new text document")

    def test_drive_h_persian_alias_is_understood(self) -> None:
        request = parse_named_file_request(
            "برو درایو اچ جرمگیری دیژیتال رو برام باز کن"
        )
        self.assertEqual(request.folder_hint, "drive:H")
        self.assertEqual(request.name_hint, "جرمگیری دیژیتال")

    def test_drive_h_compact_english_alias_is_understood(self) -> None:
        request = parse_named_file_request(
            "برو DriveH و فایل level up.mp3 رو برام باز کن"
        )
        self.assertEqual(request.folder_hint, "drive:H")
        self.assertEqual(request.name_hint, "level up")
        self.assertEqual(request.suffix_hint, ".mp3")

    def test_j_zh_confusion_is_relaxed_for_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "جرمگیری دیجیتال.pdf"
            target.write_text("x", encoding="utf-8")
            resolver = KnownFolderFileResolver(
                roots={"desktop": (root,), "documents": (), "downloads": ()},
                max_entries=100,
                max_depth=2,
            )
            request = parse_named_file_request(
                "فایل جرمگیری دیژیتال روی دسکتاپ رو باز کن"
            )
            self.assertEqual(resolver.resolve(request).path, target)

    def test_downloadz_stt_variant_opens_downloads_location(self) -> None:
        request = parse_location_request("ببین برو توی فایل فولدر دانلودز")
        self.assertEqual(request.folder_hint, "downloads")

    def test_drive_root_can_be_a_location_request(self) -> None:
        request = parse_location_request("برو درایو اچ")
        self.assertEqual(request.folder_hint, "drive:H")

    def test_filename_after_drive_is_not_misread_as_open_drive(self) -> None:
        with self.assertRaises(NamedLocationParseError):
            parse_location_request(
                "برو درایو اچ جرمگیری دیژیتال رو برام باز کن"
            )

    def test_polite_mitooni_open_request_is_not_misread_as_capability(self) -> None:
        text = "میتونی فایل در سال 1714 روی دسکتاپ رو باز کنی"
        self.assertFalse(is_computer_capability_query(text))

    def test_capability_question_is_not_an_open_file_action(self) -> None:
        text = "خب الان چه فایل هایی رو میتونی برام باز کنی؟"
        self.assertTrue(is_computer_capability_query(text))
        with self.assertRaises(NamedFileParseError):
            parse_named_file_request(text)


if __name__ == "__main__":
    unittest.main()
