from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.action_audit import ActionAuditLog
from core.actions import ActionOutcome, InvalidAction
from core.computer_executor import (
    ComputerAction,
    ComputerExecutor,
    ComputerOperation,
)
from core.undo_journal import UndoJournal
from core.window_binding import WindowBindingUnavailable
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


    def test_packaged_app_uses_aumid_activation_and_binds_exact_returned_pid(self) -> None:
        executable_calls = []
        packaged_calls = []
        bind_calls = []
        binding = SimpleNamespace(hwnd=9876)

        class FakeBinder:
            def bind(
                self,
                target,
                *,
                require_foreground=False,
                expected_pid=None,
            ):
                bind_calls.append((target.app_id, require_foreground, expected_pid))
                return binding

            def revalidate(self, binding, **kwargs):
                return "snapshot"

        executor = ComputerExecutor(
            audit=ActionAuditLog(path=None),
            journal=UndoJournal(path=None),
            evaluate_fn=evaluator_for(PermissionDecision.ALLOW),
            launcher=lambda command: executable_calls.append(command),
            packaged_launcher=lambda aumid: packaged_calls.append(aumid) or 4321,
            window_binder=FakeBinder(),
            verification_delay_seconds=0,
        )

        result = executor.execute(
            ComputerAction(ComputerOperation.OPEN_APP, app_id="paint")
        )

        self.assertIs(result.result.outcome, ActionOutcome.SUCCEEDED)
        self.assertEqual(executable_calls, [])
        self.assertEqual(
            packaged_calls,
            ["Microsoft.Paint_8wekyb3d8bbwe!App"],
        )
        self.assertEqual(bind_calls, [("paint", False, 4321)])
        self.assertIs(result.window_binding, binding)
        self.assertIn("pid=4321", result.result.detail)
        self.assertIn("hwnd=9876", result.result.detail)
        self.assertIn("exact-pid window binding succeeded", result.result.detail)


    def test_calculator_uses_packaged_activation_and_exact_pid_binding(self) -> None:
        executable_calls = []
        packaged_calls = []
        bind_calls = []

        binding = SimpleNamespace(
            hwnd=2468
        )

        class FakeBinder:
            def bind(
                self,
                target,
                *,
                require_foreground=False,
                expected_pid=None,
            ):
                bind_calls.append(
                    (
                        target.app_id,
                        tuple(target.process_names),
                        require_foreground,
                        expected_pid,
                    )
                )

                return binding

            def revalidate(
                self,
                binding,
                **kwargs,
            ):
                return "snapshot"

        executor = ComputerExecutor(
            audit=ActionAuditLog(
                path=None
            ),
            journal=UndoJournal(
                path=None
            ),
            evaluate_fn=evaluator_for(
                PermissionDecision.ALLOW
            ),
            launcher=lambda command: executable_calls.append(
                command
            ),
            packaged_launcher=lambda aumid: (
                packaged_calls.append(
                    aumid
                )
                or 6728
            ),
            window_binder=FakeBinder(),
            verification_delay_seconds=0,
        )

        result = executor.execute(
            ComputerAction(
                ComputerOperation.OPEN_APP,
                app_id="calculator",
            )
        )

        self.assertIs(
            result.result.outcome,
            ActionOutcome.SUCCEEDED,
        )

        # calc.exe must NOT be used anymore.
        self.assertEqual(
            executable_calls,
            [],
        )

        self.assertEqual(
            packaged_calls,
            [
                "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
            ],
        )

        self.assertEqual(
            len(bind_calls),
            1,
        )

        (
            app_id,
            process_names,
            require_foreground,
            expected_pid,
        ) = bind_calls[0]

        self.assertEqual(
            app_id,
            "calculator",
        )

        self.assertIn(
            "calculatorapp.exe",
            process_names,
        )

        self.assertFalse(
            require_foreground
        )

        self.assertEqual(
            expected_pid,
            6728,
        )

        self.assertIs(
            result.window_binding,
            binding,
        )

        self.assertIn(
            "pid=6728",
            result.result.detail,
        )

        self.assertIn(
            "hwnd=2468",
            result.result.detail,
        )

        self.assertIn(
            "exact-pid window binding succeeded",
            result.result.detail,
        )


    def test_packaged_app_launch_fails_when_exact_returned_pid_has_no_window(self) -> None:
        class FakeBinder:
            def bind(
                self,
                target,
                *,
                require_foreground=False,
                expected_pid=None,
            ):
                raise WindowBindingUnavailable("not ready")

            def revalidate(self, binding, **kwargs):
                return "snapshot"

        executor = ComputerExecutor(
            audit=ActionAuditLog(path=None),
            journal=UndoJournal(path=None),
            evaluate_fn=evaluator_for(PermissionDecision.ALLOW),
            packaged_launcher=lambda aumid: 4321,
            window_binder=FakeBinder(),
            launch_binding_attempts=1,
            launch_binding_poll_seconds=0,
            verification_delay_seconds=0,
        )

        result = executor.execute(
            ComputerAction(ComputerOperation.OPEN_APP, app_id="paint")
        )

        self.assertIs(result.result.outcome, ActionOutcome.FAILED)
        self.assertIsNone(result.window_binding)
        self.assertIn("exact process", result.result.detail)
        self.assertIn("pid=4321", result.result.detail)

    def test_packaged_app_activation_requires_a_valid_pid(self) -> None:
        executor = ComputerExecutor(
            audit=ActionAuditLog(path=None),
            journal=UndoJournal(path=None),
            evaluate_fn=evaluator_for(PermissionDecision.ALLOW),
            packaged_launcher=lambda aumid: 0,
            verification_delay_seconds=0,
        )

        result = executor.execute(
            ComputerAction(ComputerOperation.OPEN_APP, app_id="paint")
        )

        self.assertIs(result.result.outcome, ActionOutcome.FAILED)
        self.assertIn("valid process identifier", result.result.detail)

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


    def test_window_binding_uses_exact_app_identity(self) -> None:
        seen = []

        class FakeBinder:
            def bind(self, target, *, require_foreground=False):
                seen.append((target, require_foreground))
                return "binding"

            def revalidate(self, binding, **kwargs):
                return "snapshot"

        executor = ComputerExecutor(
            window_binder=FakeBinder(),
            verification_delay_seconds=0,
        )

        result = executor.bind_app_window(
            "paint",
            require_foreground=True,
        )

        self.assertEqual(result, "binding")
        target, foreground = seen[0]
        self.assertEqual(target.app_id, "paint")
        self.assertEqual(target.executable, "mspaint.exe")
        self.assertIn("mspaint.exe", target.process_names)
        self.assertTrue(foreground)

    def test_window_revalidation_delegates_to_same_binder(self) -> None:
        calls = []

        class FakeBinder:
            def bind(self, target, *, require_foreground=False):
                return "binding"

            def revalidate(self, binding, **kwargs):
                calls.append((binding, kwargs))
                return "snapshot"

        executor = ComputerExecutor(
            window_binder=FakeBinder(),
            verification_delay_seconds=0,
        )

        result = executor.revalidate_window_binding(
            "binding",
            require_foreground=False,
            require_same_layout=True,
        )

        self.assertEqual(result, "snapshot")
        self.assertEqual(
            calls,
            [(
                "binding",
                {
                    "require_foreground": False,
                    "require_same_layout": True,
                },
            )],
        )


    def test_packaged_app_waits_for_window_hosting_and_layout_to_settle(self) -> None:
        from unittest.mock import patch

        from core.window_binding import (
            WindowRect,
            WindowSnapshot,
        )

        def snap(
            *,
            client,
            rect,
        ):
            return WindowSnapshot(
                hwnd=9876,
                pid=4321,
                thread_id=55,
                title="Paint",
                process_path=r"C:\Windows\System32\mspaint.exe",
                process_start_time_100ns=123456,
                window_rect=rect,
                client_rect=client,
                dpi=96,
                visible=True,
                minimized=False,
                foreground=False,
            )

        early = snap(
            rect=WindowRect(
                0,
                0,
                600,
                600,
            ),
            client=WindowRect(
                0,
                1,
                587,
                585,
            ),
        )

        hosted = snap(
            rect=WindowRect(
                1280,
                140,
                1880,
                740,
            ),
            client=WindowRect(
                1280,
                142,
                1867,
                726,
            ),
        )

        observations = [
            early,
            hosted,
            hosted,
            hosted,
            hosted,
        ]

        roots = [
            9876,
            7000,
            7000,
            7000,
            7000,
        ]

        class SettlingBinder:
            def __init__(self):
                self.bind_calls = []
                self.bind_snapshot_calls = []
                self.revalidate_calls = 0

            def bind(
                self,
                target,
                *,
                require_foreground=False,
                expected_pid=None,
            ):
                self.bind_calls.append(
                    (
                        target.app_id,
                        expected_pid,
                    )
                )

                return SimpleNamespace(
                    hwnd=9876
                )

            def bind_snapshot(
                self,
                snapshot,
                *,
                app_id,
                display_name,
                require_foreground=False,
            ):
                self.bind_snapshot_calls.append(
                    (
                        snapshot.hwnd,
                        snapshot.pid,
                        app_id,
                        display_name,
                        require_foreground,
                    )
                )

                return SimpleNamespace(
                    hwnd=snapshot.hwnd
                )

            def revalidate(
                self,
                binding,
                **kwargs,
            ):
                index = min(
                    self.revalidate_calls,
                    len(observations) - 1,
                )

                self.revalidate_calls += 1

                return observations[
                    index
                ]

        binder = SettlingBinder()

        executor = ComputerExecutor(
            audit=ActionAuditLog(
                path=None
            ),
            journal=UndoJournal(
                path=None
            ),
            evaluate_fn=evaluator_for(
                PermissionDecision.ALLOW
            ),
            packaged_launcher=lambda aumid: 4321,
            window_binder=binder,
            sleep_fn=lambda seconds: None,
            launch_binding_attempts=10,
            launch_binding_poll_seconds=0,
            launch_binding_settle_observations=3,
            verification_delay_seconds=0,
        )

        root_values = iter(
            roots
        )

        with patch(
            "core.computer_executor._root_window_handle",
            side_effect=lambda hwnd: next(
                root_values,
                7000,
            ),
        ):
            result = executor.execute(
                ComputerAction(
                    ComputerOperation.OPEN_APP,
                    app_id="paint",
                )
            )

        self.assertIs(
            result.result.outcome,
            ActionOutcome.SUCCEEDED,
        )

        # Only the initial exact-PID acquisition may use top-level
        # enumeration. The final settled capability must be issued directly
        # from the retained content HWND snapshot.
        self.assertEqual(
            binder.bind_calls,
            [
                ("paint", 4321),
            ],
        )

        self.assertEqual(
            len(
                binder.bind_snapshot_calls
            ),
            1,
        )

        (
            final_hwnd,
            final_pid,
            final_app_id,
            final_display_name,
            final_foreground,
        ) = binder.bind_snapshot_calls[0]

        self.assertEqual(
            final_hwnd,
            9876,
        )

        self.assertEqual(
            final_pid,
            4321,
        )

        self.assertEqual(
            final_app_id,
            "paint",
        )

        self.assertEqual(
            final_display_name,
            "Paint",
        )

        self.assertFalse(
            final_foreground
        )

        # The early unhosted layout must not be accepted as settled.
        self.assertGreaterEqual(
            binder.revalidate_calls,
            5,
        )

        self.assertIn(
            "exact-pid window binding succeeded",
            result.result.detail,
        )


if __name__ == "__main__":
    unittest.main()
