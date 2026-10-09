"""Offline update lifecycle (core.update_service, core.update_cli, core.update_stores, payload schema 2).

Safety of these fixtures: every key is a fresh random Ed25519 key generated in
memory; data roots are tmp_path folders; the launcher is a recording fake (no
installer or other program is ever started); the authoriser is a fake unless a
test points the real gate and approval-secret store at tmp_path files through
their documented environment overrides. Native calls: hold_and_hash on fixture
files, and named mutexes with unique test names (never the runtime's own names).
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from core import ed25519, local_update, update_cli, update_service, update_stores
from core.local_update import DataProfile, InstalledState, UpdateError
from core.update_service import UpdateService

ROOT = Path(__file__).resolve().parent.parent
BASE = update_stores.current_profile()
SECRET = "fixture approval secret"


def new_key() -> bytes:
    return os.urandom(32)


def trusted(*secrets: bytes) -> dict[str, local_update.TrustedKey]:
    keys = {}
    for secret in secrets:
        public = ed25519.public_key(secret)
        keys[local_update.key_id(public)] = local_update.TrustedKey(local_update.key_id(public), public,
                                                                    "fixture owner", False)
    return keys


def stores(profile: DataProfile = BASE, **writes: int) -> dict:
    """dataStores for a package that writes ``writes`` (default: the profile's) and reads back to 1."""
    result = {}
    for store_id, version in profile.stores.items():
        target = writes.get(store_id, version)
        result[store_id] = {"writes": target, "reads": [1, max(target, version)]}
    return result


def package(folder: Path, version: str, data: bytes | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"qronos-desktop_{version}_x64-setup.exe"
    path.write_bytes(data if data is not None else b"MZ fixture installer " + version.encode())
    return path


def signed(installer: Path, secret: bytes, **changes) -> Path:
    data = installer.read_bytes()
    payload = {
        "schema": 2, "product": "com.qronos.agent", "version": installer.name.split("_")[1],
        "architecture": "x64", "target": "x86_64-pc-windows-msvc",
        "package": {"fileName": installer.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        "dataStores": stores(), "compatibilityEpoch": 1, "minimumVersion": None, "securityFloor": "0.1.0",
        "rollback": False, "notes": "fixture",
    }
    payload.update(changes)
    manifest = installer.with_name(installer.name + local_update.MANIFEST_SUFFIX)
    manifest.write_bytes(local_update.build_envelope(payload, [secret]))
    return manifest


class Recorder:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.fail = fail

    def __call__(self, path: Path, arguments: tuple[str, ...]) -> None:
        self.calls.append((path, arguments))
        if self.fail is not None:
            raise self.fail


class FakeAuthorizer:
    def __init__(self, refuse: str | None = None, problem: str | None = None) -> None:
        self.refuse, self.problem = refuse, problem
        self.asked: list[str] = []
        self.secrets: list[str] = []

    def allowed(self, offer):
        self.asked.append(offer.package_sha256)
        return self.refuse

    def secret_problem(self):
        return self.problem

    def verify_secret(self, secret):
        self.secrets.append(secret)
        return secret == SECRET


class Quiesce:
    """Stands in for the runtime mutexes: running or stopped, and records holds."""

    def __init__(self) -> None:
        self.running = False
        self.held = 0

    def __call__(self):
        if self.running:
            raise UpdateError("runtime_running", "fixture runtime is running")
        self.held += 1

        def release():
            self.held -= 1
        return release


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class World:
    """A fixture data root with a settings file, an owner key, fakes for the gate, runtime and launcher."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.data = tmp_path / "data"
        self.data.mkdir()
        (self.data / "settings.json").write_text('{"schemaVersion": 2, "voice": "fixture"}', encoding="utf-8")
        self.owner = new_key()
        self.keys = trusted(self.owner)
        self.launcher = Recorder()
        self.authorizer = FakeAuthorizer()
        self.quiesce = Quiesce()
        self.clock = Clock()

    def service(self, version: str = "0.1.0", profile: DataProfile = BASE, **overrides) -> UpdateService:
        options = {"keys_loader": lambda: self.keys, "profile": profile, "authorizer": self.authorizer,
                   "quiesce": self.quiesce, "launcher": self.launcher, "clock": self.clock}
        options.update(overrides)
        return UpdateService(self.data, version, **options)

    def release(self, version: str, **changes) -> tuple[Path, Path]:
        installer = package(self.tmp / "chosen", version)
        return installer, signed(installer, self.owner, **changes)

    def lifecycle_text(self) -> str:
        path = self.data / "Updates" / update_service.LIFECYCLE_FILE
        return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def staged(service: UpdateService, installer: Path, manifest: Path) -> tuple[dict, dict]:
    requested = service.request(installer, manifest)
    approved = service.approve(requested["approvalToken"], SECRET)
    return requested, approved


def launched(service: UpdateService, installer: Path, manifest: Path) -> dict:
    _, approved = staged(service, installer, manifest)
    return service.launch(approved["launchToken"])


# ---------------------------------------------------------------- store registry


def _module_constant(module: str, name: str) -> int:
    tree = ast.parse((ROOT / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{module}.{name} not found")


@pytest.mark.parametrize("spec", update_stores.STORES, ids=lambda spec: spec.store_id)
def test_the_registry_matches_each_store_modules_own_version_and_layout(spec):
    """Read from the source without importing the store modules (no side effects)."""
    kind, _, where = spec.source.partition(":")
    module = where if kind == "unversioned" else kind
    source = (ROOT / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8")
    if kind != "unversioned":
        assert _module_constant(kind, where) == spec.version
    if "sqlite3" in source:
        # A SQLite layout change without a version bump here fails this test.
        assert spec.ddl_sha256 == update_stores.ddl_fingerprint(source)
        assert spec.snapshot is None  # never copied raw
    else:
        assert kind != "unversioned"


def test_every_module_that_opens_a_sqlite_file_is_in_the_registry():
    registered = {spec.source.rpartition(":")[2] if spec.source.startswith("unversioned:")
                  else spec.source.partition(":")[0] for spec in update_stores.STORES}
    opening = {f"core.{path.stem}" for path in (ROOT / "core").glob("*.py")
               if "sqlite3.connect(" in path.read_text(encoding="utf-8")}
    assert opening - registered == set(), "a SQLite store the updater does not know about"


def test_the_registry_is_closed_and_snapshot_files_are_plain_names():
    assert len({spec.store_id for spec in update_stores.STORES}) == len(update_stores.STORES)
    for spec in update_stores.STORES:
        assert spec.snapshot is None or (Path(spec.snapshot).name == spec.snapshot and ".." not in spec.snapshot)
    assert BASE.stores["settings"] == 2 and BASE.epoch == update_stores.COMPATIBILITY_EPOCH


def test_the_fingerprint_changes_when_a_layout_changes():
    before = update_stores.ddl_fingerprint('X = "CREATE TABLE t (a TEXT)"')
    assert before != update_stores.ddl_fingerprint('X = "CREATE TABLE t (a TEXT, b TEXT)"')
    assert before == update_stores.ddl_fingerprint('X = """CREATE   TABLE t\n (a TEXT)"""')


# ------------------------------------------------------- payload schema 2 rules


def verify2(world: World, profile: DataProfile = BASE, version: str = "0.2.0", running: str = "0.1.0",
            **changes):
    installer, manifest = world.release(version, **changes)
    return local_update.verify_update(manifest.read_bytes(), installer, world.keys,
                                      InstalledState(running, profile))


def test_a_schema_2_upgrade_that_reads_every_store_is_accepted(world):
    assert verify2(world, dataStores=stores(settings=3)).kind == "upgrade"


@pytest.mark.parametrize(("data_stores", "code"), [
    ({k: v for k, v in stores().items() if k != "settings"}, "data_schema"),     # omits an active store
    ({**stores(), "settings": {"writes": 3, "reads": [3, 3]}}, "data_schema"),  # cannot read settings 2
    ({**stores(), "settings": {"writes": 1, "reads": [1, 2]}}, "data_schema"),  # upgrade lowers a format
])
def test_a_schema_2_package_that_cannot_handle_the_data_is_refused(world, data_stores, code):
    with pytest.raises(UpdateError) as refused:
        verify2(world, dataStores=data_stores)
    assert refused.value.code == code


def test_another_compatibility_epoch_is_refused(world):
    with pytest.raises(UpdateError) as refused:
        verify2(world, compatibilityEpoch=2)
    assert refused.value.code == "data_epoch"


def test_a_schema_1_package_cannot_describe_the_products_stores(world):
    installer = package(world.tmp / "chosen", "0.2.0")
    data = installer.read_bytes()
    payload = {"schema": 1, "product": "com.qronos.agent", "version": "0.2.0", "architecture": "x64",
               "target": "x86_64-pc-windows-msvc",
               "package": {"fileName": installer.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
               "dataSchema": {"writes": 2, "reads": [1, 2]}, "minimumVersion": None, "securityFloor": "0.1.0",
               "rollback": False, "notes": ""}
    envelope = local_update.build_envelope(payload, [world.owner])
    with pytest.raises(UpdateError) as refused:
        local_update.verify_update(envelope, installer, world.keys, InstalledState("0.1.0", BASE))
    assert refused.value.code == "unsupported_schema"


@pytest.mark.parametrize("data_stores", [
    {}, {"Bad Id": {"writes": 1, "reads": [1, 1]}}, {"settings": {"writes": True, "reads": [1, 1]}},
    {"settings": {"writes": 2, "reads": [3, 4]}}, {"settings": {"writes": 2, "reads": [1]}},
    {"settings": {"writes": 2, "reads": [1, 2], "extra": 1}}, ["settings"],
    {"settings": {"writes": 2, "reads": [1.0, 2]}}, {"settings": {"writes": 2, "reads": "1-2"}},
])
def test_malformed_data_stores_are_refused(world, data_stores):
    with pytest.raises(UpdateError) as refused:
        verify2(world, dataStores=data_stores)
    assert refused.value.code == "malformed"


def test_a_schema_2_payload_with_the_schema_1_field_is_refused(world):
    with pytest.raises(UpdateError) as refused:
        verify2(world, dataSchema={"writes": 2, "reads": [1, 2]})
    assert refused.value.code == "malformed"


def test_an_upgrade_may_introduce_a_new_store(world):
    offer = world.service().verify(*world.release("0.2.0", dataStores={**stores(),
                                                                       "new_store": {"writes": 1, "reads": [1, 1]}}))
    assert offer.kind == "upgrade" and offer.changed_stores == ()


def test_a_repair_that_claims_other_data_formats_is_refused(world):
    service = world.service("0.2.0")
    for data_stores in (stores(settings=3), {**stores(), "new_store": {"writes": 1, "reads": [1, 1]}}):
        with pytest.raises(UpdateError) as refused:
            service.verify(*world.release("0.2.0", dataStores=data_stores))
        assert refused.value.code == "data_schema"


def test_a_rollback_naming_stores_this_build_does_not_have_is_refused(world):
    service = world.service("0.2.0")
    with pytest.raises(UpdateError) as refused:
        service.verify(*world.release("0.1.0", rollback=True,
                                      dataStores={**stores(), "ghost": {"writes": 1, "reads": [1, 1]}}))
    assert refused.value.code == "data_schema"


@pytest.mark.parametrize("store_id", ["queue_state", "conversation_history", "approval_secret"])
def test_an_upgrade_changing_a_store_that_cannot_be_kept_is_refused(world, store_id):
    with pytest.raises(UpdateError) as refused:
        world.service().verify(*world.release("0.2.0", dataStores=stores(**{store_id: 2})))
    assert refused.value.code == "unsupported_migration"
    assert world.authorizer.asked == []


# --------------------------------------------------------------- trust gates


def test_with_the_shipped_empty_key_file_every_package_is_refused(world):
    shipped = json.loads((ROOT / "config" / "update_trusted_keys.json").read_text(encoding="utf-8"))
    assert shipped["keys"] == []
    service = world.service(keys_loader=local_update.load_trusted_keys)  # the real loader and file
    with pytest.raises(UpdateError) as refused:
        service.request(*world.release("0.2.0"))
    assert refused.value.code == "no_trusted_key"
    assert service.status()["trust"] == {"configured": False, "activeKeys": 0, "error": None}
    assert service.status()["events"][-1]["event"] == "refused"
    assert world.authorizer.asked == []


def test_a_self_signed_package_is_never_trusted(world):
    installer = package(world.tmp / "chosen", "0.2.0")
    manifest = signed(installer, new_key())  # signed, but by a key nobody configured
    with pytest.raises(UpdateError) as refused:
        world.service().request(installer, manifest)
    assert refused.value.code == "untrusted_signature"
    assert world.authorizer.asked == [] and world.launcher.calls == []


# --------------------------------------------------------------- lifecycle


def test_the_full_upgrade_lifecycle(world):
    service = world.service()
    installer, manifest = world.release("0.2.0", dataStores=stores(settings=3))

    requested = service.request(installer, manifest)
    offer = requested["offer"]
    assert (offer["kind"], offer["version"], offer["owner"]) == ("upgrade", "0.2.0", "fixture owner")
    assert offer["changedStores"] == ["settings"] and offer["snapshotStores"] == ["settings"]
    assert world.authorizer.asked == [offer["packageSha256"]]

    approved = service.approve(requested["approvalToken"], SECRET)
    assert approved["staged"]["packageSha256"] == offer["packageSha256"]
    assert world.launcher.calls == []  # approving stages; it never starts anything

    handoff = service.launch(approved["launchToken"])

    (started, arguments), = world.launcher.calls
    assert started == service.store.staged_dir / f"{offer['packageSha256']}.exe" and arguments == ()
    assert started.read_bytes() == installer.read_bytes()
    assert handoff["version"] == "0.2.0" and handoff["dataStores"]["settings"] == 3
    assert world.quiesce.held == 0  # the runtime hold was released after the start
    status = service.status()
    assert status["phase"] == "handed_off" and status["pending"] is None and status["launchReady"] is None
    snapshot, = status["snapshots"]
    assert snapshot["stores"] == ["settings"] and snapshot["reason"] == "upgrade"
    kept = service.snapshot_root / snapshot["id"] / "settings.json"
    assert kept.read_bytes() == (world.data / "settings.json").read_bytes()

    after = world.service("0.2.0", profile=DataProfile({**BASE.stores, "settings": 3}, 1))
    result = after.startup_check()
    assert result["result"] == "applied" and result["kind"] == "upgrade" and result["dataMismatch"] == []
    assert [e["event"] for e in after.status()["events"]] == [
        "verified", "requested", "verified", "approved", "staged", "snapshot", "launched", "applied"]


def test_tokens_and_the_secret_never_reach_the_state_events_or_status(world):
    service = world.service()
    requested, approved = staged(service, *world.release("0.2.0"))
    text = world.lifecycle_text() + json.dumps(service.status())
    for value in (requested["approvalToken"], approved["launchToken"], SECRET):
        assert value not in text


def test_an_installer_that_did_not_apply_is_reported_and_kept_for_a_retry(world):
    launched(world.service(), *world.release("0.2.0"))

    result = world.service("0.1.0").startup_check()  # the old version started again: cancelled or failed

    assert result["result"] == "not_applied" and result["phase"] == "handed_off"
    assert world.service().status()["staged"]["version"] == "0.2.0"
    world.service().discard()
    assert world.service().status()["phase"] == "idle"
    assert list(world.service().store.staged_dir.iterdir()) == []


def test_a_running_build_with_other_data_formats_than_signed_is_reported(world):
    launched(world.service(), *world.release("0.2.0", dataStores=stores(settings=3)))
    result = world.service("0.2.0").startup_check()  # still says settings 2: not what was signed
    assert result["result"] == "applied" and result["dataMismatch"] == ["settings"]


def test_a_startup_with_nothing_handed_off_changes_nothing(world):
    assert world.service().startup_check() == {"result": "nothing", "phase": "idle"}
    assert not (world.data / "Updates").exists()


# ------------------------------------------------ authorisation (A8-P1 contract)


def test_approve_without_a_token_from_request_changes_nothing(world):
    service = world.service()
    service.verify(*world.release("0.2.0"))
    for token in (None, "", "x" * 43, "not a token at all", 12345):
        with pytest.raises(UpdateError) as refused:
            service.approve(token, SECRET)
        assert refused.value.code == "not_approved"
    assert service.status()["phase"] == "idle" and world.launcher.calls == []
    assert not service.store.staged_dir.exists()
    assert world.authorizer.secrets == []  # no token, no secret check: no guessing oracle, no lockout spent


def test_a_guessed_token_is_refused_before_the_secret_is_checked(world):
    """With a real request pending, a wrong token never reaches secret verification."""
    service = world.service()
    requested = service.request(*world.release("0.2.0"))
    for token in ("B" * 43, requested["approvalToken"][:-1] + "x"):
        with pytest.raises(UpdateError) as refused:
            service.approve(token, SECRET)
        assert refused.value.code == "not_approved"
    assert world.authorizer.secrets == [] and service.status()["phase"] == "idle"
    assert service.approve(requested["approvalToken"], SECRET)["staged"]["version"] == "0.2.0"


def test_a_wrong_secret_stages_nothing_and_the_request_stays_usable(world):
    service = world.service()
    requested = service.request(*world.release("0.2.0"))
    for secret in ("wrong", "", None):
        with pytest.raises(UpdateError) as refused:
            service.approve(requested["approvalToken"], secret)
        assert refused.value.code == "approval_secret_rejected"
    assert service.status()["phase"] == "idle"
    assert service.approve(requested["approvalToken"], SECRET)["staged"]["version"] == "0.2.0"


def test_an_approval_token_works_once(world):
    service = world.service()
    requested = service.request(*world.release("0.2.0"))
    service.approve(requested["approvalToken"], SECRET)
    with pytest.raises(UpdateError) as refused:
        service.approve(requested["approvalToken"], SECRET)
    assert refused.value.code == "not_approved"


def test_launch_without_a_launch_token_starts_nothing(world):
    service = world.service()
    requested, _ = staged(service, *world.release("0.2.0"))
    for token in (None, "x" * 43, requested["approvalToken"]):  # the approval token is not a launch token
        with pytest.raises(UpdateError) as refused:
            service.launch(token)
        assert refused.value.code == "not_approved"
    assert world.launcher.calls == [] and service.status()["phase"] == "staged"
    assert not service.snapshot_root.exists()


def test_a_launch_token_works_once(world):
    service = world.service()
    _, approved = staged(service, *world.release("0.2.0"))
    service.launch(approved["launchToken"])
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "not_approved"
    assert len(world.launcher.calls) == 1


def test_a_refusing_permission_policy_refuses_the_request(world):
    world.authorizer.refuse = "install_software is forbidden by policy."
    with pytest.raises(UpdateError) as refused:
        world.service().request(*world.release("0.2.0"))
    assert refused.value.code == "permission_denied"
    assert world.service().status()["pending"] is None


def test_without_an_approval_secret_no_request_is_issued(world):
    world.authorizer.problem = "Set an approval secret in Settings first; updates need it."
    with pytest.raises(UpdateError) as refused:
        world.service().request(*world.release("0.2.0"))
    assert refused.value.code == "approval_secret_missing"
    assert world.service().status()["pending"] is None


def test_an_approval_expires(world):
    service = world.service()
    requested = service.request(*world.release("0.2.0"))
    world.clock.now += update_service.APPROVAL_SECONDS + 1
    with pytest.raises(UpdateError) as refused:
        service.approve(requested["approvalToken"], SECRET)
    assert refused.value.code == "approval_expired"
    assert service.status()["phase"] == "idle" and world.authorizer.secrets == []


def test_a_launch_grant_expires(world):
    service = world.service()
    _, approved = staged(service, *world.release("0.2.0"))
    world.clock.now += update_service.LAUNCH_SECONDS + 1
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "approval_expired"
    assert world.launcher.calls == []


def test_files_changed_after_the_request_are_never_staged(world):
    service = world.service()
    installer, manifest = world.release("0.2.0")
    requested = service.request(installer, manifest)
    installer.write_bytes(b"MZ something else")
    with pytest.raises(UpdateError) as refused:
        service.approve(requested["approvalToken"], SECRET)
    assert refused.value.code == "package_mismatch"
    assert service.status()["phase"] == "idle" and service.status()["pending"] is None


def test_a_different_validly_signed_package_swapped_in_after_the_request_is_refused(world):
    service = world.service()
    installer, manifest = world.release("0.2.0")
    requested = service.request(installer, manifest)
    installer.write_bytes(b"MZ another genuine build of 0.2.0")
    signed(installer, world.owner)  # re-signed by the owner: still not what the person approved
    with pytest.raises(UpdateError) as refused:
        service.approve(requested["approvalToken"], SECRET)
    assert refused.value.code == "approval_mismatch"
    assert service.status()["phase"] == "idle"


def test_the_same_package_re_signed_by_another_trusted_key_is_not_what_was_approved(world):
    """The approval binds the signer too: a second trusted key re-signing after the request is refused."""
    second = new_key()
    world.keys.update(trusted(second))
    service = world.service()
    installer, manifest = world.release("0.2.0")
    requested = service.request(installer, manifest)
    signed(installer, second)  # same bytes, same version, another trusted signer
    with pytest.raises(UpdateError) as refused:
        service.approve(requested["approvalToken"], SECRET)
    assert refused.value.code == "approval_mismatch"
    assert service.status()["phase"] == "idle"
    assert "approved" not in [event["event"] for event in service.status()["events"]]


def test_the_real_gate_asks_for_install_software_and_allows_asking(world, monkeypatch, tmp_path):
    """security.gate with the default policy (tmp policy file through its override): not refused."""
    monkeypatch.setenv("QRONOS_PERMISSION_POLICY_PATH", str(tmp_path / "policy" / "permissions_v2.json"))
    verdicts = []
    authorizer = update_service.GateAuthorizer(audit=verdicts.append)
    offer = world.service().verify(*world.release("0.2.0"))
    assert authorizer.allowed(offer) is None
    verdict, = verdicts
    assert verdict.request.category.value == "install_software"
    assert verdict.decision.value == "require_typed_secret"
    assert verdict.request.parameters["packageSha256"] == offer.package_sha256


def test_the_real_secret_store_without_a_secret_refuses(monkeypatch, tmp_path):
    monkeypatch.setenv("QRONOS_APPROVAL_SECRET_PATH", str(tmp_path / "secret" / "approval.sqlite3"))
    authorizer = update_service.GateAuthorizer()
    assert authorizer.secret_problem() is not None
    with pytest.raises(UpdateError) as refused:
        authorizer.verify_secret("anything")
    assert refused.value.code == "approval_secret_unavailable"


# ---------------------------------------------------------------- launch rules


def test_a_running_runtime_blocks_the_launch_without_side_effects(world):
    service = world.service()
    _, approved = staged(service, *world.release("0.2.0", dataStores=stores(settings=3)))
    world.quiesce.running = True
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "runtime_running"
    assert world.launcher.calls == [] and not service.snapshot_root.exists()
    assert service.status()["launchReady"] is not None  # still usable once the runtime has stopped
    world.quiesce.running = False
    service.launch(approved["launchToken"])
    assert len(world.launcher.calls) == 1


def test_the_real_mutex_check_sees_a_running_holder(tmp_path):
    name = f"Local\\QronosUpdateTest-{uuid.uuid4().hex}"
    release = update_service.hold_runtime_mutexes((name,))
    release()
    ready = tmp_path / "ready"
    holder = subprocess.Popen([sys.executable, "-B", "-c", (
        "import ctypes, sys, time, pathlib\n"
        "h = ctypes.windll.kernel32.CreateMutexW(None, False, sys.argv[1])\n"
        "pathlib.Path(sys.argv[2]).write_text('held')\n"
        "time.sleep(30)\n"), name, str(ready)])
    try:
        deadline = time.time() + 30
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        with pytest.raises(UpdateError) as refused:
            update_service.hold_runtime_mutexes((f"Local\\QronosUpdateTest-free-{uuid.uuid4().hex}", name))
        assert refused.value.code == "runtime_running"
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_approving_another_version_does_not_launch_the_one_still_staged(world):
    service = world.service()
    _, first = staged(service, *world.release("0.2.0"))
    requested = service.request(*world.release("0.3.0"))
    with pytest.raises(UpdateError):
        service.launch(requested["approvalToken"])
    assert world.launcher.calls == []
    service.launch(first["launchToken"])
    assert world.launcher.calls[0][0].name.startswith(service.status()["staged"]["packageSha256"])


def test_a_staged_package_replaced_behind_the_grant_is_not_launched(world):
    service = world.service()
    _, approved = staged(service, *world.release("0.2.0"))
    other_installer, other_manifest = world.release("0.3.0")
    service.store.stage(other_manifest, other_installer, world.keys, "0.1.0", BASE)  # another library caller
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "not_staged"
    assert world.launcher.calls == []


def test_a_staged_copy_changed_before_launch_is_not_started(world):
    service = world.service()
    _, approved = staged(service, *world.release("0.2.0"))
    record = service.status()["staged"]
    copy = service.store.staged_dir / f"{record['packageSha256']}.exe"
    copy.write_bytes(b"MZ tampered" + b"\0" * (record["packageBytes"] - 11))
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "package_mismatch"
    assert world.launcher.calls == [] and service.status()["launching"] is False


def test_the_hash_at_launch_is_taken_through_the_held_handle(world):
    """A held file whose bytes differ from the approval (as if swapped after verification) is refused."""
    service = world.service(hold=lambda path: (lambda: None, "1" * 64, path.stat().st_size))
    _, approved = staged(service, *world.release("0.2.0"))
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "package_mismatch"
    assert world.launcher.calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing modes")
def test_a_held_installer_cannot_be_written_renamed_or_deleted(tmp_path):
    target = tmp_path / "staged.exe"
    target.write_bytes(b"MZ held")
    close, digest, size = update_service.hold_and_hash(target)
    try:
        assert (digest, size) == (hashlib.sha256(b"MZ held").hexdigest(), 7)
        with pytest.raises(PermissionError):
            open(target, "r+b")
        with pytest.raises(PermissionError):
            os.replace(target, tmp_path / "moved.exe")
        with pytest.raises(PermissionError):
            os.unlink(target)
        assert target.read_bytes() == b"MZ held"  # reading stays possible, as starting the process needs
    finally:
        close()
    os.unlink(target)


def test_a_failed_launch_records_no_handoff_and_releases_the_claim(world):
    service = world.service(launcher=Recorder(fail=UpdateError("launch_failed", "fixture")))
    _, approved = staged(service, *world.release("0.2.0"))
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "launch_failed"
    status = service.status()
    assert status["handoff"] is None and status["launching"] is False
    assert world.quiesce.held == 0


# ------------------------------------------------------- data and rollback


def upgraded_world(world: World) -> UpdateService:
    """0.1.0 -> 0.2.0 (settings 2 -> 3, snapshot kept), now running 0.2.0 with new settings."""
    launched(world.service(), *world.release("0.2.0", dataStores=stores(settings=3)))
    (world.data / "settings.json").write_text('{"schemaVersion": 3, "voice": "after upgrade"}', encoding="utf-8")
    after = world.service("0.2.0", profile=DataProfile({**BASE.stores, "settings": 3}, 1))
    assert after.startup_check()["result"] == "applied"
    return after


def voice(world: World) -> str:
    return json.loads((world.data / "settings.json").read_text(encoding="utf-8"))["voice"]


def test_a_rollback_that_cannot_read_todays_data_restores_the_saved_copy(world):
    after = upgraded_world(world)
    installer, manifest = world.release("0.1.0", rollback=True, dataStores=stores())  # reads settings 1..2 only

    requested = after.request(installer, manifest)
    assert requested["offer"]["kind"] == "rollback" and requested["offer"]["restoreSnapshot"]
    handoff = after.launch(after.approve(requested["approvalToken"], SECRET)["launchToken"])

    assert voice(world) == "fixture"
    assert handoff["restoredSnapshot"] == requested["offer"]["restoreSnapshot"] and handoff["safetySnapshot"]
    safety = after.snapshot_root / handoff["safetySnapshot"] / "settings.json"
    assert json.loads(safety.read_text(encoding="utf-8"))["voice"] == "after upgrade"  # nothing lost for good


def test_a_rollback_without_a_saved_copy_is_refused_honestly(world):
    service = world.service("0.2.0", profile=DataProfile({**BASE.stores, "settings": 3}, 1))
    with pytest.raises(UpdateError) as refused:
        service.request(*world.release("0.1.0", rollback=True, dataStores=stores()))
    assert refused.value.code == "rollback_data"
    assert voice(world) == "fixture" and world.authorizer.asked == []


def test_a_rollback_that_reads_todays_data_needs_no_restore(world):
    offer = world.service("0.2.0").verify(*world.release("0.1.0", rollback=True))
    assert offer.kind == "rollback" and offer.restore_snapshot is None


def test_a_damaged_saved_copy_is_never_restored(world):
    after = upgraded_world(world)
    requested = after.request(*world.release("0.1.0", rollback=True, dataStores=stores()))
    approved = after.approve(requested["approvalToken"], SECRET)
    (after.snapshot_root / requested["offer"]["restoreSnapshot"] / "settings.json").write_text("{}", encoding="utf-8")
    with pytest.raises(UpdateError) as refused:
        after.launch(approved["launchToken"])
    assert refused.value.code == "rollback_data"
    assert voice(world) == "after upgrade"
    assert len(world.launcher.calls) == 1  # only the earlier upgrade; the rollback was never started


def test_a_restore_that_fails_part_way_puts_the_current_data_back(world, monkeypatch):
    """Two stores: the first is restored, the second fails (disk full); the first must be put back."""
    conversations = world.data / "conversations.json"
    conversations.write_text('{"schemaVersion": 1, "talk": "before"}', encoding="utf-8")
    launched(world.service(), *world.release("0.2.0", dataStores=stores(settings=3, conversations=2)))
    (world.data / "settings.json").write_text('{"schemaVersion": 3, "voice": "after upgrade"}', encoding="utf-8")
    conversations.write_text('{"schemaVersion": 2, "talk": "after"}', encoding="utf-8")
    profile = DataProfile({**BASE.stores, "settings": 3, "conversations": 2}, 1)
    after = world.service("0.2.0", profile=profile)
    after.startup_check()
    requested = after.request(*world.release("0.1.0", rollback=True, dataStores=stores()))
    approved = after.approve(requested["approvalToken"], SECRET)
    real_publish = after.store._publish
    writes = {"count": 0}

    def disk_full_on_second(directory, name, write):
        if Path(directory) == world.data:
            writes["count"] += 1
            if writes["count"] == 2:
                raise UpdateError("store", f"{name} could not be written; nothing was changed.")
        return real_publish(directory, name, write)

    monkeypatch.setattr(after.store, "_publish", disk_full_on_second)
    with pytest.raises(UpdateError) as refused:
        after.launch(approved["launchToken"])
    assert refused.value.code == "store"
    assert voice(world) == "after upgrade"
    assert json.loads(conversations.read_text(encoding="utf-8"))["talk"] == "after"
    assert len(world.launcher.calls) == 1


def test_a_crash_after_restoring_is_undone_at_the_next_start(world, monkeypatch):
    after = upgraded_world(world)
    requested = after.request(*world.release("0.1.0", rollback=True, dataStores=stores()))
    approved = after.approve(requested["approvalToken"], SECRET)
    crashing = world.service("0.2.0", profile=after.profile, launcher=Recorder(fail=KeyboardInterrupt()))
    monkeypatch.setattr(crashing, "_release_claim", lambda binding, error: None)  # as if the process died
    with pytest.raises(KeyboardInterrupt):
        crashing.launch(approved["launchToken"])
    assert voice(world) == "fixture" and after.status()["launching"] is True

    world.clock.now += update_service.CLAIM_STALE_SECONDS + 1
    result = after.startup_check()

    assert result["recovered"] is True and voice(world) == "after upgrade"
    assert after.status()["launching"] is False and after.status()["events"][-2]["event"] == "recovered"


def test_a_linked_store_file_is_not_snapshotted_through(world):
    outside = world.tmp / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (world.data / "settings.json").unlink()
    os.link(outside, world.data / "settings.json")  # a hard link: not this store's own file
    service = world.service()
    _, approved = staged(service, *world.release("0.2.0", dataStores=stores(settings=3)))
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "store"
    assert world.launcher.calls == [] and outside.read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------- recovery and state file


def test_an_interrupted_snapshot_is_removed_at_start_up_and_foreign_files_stay(world):
    service = world.service()
    launched(service, *world.release("0.2.0"))
    partial = service.snapshot_root / ".partial-1800000000-0123456789ab"
    partial.mkdir(parents=True)
    (partial / "settings.json").write_text("{}", encoding="utf-8")
    (partial / ".qronos-abc.tmp").write_bytes(b"")
    stranger = service.snapshot_root / ".partial-1800000000-ba9876543210"
    stranger.mkdir()
    (stranger / "user-notes.txt").write_text("not ours", encoding="utf-8")

    world.service("0.2.0").startup_check()

    assert not partial.exists()
    assert (stranger / "user-notes.txt").read_text(encoding="utf-8") == "not ours"


@pytest.mark.parametrize("damage", [
    lambda s: s.update(schema=True), lambda s: s.update(events="many"),
    lambda s: s.update(pending={"tokenHash": "0" * 64}), lambda s: s.update(snapshots=[{"id": "../x"}]),
    lambda s: s.update(events=[{"at": 1, "event": "exploded", "version": None, "detail": ""}]),
    lambda s: s.update(launching={"claimedAt": 1, "version": "0.2.0", "packageSha256": "0" * 64,
                                  "step": "anything", "safetySnapshot": None}),
])
def test_a_malformed_lifecycle_file_is_a_typed_refusal(world, damage):
    service = world.service()
    service.verify(*world.release("0.2.0"))
    state = json.loads(world.lifecycle_text())
    damage(state)
    service.lifecycle_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(UpdateError) as refused:
        service.status()
    assert refused.value.code == "store"


def test_the_event_history_is_bounded(world):
    service = world.service()
    installer, manifest = world.release("0.2.0")
    for _ in range(update_service._EVENT_LIMIT + 10):
        service.verify(installer, manifest)
    assert len(service.status()["events"]) == update_service._EVENT_LIMIT


# --------------------------------------------------------------------- discover


def test_discover_lists_only_installer_and_manifest_pairs(world):
    folder = world.tmp / "usb"
    for version in ("0.2.0", "0.3.0"):
        signed(package(folder, version), world.owner)
    package(folder, "0.4.0")                                   # no manifest
    (folder / "notes.txt").write_text("hello", encoding="utf-8")

    found = world.service().discover(folder)

    assert [Path(item["installer"]).name for item in found] == [
        "qronos-desktop_0.2.0_x64-setup.exe", "qronos-desktop_0.3.0_x64-setup.exe"]
    assert not (world.data / "Updates").exists()  # discovering opens and records nothing


def test_discover_refuses_a_linked_folder(world):
    winapi = pytest.importorskip("_winapi")
    real = world.tmp / "real"
    real.mkdir()
    winapi.CreateJunction(str(real), str(world.tmp / "linked"))
    with pytest.raises(UpdateError) as refused:
        world.service().discover(world.tmp / "linked")
    assert refused.value.code == "not_a_folder"


# -------------------------------------------------------------------------- CLI


def test_the_cli_runs_the_lifecycle_with_tokens(world):
    def factory(version):
        return world.service(version)

    installer, manifest = world.release("0.2.0")

    def run(operation, payload):
        return update_cli.run(operation, payload, factory, version="0.1.0")

    requested = run("request", {"installer": str(installer), "manifest": str(manifest)})
    assert requested["ok"] and requested["offer"]["kind"] == "upgrade"
    approved = run("approve", {"approvalToken": requested["approvalToken"], "secret": SECRET})
    assert approved["staged"]["version"] == "0.2.0" and SECRET not in json.dumps(approved)
    with pytest.raises(UpdateError) as refused:
        run("launch", {"runtimeStopped": True})  # a claim is not a grant
    assert refused.value.code == "not_approved"
    assert run("launch", {"launchToken": approved["launchToken"]})["handoff"]
    assert len(world.launcher.calls) == 1


@pytest.mark.parametrize(("operation", "payload"), [
    ("format_disk", {}),
    ("stage", {}),
    ("status", {"runningVersion": "9.9.9"}),          # the caller never says which version runs
    ("request", {"installer": "relative.exe", "manifest": "m.json"}),
    ("discover", {"folder": "x" * 5000}),
])
def test_the_cli_refuses_bad_requests(world, operation, payload):
    with pytest.raises(UpdateError) as refused:
        update_cli.run(operation, payload, lambda version: world.service(version), version="0.1.0")
    assert refused.value.code == "bad_request"


def test_the_running_version_comes_from_the_version_authority(tmp_path):
    assert update_cli.running_version({"QRONOS_APP_VERSION": "1.2.3"}) == "1.2.3"
    source = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))["version"]
    assert update_cli.running_version({}) == source
    for environment in ({"QRONOS_PACKAGED_RUNTIME": "1"}, {"QRONOS_APP_VERSION": "not-a-version"}):
        with pytest.raises(UpdateError):
            update_cli.running_version(environment)
    with pytest.raises(UpdateError) as refused:
        update_cli.running_version({}, root=tmp_path)  # no source checkout either
    assert refused.value.code == "version_unknown"


def test_the_runtime_start_up_hook_is_bounded_and_never_raises(world, monkeypatch):
    from core import config

    monkeypatch.setattr(config, "CONFIG", config.QronosConfig(paths=config.resolve_paths(
        environ={"QRONOS_DATA_ROOT": str(world.data), "LOCALAPPDATA": str(world.tmp)}, packaged=True)))
    assert update_service.startup_check({"appVersion": "0.1.0"}) == {"result": "nothing", "phase": "idle"}
    for context in (None, {}, {"appVersion": "0.1.0", "extra": 1}, {"appVersion": "nope"}):
        result = update_service.startup_check(context)
        assert result["result"] == "error" and set(result) == {"result", "code"}


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutexes")
def test_an_update_in_progress_is_visible_to_other_services_and_blocks_a_second_launch():
    name = f"Local\\QronosUpdateTest-progress-{uuid.uuid4().hex}"
    assert update_service.update_in_progress(name) is False
    release = update_service.hold_runtime_mutexes((name,))
    try:
        assert update_service.update_in_progress(name) is True
    finally:
        release()
    assert update_service.update_in_progress(name) is False


def test_the_cli_as_a_real_process_is_fail_closed_with_the_shipped_trust(world):
    """python -m core.update_cli, as the packaged backend would run it, with every state path in tmp_path."""
    installer, manifest = world.release("0.2.0")
    environment = {**os.environ, "QRONOS_PACKAGED_RUNTIME": "1", "QRONOS_DATA_ROOT": str(world.data),
                   "QRONOS_APP_VERSION": "0.1.0",
                   "QRONOS_ASSET_ROOT": str(world.tmp / "assets"), "LOCALAPPDATA": str(world.tmp / "local"),
                   "QRONOS_PERMISSION_AUDIT_PATH": str(world.tmp / "audit.jsonl"),
                   "QRONOS_PERMISSION_POLICY_PATH": str(world.tmp / "policy.json"),
                   "QRONOS_APPROVAL_SECRET_PATH": str(world.tmp / "secret.sqlite3"),
                   "PYTHONDONTWRITEBYTECODE": "1"}

    def run(operation, payload):
        done = subprocess.run([sys.executable, "-B", "-m", "core.update_cli", operation], cwd=ROOT, env=environment,
                              input=json.dumps(payload), capture_output=True, text=True, timeout=120)
        return done.returncode, json.loads(done.stdout)

    code, status = run("status", {})
    assert code == 0 and status["status"]["trust"]["configured"] is False
    assert status["status"]["runningVersion"] == "0.1.0"
    code, refused = run("request", {"installer": str(installer), "manifest": str(manifest)})
    assert code == 1 and refused["code"] == "no_trusted_key"
    # launch is not run here: it would create the real runtime's named mutexes on this PC.
    assert (world.data / "Updates").is_dir() and not (world.data / "Updates" / "staged").exists()


def test_the_cli_main_prints_a_typed_refusal(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["update", "format_disk"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({})))
    assert update_cli.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": False, "code": "bad_request", "error": "Unsupported update operation: format_disk"}


# ------------------------------------------------------------ store lock bounds


def test_another_thread_holding_the_store_gives_busy_not_a_hang(world):
    import threading

    store = local_update.LocalUpdateStore(world.data / "Updates")
    store.lock_timeout = 0.3
    entered, release = threading.Event(), threading.Event()

    def hold():
        with store._locked():
            entered.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(10)
        started = time.monotonic()
        with pytest.raises(UpdateError) as refused:
            with store._locked():
                pass
        assert refused.value.code == "busy" and time.monotonic() - started < 5
    finally:
        release.set()
        thread.join(10)
    with store._locked():  # the same thread may nest
        with store._locked():
            pass
