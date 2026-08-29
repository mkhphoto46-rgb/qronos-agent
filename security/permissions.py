from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum


class PermissionLevel(IntEnum):
    """Five authorization levels for Qronos actions."""

    AUTO_ALLOW = 1
    VOICE_CONFIRMATION = 2
    UI_CONFIRMATION = 3
    TYPED_SECRET = 4
    FORBIDDEN = 5


class PermissionDecision(Enum):
    """Authorization result required before an action may execute."""

    ALLOW = "allow"
    REQUIRE_VOICE_CONFIRMATION = "require_voice_confirmation"
    REQUIRE_UI_CONFIRMATION = "require_ui_confirmation"
    REQUIRE_TYPED_SECRET = "require_typed_secret"
    DENY = "deny"


class ActionCategory(Enum):
    """Security-relevant categories used by Qronos tools."""

    CONVERSATION = "conversation"
    SYSTEM_STATUS = "system_status"
    READ_NON_SENSITIVE = "read_non_sensitive"
    OPEN_APPLICATION = "open_application"
    BROWSER_NAVIGATION = "browser_navigation"
    CREATE_OR_EDIT_FILE = "create_or_edit_file"
    EXTERNAL_COMMUNICATION = "external_communication"
    UPLOAD_DATA = "upload_data"
    DELETE_RECOVERABLE = "delete_recoverable"
    INSTALL_SOFTWARE = "install_software"
    UNINSTALL_SOFTWARE = "uninstall_software"
    SYSTEM_CONFIGURATION = "system_configuration"
    REMOTE_CONTROL = "remote_control"
    DEVICE_CONTROL = "device_control"
    CODE_GENERATION = "code_generation"
    CODE_ANALYSIS = "code_analysis"
    CODE_MODIFICATION = "code_modification"
    SCRIPT_EXECUTION = "script_execution"
    REGISTRY_MODIFICATION = "registry_modification"
    BOOT_CONFIGURATION = "boot_configuration"
    RAW_DISK_ACCESS = "raw_disk_access"
    SECURITY_CONFIGURATION = "security_configuration"
    BACKUP_DESTRUCTION = "backup_destruction"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    CYBER_ATTACK = "cyber_attack"
    UNAUTHORIZED_PERSISTENCE = "unauthorized_persistence"
    CREDENTIAL_ACCESS = "credential_access"
    SECURITY_BYPASS = "security_bypass"
    IRREVERSIBLE_DESTRUCTION = "irreversible_destruction"
    HIDDEN_SURVEILLANCE = "hidden_surveillance"
    MODIFY_SECURITY_POLICY = "modify_security_policy"


@dataclass(frozen=True)
class PermissionPolicy:
    """Static default policy for one action category."""

    category: ActionCategory
    level: PermissionLevel
    reversible: bool
    description: str


def _policy(
    category: ActionCategory,
    level: PermissionLevel,
    reversible: bool,
    description: str,
) -> PermissionPolicy:
    return PermissionPolicy(
        category=category,
        level=level,
        reversible=reversible,
        description=description,
    )


ACTION_POLICIES = {
    ActionCategory.CONVERSATION: _policy(
        ActionCategory.CONVERSATION,
        PermissionLevel.AUTO_ALLOW,
        True,
        "Generate a local conversational response.",
    ),
    ActionCategory.SYSTEM_STATUS: _policy(
        ActionCategory.SYSTEM_STATUS,
        PermissionLevel.AUTO_ALLOW,
        True,
        "Read local CPU, RAM, GPU, VRAM, and temperature status.",
    ),
    ActionCategory.READ_NON_SENSITIVE: _policy(
        ActionCategory.READ_NON_SENSITIVE,
        PermissionLevel.AUTO_ALLOW,
        True,
        "Read explicitly selected non-sensitive local information.",
    ),
    ActionCategory.OPEN_APPLICATION: _policy(
        ActionCategory.OPEN_APPLICATION,
        PermissionLevel.VOICE_CONFIRMATION,
        True,
        "Open a known non-elevated application.",
    ),
    ActionCategory.BROWSER_NAVIGATION: _policy(
        ActionCategory.BROWSER_NAVIGATION,
        PermissionLevel.VOICE_CONFIRMATION,
        True,
        "Navigate to a website without submitting data.",
    ),
    ActionCategory.CREATE_OR_EDIT_FILE: _policy(
        ActionCategory.CREATE_OR_EDIT_FILE,
        PermissionLevel.UI_CONFIRMATION,
        True,
        "Create or edit a file after showing an exact preview.",
    ),
    ActionCategory.EXTERNAL_COMMUNICATION: _policy(
        ActionCategory.EXTERNAL_COMMUNICATION,
        PermissionLevel.UI_CONFIRMATION,
        False,
        "Send a message, post, form, email, or other external communication.",
    ),
    ActionCategory.UPLOAD_DATA: _policy(
        ActionCategory.UPLOAD_DATA,
        PermissionLevel.UI_CONFIRMATION,
        False,
        "Upload local data to an external destination.",
    ),
    ActionCategory.DELETE_RECOVERABLE: _policy(
        ActionCategory.DELETE_RECOVERABLE,
        PermissionLevel.TYPED_SECRET,
        True,
        "Move exact reviewed targets to Recycle Bin or quarantine.",
    ),
    ActionCategory.INSTALL_SOFTWARE: _policy(
        ActionCategory.INSTALL_SOFTWARE,
        PermissionLevel.TYPED_SECRET,
        False,
        "Install an exact package, publisher, source, and version.",
    ),
    ActionCategory.UNINSTALL_SOFTWARE: _policy(
        ActionCategory.UNINSTALL_SOFTWARE,
        PermissionLevel.TYPED_SECRET,
        False,
        "Uninstall an explicitly selected application.",
    ),
    ActionCategory.SYSTEM_CONFIGURATION: _policy(
        ActionCategory.SYSTEM_CONFIGURATION,
        PermissionLevel.TYPED_SECRET,
        False,
        "Change a whitelisted reversible user setting without registry access.",
    ),
    ActionCategory.REMOTE_CONTROL: _policy(
        ActionCategory.REMOTE_CONTROL,
        PermissionLevel.TYPED_SECRET,
        False,
        "Start or authorize a remote-control session.",
    ),
    ActionCategory.DEVICE_CONTROL: _policy(
        ActionCategory.DEVICE_CONTROL,
        PermissionLevel.TYPED_SECRET,
        False,
        "Control a printer, 3D printer, or third-party device.",
    ),
    ActionCategory.CODE_GENERATION: _policy(
        ActionCategory.CODE_GENERATION,
        PermissionLevel.FORBIDDEN,
        False,
        "Generate source code, scripts, macros, or executable instructions.",
    ),
    ActionCategory.CODE_ANALYSIS: _policy(
        ActionCategory.CODE_ANALYSIS,
        PermissionLevel.AUTO_ALLOW,
        True,
        "Read and analyze existing source code, scripts, errors, tracebacks, "
        "binaries, or program behavior without generating, modifying, or "
        "executing code.",
    ),
    ActionCategory.CODE_MODIFICATION: _policy(
        ActionCategory.CODE_MODIFICATION,
        PermissionLevel.FORBIDDEN,
        False,
        "Modify source code, scripts, macros, or executable instructions.",
    ),
    ActionCategory.SCRIPT_EXECUTION: _policy(
        ActionCategory.SCRIPT_EXECUTION,
        PermissionLevel.FORBIDDEN,
        False,
        "Execute arbitrary shell, PowerShell, batch, Python, or macro code.",
    ),
    ActionCategory.REGISTRY_MODIFICATION: _policy(
        ActionCategory.REGISTRY_MODIFICATION,
        PermissionLevel.FORBIDDEN,
        False,
        "Create, edit, or delete Windows Registry values or keys.",
    ),
    ActionCategory.BOOT_CONFIGURATION: _policy(
        ActionCategory.BOOT_CONFIGURATION,
        PermissionLevel.FORBIDDEN,
        False,
        "Change boot configuration, recovery, firmware, or startup security.",
    ),
    ActionCategory.RAW_DISK_ACCESS: _policy(
        ActionCategory.RAW_DISK_ACCESS,
        PermissionLevel.FORBIDDEN,
        False,
        "Read or write raw disks, partitions, volume metadata, or firmware.",
    ),
    ActionCategory.SECURITY_CONFIGURATION: _policy(
        ActionCategory.SECURITY_CONFIGURATION,
        PermissionLevel.FORBIDDEN,
        False,
        "Change antivirus, firewall, UAC, audit, or access-control settings.",
    ),
    ActionCategory.BACKUP_DESTRUCTION: _policy(
        ActionCategory.BACKUP_DESTRUCTION,
        PermissionLevel.FORBIDDEN,
        False,
        "Delete, encrypt, corrupt, or disable backups and recovery data.",
    ),
    ActionCategory.PRIVILEGE_ESCALATION: _policy(
        ActionCategory.PRIVILEGE_ESCALATION,
        PermissionLevel.FORBIDDEN,
        False,
        "Escalate privileges or obtain authority beyond an approved broker action.",
    ),
    ActionCategory.CYBER_ATTACK: _policy(
        ActionCategory.CYBER_ATTACK,
        PermissionLevel.FORBIDDEN,
        False,
        "Create or perform malware, exploitation, credential attacks, or intrusion.",
    ),
    ActionCategory.UNAUTHORIZED_PERSISTENCE: _policy(
        ActionCategory.UNAUTHORIZED_PERSISTENCE,
        PermissionLevel.FORBIDDEN,
        False,
        "Create a hidden service, startup task, backdoor, or remote persistence.",
    ),
    ActionCategory.CREDENTIAL_ACCESS: _policy(
        ActionCategory.CREDENTIAL_ACCESS,
        PermissionLevel.FORBIDDEN,
        False,
        "Read, reveal, export, or transmit passwords, tokens, or private keys.",
    ),
    ActionCategory.SECURITY_BYPASS: _policy(
        ActionCategory.SECURITY_BYPASS,
        PermissionLevel.FORBIDDEN,
        False,
        "Bypass UAC, security controls, approval checks, or access controls.",
    ),
    ActionCategory.IRREVERSIBLE_DESTRUCTION: _policy(
        ActionCategory.IRREVERSIBLE_DESTRUCTION,
        PermissionLevel.FORBIDDEN,
        False,
        "Irreversibly erase disks, backups, partitions, or broad data sets.",
    ),
    ActionCategory.HIDDEN_SURVEILLANCE: _policy(
        ActionCategory.HIDDEN_SURVEILLANCE,
        PermissionLevel.FORBIDDEN,
        False,
        "Record microphone, camera, screen, or activity without visible consent.",
    ),
    ActionCategory.MODIFY_SECURITY_POLICY: _policy(
        ActionCategory.MODIFY_SECURITY_POLICY,
        PermissionLevel.FORBIDDEN,
        False,
        "Allow the agent to weaken or rewrite its own security policy.",
    ),
}


def evaluate_permission(level: PermissionLevel) -> PermissionDecision:
    """Return the authorization mechanism required by a level."""

    decisions = {
        PermissionLevel.AUTO_ALLOW: PermissionDecision.ALLOW,
        PermissionLevel.VOICE_CONFIRMATION: (
            PermissionDecision.REQUIRE_VOICE_CONFIRMATION
        ),
        PermissionLevel.UI_CONFIRMATION: (
            PermissionDecision.REQUIRE_UI_CONFIRMATION
        ),
        PermissionLevel.TYPED_SECRET: (
            PermissionDecision.REQUIRE_TYPED_SECRET
        ),
        PermissionLevel.FORBIDDEN: PermissionDecision.DENY,
    }

    return decisions[level]


def get_permission_policy(
    category: ActionCategory,
) -> PermissionPolicy:
    """Return the immutable default policy for an action category."""

    return ACTION_POLICIES[category]


def evaluate_action(
    category: ActionCategory,
) -> PermissionDecision:
    """Evaluate one categorized action against the default policy."""

    return evaluate_permission(
        get_permission_policy(category).level,
    )


if __name__ == "__main__":
    for category in ActionCategory:
        policy = get_permission_policy(category)
        decision = evaluate_action(category)
        print(
            f"{category.value}: "
            f"{policy.level.value} -> {decision.value}"
        )