from __future__ import annotations

from enum import Enum


class PermissionLevel(Enum):
    """Risk levels for Qronos actions."""

    SAFE_READ = "safe_read"
    CREATE_OR_EDIT = "create_or_edit"
    RUN_APPLICATION = "run_application"
    CONTROL_SYSTEM = "control_system"
    SENSITIVE = "sensitive"


class PermissionDecision(Enum):
    """Possible permission decisions."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


def evaluate_permission(level: PermissionLevel) -> PermissionDecision:
    """
    Evaluate whether an action can run.

    For now, Qronos only allows read-only actions automatically.
    All actions that can change the computer require user approval.
    Sensitive actions are denied by default.
    """

    if level is PermissionLevel.SAFE_READ:
        return PermissionDecision.ALLOW

    if level in {
        PermissionLevel.CREATE_OR_EDIT,
        PermissionLevel.RUN_APPLICATION,
        PermissionLevel.CONTROL_SYSTEM,
    }:
        return PermissionDecision.REQUIRE_APPROVAL

    return PermissionDecision.DENY


if __name__ == "__main__":
    for level in PermissionLevel:
        decision = evaluate_permission(level)
        print(f"{level.value}: {decision.value}")