from __future__ import annotations

import unittest

from security.permissions import (
    PermissionDecision,
    PermissionLevel,
    evaluate_permission,
)


class TestPermissionEngine(unittest.TestCase):
    def test_safe_read_is_allowed(self) -> None:
        result = evaluate_permission(PermissionLevel.SAFE_READ)
        self.assertEqual(result, PermissionDecision.ALLOW)

    def test_file_changes_require_approval(self) -> None:
        result = evaluate_permission(PermissionLevel.CREATE_OR_EDIT)
        self.assertEqual(result, PermissionDecision.REQUIRE_APPROVAL)

    def test_running_app_requires_approval(self) -> None:
        result = evaluate_permission(PermissionLevel.RUN_APPLICATION)
        self.assertEqual(result, PermissionDecision.REQUIRE_APPROVAL)

    def test_system_control_requires_approval(self) -> None:
        result = evaluate_permission(PermissionLevel.CONTROL_SYSTEM)
        self.assertEqual(result, PermissionDecision.REQUIRE_APPROVAL)

    def test_sensitive_actions_are_denied(self) -> None:
        result = evaluate_permission(PermissionLevel.SENSITIVE)
        self.assertEqual(result, PermissionDecision.DENY)


if __name__ == "__main__":
    unittest.main()