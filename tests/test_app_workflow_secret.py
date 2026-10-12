"""The typed approval secret behind printing, through the Library's store.

Every test uses the per-test secret path from conftest or its own tmp_path
file; nothing touches the user's real approval secret.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.approval_secret import (
    MAX_FAILURES_BEFORE_LOCK,
    ApprovalSecretStore,
    approval_secret_path,
)
from core.library_executor import WorkflowSecretCheck

SECRET = "correct horse battery"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> ApprovalSecretStore:
    return ApprovalSecretStore(tmp_path / "state" / "approval_secret.sqlite3", clock=clock,
                               audit_path=tmp_path / "audit.jsonl")


def test_without_a_configured_secret_printing_is_not_ready_and_nothing_verifies(store):
    check = WorkflowSecretCheck(store)
    assert check.status() == "not_configured"
    assert not store.path.exists(), "the check made before prompting never creates the store"
    assert check.verify(SECRET) is False


def test_only_the_configured_secret_verifies(store):
    store.set_secret(SECRET)
    check = WorkflowSecretCheck(store)
    assert check.status() == "ready"
    assert check.verify("not the secret") is False
    assert check.verify(SECRET) is True
    for value in ("", None, 12345, " " * 3):
        assert check.verify(value) is False


def test_wrong_guesses_lock_the_same_store_the_library_uses(store, clock, tmp_path):
    store.set_secret(SECRET)
    check = WorkflowSecretCheck(store)
    for _ in range(MAX_FAILURES_BEFORE_LOCK):
        assert check.verify("wrong guess") is False
    assert check.status() == "locked"
    assert check.verify(SECRET) is False, "a locked store refuses even the right secret"
    # A fresh instance over the same file (the Library CLI's next process) sees the lockout.
    library_view = ApprovalSecretStore(store.path, clock=clock, audit_path=tmp_path / "audit.jsonl")
    assert library_view.status().locked_until > clock.now


def test_an_unreadable_store_is_unavailable_never_ready(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b"not a database")
    check = WorkflowSecretCheck(store)
    assert check.status() == "unavailable"
    assert check.verify(SECRET) is False
    assert store.path.read_bytes() == b"not a database", "a damaged store is never reinitialised"


def test_the_default_check_uses_the_per_profile_secret_path(tmp_path):
    # conftest points the per-profile path at this test's temporary folder.
    assert WorkflowSecretCheck().store.path == approval_secret_path()
    assert approval_secret_path().resolve() == (tmp_path / "approval_secret.sqlite3").resolve()
