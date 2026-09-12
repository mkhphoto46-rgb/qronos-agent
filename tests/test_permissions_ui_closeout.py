from __future__ import annotations

from pathlib import Path

from core.permissions_cli import build_snapshot
from security.permissions import (
    ActionCategory,
    evaluate_action,
    get_effective_permission_policy,
)


ROOT = Path(__file__).resolve().parents[1]


def test_permissions_snapshot_is_effective_security_truth(tmp_path: Path) -> None:
    snapshot = build_snapshot(audit_path=tmp_path / "missing-audit.jsonl")

    assert snapshot["ok"] is True
    assert snapshot["mutable"] is True
    assert snapshot["policySource"] == (
        "security/permissions.py + permissions_v2.json"
    )
    assert snapshot["audit"] == []
    assert isinstance(snapshot["userPermissions"], list)

    policies = snapshot["policies"]
    by_category = {
        item["category"]: item
        for item in policies
    }

    assert set(by_category) == {
        category.value
        for category in ActionCategory
    }

    for category in ActionCategory:
        backend = get_effective_permission_policy(category)
        item = by_category[category.value]

        assert item["level"] == backend.level.name
        assert item["levelValue"] == int(backend.level.value)
        assert item["decision"] == evaluate_action(category).value
        assert item["reversible"] is backend.reversible
        assert item["description"] == backend.description


def test_permissions_desktop_uses_real_v2_mutation_controls() -> None:
    view = (
        ROOT / "desktop" / "src" / "components" / "PermissionsView.tsx"
    ).read_text(encoding="utf-8")

    store = (
        ROOT / "desktop" / "src" / "permissionStore.ts"
    ).read_text(encoding="utf-8")

    assert "usePermissionStore" in view
    assert "refreshPermissions" in view
    assert "setPermissionPolicy" in view
    assert "setPermissionCustomRule" in view
    assert "resetPermission" in view
    assert "customRules" in view

    assert "READ-ONLY" not in view
    assert "setInterval" not in store

    assert 'invoke<T>("permissions_command"' in store
    assert 'mutate("set_policy"' in store
    assert 'mutate("set_custom_rule"' in store
    assert 'mutate("reset_permission"' in store


def test_permissions_tauri_command_is_registered() -> None:
    runtime = (
        ROOT / "desktop" / "src-tauri" / "src" / "runtime" / "mod.rs"
    ).read_text(encoding="utf-8")

    lib = (
        ROOT / "desktop" / "src-tauri" / "src" / "lib.rs"
    ).read_text(encoding="utf-8")

    assert "pub fn permissions_command(" in runtime
    assert 'join("permissions_cli.py")' in runtime
    assert "runtime::permissions_command" in lib


def test_permissions_ui_keeps_audit_truth_and_no_background_poll() -> None:
    view = (
        ROOT / "desktop" / "src" / "components" / "PermissionsView.tsx"
    ).read_text(encoding="utf-8")

    cli = (
        ROOT / "core" / "permissions_cli.py"
    ).read_text(encoding="utf-8")

    assert "action_audit.jsonl" not in view
    assert "auditSource" in view
    assert "no fabricated activity" in view
    assert "read_audit_records" in cli
    assert "MAX_AUDIT_RECORDS" in cli
    assert "Unsupported permissions operation" in cli
