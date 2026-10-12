from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.actions import ActionOutcome, ActionRequest
from core.permission_control import (
    HARD_LOCKED_CATEGORIES,
    permission_store_path,
    reset_permission,
    set_custom_rule,
    set_permission_policy,
    snapshot_user_permissions,
)
from security.gate import evaluate
from security.permissions import (
    ActionCategory,
    PermissionDecision,
    PermissionLevel,
    evaluate_action,
    get_effective_permission_policy,
    get_permission_policy,
)


class PermissionControlV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        root = Path(self.temp.name)
        self.store = root / "permissions_v2.json"
        self.audit = root / "action_audit.jsonl"

        self.old_store = os.environ.get("QRONOS_PERMISSION_POLICY_PATH")
        self.old_audit = os.environ.get("QRONOS_PERMISSION_AUDIT_PATH")

        os.environ["QRONOS_PERMISSION_POLICY_PATH"] = str(self.store)
        os.environ["QRONOS_PERMISSION_AUDIT_PATH"] = str(self.audit)

        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self.old_store is None:
            os.environ.pop("QRONOS_PERMISSION_POLICY_PATH", None)
        else:
            os.environ["QRONOS_PERMISSION_POLICY_PATH"] = self.old_store

        if self.old_audit is None:
            os.environ.pop("QRONOS_PERMISSION_AUDIT_PATH", None)
        else:
            os.environ["QRONOS_PERMISSION_AUDIT_PATH"] = self.old_audit

    def test_user_facing_permission_count_is_nine(self) -> None:
        items = snapshot_user_permissions()
        self.assertEqual(
            [item["id"] for item in items],
            [
                "files",
                "apps",
                "browser",
                "microphone",
                "camera",
                "screen",
                "clipboard",
                "automation",
                "system",
            ],
        )

    def test_no_override_preserves_security_baseline(self) -> None:
        default = get_permission_policy(ActionCategory.READ_SCREEN)
        effective = get_effective_permission_policy(ActionCategory.READ_SCREEN)
        self.assertIs(effective.level, default.level)
        self.assertIs(
            evaluate_action(ActionCategory.READ_SCREEN),
            PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

    def test_user_policy_persists_and_changes_gate_decision(self) -> None:
        set_permission_policy("screen", "DENY")

        self.assertTrue(self.store.is_file())
        data = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(data["permissions"]["screen"]["policy"], "DENY")

        self.assertIs(
            get_effective_permission_policy(ActionCategory.READ_SCREEN).level,
            PermissionLevel.FORBIDDEN,
        )

        request = ActionRequest(
            category=ActionCategory.READ_SCREEN,
            target="desktop",
            summary="Read the current screen.",
        )
        verdict = evaluate(request)
        self.assertIs(verdict.outcome, ActionOutcome.REFUSED)
        self.assertIs(verdict.decision, PermissionDecision.DENY)

    def test_allow_changes_non_locked_category(self) -> None:
        set_permission_policy("browser", "ALLOW")
        self.assertIs(
            get_effective_permission_policy(
                ActionCategory.BROWSER_NAVIGATION
            ).level,
            PermissionLevel.AUTO_ALLOW,
        )
        self.assertIs(
            evaluate_action(ActionCategory.BROWSER_NAVIGATION),
            PermissionDecision.ALLOW,
        )

    def test_custom_rule_is_per_category(self) -> None:
        set_permission_policy("browser", "CUSTOM")
        set_custom_rule("browser", "browser_navigation", "ALLOW")
        set_custom_rule("browser", "upload_data", "DENY")

        self.assertIs(
            evaluate_action(ActionCategory.BROWSER_NAVIGATION),
            PermissionDecision.ALLOW,
        )
        self.assertIs(
            evaluate_action(ActionCategory.UPLOAD_DATA),
            PermissionDecision.DENY,
        )

    def test_hard_floor_rejects_unlock_attempt(self) -> None:
        self.assertIn("security_bypass", HARD_LOCKED_CATEGORIES)

        with self.assertRaises(ValueError):
            set_custom_rule("system", "security_bypass", "ALLOW")

        self.assertIs(
            get_effective_permission_policy(
                ActionCategory.SECURITY_BYPASS
            ).level,
            PermissionLevel.FORBIDDEN,
        )

    def test_broad_system_allow_cannot_unlock_hard_floor(self) -> None:
        set_permission_policy("system", "ALLOW")

        self.assertIs(
            get_effective_permission_policy(
                ActionCategory.SYSTEM_CONFIGURATION
            ).level,
            PermissionLevel.AUTO_ALLOW,
        )

        self.assertIs(
            get_effective_permission_policy(
                ActionCategory.PRIVILEGE_ESCALATION
            ).level,
            PermissionLevel.FORBIDDEN,
        )

    def test_reset_restores_default(self) -> None:
        set_permission_policy("screen", "DENY")
        reset_permission("screen")

        self.assertIs(
            evaluate_action(ActionCategory.READ_SCREEN),
            PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

    def test_corrupt_store_fails_back_to_security_default(self) -> None:
        self.store.write_text("{not-json", encoding="utf-8")

        self.assertIs(
            evaluate_action(ActionCategory.READ_SCREEN),
            PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

    def test_permission_changes_are_audited(self) -> None:
        set_permission_policy("screen", "DENY")

        records = [
            json.loads(line)
            for line in self.audit.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        self.assertEqual(records[-1]["event"], "permission_changed")
        self.assertEqual(records[-1]["category"], "screen")
        self.assertEqual(records[-1]["decision"], "deny")

    def test_permission_store_path_uses_override(self) -> None:
        self.assertEqual(permission_store_path(), self.store)


if __name__ == "__main__":
    unittest.main()
