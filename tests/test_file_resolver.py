from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.file_resolver import (
    KnownFolderFileResolver,
    NamedFileAmbiguous,
    NamedFileNotFound,
    parse_named_file_request,
)


class ParseNamedFileRequestTests(unittest.TestCase):
    def test_quoted_partial_name_on_desktop(self) -> None:
        request = parse_named_file_request(
            'یه فایل نوت هست توی دسکتاپ به اسم "در سال 1714..." اون رو برام باز کن'
        )
        self.assertEqual(request.folder_hint, "desktop")
        self.assertEqual(request.name_hint, "در سال 1714")
        self.assertTrue(request.note_like)

    def test_unquoted_partial_name_and_desktop_typo(self) -> None:
        request = parse_named_file_request(
            "یه فایل نوت هست توی دسکتاب به اسم در سال 1714 اون رو برام بازش کن"
        )
        self.assertEqual(request.folder_hint, "desktop")
        self.assertEqual(request.name_hint, "در سال 1714")

    def test_short_form_between_file_and_location(self) -> None:
        request = parse_named_file_request(
            "فایل در سال 1714 روی دسکتاپ رو باز کن"
        )
        self.assertEqual(request.folder_hint, "desktop")
        self.assertEqual(request.name_hint, "در سال 1714")


class ResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.desktop = self.root / "Desktop"
        self.documents = self.root / "Documents"
        self.downloads = self.root / "Downloads"
        self.desktop.mkdir()
        self.documents.mkdir()
        self.downloads.mkdir()

        self.resolver = KnownFolderFileResolver(
            roots={
                "desktop": (self.desktop,),
                "documents": (self.documents,),
                "downloads": (self.downloads,),
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prefix_words_resolve_one_file(self) -> None:
        target = self.desktop / "در سال 1714 آغاز ماجرا.txt"
        target.write_text("x", encoding="utf-8")

        request = parse_named_file_request(
            "فایل در سال 1714 روی دسکتاپ رو باز کن"
        )
        resolved = self.resolver.resolve(request)

        self.assertEqual(resolved.path, target)
        self.assertGreaterEqual(resolved.score, 950)

    def test_partial_words_inside_name_resolve(self) -> None:
        target = self.desktop / "یادداشت کمپین در سال 1714 نهایی.txt"
        target.write_text("x", encoding="utf-8")

        request = parse_named_file_request(
            'فایل "سال 1714" روی دسکتاپ رو باز کن'
        )
        resolved = self.resolver.resolve(request)

        self.assertEqual(resolved.path, target)
        self.assertGreaterEqual(resolved.score, 900)

    def test_equal_prefix_matches_are_ambiguous(self) -> None:
        (self.desktop / "در سال 1714 نسخه اول.txt").write_text(
            "x", encoding="utf-8"
        )
        (self.desktop / "در سال 1714 نسخه دوم.txt").write_text(
            "x", encoding="utf-8"
        )

        request = parse_named_file_request(
            "فایل در سال 1714 روی دسکتاپ رو باز کن"
        )

        with self.assertRaises(NamedFileAmbiguous):
            self.resolver.resolve(request)

    def test_dangerous_shortcut_is_never_returned(self) -> None:
        (self.desktop / "در سال 1714.lnk").write_text(
            "shortcut", encoding="utf-8"
        )
        request = parse_named_file_request(
            "فایل در سال 1714 روی دسکتاپ رو باز کن"
        )
        with self.assertRaises(NamedFileNotFound):
            self.resolver.resolve(request)

    def test_no_location_searches_reviewed_known_folders(self) -> None:
        target = self.documents / "جلسه پروژه کرونوس.txt"
        target.write_text("x", encoding="utf-8")

        request = parse_named_file_request(
            "فایل جلسه پروژه رو باز کن"
        )
        resolved = self.resolver.resolve(request)

        self.assertEqual(resolved.path, target)
        self.assertEqual(resolved.folder_key, "documents")


if __name__ == "__main__":
    unittest.main()
