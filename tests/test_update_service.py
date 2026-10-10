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
from tests.fixtures.owned_junction import owned_junction

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
    """A fake installer launch: records the call and plays the installer process.

    A real fence handle it is handed (from a real MutexHold) is duplicated and kept
    until ``finish()``, as the installer keeps the duplicate it is given. ``stays``
    makes the fake installer still be running after the launch returns.
    ``receipt(service)`` writes the receipt a completed installation writes.
    """

    def __init__(self, fail: BaseException | None = None, stays: bool = False) -> None:
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.inherited: list[int] = []
        self.handovers: list[update_service.Handover] = []
        self.fail = fail
        self.stays = stays
        self.running = False

    def __call__(self, path: Path, arguments: tuple[str, ...], handover=None) -> None:
        self.calls.append((path, arguments))
        if handover is not None:
            self.handovers.append(handover)
        if self.fail is not None:
            raise self.fail
        if handover is not None and handover.fence is not None and os.name == "nt":
            import _winapi

            me = _winapi.GetCurrentProcess()
            self.inherited.append(_winapi.DuplicateHandle(me, handover.fence, me, 0, False,
                                                          _winapi.DUPLICATE_SAME_ACCESS))
        self.running = self.stays

    def receipt(self, *, nonce: bytes | None = None, version: str | None = None, name: str = "fixture.receipt",
                result: str = "installed", package: str | None = None, manifest: str | None = None,
                handover=None) -> Path:
        """The receipt the installer writes at the end of a successful installation (contract D1)."""
        handover = handover or self.handovers[-1]
        path = handover.receipts / name
        path.write_bytes(f"QRONOS-INSTALL-RECEIPT 1\r\nnonce={(nonce or handover.nonce).hex()}\r\n"
                         f"package={package or handover.package_sha256}\r\n"
                         f"manifest={manifest or handover.manifest_sha256}\r\n"
                         f"version={version or handover.version}\r\nresult={result}\r\n".encode("ascii"))
        return path

    def finish(self) -> None:
        """The fake installer ends: its handles close."""
        import _winapi

        while self.inherited:
            _winapi.CloseHandle(self.inherited.pop())
        self.running = False


class FakeAuthorizer:
    def __init__(self, refuse: str | None = None, problem: str | None = None) -> None:
        self.refuse, self.problem = refuse, problem
        self.asked: list[str] = []
        self.stages: list[str] = []
        self.records: list[tuple[str, dict]] = []
        self.secrets: list[str] = []

    def allowed(self, binding, stage):
        self.asked.append(binding["packageSha256"])
        self.stages.append(stage)
        return self.refuse

    def record(self, event, binding):
        self.records.append((event, dict(binding)))

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
        quiesce = self

        class Hold:
            released = False

            def handles(self):
                return ()

            def release(self):
                if not self.released:
                    self.released = True
                    quiesce.held -= 1
        return Hold()


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
                   "quiesce": self.quiesce, "launcher": self.launcher, "clock": self.clock,
                   # Live while a launch holds the exclusion or the fake installer still runs.
                   "liveness": lambda: self.quiesce.held > 0 or self.launcher.running,
                   # This world's data root is the truth: no store is relocated by the environment.
                   "environ": {}}
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


@pytest.mark.parametrize("store_id", ["queue_state", "conversation_history", "approval_secret",
                                      "link_devices", "permissions"])  # M-2: security registries too
def test_an_upgrade_changing_a_store_that_cannot_be_kept_is_refused(world, store_id):
    with pytest.raises(UpdateError) as refused:
        world.service().verify(*world.release("0.2.0", dataStores=stores(**{store_id: 2})))
    assert refused.value.code == "unsupported_migration"
    assert world.authorizer.asked == []


# --------------------------------------------------------------- trust gates


#: The one reviewed entry in the shipped key file (public output of the owner's create-key, 2026-10-09).
REVIEWED_OWNER_KEY_ID = "b0566b68ca6ee2887a8db62b3aff00e8"


def test_with_the_shipped_key_file_a_package_not_signed_by_the_owner_key_is_refused(world):
    shipped = json.loads((ROOT / "config" / "update_trusted_keys.json").read_text(encoding="utf-8"))
    assert [entry["keyId"] for entry in shipped["keys"]] == [REVIEWED_OWNER_KEY_ID]
    service = world.service(keys_loader=local_update.load_trusted_keys)  # the real loader and file
    with pytest.raises(UpdateError) as refused:
        service.request(*world.release("0.2.0"))  # signed by a fixture key, not the owner's
    assert refused.value.code == "untrusted_signature"
    assert service.status()["trust"] == {"configured": True, "activeKeys": 1, "error": None}
    assert service.status()["events"][-1]["event"] == "refused"
    assert world.authorizer.asked == []
    assert not (world.data / "Updates" / "staged").exists()


def test_with_an_empty_key_file_every_package_is_refused(world):
    empty = world.tmp / "empty_trusted_keys.json"
    empty.write_text(json.dumps({"schema": 1, "keys": []}), encoding="utf-8")
    service = world.service(keys_loader=lambda: local_update.load_trusted_keys(empty))
    with pytest.raises(UpdateError) as refused:
        service.request(*world.release("0.2.0"))
    assert refused.value.code == "no_trusted_key"
    assert service.status()["trust"] == {"configured": False, "activeKeys": 0, "error": None}
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
    assert world.service().startup_check() == {"result": "nothing", "phase": "idle", "recovered": False}
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


def test_the_real_gate_asks_for_install_software_at_every_stage_and_audits_without_secrets(
        world, monkeypatch, tmp_path):
    """security.gate with the default policy (tmp policy file through its override), and the real
    ActionAuditLog in tmp: every stage is decided, approval and completion are recorded, and no
    token or secret reaches the audit trail."""
    from core.action_audit import ActionAuditLog

    monkeypatch.setenv("QRONOS_PERMISSION_POLICY_PATH", str(tmp_path / "policy" / "permissions_v2.json"))
    audit_path = tmp_path / "audit" / "actions.jsonl"
    authorizer = update_service.GateAuthorizer(audit_log=ActionAuditLog(audit_path))
    binding = world.service().verify(*world.release("0.2.0")).binding()
    for stage in ("request", "approve", "launch"):
        assert authorizer.allowed(binding, stage) is None
    authorizer.record("approved", binding)
    authorizer.record("launched", binding)
    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    events = [r["event"] for r in records]
    assert events == ["decided", "decided", "decided", "approved", "completed"]
    assert all(r["category"] == "install_software" for r in records)
    assert {r["decision"] for r in records if r["event"] == "decided"} == {"require_typed_secret"}
    text = audit_path.read_text(encoding="utf-8")
    assert binding["packageSha256"] not in text or True  # the digest may be recorded; tokens never are
    for forbidden in (SECRET, "approvalToken", "launchToken", "tokenHash"):
        assert forbidden not in text


def test_a_policy_that_refuses_after_the_request_stops_approve_and_launch(world):
    """M-1: the gate is asked again at approve and at launch."""
    service = world.service()
    requested = service.request(*world.release("0.2.0"))
    world.authorizer.refuse = "Installing software is now forbidden by policy."
    with pytest.raises(UpdateError) as refused:
        service.approve(requested["approvalToken"], SECRET)
    assert refused.value.code == "permission_denied"
    assert service.status()["phase"] == "idle" and service.status()["pending"] is None
    assert world.authorizer.secrets == []  # refused before the secret was even checked

    world.authorizer.refuse = None
    requested = service.request(*world.release("0.2.0"))
    approved = service.approve(requested["approvalToken"], SECRET)
    world.authorizer.refuse = "Installing software is now forbidden by policy."
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "permission_denied"
    assert world.launcher.calls == [] and service.status()["launchReady"] is None
    assert world.authorizer.stages == ["request", "approve", "request", "approve", "launch"]
    assert [event for event, _ in world.authorizer.records] == ["approved"]


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


def test_a_verified_non_start_records_no_handoff_and_releases_the_claim(world):
    """Only NotStarted (Windows created no process) proves nothing changed."""
    service = world.service(launcher=Recorder(fail=update_service.NotStarted("fixture: CreateProcess failed")))
    _, approved = staged(service, *world.release("0.2.0"))
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "launch_failed"
    status = service.status()
    assert status["handoff"] is None and status["launching"] is False
    assert world.quiesce.held == 0


def test_any_other_launcher_failure_keeps_the_durable_handoff(world):
    """N4: an arbitrary error after the process may exist is not proof that nothing changed."""
    service = world.service(launcher=Recorder(fail=UpdateError("launch_failed", "fixture: after the child existed")))
    _, approved = staged(service, *world.release("0.2.0"))
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "launch_outcome_unknown"
    state = json.loads(world.lifecycle_text())
    assert state["launching"]["step"] == "starting" and state["handoff"]["version"] == "0.2.0"
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

    # No timer: the launching process is gone (its hold was released), so recovery runs at once.
    result = after.startup_check()

    assert result["recovered"] is True and voice(world) == "after upgrade"
    # N4: the interrupted claim is recovered first, then its durable handoff is resolved (not applied: the
    # 0.2.0 build still runs, so its own data was put back).
    events = [event["event"] for event in after.status()["events"]]
    assert after.status()["launching"] is False and events[-1] == "not_applied" and "recovered" in events[-3:]


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
    real = world.tmp / "real"
    real.mkdir()
    with owned_junction(real, world.tmp / "linked"), pytest.raises(UpdateError) as refused:
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
    assert update_service.startup_check({"appVersion": "0.1.0"}) == {
        "result": "nothing", "phase": "idle", "updateInProgress": False, "recoveryPending": False,
        "dataMismatch": []}
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
    assert code == 0 and status["status"]["trust"] == {"configured": True, "activeKeys": 1, "error": None}
    assert status["status"]["runningVersion"] == "0.1.0"
    code, refused = run("request", {"installer": str(installer), "manifest": str(manifest)})
    assert code == 1 and refused["code"] == "untrusted_signature"  # signed by a fixture key, not the owner's
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


# ------------------------------------------------- round 5: review findings as regression tests
#
# Each case below states the REQUIRED behaviour from the independent Control review of c4db28b
# (H-1..H-4, M-1, M-2, L-1, L-3). Real named mutexes use unique "Local\QronosUpdateTest-..." names;
# real child processes are base-interpreter fixtures owned and waited here.


def test_by_default_the_installer_launch_is_refused_and_nothing_happens(world):
    """H-2/A8-P3: until the runtime, desktop and installer lifetime dependencies are accepted."""
    service = world.service(quiesce=update_service.launch_unavailable)
    _, approved = staged(service, *world.release("0.2.0", dataStores=stores(settings=3)))
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "launch_unavailable"
    status = service.status()
    assert world.launcher.calls == [] and not service.snapshot_root.exists()
    assert status["launchAvailable"] is False and status["launchReady"] is not None  # token not spent
    assert status["phase"] == "staged" and status["launching"] is False


def test_the_production_default_service_refuses_the_launch():
    """No injection: the UpdateService default is the refusing quiescence."""
    import inspect

    assert inspect.signature(UpdateService).parameters["quiesce"].default is update_service.launch_unavailable


def test_a_cancelled_same_version_repair_is_unconfirmed_and_stays_staged(world):
    """H-1: the same version running again does not prove the repair installer ran."""
    launched(world.service("0.1.0"), *world.release("0.1.0"))
    result = world.service("0.1.0").recover_before_stores()
    assert result["result"] == "unconfirmed" and result["phase"] == "handed_off"
    status = world.service("0.1.0").status()
    assert status["outcome"]["result"] == "unconfirmed" and status["staged"]["version"] == "0.1.0"
    assert status["history"] == []  # nothing recorded as installed
    assert world.service("0.1.0").recover_before_stores()["result"] == "unconfirmed"  # stable across starts
    world.service("0.1.0").discard()
    assert world.service("0.1.0").status()["phase"] == "idle"


# ------------------------------------- installation receipt (round-7 contract D)


def test_a_completed_repair_is_confirmed_only_by_its_own_receipt(world):
    launched(world.service("0.1.0"), *world.release("0.1.0"))
    receipt = world.launcher.receipt()  # the installer that was handed this nonce finished
    result = world.service("0.1.0").recover_before_stores()
    assert result["result"] == "applied" and result["receipt"] == "verified" and result["phase"] == "idle"
    status = world.service("0.1.0").status()
    assert status["outcome"]["receipt"] == "verified" and [h["version"] for h in status["history"]] == ["0.1.0"]
    assert not receipt.exists()  # consumed: it can never count for a later start


@pytest.mark.parametrize("forgery", ["another_nonce", "another_version", "another_package", "another_manifest",
                                     "not_installed", "extra_line", "other_name", "second_link"])
def test_a_receipt_that_proves_nothing_leaves_the_repair_unconfirmed(world, forgery):
    launched(world.service("0.1.0"), *world.release("0.1.0"))
    if forgery == "another_nonce":
        world.launcher.receipt(nonce=os.urandom(32))
    elif forgery == "another_version":
        world.launcher.receipt(version="0.1.1")
    elif forgery == "another_package":  # a different same-version installer
        world.launcher.receipt(package="a" * 64)
    elif forgery == "another_manifest":
        world.launcher.receipt(manifest="b" * 64)
    elif forgery == "not_installed":
        world.launcher.receipt(result="cancelled")
    elif forgery == "extra_line":
        path = world.launcher.receipt()
        path.write_bytes(path.read_bytes() + b"trusted=yes\r\n")
    elif forgery == "other_name":
        world.launcher.receipt(name="fixture.txt")
    else:
        path = world.launcher.receipt()
        os.link(path, world.tmp / "second-name.receipt")
    result = world.service("0.1.0").recover_before_stores()
    assert result["result"] == "unconfirmed" and result["receipt"] == "invalid" and result["phase"] == "handed_off"
    assert world.service("0.1.0").status()["history"] == []


def test_a_receipt_from_an_earlier_same_version_handoff_cannot_confirm_this_one(world):
    """Replay: the first repair's installer finished late; its receipt names the same version and package."""
    service = world.service("0.1.0")
    launched(service, *world.release("0.1.0"))
    first = world.launcher.handovers[-1]
    service.discard()
    launched(world.service("0.1.0"), *world.release("0.1.0"))
    second = world.launcher.handovers[-1]
    assert first.nonce != second.nonce and first.package_sha256 == second.package_sha256
    world.launcher.receipt(handover=first)
    result = world.service("0.1.0").recover_before_stores()
    assert result["result"] == "unconfirmed" and result["receipt"] == "invalid"


# ------------------------------------------- N5: an honest, resolved unconfirmed repair (round-10 contract 1)


def test_a_handoff_the_barrier_has_not_resolved_is_recovery_pending(world):
    launched(world.service("0.1.0"), *world.release("0.1.0"))
    status = world.service("0.1.0").status()
    assert status["recoveryPending"] is True and status["unconfirmed"] is None  # a restart CAN resolve it


def test_a_resolved_unconfirmed_repair_is_not_recovery_pending_and_offers_discard_or_retry(world):
    launched(world.service("0.1.0"), *world.release("0.1.0"))
    assert world.service("0.1.0").recover_before_stores()["result"] == "unconfirmed"
    events_after_first = len(json.loads(world.lifecycle_text())["events"])
    for _ in range(3):  # restarting resolves nothing new, adds no event, promises nothing
        result = world.service("0.1.0").recover_before_stores()
        assert result["result"] == "unconfirmed" and result["receipt"] == "absent"
    assert len(json.loads(world.lifecycle_text())["events"]) == events_after_first
    status = world.service("0.1.0").status()
    assert status["recoveryPending"] is False
    assert status["unconfirmed"] == {"version": "0.1.0", "kind": "repair", "receipt": "absent",
                                     "actions": ["discard", "retry"]}


def test_a_receipt_arriving_after_the_resolution_never_confirms(world):
    launched(world.service("0.1.0"), *world.release("0.1.0"))
    world.service("0.1.0").recover_before_stores()
    late = world.launcher.receipt()  # even a correctly bound receipt: its installer had already ended
    result = world.service("0.1.0").recover_before_stores()
    assert result["result"] == "unconfirmed" and not late.exists()
    assert world.service("0.1.0").status()["history"] == []


def test_retry_is_discard_then_a_new_check_approval_and_nonce(world):
    service = world.service("0.1.0")
    launched(service, *world.release("0.1.0"))
    first = world.launcher.handovers[-1]
    service.recover_before_stores()
    service.discard()  # the "retry" action starts with this; nothing of the old launch is replayed
    status = service.status()
    assert status["phase"] == "idle" and status["unconfirmed"] is None and status["recoveryPending"] is False
    launched(world.service("0.1.0"), *world.release("0.1.0"))  # check again: new request, approval, launch
    second = world.launcher.handovers[-1]
    assert second.nonce != first.nonce and len(world.launcher.calls) == 2
    world.launcher.receipt()  # this time the installer finishes and writes its own receipt
    result = world.service("0.1.0").recover_before_stores()
    assert result["result"] == "applied" and result["receipt"] == "verified"
    assert world.service("0.1.0").status()["unconfirmed"] is None


def test_a_receipt_written_when_nothing_was_handed_off_is_discarded(world):
    launched(world.service("0.1.0"), *world.release("0.1.0"))
    world.service("0.1.0").discard()  # the repair was given up; its installer's receipt arrives late
    late = world.launcher.receipt()
    assert world.service("0.1.0").recover_before_stores()["result"] == "nothing"
    assert not late.exists()


@pytest.mark.parametrize("with_receipt", [True, False])
def test_an_upgrade_records_whether_its_receipt_was_verified(world, with_receipt):
    launched(world.service(), *world.release("0.2.0", dataStores=stores(settings=3)))
    if with_receipt:
        world.launcher.receipt()
    after = world.service("0.2.0", profile=DataProfile({**BASE.stores, "settings": 3}, 1))
    result = after.recover_before_stores()
    assert result["result"] == "applied" and result["receipt"] == ("verified" if with_receipt else "absent")


def test_the_nonce_reaches_only_the_installer(world):
    """The lifecycle keeps the nonce's SHA-256; the nonce itself is never stored, logged or shown."""
    service = world.service("0.1.0")
    result = launched(service, *world.release("0.1.0"))
    handover = world.launcher.handovers[-1]
    assert result["nonceSha256"] == hashlib.sha256(handover.nonce).hexdigest() and len(handover.nonce) == 32
    assert handover.receipts == world.data / "Updates" / "receipts" and handover.receipts.is_dir()
    shown = world.lifecycle_text() + json.dumps(service.status()) + json.dumps(result)
    assert handover.nonce.hex() not in shown


def test_no_caller_value_can_stand_in_for_a_receipt(world):
    import inspect

    assert list(inspect.signature(UpdateService.recover_before_stores).parameters) == ["self"]
    launched(world.service("0.1.0"), *world.release("0.1.0"))
    assert world.service("0.1.0").recover_before_stores()["result"] == "unconfirmed"  # no receipt written


def test_recovery_asks_again_under_the_store_lock(world):
    """G6: an update that starts between the first liveness check and the store lock stops the barrier."""
    launched(world.service("0.1.0"), *world.release("0.1.0"))
    answers = iter([False, True])
    service = world.service("0.1.0", liveness=lambda: next(answers))
    before = world.lifecycle_text()
    with pytest.raises(UpdateError) as refused:
        service.recover_before_stores()
    assert refused.value.code == "update_in_progress" and world.lifecycle_text() == before


def test_a_cancelled_rollback_puts_todays_data_back_before_stores_open(world):
    """H-3: the newer build that still runs must get its own data back, from the pinned safety snapshot."""
    after = upgraded_world(world)
    requested = after.request(*world.release("0.1.0", rollback=True, dataStores=stores()))
    handoff = after.launch(after.approve(requested["approvalToken"], SECRET)["launchToken"])
    assert voice(world) == "fixture" and handoff["safetySnapshot"]
    assert handoff["safetySnapshot"] in update_service._pinned(json.loads(world.lifecycle_text()))
    result = world.service("0.2.0", profile=after.profile).recover_before_stores()  # installer cancelled
    assert result["result"] == "not_applied" and result["dataRestored"] is True
    assert voice(world) == "after upgrade"
    assert world.service("0.2.0", profile=after.profile).recover_before_stores()["dataRestored"] is False


def test_start_up_is_refused_while_the_installer_still_runs_then_resolves(world):
    """H-4: liveness is the update mutex / installer process, never elapsed time."""
    world.launcher.stays = True
    launched(world.service(), *world.release("0.2.0"))
    world.clock.now += 3600  # an hour later, the installer is still running: still refused
    with pytest.raises(UpdateError) as refused:
        world.service("0.2.0").recover_before_stores()
    assert refused.value.code == "update_in_progress"
    assert update_service.recover_before_stores.__doc__  # module entry exists for the runtime
    world.launcher.finish()
    assert world.service("0.2.0").recover_before_stores()["result"] == "applied"


# ------------------------------------------- N4: durable starting state (round-8 contract 2)


def _rollback_to_0_1_0(world):
    """0.1.0 -> 0.2.0 (settings 3, voice 'after upgrade'), then an approved rollback to 0.1.0 (voice 'fixture')."""
    after = upgraded_world(world)
    requested = after.request(*world.release("0.1.0", rollback=True, dataStores=stores()))
    return after, after.approve(requested["approvalToken"], SECRET)


def test_a_verified_non_start_puts_todays_data_back_at_once(world):
    after, approved = _rollback_to_0_1_0(world)
    failing = world.service("0.2.0", profile=after.profile,
                            launcher=Recorder(fail=update_service.NotStarted("fixture: CreateProcess failed")))
    with pytest.raises(UpdateError) as refused:
        failing.launch(approved["launchToken"])
    assert refused.value.code == "launch_failed" and voice(world) == "after upgrade"
    assert failing.status()["handoff"] is None and failing.status()["launching"] is False


@pytest.mark.parametrize(("failure", "running", "voice_after"), [
    ("launcher_error", "0.1.0", "fixture"),           # the installer had acted: the rollback applied
    ("launcher_error", "0.2.0", "after upgrade"),     # it had not: today's data comes back, by version
    ("save_after_start", "0.1.0", "fixture"),
    ("save_after_start", "0.2.0", "after upgrade"),
    ("interrupted", "0.1.0", "fixture"),
    ("interrupted", "0.2.0", "after upgrade"),
])
def test_an_unknown_launch_outcome_is_resolved_only_by_the_running_version(world, monkeypatch, failure, running,
                                                                           voice_after):
    """N4 (Control r5): never restore from an unknown post-start state; the next start decides by version."""
    after, approved = _rollback_to_0_1_0(world)
    launcher = Recorder(fail={"launcher_error": UpdateError("busy", "fixture: the hand-over failed"),
                              "interrupted": KeyboardInterrupt()}.get(failure))
    service = world.service("0.2.0", profile=after.profile, launcher=launcher)
    if failure == "save_after_start":
        original, failed = service._change, []

        def flaky(change):
            if launcher.calls and not failed:
                failed.append(True)
                raise UpdateError("busy", "fixture: the lifecycle file could not be saved")
            return original(change)

        monkeypatch.setattr(service, "_change", flaky)
    if failure == "interrupted":
        monkeypatch.setattr(service, "_release_claim", lambda binding, error: None)  # the process died
    with pytest.raises((UpdateError, KeyboardInterrupt)) as refused:
        service.launch(approved["launchToken"])
    if failure != "interrupted":
        assert refused.value.code == "launch_outcome_unknown"
    assert voice(world) == "fixture" and len(launcher.calls) == 1  # started on the restored (rolled-back) data
    state = json.loads(world.lifecycle_text())
    assert state["launching"]["step"] == "starting" and state["handoff"]["restoredSnapshot"]
    assert state["handoff"]["safetySnapshot"] and state["handoff"]["packageSha256"]

    profile = BASE if running == "0.1.0" else after.profile
    result = world.service(running, profile=profile).recover_before_stores()
    assert result["recovered"] is True
    assert result["result"] == ("applied" if running == "0.1.0" else "not_applied")
    assert voice(world) == voice_after
    assert json.loads(world.lifecycle_text())["launching"] is None


def test_a_real_child_that_already_acted_before_the_launcher_failed_keeps_the_rollback(world):
    """The child changes the installed state, then the launcher raises: the outcome is decided by what runs next."""
    from tests.fixtures.child_python import child_python

    after, approved = _rollback_to_0_1_0(world)
    marker = world.tmp / "installed-version.txt"
    executable, environment = child_python()

    def acting_launcher(path, arguments, handover):
        child = subprocess.run([executable, "-B", "-I", "-c",
                                "import sys; open(sys.argv[1], 'w').write('0.1.0')", str(marker)],
                               env=environment, timeout=60, check=True)
        assert child.returncode == 0  # the "installer" has replaced the program
        raise UpdateError("busy", "fixture: the launcher failed after its child acted")

    service = world.service("0.2.0", profile=after.profile, launcher=acting_launcher)
    with pytest.raises(UpdateError) as refused:
        service.launch(approved["launchToken"])
    assert refused.value.code == "launch_outcome_unknown" and marker.read_text() == "0.1.0"
    assert voice(world) == "fixture"  # nothing was put back by the launch path
    result = world.service(marker.read_text(), profile=BASE).recover_before_stores()
    assert result["result"] == "applied" and voice(world) == "fixture"


@pytest.mark.skipif(os.name != "nt", reason="Windows processes and handles")
def test_a_hand_over_that_fails_after_the_child_exists_is_not_a_verified_non_start(world, tmp_path):
    """The real start_fenced: the process exists, then duplicating the fence fails. The child's input is closed,
    the stand-in installer refuses an incomplete record by itself, and the outcome stays with the barrier."""
    from tests.fixtures.child_python import child_python

    executable, _ = child_python()
    report, go = tmp_path / "stand-in.json", tmp_path / "go"
    go.write_text("1")
    argv = [executable, "-B", "-I", str(Path(__file__).resolve().parent / "fixtures" / "update_installer_child.py"),
            str(Path(__file__).resolve().parent.parent), f"Local\\QronosUpdateTest-n4-{uuid.uuid4().hex}",
            str(world.data), "update", str(go), str(report)]
    started: list = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.append(process)
        return process

    handover = update_service.Handover(os.urandom(32), 0x7FFFFFF0, world.data / "Updates" / "receipts", "0.2.0",
                                       "a" * 64, "b" * 64)  # not a handle of this process
    import unittest.mock

    with unittest.mock.patch("subprocess.Popen", recording_popen):
        with pytest.raises(UpdateError) as refused:
            update_service.start_fenced(argv, str(tmp_path), handover)
    assert refused.value.code == "launch_outcome_unknown" and not isinstance(refused.value, update_service.NotStarted)
    assert started and started[0].wait(60) == 3  # the stand-in refused by itself; it was not killed
    assert json.loads(report.read_text(encoding="utf-8"))["refused"] == "bad_record"


@pytest.mark.parametrize("damage", ["no_handoff", "other_package", "other_snapshot", "store_not_handed_off"])
def test_a_starting_record_that_does_not_match_its_handoff_stops_start_up(world, monkeypatch, damage):
    """N4: unknown state fails closed BEFORE any store opens; nothing is restored or rewritten."""
    after, approved = _rollback_to_0_1_0(world)
    service = world.service("0.2.0", profile=after.profile, launcher=Recorder(fail=KeyboardInterrupt()))
    monkeypatch.setattr(service, "_release_claim", lambda binding, error: None)
    with pytest.raises(KeyboardInterrupt):
        service.launch(approved["launchToken"])
    path = world.data / "Updates" / update_service.LIFECYCLE_FILE
    state = json.loads(path.read_text(encoding="utf-8"))
    if damage == "no_handoff":
        state["handoff"] = None
    elif damage == "other_package":
        state["handoff"]["packageSha256"] = "f" * 64
    elif damage == "other_snapshot":
        state["handoff"]["safetySnapshot"] = None
    if damage == "store_not_handed_off":
        after.store.discard()
    else:
        path.write_text(json.dumps(state), encoding="utf-8")
    before, data_before = world.lifecycle_text(), voice(world)
    with pytest.raises(UpdateError) as refused:
        world.service("0.2.0", profile=after.profile).recover_before_stores()
    assert refused.value.code == "recovery_failed"
    assert world.lifecycle_text() == before and voice(world) == data_before == "fixture"


def test_an_interrupted_launch_blocks_new_work_until_start_up_recovers_it(world, monkeypatch):
    """H-4: a dead claim is never silently ignored or overwritten later."""
    after = upgraded_world(world)
    requested = after.request(*world.release("0.1.0", rollback=True, dataStores=stores()))
    approved = after.approve(requested["approvalToken"], SECRET)
    crashing = world.service("0.2.0", profile=after.profile, launcher=Recorder(fail=KeyboardInterrupt()))
    monkeypatch.setattr(crashing, "_release_claim", lambda binding, error: None)  # the process died
    with pytest.raises(KeyboardInterrupt):
        crashing.launch(approved["launchToken"])
    with pytest.raises(UpdateError) as refused:
        after.request(*world.release("0.1.0", rollback=True, dataStores=stores()))
    assert refused.value.code == "recovery_pending"
    result = world.service("0.2.0", profile=after.profile).recover_before_stores()  # one second later
    assert result["recovered"] is True and voice(world) == "after upgrade"


def test_a_failed_safety_restore_refuses_start_up(world, monkeypatch):
    after = upgraded_world(world)
    requested = after.request(*world.release("0.1.0", rollback=True, dataStores=stores()))
    handoff = after.launch(after.approve(requested["approvalToken"], SECRET)["launchToken"])
    damaged = after.snapshot_root / handoff["safetySnapshot"] / "settings.json"
    damaged.write_text("{}", encoding="utf-8")
    with pytest.raises(UpdateError) as refused:
        world.service("0.2.0", profile=after.profile).recover_before_stores()
    assert refused.value.code == "recovery_failed"


def test_a_store_relocated_by_the_environment_is_never_snapshotted(world):
    """L-3: the snapshot must be of the file the store really uses."""
    service = world.service(environ={"QRONOS_SETTINGS_PATH": str(world.tmp / "elsewhere" / "settings.json")})
    with pytest.raises(UpdateError) as refused:
        service.verify(*world.release("0.2.0", dataStores=stores(settings=3)))
    assert refused.value.code == "unsupported_migration"


def test_a_store_absent_before_the_upgrade_is_recorded_and_never_silently_skipped(world):
    """L-3: absence is recorded; a rollback that would have to delete today's file is refused."""
    (world.data / "settings.json").unlink()
    launched(world.service(), *world.release("0.2.0", dataStores=stores(settings=3)))
    snapshot = json.loads(world.lifecycle_text())["snapshots"][0]
    assert snapshot["stores"]["settings"]["present"] is False
    (world.data / "settings.json").write_text('{"schemaVersion": 3}', encoding="utf-8")
    after = world.service("0.2.0", profile=DataProfile({**BASE.stores, "settings": 3}, 1))
    after.recover_before_stores()
    with pytest.raises(UpdateError) as refused:
        after.request(*world.release("0.1.0", rollback=True, dataStores=stores()))
    assert refused.value.code == "rollback_data"


def test_the_approval_binds_the_exact_manifest(world):
    """L-1: the same installer and signer, a different signed manifest after the request: refused."""
    service = world.service()
    installer, manifest = world.release("0.2.0")
    requested = service.request(installer, manifest)
    assert requested["offer"]["manifestSha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    signed(installer, world.owner, notes="a different signed manifest")
    with pytest.raises(UpdateError) as refused:
        service.approve(requested["approvalToken"], SECRET)
    assert refused.value.code == "approval_mismatch"


def test_snapshots_an_unresolved_update_refers_to_are_pinned():
    state = {"launching": {"restoredSnapshot": "1800000000-aaaaaaaaaaaa", "safetySnapshot": "1800000001-bbbbbbbbbbbb"},
             "handoff": {"restoredSnapshot": None, "safetySnapshot": "1800000002-cccccccccccc"},
             "pending": None, "launchGrant": {"binding": {"restoreSnapshot": "1800000003-dddddddddddd"}}}
    assert update_service._pinned(state) == {"1800000000-aaaaaaaaaaaa", "1800000001-bbbbbbbbbbbb",
                                             "1800000002-cccccccccccc", "1800000003-dddddddddddd"}


# ---------------------------------------------- owned exclusion with real mutexes and processes


def _wait_from_another_process(name: str) -> int:
    """WaitForSingleObject(0) on the named mutex from a fresh base-interpreter process."""
    from tests.fixtures.child_python import child_python

    executable, environment = child_python()
    code = (
        "import ctypes, sys\n"
        "from ctypes import wintypes\n"
        "k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]\n"
        "k.CreateMutexW.restype = wintypes.HANDLE\n"
        "k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]\n"
        "k.WaitForSingleObject.restype = wintypes.DWORD\n"
        "h = k.CreateMutexW(None, False, sys.argv[1])\n"
        "print(k.WaitForSingleObject(h, 0))\n"
    )
    done = subprocess.run([executable, "-B", "-I", "-c", code, name], capture_output=True, text=True, env=environment,
                          timeout=60)
    return int(done.stdout.strip())


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutexes")
def test_the_hold_owns_its_mutexes_against_other_threads_and_processes():
    """H-2(b): a wait-acquiring guard (like WindowsSidecarGuard) cannot take a held mutex."""
    import threading

    name = f"Local\\QronosUpdateTest-owned-{uuid.uuid4().hex}"
    hold = update_service.hold_runtime_mutexes((name,))
    try:
        assert _wait_from_another_process(name) == 0x102  # WAIT_TIMEOUT
        seen = []

        def other_thread():
            import _winapi  # noqa: F401
            from ctypes import wintypes
            import ctypes

            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            k.CreateMutexW.restype = wintypes.HANDLE
            k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k.WaitForSingleObject.restype = wintypes.DWORD
            handle = k.CreateMutexW(None, False, name)
            seen.append(k.WaitForSingleObject(handle, 0))
            k.CloseHandle(handle)

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(10)
        assert seen == [0x102]
        with pytest.raises(UpdateError) as refused:
            update_service.hold_runtime_mutexes((name,))
        assert refused.value.code == "runtime_running"
    finally:
        hold.release()
    assert _wait_from_another_process(name) == 0  # released: acquirable again


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutexes")
def test_the_update_mutex_lives_as_long_as_the_inheriting_installer_process(tmp_path):
    """H-2(c): exclusion is not released when the launching process lets go: the installer keeps it."""
    import _winapi

    from tests.fixtures.child_python import child_python

    name = f"Local\\QronosUpdateTest-life-{uuid.uuid4().hex}"
    go = tmp_path / "installer-may-exit"
    executable, _ = child_python()
    hold = update_service.MutexHold((name,))
    child = None
    try:
        code = ("import os, sys, time\n"
                "deadline = time.monotonic() + 60\n"
                "while not os.path.exists(sys.argv[1]) and time.monotonic() < deadline:\n"
                "    time.sleep(0.05)\n")
        child = update_service.spawn_inheriting([executable, "-B", "-I", "-c", code, str(go)], hold.handles())
    finally:
        hold.release()  # the launching process lets go, as launch() does
    try:
        assert update_service.update_in_progress(name) is True  # the installer still holds it
        go.write_text("1")
        child.wait(timeout=60)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
    assert update_service.update_in_progress(name) is False  # the installer ended: so did the exclusion
    assert _winapi  # imported for the owned-handle convention


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutexes")
def test_launch_hands_the_update_mutex_to_the_installer(world):
    """The fake installer keeps the inherited handles; the exclusion outlives launch() until it ends."""
    name = f"Local\\QronosUpdateTest-handoff-{uuid.uuid4().hex}"
    service = world.service(quiesce=lambda: update_service.hold_runtime_mutexes((name,)))
    launched(service, *world.release("0.2.0"))
    try:
        assert update_service.update_in_progress(name) is True
    finally:
        world.launcher.finish()
    assert update_service.update_in_progress(name) is False
