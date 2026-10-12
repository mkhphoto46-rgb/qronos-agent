from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.action_audit import ActionAuditLog
from core.file_executor import FileExecutor
from core.file_resolver import KnownFolderFileResolver
from core.runtime_bridge import QronosRuntime
from core.undo_journal import UndoJournal
from security.gate import set_default_audit_sink


class NamedFileVoiceRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.desktop = self.root / "Desktop"
        self.documents = self.root / "Documents"
        self.downloads = self.root / "Downloads"
        self.desktop.mkdir()
        self.documents.mkdir()
        self.downloads.mkdir()

        self.old_policy = os.environ.get(
            "QRONOS_PERMISSION_POLICY_PATH"
        )
        self.old_audit = os.environ.get(
            "QRONOS_PERMISSION_AUDIT_PATH"
        )
        os.environ["QRONOS_PERMISSION_POLICY_PATH"] = str(
            self.root / "permissions.json"
        )
        os.environ["QRONOS_PERMISSION_AUDIT_PATH"] = str(
            self.root / "permission_audit.jsonl"
        )

        self.events = []
        self.runtime = QronosRuntime(
            notify=lambda event, status, message: self.events.append(
                (event, status, message)
            )
        )

        audit = ActionAuditLog(path=None)
        journal = UndoJournal(path=None)
        self.opened: list[str] = []
        self.runtime.action_audit = audit
        self.runtime.undo_journal = journal
        self.runtime.file_executor = FileExecutor(
            audit=audit,
            journal=journal,
            opener=self.opened.append,
        )
        self.runtime.file_resolver = KnownFolderFileResolver(
            roots={
                "desktop": (self.desktop,),
                "documents": (self.documents,),
                "downloads": (self.downloads,),
            }
        )

    def tearDown(self) -> None:
        set_default_audit_sink(None)

        if self.old_policy is None:
            os.environ.pop("QRONOS_PERMISSION_POLICY_PATH", None)
        else:
            os.environ["QRONOS_PERMISSION_POLICY_PATH"] = self.old_policy

        if self.old_audit is None:
            os.environ.pop("QRONOS_PERMISSION_AUDIT_PATH", None)
        else:
            os.environ["QRONOS_PERMISSION_AUDIT_PATH"] = self.old_audit

        self.runtime.close()
        self.temp.cleanup()

    def test_voice_partial_filename_opens_unique_desktop_file(self) -> None:
        target = self.desktop / "در سال 1714 آغاز عصر دزدان دریایی.txt"
        target.write_text("qronos", encoding="utf-8")

        report = {}
        response = self.runtime._execute_voice_computer_action(
            "یه فایل نوت هست توی دسکتاب به اسم در سال 1714 اون رو برام باز کن",
            report,
        )

        self.assertEqual(self.opened, [str(target.resolve())])
        self.assertTrue(report.get("voiceNamedFileExecuted"))
        self.assertFalse(report.get("voiceComputerDeferred"))
        self.assertIn("باز شد", response)

    def test_voice_quoted_partial_filename_works(self) -> None:
        target = self.desktop / "در سال 1714 شروع کمپین.txt"
        target.write_text("qronos", encoding="utf-8")

        report = {}
        response = self.runtime._execute_voice_computer_action(
            'فایل "در سال 1714..." روی دسکتاپ رو باز کن',
            report,
        )

        self.assertEqual(self.opened, [str(target.resolve())])
        self.assertIn(target.name, response)

    def test_voice_ambiguous_prefix_does_not_open_anything(self) -> None:
        (self.desktop / "در سال 1714 نسخه اول.txt").write_text(
            "x", encoding="utf-8"
        )
        (self.desktop / "در سال 1714 نسخه دوم.txt").write_text(
            "x", encoding="utf-8"
        )

        report = {}
        response = self.runtime._execute_voice_computer_action(
            "فایل در سال 1714 روی دسکتاپ رو باز کن",
            report,
        )

        self.assertEqual(self.opened, [])
        self.assertTrue(report.get("voiceNamedFileAmbiguous"))
        self.assertIn("چند فایل", response)


if __name__ == "__main__":
    unittest.main()
