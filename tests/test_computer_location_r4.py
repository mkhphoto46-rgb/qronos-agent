from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.action_audit import ActionAuditLog
from core.actions import ActionOutcome, InvalidAction
from core.computer_executor import ComputerAction, ComputerExecutor, ComputerOperation
from core.undo_journal import UndoJournal
from security.gate import Verdict
from security.permissions import ActionCategory, PermissionDecision, PermissionLevel, PermissionPolicy


def evaluator(request, audit=None):
    verdict = Verdict(
        request=request,
        decision=PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
        outcome=ActionOutcome.AWAITING_APPROVAL,
        reason="test",
        policy=PermissionPolicy(
            category=request.category,
            level=PermissionLevel.VOICE_CONFIRMATION,
            reversible=True,
            description="test",
        ),
    )
    if audit is not None:
        audit(verdict)
    return verdict


class ComputerOpenLocationR4Tests(unittest.TestCase):
    def test_open_location_uses_open_application_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            action = ComputerAction(
                ComputerOperation.OPEN_LOCATION,
                location=str(Path(temp).resolve()),
            )
            request = action.to_request()
            self.assertIs(request.category, ActionCategory.OPEN_APPLICATION)
            self.assertEqual(request.parameters["operation"], "open_location")

    def test_relative_location_is_rejected(self) -> None:
        with self.assertRaises(InvalidAction):
            ComputerAction(ComputerOperation.OPEN_LOCATION, location="relative")

    def test_location_does_not_open_before_voice_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            calls: list[str] = []
            executor = ComputerExecutor(
                audit=ActionAuditLog(path=None),
                journal=UndoJournal(path=None),
                evaluate_fn=evaluator,
                location_opener=calls.append,
                verification_delay_seconds=0,
            )
            action = ComputerAction(
                ComputerOperation.OPEN_LOCATION,
                location=str(Path(temp).resolve()),
            )
            pending = executor.execute(action)
            self.assertIs(pending.result.outcome, ActionOutcome.AWAITING_APPROVAL)
            self.assertEqual(calls, [])

            result = executor.execute(
                action,
                approval=PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
            )
            self.assertIs(result.result.outcome, ActionOutcome.SUCCEEDED)
            self.assertEqual(calls, [str(Path(temp).resolve())])


if __name__ == "__main__":
    unittest.main()
