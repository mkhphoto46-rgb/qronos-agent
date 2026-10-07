from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.action_audit import ActionAuditLog
from core.computer_executor import ComputerExecutor
from core.runtime_bridge import (
    QronosRuntime,
    VOICE_COMPUTER_DEFERRED_RESPONSE,
)
from core.undo_journal import UndoJournal
from security.gate import set_default_audit_sink


class FakeProcess:
    pid = 5151

    def poll(self):
        return None


class ComputerRuntimeV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        self.old_policy = os.environ.get(
            "QRONOS_PERMISSION_POLICY_PATH"
        )
        self.old_permission_audit = os.environ.get(
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

        self.calls = []

        audit = ActionAuditLog(path=None)
        journal = UndoJournal(path=None)

        self.runtime.action_audit = audit
        self.runtime.undo_journal = journal

        # Unit tests must never inspect or launch applications installed on
        # the developer's real Windows machine.
        #
        # Without this isolation, a phrase such as "Premiere رو باز کن" may
        # resolve the real Premiere installation through Start Menu / App Paths
        # discovery and launch it during pytest.
        self.runtime.computer_executor = ComputerExecutor(
            audit=audit,
            journal=journal,
            launcher=self.launch,
            verification_delay_seconds=0,
            dynamic_apps=False,
        )

    def launch(self, command):
        self.calls.append(command)
        return FakeProcess()

    def tearDown(self) -> None:
        set_default_audit_sink(None)

        if self.old_policy is None:
            os.environ.pop(
                "QRONOS_PERMISSION_POLICY_PATH",
                None,
            )
        else:
            os.environ[
                "QRONOS_PERMISSION_POLICY_PATH"
            ] = self.old_policy

        if self.old_permission_audit is None:
            os.environ.pop(
                "QRONOS_PERMISSION_AUDIT_PATH",
                None,
            )
        else:
            os.environ[
                "QRONOS_PERMISSION_AUDIT_PATH"
            ] = self.old_permission_audit

        self.runtime.close()
        self.temp.cleanup()

    def test_explicit_voice_command_satisfies_voice_confirmation(self) -> None:
        report = {}

        response = self.runtime._execute_voice_computer_action(
            "نوت پد رو باز کن",
            report,
        )

        self.assertEqual(
            self.calls,
            [("notepad.exe",)],
        )
        self.assertTrue(
            report.get("voiceComputerExecuted")
        )
        self.assertFalse(
            report.get("voiceComputerDeferred")
        )
        self.assertIn("باز شد", response)

    def test_unsupported_app_stays_deferred_and_never_launches(self) -> None:
        report = {}

        response = self.runtime._execute_voice_computer_action(
            "Premiere رو باز کن",
            report,
        )

        self.assertEqual(self.calls, [])
        self.assertTrue(
            report.get("voiceComputerDeferred")
        )
        self.assertEqual(
            response,
            VOICE_COMPUTER_DEFERRED_RESPONSE,
        )


if __name__ == "__main__":
    unittest.main()
