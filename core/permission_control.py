from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

VALID_POLICIES = frozenset(
    {
        "DENY",
        "ALWAYS_ASK",
        "SESSION",
        "ALLOW",
        "CUSTOM",
    }
)

USER_PERMISSION_DEFINITIONS: tuple[dict[str, object], ...] = (
    {
        "id": "files",
        "title": "Files & Storage",
        "defaultPolicy": "CUSTOM",
        "categories": (
            "read_non_sensitive",
            "create_or_edit_file",
            "delete_recoverable",
        ),
    },
    {
        "id": "apps",
        "title": "Applications",
        "defaultPolicy": "ALWAYS_ASK",
        "categories": (
            "open_application",
            "install_software",
            "uninstall_software",
            "remote_control",
            "device_control",
        ),
    },
    {
        "id": "browser",
        "title": "Browser & Web",
        "defaultPolicy": "CUSTOM",
        "categories": (
            "browser_navigation",
            "external_communication",
            "upload_data",
        ),
    },
    {
        "id": "microphone",
        "title": "Microphone",
        "defaultPolicy": "SESSION",
        "categories": (),
    },
    {
        "id": "camera",
        "title": "Camera / Webcam",
        "defaultPolicy": "ALWAYS_ASK",
        "categories": ("watch_camera",),
    },
    {
        "id": "screen",
        "title": "Screen Vision",
        "defaultPolicy": "ALWAYS_ASK",
        "categories": ("read_screen",),
    },
    {
        "id": "clipboard",
        "title": "Clipboard",
        "defaultPolicy": "CUSTOM",
        "categories": (),
    },
    {
        "id": "automation",
        "title": "Automation",
        "defaultPolicy": "CUSTOM",
        "categories": ("code_analysis", "gui_automation"),
    },
    {
        "id": "system",
        "title": "System Settings",
        "defaultPolicy": "ALWAYS_ASK",
        "categories": (
            "system_configuration",
            "registry_modification",
            "boot_configuration",
            "raw_disk_access",
            "security_configuration",
            "backup_destruction",
            "privilege_escalation",
            "unauthorized_persistence",
            "security_bypass",
            "irreversible_destruction",
            "modify_security_policy",
        ),
    },
)

HARD_LOCKED_CATEGORIES = frozenset(
    {
        "code_generation",
        "code_modification",
        "script_execution",
        "registry_modification",
        "boot_configuration",
        "raw_disk_access",
        "security_configuration",
        "backup_destruction",
        "privilege_escalation",
        "cyber_attack",
        "unauthorized_persistence",
        "credential_access",
        "security_bypass",
        "irreversible_destruction",
        "hidden_surveillance",
        "modify_security_policy",
    }
)

_DEFINITION_BY_ID = {
    str(item["id"]): item
    for item in USER_PERMISSION_DEFINITIONS
}

_CATEGORY_TO_PERMISSION: dict[str, str] = {}
for _definition in USER_PERMISSION_DEFINITIONS:
    _permission_id = str(_definition["id"])
    for _category in tuple(_definition["categories"]):
        _CATEGORY_TO_PERMISSION[str(_category)] = _permission_id


class PermissionControlError(ValueError):
    pass


def permission_store_path() -> Path:
    override = os.environ.get("QRONOS_PERMISSION_POLICY_PATH", "").strip()
    if override:
        return Path(override).expanduser()

    from core.config import CONFIG

    if CONFIG.paths.packaged:
        return CONFIG.paths.data / "permissions_v2.json"

    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "Qronos" / "permissions_v2.json"

    return Path.home() / ".qronos" / "permissions_v2.json"


def _audit_path() -> Path:
    override = os.environ.get("QRONOS_PERMISSION_AUDIT_PATH", "").strip()
    if override:
        return Path(override).expanduser()

    from core.action_audit import DEFAULT_ACTION_AUDIT_PATH

    return Path(DEFAULT_ACTION_AUDIT_PATH)


def _empty_document() -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "permissions": {},
    }


def _validated_document(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return _empty_document()

    if value.get("schemaVersion") != SCHEMA_VERSION:
        return _empty_document()

    raw_permissions = value.get("permissions")
    if not isinstance(raw_permissions, dict):
        return _empty_document()

    clean_permissions: dict[str, object] = {}

    for permission_id, raw_state in raw_permissions.items():
        if permission_id not in _DEFINITION_BY_ID or not isinstance(raw_state, dict):
            continue

        raw_policy = raw_state.get("policy")
        if not isinstance(raw_policy, str) or raw_policy not in VALID_POLICIES:
            continue

        raw_rules = raw_state.get("customRules", {})
        clean_rules: dict[str, str] = {}

        if isinstance(raw_rules, dict):
            allowed_categories = {
                str(category)
                for category in tuple(_DEFINITION_BY_ID[permission_id]["categories"])
            }

            for category, raw_rule in raw_rules.items():
                if (
                    category in allowed_categories
                    and isinstance(raw_rule, str)
                    and raw_rule in VALID_POLICIES
                    and raw_rule != "CUSTOM"
                ):
                    if category in HARD_LOCKED_CATEGORIES and raw_rule != "DENY":
                        continue
                    clean_rules[category] = raw_rule

        clean_permissions[permission_id] = {
            "policy": raw_policy,
            "customRules": clean_rules,
        }

    return {
        "schemaVersion": SCHEMA_VERSION,
        "permissions": clean_permissions,
    }


def _read_document() -> dict[str, object]:
    path = permission_store_path()

    if not path.is_file():
        return _empty_document()

    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_document()

    return _validated_document(parsed)


def _write_document(document: dict[str, object]) -> None:
    path = permission_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(
        _validated_document(document),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"

    temporary = path.with_name(
        f".{path.name}.{uuid.uuid4().hex}.tmp"
    )

    try:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _require_permission_id(permission_id: object) -> str:
    value = str(permission_id).strip()
    if value not in _DEFINITION_BY_ID:
        raise PermissionControlError(f"Unknown user permission: {value!r}.")
    return value


def _require_policy(policy: object, *, allow_custom: bool = True) -> str:
    value = str(policy).strip().upper().replace(" ", "_")

    if value == "ALWAYSASK":
        value = "ALWAYS_ASK"

    if value not in VALID_POLICIES:
        raise PermissionControlError(f"Unknown permission policy: {value!r}.")

    if not allow_custom and value == "CUSTOM":
        raise PermissionControlError("CUSTOM is not valid for one action rule.")

    return value


def _append_audit(
    *,
    event: str,
    permission_id: str,
    target: str,
    summary: str,
    decision: str,
) -> None:
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "schemaVersion": 1,
        "at": round(time.time(), 3),
        "event": event,
        "actionId": f"permission-{uuid.uuid4().hex}",
        "category": permission_id,
        "target": target[:200],
        "summary": summary[:200],
        "outcome": "succeeded",
        "decision": decision[:80],
    }

    line = json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.write("\n")


def snapshot_user_permissions() -> list[dict[str, object]]:
    document = _read_document()
    stored = document["permissions"]
    assert isinstance(stored, dict)

    result: list[dict[str, object]] = []

    for definition in USER_PERMISSION_DEFINITIONS:
        permission_id = str(definition["id"])
        raw_state = stored.get(permission_id)
        state = raw_state if isinstance(raw_state, dict) else {}

        policy = state.get("policy")
        if not isinstance(policy, str):
            policy = str(definition["defaultPolicy"])

        custom_rules = state.get("customRules")
        if not isinstance(custom_rules, dict):
            custom_rules = {}

        categories = tuple(str(value) for value in tuple(definition["categories"]))
        locked_categories = [
            category
            for category in categories
            if category in HARD_LOCKED_CATEGORIES
        ]

        result.append(
            {
                "id": permission_id,
                "title": str(definition["title"]),
                "policy": policy,
                "defaultPolicy": str(definition["defaultPolicy"]),
                "customRules": dict(custom_rules),
                "categories": list(categories),
                "lockedCategories": locked_categories,
                "configured": permission_id in stored,
            }
        )

    return result


def set_permission_policy(permission_id: object, policy: object) -> dict[str, object]:
    pid = _require_permission_id(permission_id)
    normalized = _require_policy(policy)

    document = _read_document()
    stored = document["permissions"]
    assert isinstance(stored, dict)

    current = stored.get(pid)
    state = dict(current) if isinstance(current, dict) else {}

    state["policy"] = normalized
    if not isinstance(state.get("customRules"), dict):
        state["customRules"] = {}

    stored[pid] = state
    _write_document(document)

    _append_audit(
        event="permission_changed",
        permission_id=pid,
        target=pid,
        summary=f"User permission policy changed to {normalized}.",
        decision=normalized.lower(),
    )

    return permission_state(pid)


def set_custom_rule(
    permission_id: object,
    category: object,
    policy: object,
) -> dict[str, object]:
    pid = _require_permission_id(permission_id)
    category_name = str(category).strip()

    allowed_categories = {
        str(value)
        for value in tuple(_DEFINITION_BY_ID[pid]["categories"])
    }

    if category_name not in allowed_categories:
        raise PermissionControlError(
            f"{category_name!r} is not controlled by user permission {pid!r}."
        )

    normalized = _require_policy(policy, allow_custom=False)

    if category_name in HARD_LOCKED_CATEGORIES and normalized != "DENY":
        raise PermissionControlError(
            f"{category_name!r} is locked by the security hard floor."
        )

    document = _read_document()
    stored = document["permissions"]
    assert isinstance(stored, dict)

    current = stored.get(pid)
    state = dict(current) if isinstance(current, dict) else {}
    rules = state.get("customRules")
    clean_rules = dict(rules) if isinstance(rules, dict) else {}

    state["policy"] = "CUSTOM"
    clean_rules[category_name] = normalized
    state["customRules"] = clean_rules
    stored[pid] = state

    _write_document(document)

    _append_audit(
        event="custom_rule_changed",
        permission_id=pid,
        target=category_name,
        summary=f"Custom permission rule changed to {normalized}.",
        decision=normalized.lower(),
    )

    return permission_state(pid)


def reset_permission(permission_id: object) -> dict[str, object]:
    pid = _require_permission_id(permission_id)

    document = _read_document()
    stored = document["permissions"]
    assert isinstance(stored, dict)

    stored.pop(pid, None)
    _write_document(document)

    _append_audit(
        event="permission_reset",
        permission_id=pid,
        target=pid,
        summary="User permission reset to security defaults.",
        decision="reset",
    )

    return permission_state(pid)


def permission_state(permission_id: object) -> dict[str, object]:
    pid = _require_permission_id(permission_id)

    for item in snapshot_user_permissions():
        if item["id"] == pid:
            return item

    raise AssertionError(f"Missing permission definition for {pid!r}.")


def effective_level_name(
    category_name: str,
    default_level_name: str,
) -> str:
    category = str(category_name).strip()
    default = str(default_level_name).strip()

    if category in HARD_LOCKED_CATEGORIES:
        return default

    permission_id = _CATEGORY_TO_PERMISSION.get(category)
    if permission_id is None:
        return default

    document = _read_document()
    stored = document["permissions"]
    assert isinstance(stored, dict)

    raw_state = stored.get(permission_id)
    if not isinstance(raw_state, dict):
        return default

    policy = raw_state.get("policy")
    if not isinstance(policy, str) or policy not in VALID_POLICIES:
        return default

    if policy == "CUSTOM":
        rules = raw_state.get("customRules")
        if not isinstance(rules, dict):
            return default

        rule = rules.get(category)
        if not isinstance(rule, str):
            return default

        policy = rule

    if policy == "DENY":
        return "FORBIDDEN"

    if policy == "ALLOW":
        return "AUTO_ALLOW"

    if policy in {"ALWAYS_ASK", "SESSION"}:
        return "UI_CONFIRMATION"

    return default
