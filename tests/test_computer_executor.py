from __future__ import annotations

import unittest

from core.action_audit import ActionAuditLog
from core.actions import ActionOutcome, InvalidAction
from core.computer_executor import (
    ComputerAction,
    ComputerExecutor,
    ComputerOperation,
)
from core.undo_journal import UndoJournal
from security.gate import Verdict
from security.permissions import (
    ActionCategory,
    PermissionDecision,
    PermissionLevel,
    PermissionPolicy,
)


class FakeProcess:
    def __init__(
        self,
        *,
        pid: int = 4242,
        return_code: int | None = None,
    ) -> None:
        self.pid = pid
        self.return_code = return_code

    def poll(self) -> int | None:
        return self.return_code


def evaluator_for(
    decision: PermissionDecision,
):
    outcomes = {
        PermissionDecision.ALLOW:
            ActionOutcome.SUCCEEDED,
        PermissionDecision.REQUIRE_VOICE_CONFIRMATION:
            ActionOutcome.AWAITING_APPROVAL,
        PermissionDecision.REQUIRE_UI_CONFIRMATION:
            ActionOutcome.AWAITING_APPROVAL,
        PermissionDecision.REQUIRE_TYPED_SECRET:
            ActionOutcome.AWAITING_APPROVAL,
        PermissionDecision.DENY:
            ActionOutcome.REFUSED,
    }

    levels = {
        PermissionDecision.ALLOW:
            PermissionLevel.AUTO_ALLOW,
        PermissionDecision.REQUIRE_VOICE_CONFIRMATION:
            PermissionLevel.VOICE_CONFIRMATION,
        PermissionDecision.REQUIRE_UI_CONFIRMATION:
            PermissionLevel.UI_CONFIRMATION,
        PermissionDecision.REQUIRE_TYPED_SECRET:
            PermissionLevel.TYPED_SECRET,
        PermissionDecision.DENY:
            PermissionLevel.FORBIDDEN,
    }

    def evaluate(request, audit=None):
        verdict = Verdict(
            request=request,
            decision=decision,
            outcome=outcomes[decision],
            reason="test verdict",
            policy=PermissionPolicy(
                category=request.category,
                level=levels[decision],
                reversible=True,
                description="test policy",
            ),
        )
        if audit is not None:
            audit(verdict)
        return verdict

    return evaluate


class ComputerExecutorTests(unittest.TestCase):
    def action(self) -> ComputerAction:
        return ComputerAction(
            ComputerOperation.OPEN_APP,
            app_id="notepad",
        )

    def executor(
        self,
        decision: PermissionDecision,
        *,
        launcher=None,
    ) -> ComputerExecutor:
        return ComputerExecutor(
            audit=ActionAuditLog(path=None),
            journal=UndoJournal(path=None),
            evaluate_fn=evaluator_for(decision),
            launcher=launcher,
            verification_delay_seconds=0,
        )

    def test_open_app_uses_open_application_policy(self) -> None:
        request = self.action().to_request()
        self.assertIs(
            request.category,
            ActionCategory.OPEN_APPLICATION,
        )
        self.assertEqual(request.target, "app:notepad")
        self.assertEqual(
            request.parameters,
            {
                "operation": "open_app",
                "appId": "notepad",
            },
        )

    def test_unknown_application_is_rejected_before_gate(self) -> None:
        with self.assertRaises(InvalidAction):
            ComputerAction(
                ComputerOperation.OPEN_APP,
                app_id="powershell",
            )

    def test_refused_action_never_launches(self) -> None:
        calls = []
        executor = self.executor(
            PermissionDecision.DENY,
            launcher=lambda command: calls.append(command),
        )
        result = executor.execute(self.action())
        self.assertIs(
            result.result.outcome,
            ActionOutcome.REFUSED,
        )
        self.assertEqual(calls, [])

    def test_confirmation_action_never_runs_without_exact_approval(self) -> None:
        calls = []
        executor = self.executor(
            PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
            launcher=lambda command: calls.append(command),
        )
        result = executor.execute(self.action())
        self.assertIs(
            result.result.outcome,
            ActionOutcome.AWAITING_APPROVAL,
        )
        self.assertEqual(calls, [])

    def test_wrong_confirmation_kind_does_not_run(self) -> None:
        calls = []
        executor = self.executor(
            PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
            launcher=lambda command: calls.append(command),
        )
        result = executor.execute(
            self.action(),
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )
        self.assertIs(
            result.result.outcome,
            ActionOutcome.AWAITING_APPROVAL,
        )
        self.assertEqual(calls, [])

    def test_exact_voice_confirmation_launches_one_static_executable(self) -> None:
        calls = []

        def launch(command):
            calls.append(command)
            return FakeProcess()

        executor = self.executor(
            PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
            launcher=launch,
        )
        result = executor.execute(
            self.action(),
            approval=PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
        )
        self.assertIs(
            result.result.outcome,
            ActionOutcome.SUCCEEDED,
        )
        self.assertEqual(calls, [("notepad.exe",)])

    def test_nonzero_immediate_exit_is_failed(self) -> None:
        executor = self.executor(
            PermissionDecision.ALLOW,
            launcher=lambda command: FakeProcess(
                return_code=5,
            ),
        )
        result = executor.execute(self.action())
        self.assertIs(
            result.result.outcome,
            ActionOutcome.FAILED,
        )
        self.assertIn(
            "exited with code 5",
            result.result.detail,
        )

    def test_zero_exit_is_accepted_as_windows_delegation(self) -> None:
        executor = self.executor(
            PermissionDecision.ALLOW,
            launcher=lambda command: FakeProcess(
                return_code=0,
            ),
        )
        result = executor.execute(self.action())
        self.assertIs(
            result.result.outcome,
            ActionOutcome.SUCCEEDED,
        )
        self.assertIn(
            "accepted/delegated",
            result.result.detail,
        )


if __name__ == "__main__":
    unittest.main()
