from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_qronos_permission_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Prevent the test suite from ever reading or mutating the real user's
    persisted Qronos permission state.

    Every test receives its own isolated permission-policy and audit files.
    Tests that intentionally override these environment variables remain free
    to do so for the duration of that test.
    """

    monkeypatch.setenv(
        "QRONOS_PERMISSION_POLICY_PATH",
        str(tmp_path / "permissions_v2.json"),
    )
    monkeypatch.setenv(
        "QRONOS_PERMISSION_AUDIT_PATH",
        str(tmp_path / "action_audit.jsonl"),
    )
