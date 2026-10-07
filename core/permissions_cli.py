from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.action_audit import DEFAULT_ACTION_AUDIT_PATH
from core.approval_secret import ApprovalSecretError, ApprovalSecretStore
from core.permission_control import (
    permission_store_path,
    reset_permission,
    set_custom_rule,
    set_permission_policy,
    snapshot_user_permissions,
)
from security.permissions import (
    ActionCategory,
    evaluate_action,
    get_effective_permission_policy,
)

MAX_AUDIT_RECORDS = 120


def _safe_audit_record(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None

    allowed = {
        "schemaVersion": int,
        "at": (int, float),
        "event": str,
        "actionId": str,
        "category": str,
        "target": str,
        "summary": str,
        "outcome": str,
        "decision": str,
    }

    clean: dict[str, object] = {}
    for key, expected in allowed.items():
        raw = value.get(key)
        if raw is None:
            if key in {"outcome", "decision"}:
                raw = ""
            else:
                continue
        if not isinstance(raw, expected):
            return None
        clean[key] = raw

    if "at" not in clean or "event" not in clean or "actionId" not in clean:
        return None

    return clean


def read_audit_records(
    path: str | Path = DEFAULT_ACTION_AUDIT_PATH,
    *,
    limit: int = MAX_AUDIT_RECORDS,
) -> list[dict[str, object]]:
    target = Path(path)
    if not target.is_file():
        return []

    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    safe_limit = max(1, min(int(limit), MAX_AUDIT_RECORDS))
    result: list[dict[str, object]] = []

    for line in lines[-safe_limit:]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue

        record = _safe_audit_record(parsed)
        if record is not None:
            result.append(record)

    result.reverse()
    return result


def policy_snapshot() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []

    for category in ActionCategory:
        policy = get_effective_permission_policy(category)
        decision = evaluate_action(category)
        items.append(
            {
                "category": category.value,
                "level": policy.level.name,
                "levelValue": int(policy.level.value),
                "decision": decision.value,
                "reversible": bool(policy.reversible),
                "description": policy.description,
            }
        )

    return items


def approval_secret_status(
    store: ApprovalSecretStore | None = None,
) -> dict[str, object]:
    """Whether a secret is set and whether verification is locked. Never the secret."""
    try:
        return (store or ApprovalSecretStore()).status().public()
    except ApprovalSecretError as exc:
        return {
            "configured": False,
            "available": False,
            "lockedUntil": 0.0,
            "remainingAttempts": 0,
            "error": str(exc),
        }


def set_approval_secret(
    payload: dict[str, object],
    store: ApprovalSecretStore | None = None,
) -> dict[str, object]:
    """
    Explicit user operation from the Permissions view: set or change the secret.

    The submitted values are removed from the payload before anything else can
    see it, and only the resulting status is returned.
    """
    new_secret = payload.pop("newSecret", "")
    current_secret = payload.pop("currentSecret", None)
    try:
        status = (store or ApprovalSecretStore()).set_secret(
            new_secret,
            current_secret=current_secret,
        )
    except ApprovalSecretError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        del new_secret, current_secret
    return {"ok": True, "approvalSecret": status.public()}


def build_snapshot(
    *,
    audit_path: str | Path = DEFAULT_ACTION_AUDIT_PATH,
) -> dict[str, object]:
    path = Path(audit_path)

    return {
        "approvalSecret": approval_secret_status(),
        "ok": True,
        "mutable": True,
        "policySource": "security/permissions.py + permissions_v2.json",
        "policyStoreSource": str(permission_store_path()),
        "auditSource": path.name,
        "policies": policy_snapshot(),
        "userPermissions": snapshot_user_permissions(),
        "audit": read_audit_records(path),
    }


def _payload() -> dict[str, object]:
    raw = sys.stdin.read()

    if not raw.strip():
        return {}

    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Permissions payload must be a JSON object.")

    return parsed


def main() -> int:
    operation = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "snapshot"
    payload = _payload()

    if operation == "snapshot":
        result = build_snapshot()
    elif operation == "set_policy":
        result = {
            "ok": True,
            "permission": set_permission_policy(
                payload.get("permissionId", ""),
                payload.get("policy", ""),
            ),
        }
    elif operation == "set_custom_rule":
        result = {
            "ok": True,
            "permission": set_custom_rule(
                payload.get("permissionId", ""),
                payload.get("category", ""),
                payload.get("policy", ""),
            ),
        }
    elif operation == "reset_permission":
        result = {
            "ok": True,
            "permission": reset_permission(
                payload.get("permissionId", ""),
            ),
        }
    elif operation == "approval_secret_status":
        result = {"ok": True, "approvalSecret": approval_secret_status()}
    elif operation == "approval_secret_set":
        result = set_approval_secret(payload)
    else:
        raise ValueError(f"Unsupported permissions operation: {operation}")

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
