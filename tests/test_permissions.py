from __future__ import annotations

import unittest

from security.permissions import (
    ACTION_POLICIES,
    ActionCategory,
    PermissionDecision,
    PermissionLevel,
    evaluate_action,
    evaluate_permission,
    get_permission_policy,
)


class TestPermissionEngine(unittest.TestCase):
    def test_every_action_category_has_an_explicit_policy(self) -> None:
        self.assertEqual(
            set(ACTION_POLICIES),
            set(ActionCategory),
        )

    def test_auto_allow_level_is_allowed(self) -> None:
        result = evaluate_permission(
            PermissionLevel.AUTO_ALLOW,
        )
        self.assertEqual(result, PermissionDecision.ALLOW)

    def test_voice_level_requires_voice_confirmation(self) -> None:
        result = evaluate_permission(
            PermissionLevel.VOICE_CONFIRMATION,
        )
        self.assertEqual(
            result,
            PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
        )

    def test_ui_level_requires_ui_confirmation(self) -> None:
        result = evaluate_permission(
            PermissionLevel.UI_CONFIRMATION,
        )
        self.assertEqual(
            result,
            PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

    def test_typed_secret_level_requires_typed_secret(self) -> None:
        result = evaluate_permission(
            PermissionLevel.TYPED_SECRET,
        )
        self.assertEqual(
            result,
            PermissionDecision.REQUIRE_TYPED_SECRET,
        )

    def test_forbidden_level_is_denied(self) -> None:
        result = evaluate_permission(
            PermissionLevel.FORBIDDEN,
        )
        self.assertEqual(result, PermissionDecision.DENY)
        self.assertEqual(PermissionLevel.FORBIDDEN.value, 5)

    def test_open_application_accepts_voice_confirmation(self) -> None:
        result = evaluate_action(
            ActionCategory.OPEN_APPLICATION,
        )
        self.assertEqual(
            result,
            PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
        )

    def test_file_edit_requires_ui_confirmation(self) -> None:
        result = evaluate_action(
            ActionCategory.CREATE_OR_EDIT_FILE,
        )
        self.assertEqual(
            result,
            PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

    def test_install_requires_typed_secret(self) -> None:
        result = evaluate_action(
            ActionCategory.INSTALL_SOFTWARE,
        )
        self.assertEqual(
            result,
            PermissionDecision.REQUIRE_TYPED_SECRET,
        )

    def test_security_bypass_is_always_denied(self) -> None:
        result = evaluate_action(
            ActionCategory.SECURITY_BYPASS,
        )
        self.assertEqual(result, PermissionDecision.DENY)

    def test_hidden_surveillance_is_always_denied(self) -> None:
        result = evaluate_action(
            ActionCategory.HIDDEN_SURVEILLANCE,
        )
        self.assertEqual(result, PermissionDecision.DENY)

    def test_code_analysis_is_always_denied(self) -> None:
        result = evaluate_action(
            ActionCategory.CODE_ANALYSIS,
        )
        self.assertEqual(result, PermissionDecision.DENY)

    def test_code_generation_is_always_denied(self) -> None:
        result = evaluate_action(
            ActionCategory.CODE_GENERATION,
        )
        self.assertEqual(result, PermissionDecision.DENY)

    def test_registry_modification_is_always_denied(self) -> None:
        result = evaluate_action(
            ActionCategory.REGISTRY_MODIFICATION,
        )
        self.assertEqual(result, PermissionDecision.DENY)

    def test_raw_disk_access_is_always_denied(self) -> None:
        result = evaluate_action(
            ActionCategory.RAW_DISK_ACCESS,
        )
        self.assertEqual(result, PermissionDecision.DENY)

    def test_cyber_attack_is_always_denied(self) -> None:
        result = evaluate_action(ActionCategory.CYBER_ATTACK)
        self.assertEqual(result, PermissionDecision.DENY)

    def test_unauthorized_persistence_is_always_denied(self) -> None:
        result = evaluate_action(
            ActionCategory.UNAUTHORIZED_PERSISTENCE,
        )
        self.assertEqual(result, PermissionDecision.DENY)

    def test_recoverable_delete_policy_is_reversible(self) -> None:
        policy = get_permission_policy(
            ActionCategory.DELETE_RECOVERABLE,
        )
        self.assertTrue(policy.reversible)
        self.assertEqual(
            policy.level,
            PermissionLevel.TYPED_SECRET,
        )

    def test_irreversible_destruction_is_not_reversible(self) -> None:
        policy = get_permission_policy(
            ActionCategory.IRREVERSIBLE_DESTRUCTION,
        )
        self.assertFalse(policy.reversible)
        self.assertEqual(
            policy.level,
            PermissionLevel.FORBIDDEN,
        )


if __name__ == "__main__":
    unittest.main()
