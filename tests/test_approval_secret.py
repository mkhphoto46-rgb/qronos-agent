"""
The approval secret behind TYPED_SECRET, and the Library actions that need it.

Everything runs against isolated temporary state: conftest points the secret
store, permission policy and audit log into tmp_path, and the Library tests
here also redirect LOCALAPPDATA/USERPROFILE. Secrets below are synthetic.
"""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unicodedata

import pytest

from core import library_cli, permissions_cli
from core.action_audit import ActionAuditLog
from core.approval_secret import (
    MAX_FAILURES_BEFORE_LOCK,
    SETUP_HINT,
    ApprovalSecretLocked,
    ApprovalSecretNotConfigured,
    ApprovalSecretRejected,
    ApprovalSecretStore,
    ApprovalSecretUnavailable,
    InvalidApprovalSecret,
    approval_secret_path,
)
from core.artifact_registry import ArtifactRegistry
from core.artifact_service import ArtifactProducerRegistry
from core.library_executor import LibraryActionRefused, LibraryExecutor
from core.permission_control import set_custom_rule
from security.gate import Verdict
from core.actions import ActionOutcome
from security.permissions import PermissionDecision


ROOT = Path(__file__).resolve().parents[1]
SECRET = "correct horse battery"
OTHER = "a different passphrase"


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "approval_secret.sqlite3"


@pytest.fixture
def store(store_path: Path, clock: Clock, tmp_path: Path) -> ApprovalSecretStore:
    return ApprovalSecretStore(store_path, clock=clock, audit_path=tmp_path / "audit.jsonl")


def reopen(store: ApprovalSecretStore, clock: Clock) -> ApprovalSecretStore:
    """A fresh instance over the same file: what the next CLI process sees."""
    return ApprovalSecretStore(store.path, clock=clock, audit_path=store._audit.path)


# ---------------------------------------------------------------- not configured


def test_tests_never_use_the_real_profile_store(tmp_path: Path) -> None:
    path = approval_secret_path().resolve()
    assert path == (tmp_path / "approval_secret.sqlite3").resolve()
    assert path.is_relative_to(tmp_path.resolve())


def test_unconfigured_store_fails_closed_with_a_setup_path(store: ApprovalSecretStore) -> None:
    status = store.status()
    assert status.configured is False
    assert not store.path.exists(), "reading status must not create state"

    with pytest.raises(ApprovalSecretNotConfigured) as caught:
        store.verify(SECRET)
    assert "Permissions" in str(caught.value) and "APPROVAL SECRET" in str(caught.value)


# ---------------------------------------------------------------- verify


def test_correct_and_wrong_secret(store: ApprovalSecretStore) -> None:
    store.set_secret(SECRET)
    assert store.verify(SECRET) is True
    assert store.verify(OTHER) is False
    assert store.verify("") is False
    assert store.verify(SECRET.upper()) is False


def test_unicode_is_normalised(store: ApprovalSecretStore) -> None:
    composed = "café secret phrase"
    store.set_secret(composed)
    assert store.verify(unicodedata.normalize("NFD", composed)) is True


def test_each_credential_has_its_own_salt(tmp_path: Path, clock: Clock) -> None:
    digests = []
    for name in ("a", "b"):
        s = ApprovalSecretStore(tmp_path / name / "s.sqlite3", clock=clock, audit_path=None)
        s.set_secret(SECRET)
        with sqlite3.connect(s.path) as c:
            digests.append(c.execute("SELECT salt, digest FROM credential").fetchone())
    assert digests[0][0] != digests[1][0] and digests[0][1] != digests[1][1]


# ---------------------------------------------------------------- attempt limiting


def test_failures_persist_across_processes_and_lock(store: ApprovalSecretStore, clock: Clock) -> None:
    store.set_secret(SECRET)

    for attempt in range(MAX_FAILURES_BEFORE_LOCK):
        # A new instance per attempt models one short-lived CLI process each.
        assert reopen(store, clock).verify(f"wrong guess {attempt}") is False

    status = reopen(store, clock).status()
    assert status.locked_until > clock.now and status.remaining_attempts == 0

    with pytest.raises(ApprovalSecretLocked):
        reopen(store, clock).verify(SECRET)  # even the right secret waits

    clock.now = status.locked_until + 1
    assert reopen(store, clock).verify(SECRET) is True
    assert reopen(store, clock).status().remaining_attempts == MAX_FAILURES_BEFORE_LOCK


def test_lockout_grows_after_it_expires(store: ApprovalSecretStore, clock: Clock) -> None:
    store.set_secret(SECRET)
    for attempt in range(MAX_FAILURES_BEFORE_LOCK):
        store.verify(f"wrong {attempt}")
    first = store.status().locked_until - clock.now

    clock.now += first + 1
    assert store.verify("wrong again") is False
    second = store.status().locked_until - clock.now
    assert second == pytest.approx(first * 2)


def test_locked_attempts_do_not_run_or_count(store: ApprovalSecretStore, clock: Clock) -> None:
    store.set_secret(SECRET)
    for attempt in range(MAX_FAILURES_BEFORE_LOCK):
        store.verify(f"wrong {attempt}")
    until = store.status().locked_until

    for _ in range(3):
        with pytest.raises(ApprovalSecretLocked):
            store.verify("still guessing")
    assert store.status().locked_until == until


def test_concurrent_verifications_lose_no_failures(store: ApprovalSecretStore, clock: Clock) -> None:
    store.set_secret(SECRET)

    def attempt(n: int) -> bool:
        return reopen(store, clock).verify(f"parallel wrong {n}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert not any(pool.map(attempt, range(4)))

    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT failures FROM attempts").fetchone()[0] == 4


# ---------------------------------------------------------------- provisioning and change


def test_change_requires_the_current_secret(store: ApprovalSecretStore) -> None:
    store.set_secret(SECRET)

    with pytest.raises(ApprovalSecretRejected):
        store.set_secret(OTHER)
    with pytest.raises(ApprovalSecretRejected):
        store.set_secret(OTHER, current_secret="not the current one")

    assert store.verify(SECRET) is True
    store.set_secret(OTHER, current_secret=SECRET)
    assert store.verify(SECRET) is False
    assert store.verify(OTHER) is True


def test_wrong_current_secret_counts_toward_lockout(store: ApprovalSecretStore, clock: Clock) -> None:
    store.set_secret(SECRET)
    for attempt in range(MAX_FAILURES_BEFORE_LOCK):
        with pytest.raises(ApprovalSecretRejected):
            store.set_secret(OTHER, current_secret=f"guess {attempt}")

    with pytest.raises(ApprovalSecretLocked):
        store.set_secret(OTHER, current_secret=SECRET)
    clock.now = store.status().locked_until + 1
    assert store.verify(SECRET) is True


@pytest.mark.parametrize(
    "candidate",
    ["short", "        ", "DELETE 1", "delete 12", "DELETE", "x" * 257, 12345678, None],
)
def test_weak_or_public_secrets_are_refused(store: ApprovalSecretStore, candidate: object) -> None:
    with pytest.raises(InvalidApprovalSecret):
        store.set_secret(candidate)
    assert store.status().configured is False


# ---------------------------------------------------------------- state integrity


def test_corrupt_state_is_refused_and_never_overwritten(store: ApprovalSecretStore) -> None:
    store.path.parent.mkdir(parents=True)
    garbage = b"this is not a database" * 20
    store.path.write_bytes(garbage)

    with pytest.raises(ApprovalSecretUnavailable):
        store.status()
    with pytest.raises(ApprovalSecretUnavailable):
        store.verify(SECRET)
    with pytest.raises(ApprovalSecretUnavailable):
        store.set_secret(SECRET)
    assert store.path.read_bytes() == garbage


def _snapshot(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as c:
        return [
            *c.execute("SELECT * FROM meta").fetchall(),
            *c.execute("SELECT * FROM credential").fetchall(),
            *c.execute("SELECT * FROM attempts").fetchall(),
        ]


def test_newer_schema_is_refused_and_left_untouched(store: ApprovalSecretStore) -> None:
    store.set_secret(SECRET)
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    before = _snapshot(store.path)

    for action in (
        lambda: store.verify(SECRET),
        lambda: store.verify("wrong"),
        lambda: store.set_secret(OTHER, current_secret=SECRET),
        store.status,
    ):
        with pytest.raises(ApprovalSecretUnavailable, match="newer"):
            action()
    assert _snapshot(store.path) == before


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE credential SET n=1024",  # downgrade to a cheap KDF
        "UPDATE credential SET n=1048576",  # memory exhaustion
        "UPDATE credential SET kdf='md5'",
        "UPDATE credential SET salt=x'00'",
        "DELETE FROM attempts",  # would reset the lockout
        "DROP TABLE meta",
    ],
)
def test_tampered_records_fail_closed(store: ApprovalSecretStore, tamper: str) -> None:
    store.set_secret(SECRET)
    with sqlite3.connect(store.path) as c:
        c.execute(tamper)

    with pytest.raises(ApprovalSecretUnavailable):
        store.verify(SECRET)
    with pytest.raises(ApprovalSecretUnavailable):
        store.set_secret(OTHER, current_secret=SECRET)


# ---------------------------------------------------------------- redaction


def test_secret_material_never_leaves_the_store(store: ApprovalSecretStore, tmp_path: Path) -> None:
    store.set_secret(SECRET)
    store.verify("wrong value 123")
    store.set_secret(OTHER, current_secret=SECRET)
    with pytest.raises(ApprovalSecretRejected) as caught:
        store.set_secret("third secret value", current_secret="bad current value")

    public = store.status().public()
    assert set(public) == {"configured", "available", "lockedUntil", "remainingAttempts"}

    with sqlite3.connect(store.path) as c:
        digest = c.execute("SELECT digest FROM credential").fetchone()[0]

    db_bytes = store.path.read_bytes()
    audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    for plaintext in (SECRET, OTHER, "wrong value 123", "bad current value", "third secret value"):
        assert plaintext.encode() not in db_bytes
        assert plaintext not in audit_text
        assert plaintext not in json.dumps(public)
        assert plaintext not in str(caught.value)
    assert digest.hex() not in audit_text and digest.hex() not in json.dumps(public)
    events = [json.loads(line)["event"] for line in audit_text.splitlines()]
    assert {"approval_secret_set", "approval_secret_changed", "approval_secret_rejected"} <= set(events)


def test_permissions_cli_returns_status_only_and_strips_the_payload(tmp_path: Path) -> None:
    payload: dict[str, object] = {"newSecret": SECRET}
    result = permissions_cli.set_approval_secret(payload)

    assert result == {"ok": True, "approvalSecret": permissions_cli.approval_secret_status()}
    assert "newSecret" not in payload and "currentSecret" not in payload

    payload = {"newSecret": OTHER, "currentSecret": "wrong current secret"}
    result = permissions_cli.set_approval_secret(payload)
    assert result["ok"] is False and "wrong" in str(result["error"])
    assert SECRET not in json.dumps(result) and "wrong current secret" not in json.dumps(result)

    snapshot = permissions_cli.build_snapshot(audit_path=tmp_path / "audit.jsonl")
    assert snapshot["approvalSecret"]["configured"] is True
    assert SECRET not in json.dumps(snapshot, ensure_ascii=False)


def test_permissions_snapshot_survives_a_corrupt_store() -> None:
    path = approval_secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"garbage" * 50)

    status = permissions_cli.approval_secret_status()
    assert status["available"] is False and status["configured"] is False
    assert permissions_cli.set_approval_secret({"newSecret": SECRET})["ok"] is False
    assert path.read_bytes() == b"garbage" * 50


# ---------------------------------------------------------------- Library integration


class Library:
    def __init__(self, tmp_path: Path, clock: Clock, *, configure: bool = True) -> None:
        self.clock = clock
        # Explicit, so the result does not depend on where the OS puts tmp_path
        # (under the real %LOCALAPPDATA%\Temp unless the profile is isolated).
        self.protected = (tmp_path / "protected",)
        self.registry = ArtifactRegistry(tmp_path / "db" / "library.sqlite3", tmp_path / "Artifacts")
        self.producer = ArtifactProducerRegistry(self.registry)
        self.audit = ActionAuditLog(None)
        self.secrets = ApprovalSecretStore(tmp_path / "secret.sqlite3", clock=clock, audit_path=None)
        if configure:
            self.secrets.set_secret(SECRET)
        self.recycled: list[Path] = []
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.executor = self.make()

    def recycle(self, path: Path) -> None:
        os.replace(path, self.bin / path.name)
        self.recycled.append(path)

    def make(self, **overrides) -> LibraryExecutor:
        options = dict(
            audit=self.audit,
            recycler=self.recycle,
            clock=self.clock,
            approval_secrets=self.secrets,
            protected_roots=self.protected,
        )
        options.update(overrides)
        return LibraryExecutor(self.registry, **options)

    def artifact(self, name: str = "report.md", text: str = "content"):
        path = self.registry.artifact_root / "Documents" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return self.producer.register_file(path, origin="Generated by Qronos")


def test_delete_without_a_configured_secret_fails_closed(tmp_path: Path, clock: Clock) -> None:
    lib = Library(tmp_path, clock, configure=False)
    item = lib.artifact()

    with pytest.raises(LibraryActionRefused) as caught:
        lib.executor.prepare("delete", [item.id])
    assert str(caught.value) == SETUP_HINT
    assert Path(item.path_or_url).is_file()
    assert [r.outcome for r in lib.audit.records() if r.event.value == "completed"] == ["refused"]


def test_delete_while_locked_is_refused_up_front(tmp_path: Path, clock: Clock) -> None:
    lib = Library(tmp_path, clock)
    item = lib.artifact()
    for attempt in range(MAX_FAILURES_BEFORE_LOCK):
        lib.secrets.verify(f"wrong {attempt}")

    with pytest.raises(LibraryActionRefused, match="Too many"):
        lib.executor.prepare("delete", [item.id])


def test_wrong_secret_keeps_the_approval_but_counts(tmp_path: Path, clock: Clock) -> None:
    lib = Library(tmp_path, clock)
    item = lib.artifact()
    approval = lib.executor.prepare("delete", [item.id])

    with pytest.raises(LibraryActionRefused, match="wrong"):
        lib.executor.commit(approval["token"], operation="delete", secret=OTHER)
    assert lib.secrets.status().remaining_attempts == MAX_FAILURES_BEFORE_LOCK - 1
    assert Path(item.path_or_url).is_file()

    result = lib.executor.commit(approval["token"], operation="delete", secret=SECRET)
    assert result["complete"] is True and lib.recycled == [Path(item.path_or_url)]


def test_secret_approval_is_exact_single_use_and_expiring(tmp_path: Path, clock: Clock) -> None:
    lib = Library(tmp_path, clock)
    a, b = lib.artifact("a.md", "A"), lib.artifact("b.md", "B")

    approval = lib.executor.prepare("delete", [a.id])
    with pytest.raises(LibraryActionRefused, match="different operation"):
        lib.executor.commit(approval["token"], operation="rename", secret=SECRET)
    assert Path(a.path_or_url).is_file()

    approval = lib.executor.prepare("delete", [a.id])
    result = library_cli.run(
        "commit",
        {"action": "delete", "token": approval["token"], "secret": SECRET, "ids": [b.id]},
        registry=lib.registry, executor=lib.executor,
    )
    assert result["ok"] is True and [r["id"] for r in result["results"]] == [a.id]
    assert Path(b.path_or_url).is_file()
    with pytest.raises(LibraryActionRefused, match="unknown, expired or already used"):
        lib.executor.commit(approval["token"], operation="delete", secret=SECRET)

    approval = lib.executor.prepare("delete", [b.id])
    clock.now += 10_000
    with pytest.raises(LibraryActionRefused, match="expired"):
        lib.executor.commit(approval["token"], operation="delete", secret=SECRET)
    assert Path(b.path_or_url).is_file()


def test_policy_change_after_secret_approval_is_honoured(tmp_path: Path, clock: Clock) -> None:
    lib = Library(tmp_path, clock)
    item = lib.artifact()
    approval = lib.executor.prepare("delete", [item.id])
    set_custom_rule("files", "delete_recoverable", "DENY")

    result = lib.executor.commit(approval["token"], operation="delete", secret=SECRET)
    assert result["refused"] == 1 and Path(item.path_or_url).is_file()


def test_lowered_policy_after_approval_still_needs_a_fresh_prepare(tmp_path: Path, clock: Clock) -> None:
    lib = Library(tmp_path, clock)
    item = lib.artifact()
    approval = lib.executor.prepare("delete", [item.id])
    set_custom_rule("files", "delete_recoverable", "ALLOW")

    result = lib.executor.commit(approval["token"], operation="delete", secret=SECRET)
    assert result["refused"] == 1 and Path(item.path_or_url).is_file()


def _allow_everything(request, audit=None):
    verdict = Verdict(request=request, decision=PermissionDecision.ALLOW, outcome=ActionOutcome.SUCCEEDED, reason="test double")
    if audit is not None:
        audit(verdict)
    return verdict


def test_permanent_delete_is_refused_even_with_a_valid_secret(tmp_path: Path, clock: Clock) -> None:
    lib = Library(tmp_path, clock)
    item = lib.artifact()

    for executor in (lib.executor, lib.make(evaluate_fn=_allow_everything)):
        with pytest.raises(LibraryActionRefused):
            executor.prepare("delete_forever", [item.id])
    result = library_cli.run(
        "delete_forever", {"ids": [item.id], "secret": SECRET},
        registry=lib.registry, executor=lib.executor,
    )
    assert result["ok"] is False
    assert Path(item.path_or_url).is_file() and lib.recycled == []
    assert lib.secrets.status().remaining_attempts == MAX_FAILURES_BEFORE_LOCK


def test_secret_is_not_needed_or_consulted_for_ui_confirmation(tmp_path: Path, clock: Clock) -> None:
    lib = Library(tmp_path, clock, configure=False)
    item = lib.artifact()
    dest = tmp_path / "dest"
    dest.mkdir()

    executor = lib.make()
    approval = executor.prepare("copy", [item.id], destination=str(dest))
    assert approval["confirmation"] == "click"
    assert executor.commit(approval["token"], operation="copy")["complete"] is True
    assert not lib.secrets.path.exists()


def test_concurrent_cli_commits_with_wrong_secrets_all_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real short-lived library_cli processes, all racing on one approval.
    # Only wrong secrets are ever sent to subprocesses, so none of them can
    # reach the real Recycle Bin even if this test were to regress.
    for name, sub in (("LOCALAPPDATA", "Local"), ("USERPROFILE", "Home"), ("HOME", "Home"), ("APPDATA", "Roaming")):
        (tmp_path / sub).mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(tmp_path / sub))
    # Each child's start-up admission asks Windows for its Local AppData known folder, which expands the redirected
    # profile; without that private folder every child refuses "store" (exit 78) before any secret is checked.
    # Create the private known folders, as tests/test_runtime_bridge_process.py does. Production
    # admission and every assertion below are unchanged.
    for known in ("Local", "Roaming"):
        (tmp_path / "Home" / "AppData" / known).mkdir(parents=True, exist_ok=True)

    registry = ArtifactRegistry()
    assert str(registry.path).startswith(str(tmp_path))
    path = registry.artifact_root / "Documents" / "target.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("keep", encoding="utf-8")
    item = ArtifactProducerRegistry(registry).register_file(path, origin="Generated by Qronos")

    secrets_store = ApprovalSecretStore()
    secrets_store.set_secret(SECRET)

    def no_recycling(p: Path) -> None:
        raise AssertionError("must not recycle")

    executor = LibraryExecutor(registry, recycler=no_recycling, approval_secrets=secrets_store)
    approval = executor.prepare("delete", [item.id])

    env = {**os.environ, "PYTHONUTF8": "1"}
    procs = [
        subprocess.Popen(
            [sys.executable, str(ROOT / "core" / "library_cli.py"), "commit"],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for _ in range(MAX_FAILURES_BEFORE_LOCK)
    ]
    outputs = []
    for n, proc in enumerate(procs):
        body = json.dumps({"action": "delete", "token": approval["token"], "secret": f"wrong guess {n}"})
        out, err = proc.communicate(body.encode("utf-8"), timeout=120)
        outputs.append(json.loads(out.decode("utf-8")))

    assert all(o["ok"] is False for o in outputs)
    assert all("wrong" in o["error"] or "Too many" in o["error"] for o in outputs)
    for n, o in enumerate(outputs):
        assert f"wrong guess {n}" not in json.dumps(o)

    status = ApprovalSecretStore().status()
    assert status.remaining_attempts == 0 and status.locked_until > 0

    with pytest.raises(LibraryActionRefused, match="Too many"):
        executor.commit(approval["token"], operation="delete", secret=SECRET)
    assert path.read_text(encoding="utf-8") == "keep"


# ---------------------------------------------------------------- no agent path


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_only_the_library_executor_and_permissions_cli_reach_the_secret_store() -> None:
    users = {
        ".".join(p.relative_to(ROOT).with_suffix("").parts)
        for package in ("core", "security")
        for p in (ROOT / package).rglob("*.py")
        if "core.approval_secret" in _imports(p)
    }
    assert users == {"core.library_executor", "core.permissions_cli"}

    for module in ("runtime_bridge.py", "library_cli.py"):
        assert "approval_secret_set" not in (ROOT / "core" / module).read_text(encoding="utf-8")


def test_library_cli_cannot_provision_a_secret() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        library_cli.run("approval_secret_set", {"newSecret": SECRET})


def test_frontend_keeps_secrets_transient() -> None:
    panel = (ROOT / "desktop" / "src" / "components" / "ApprovalSecretPanel.tsx").read_text(encoding="utf-8")
    library = (ROOT / "desktop" / "src" / "components" / "LibraryView.tsx").read_text(encoding="utf-8")
    store = (ROOT / "desktop" / "src" / "libraryStore.ts").read_text(encoding="utf-8")
    settings = (ROOT / "desktop" / "src" / "settingsStore.ts").read_text(encoding="utf-8")
    permissions = (ROOT / "desktop" / "src" / "permissionStore.ts").read_text(encoding="utf-8")

    assert panel.count('type="password"') >= 2 and 'autoComplete="off"' in panel
    assert "clearSecrets()" in panel and "localStorage" not in panel and "sessionStorage" not in panel
    assert 'type="password"' in library and "setSecretInput(\"\")" in library
    assert '"approval_secret_set"' in permissions
    assert "secret" in store and "typedConfirmation" not in store and "typedPhrase" not in library
    assert "secret" not in settings.lower()


# ---------------------------------------------------------------- busy store


def test_a_busy_store_is_reported_as_busy_and_counts_nothing(store: ApprovalSecretStore, clock: Clock, tmp_path: Path) -> None:
    store.set_secret(SECRET)
    impatient = ApprovalSecretStore(store.path, clock=clock, busy_timeout=0.2, audit_path=None)

    holder = sqlite3.connect(store.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ApprovalSecretUnavailable, match="busy"):
            impatient.verify(OTHER)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert store.status().remaining_attempts == MAX_FAILURES_BEFORE_LOCK
    assert impatient.verify(SECRET) is True


def test_a_commit_blocked_by_a_reader_gives_the_same_answer_for_right_and_wrong(
    store: ApprovalSecretStore, clock: Clock,
) -> None:
    store.set_secret(SECRET)
    impatient = ApprovalSecretStore(store.path, clock=clock, busy_timeout=0.2, audit_path=None)

    reader = sqlite3.connect(store.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT failures FROM attempts").fetchone()
    try:
        messages = []
        for candidate in (SECRET, OTHER):
            with pytest.raises(ApprovalSecretUnavailable) as caught:
                impatient.verify(candidate)
            messages.append(str(caught.value))
    finally:
        reader.execute("ROLLBACK")
        reader.close()

    assert messages[0] == messages[1] and "busy" in messages[0]
    assert store.status().remaining_attempts == MAX_FAILURES_BEFORE_LOCK


def test_library_view_discards_the_secret_when_hidden() -> None:
    view = (ROOT / "desktop" / "src" / "components" / "LibraryView.tsx").read_text(encoding="utf-8")
    assert 'if(visible)return; setSecretInput(""); if(confirm){ void libraryCancel(confirm.approval)' in view
