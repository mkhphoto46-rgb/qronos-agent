"""Authenticated local update packages (core.local_update, core.ed25519).

Signing keys exist only inside these tests: each fixture generates a fresh random
Ed25519 key in memory (never written to the repository). The committed trusted-key
file holds no key, and the tests prove that this refuses every package.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from core import ed25519
from core import local_update
from core.local_update import InstalledState, LocalUpdateStore, UpdateError

# ------------------------------------------------------------- fixtures


def new_key() -> bytes:
    return os.urandom(32)


def trusted(*secrets: bytes, revoked: bytes | None = None) -> dict[str, local_update.TrustedKey]:
    keys = {}
    for secret in secrets + ((revoked,) if revoked else ()):
        public = ed25519.public_key(secret)
        identifier = local_update.key_id(public)
        keys[identifier] = local_update.TrustedKey(identifier, public, "fixture owner", secret is revoked)
    return keys


def package(tmp_path: Path, version: str = "0.2.0", data: bytes | None = None) -> Path:
    path = tmp_path / "chosen" / f"qronos-desktop_{version}_x64-setup.exe"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data if data is not None else b"MZ installer " + version.encode())
    return path


def payload_for(installer: Path, **changes) -> dict:
    data = installer.read_bytes()
    payload = {
        "schema": 1, "product": "com.qronos.agent", "version": installer.name.split("_")[1],
        "architecture": "x64", "target": "x86_64-pc-windows-msvc",
        "package": {"fileName": installer.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        "dataSchema": {"writes": 3, "reads": [2, 3]}, "minimumVersion": None, "securityFloor": "0.1.0",
        "rollback": False, "notes": "fixture",
    }
    payload.update(changes)
    return payload


INSTALLED = InstalledState("0.1.0", 3)


def verify(envelope: bytes, installer: Path, keys, installed=INSTALLED):
    return local_update.verify_update(envelope, installer, keys, installed)


# ------------------------------------------------------- verification


def test_an_owner_signed_upgrade_is_verified(tmp_path):
    owner = new_key()
    installer = package(tmp_path)
    envelope = local_update.build_envelope(payload_for(installer), [owner])

    update = verify(envelope, installer, trusted(owner))

    assert (update.kind, update.version) == ("upgrade", "0.2.0")
    assert update.package_sha256 == hashlib.sha256(installer.read_bytes()).hexdigest()
    assert update.key_id == local_update.key_id(ed25519.public_key(owner))


def test_the_committed_key_file_holds_no_key_and_refuses_everything(tmp_path):
    keys = local_update.load_trusted_keys()  # config/update_trusted_keys.json
    installer = package(tmp_path)
    envelope = local_update.build_envelope(payload_for(installer), [new_key()])

    assert keys == {}
    with pytest.raises(UpdateError) as refused:
        verify(envelope, installer, keys)
    assert refused.value.code == "no_trusted_key"


def test_a_missing_key_file_means_no_key(tmp_path):
    assert local_update.load_trusted_keys(tmp_path / "absent.json") == {}


@pytest.mark.parametrize(("how", "code"), [
    ("unknown key", "untrusted_signature"),
    ("revoked key", "revoked_key"),
    ("payload edited after signing", "untrusted_signature"),
    ("signature from another payload", "untrusted_signature"),
])
def test_untrusted_or_tampered_signatures_are_refused(tmp_path, how, code):
    owner, other = new_key(), new_key()
    installer = package(tmp_path)
    payload = payload_for(installer)
    keys = trusted(owner)
    if how == "unknown key":
        envelope = local_update.build_envelope(payload, [other])
    elif how == "revoked key":
        envelope = local_update.build_envelope(payload, [other])
        keys = trusted(owner, revoked=other)
    else:
        envelope = json.loads(local_update.build_envelope(payload, [owner]))
        if how == "payload edited after signing":
            edited = json.loads(base64.b64decode(envelope["payload"]))
            edited["securityFloor"] = "0.0.1"
            envelope["payload"] = base64.b64encode(json.dumps(edited).encode()).decode()
        else:
            envelope["signatures"] = json.loads(local_update.build_envelope(
                payload_for(installer, notes="other"), [owner]))["signatures"]
        envelope = json.dumps(envelope).encode()

    with pytest.raises(UpdateError) as refused:
        verify(envelope, installer, keys)
    assert refused.value.code == code


def test_one_trusted_signature_among_unknown_ones_is_enough(tmp_path):
    owner = new_key()
    installer = package(tmp_path)
    envelope = local_update.build_envelope(payload_for(installer), [new_key(), owner])

    assert verify(envelope, installer, trusted(owner)).kind == "upgrade"


@pytest.mark.parametrize(("changes", "code"), [
    ({"product": "com.other.app"}, "wrong_product"),
    ({"architecture": "arm64"}, "wrong_platform"),
    ({"target": "aarch64-pc-windows-msvc"}, "wrong_platform"),
    ({"version": "0.2"}, "invalid_version"),
    ({"extra": True}, "malformed"),
    ({"rollback": "yes"}, "malformed"),
    ({"dataSchema": {"writes": 3, "reads": [4, 5]}}, "malformed"),
    ({"package": {"fileName": "..\\evil.exe", "bytes": 1, "sha256": "0" * 64}}, "malformed"),
])
def test_payloads_that_do_not_describe_this_product_are_refused(tmp_path, changes, code):
    owner = new_key()
    installer = package(tmp_path)
    envelope = local_update.build_envelope(payload_for(installer, **changes), [owner])

    with pytest.raises(UpdateError) as refused:
        verify(envelope, installer, trusted(owner))
    assert refused.value.code == code


def test_the_chosen_installer_must_be_the_signed_one(tmp_path):
    owner = new_key()
    installer = package(tmp_path)
    envelope = local_update.build_envelope(payload_for(installer), [owner])
    installer.write_bytes(installer.read_bytes()[:-1] + b"X")  # same size, other bytes

    with pytest.raises(UpdateError) as refused:
        verify(envelope, installer, trusted(owner))
    assert refused.value.code == "package_mismatch"


@pytest.mark.parametrize("chosen", ["folder", "missing", "junction"])
def test_something_other_than_a_regular_file_is_refused(tmp_path, chosen):
    owner = new_key()
    installer = package(tmp_path)
    envelope = local_update.build_envelope(payload_for(installer), [owner])
    target = {"folder": installer.parent, "missing": tmp_path / "absent.exe", "junction": tmp_path / "linked"}[chosen]
    if chosen == "junction":
        pytest.importorskip("_winapi").CreateJunction(str(installer.parent), str(target))

    with pytest.raises(UpdateError) as refused:
        verify(envelope, target, trusted(owner))
    assert refused.value.code == "not_a_file"


# ------------------------------------------------------------ version policy


@pytest.mark.parametrize(("installed", "changes", "kind_or_code"), [
    (InstalledState("0.1.0", 3), {}, "upgrade"),
    (InstalledState("0.2.0", 3), {}, "repair"),
    (InstalledState("0.3.0", 3), {}, "unsigned_rollback"),
    (InstalledState("0.3.0", 3), {"rollback": True}, "rollback"),
    (InstalledState("0.3.0", 3, security_floor="0.2.5"), {"rollback": True}, "below_security_floor"),
    (InstalledState("0.1.0", 3), {"securityFloor": "0.3.0"}, "below_security_floor"),
    (InstalledState("0.1.0", 3), {"minimumVersion": "0.1.5"}, "upgrade_path"),
    (InstalledState("0.1.0", 4), {}, "data_schema"),                       # cannot read newer data
    (InstalledState("0.1.0", 1), {}, "data_schema"),                       # too old to read
    (InstalledState("0.3.0", 4), {"rollback": True}, "data_schema"),        # rollback would lose data
    (InstalledState("0.1.0", 3), {"dataSchema": {"writes": 2, "reads": [2, 3]}}, "data_schema"),
    (InstalledState("0.2.0-rc.1", 3), {}, "upgrade"),                      # 0.2.0 > 0.2.0-rc.1
    (InstalledState("0.2.0+build.7", 3), {}, "repair"),                    # build metadata ignored
])
def test_version_and_data_schema_policy(tmp_path, installed, changes, kind_or_code):
    owner = new_key()
    installer = package(tmp_path)
    envelope = local_update.build_envelope(payload_for(installer, **changes), [owner])

    if kind_or_code in ("upgrade", "repair", "rollback"):
        assert verify(envelope, installer, trusted(owner), installed).kind == kind_or_code
    else:
        with pytest.raises(UpdateError) as refused:
            verify(envelope, installer, trusted(owner), installed)
        assert refused.value.code == kind_or_code


@pytest.mark.parametrize(("left", "right", "order"), [
    ("1.0.0", "1.0.0", 0), ("1.0.0-alpha", "1.0.0", -1), ("1.0.0-alpha", "1.0.0-alpha.1", -1),
    ("1.0.0-alpha.1", "1.0.0-alpha.beta", -1), ("1.0.0-beta.2", "1.0.0-beta.11", -1),
    ("1.0.0-rc.1", "1.0.0", -1), ("2.0.0", "10.0.0", -1), ("1.0.0+a", "1.0.0+b", 0),
])
def test_semantic_version_precedence(left, right, order):
    assert local_update.compare_versions(left, right) == order
    assert local_update.compare_versions(right, left) == -order


# ------------------------------------------------------------ key file


def write_keys(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"schema": 1, "keys": entries}), encoding="utf-8")
    return path


def key_entry(secret: bytes, **changes) -> dict:
    public = ed25519.public_key(secret)
    entry = {"keyId": local_update.key_id(public), "publicKey": base64.b64encode(public).decode(),
             "owner": "fixture owner", "revoked": False}
    entry.update(changes)
    return entry


def test_owner_keys_load_from_the_key_file(tmp_path):
    secret = new_key()
    keys = local_update.load_trusted_keys(write_keys(tmp_path, [key_entry(secret)]))

    assert list(keys) == [local_update.key_id(ed25519.public_key(secret))]


@pytest.mark.parametrize("changes", [{"keyId": "0" * 32}, {"publicKey": "not base64!"}, {"revoked": "no"},
                                     {"owner": ""}, {"extra": 1},
                                     {"publicKey": base64.b64encode((2).to_bytes(32, "little")).decode(),
                                      "keyId": local_update.key_id((2).to_bytes(32, "little"))}])
def test_a_malformed_key_entry_is_refused(tmp_path, changes):
    with pytest.raises(UpdateError) as refused:
        local_update.load_trusted_keys(write_keys(tmp_path, [key_entry(new_key(), **changes)]))
    assert refused.value.code == "key_file"


def test_a_duplicated_key_is_refused(tmp_path):
    secret = new_key()
    with pytest.raises(UpdateError, match="listed twice"):
        local_update.load_trusted_keys(write_keys(tmp_path, [key_entry(secret), key_entry(secret)]))


# ------------------------------------------------------------ the store


def stage_fixture(tmp_path: Path, version: str = "0.2.0", **changes):
    owner = new_key()
    installer = package(tmp_path, version)
    manifest = installer.with_name(installer.name + local_update.MANIFEST_SUFFIX)
    manifest.write_bytes(local_update.build_envelope(payload_for(installer, **changes), [owner]))
    return LocalUpdateStore(tmp_path / "data" / "Updates"), manifest, installer, trusted(owner)


def test_staging_keeps_a_verified_copy_and_records_the_phase(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path)

    update = store.stage(manifest, installer, keys, "0.1.0", 3)

    state = store.state()
    assert state["phase"] == "staged" and state["staged"]["version"] == "0.2.0"
    copy = store.staged_dir / f"{update.package_sha256}.exe"
    assert copy.read_bytes() == installer.read_bytes()
    assert sorted(path.name for path in store.staged_dir.iterdir()) == [
        f"{update.package_sha256}.exe", f"{update.package_sha256}.json"]


def test_a_refused_package_changes_nothing(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path)

    with pytest.raises(UpdateError):
        store.stage(manifest, installer, {}, "0.1.0", 3)
    assert not store.root.exists()


def test_handoff_reverifies_the_staged_copy(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    update = store.stage(manifest, installer, keys, "0.1.0", 3)

    assert store.handoff(keys, "0.1.0", 3) == store.staged_dir / f"{update.package_sha256}.exe"
    assert store.state()["phase"] == "handed_off"

    (store.staged_dir / f"{update.package_sha256}.exe").write_bytes(b"MZ swapped after staging")
    with pytest.raises(UpdateError) as refused:
        store.handoff(keys, "0.1.0", 3)
    assert refused.value.code == "package_mismatch"


def test_handoff_rechecks_trust_at_that_moment(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    store.stage(manifest, installer, keys, "0.1.0", 3)

    with pytest.raises(UpdateError) as refused:
        store.handoff({}, "0.1.0", 3)  # the owner key was removed in the meantime
    assert refused.value.code == "no_trusted_key"


def test_confirm_records_the_update_and_raises_the_security_floor(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path, securityFloor="0.1.5")
    store.stage(manifest, installer, keys, "0.1.0", 3)
    store.handoff(keys, "0.1.0", 3)

    assert store.confirm("0.1.0") == "not applied"  # the installer did not run (yet)
    assert store.confirm("0.2.0") == "upgrade"

    state = store.state()
    assert state["phase"] == "idle" and state["securityFloor"] == "0.1.5" and state["staged"] is None
    assert state["history"][-1] == {"kind": "upgrade", "fromVersion": "0.1.0", "version": "0.2.0",
                                    "packageSha256": state["previous"]["packageSha256"],
                                    "keyId": state["previous"]["keyId"]}
    assert store.installed("0.2.0", 3).security_floor == "0.1.5"


def test_the_recorded_floor_blocks_a_later_rollback(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path, version="0.3.0", securityFloor="0.2.5")
    store.stage(manifest, installer, keys, "0.2.0", 3)
    store.confirm("0.3.0")
    old = package(tmp_path, "0.2.0")
    old_manifest = old.with_name(old.name + local_update.MANIFEST_SUFFIX)
    owner = new_key()
    old_manifest.write_bytes(local_update.build_envelope(payload_for(old, rollback=True, securityFloor="0.1.0"),
                                                         [owner]))

    with pytest.raises(UpdateError) as refused:
        store.stage(old_manifest, old, trusted(owner), "0.3.0", 3)
    assert refused.value.code == "below_security_floor"


def test_discard_removes_only_the_store_s_own_files(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    store.stage(manifest, installer, keys, "0.1.0", 3)
    user_file = store.root.parent / "settings.json"
    user_file.write_text("{}", encoding="utf-8")
    (store.staged_dir / "notes.txt").write_text("not the store's", encoding="utf-8")

    store.discard()

    assert sorted(path.name for path in store.staged_dir.iterdir()) == ["notes.txt"]
    assert user_file.read_text(encoding="utf-8") == "{}"
    assert installer.exists() and manifest.exists()  # the person's own copies are untouched


def test_a_state_file_naming_foreign_files_is_refused(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    store.stage(manifest, installer, keys, "0.1.0", 3)
    state = json.loads(store.state_path.read_text(encoding="utf-8"))
    state["staged"]["packageFile"] = "..\\..\\settings.json"
    store.state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(UpdateError, match="did not create"):
        store.discard()


def test_a_junctioned_update_folder_is_refused(tmp_path):
    winapi = pytest.importorskip("_winapi")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "data").mkdir()
    winapi.CreateJunction(str(elsewhere), str(tmp_path / "data" / "Updates"))
    store, manifest, installer, keys = stage_fixture(tmp_path)

    with pytest.raises(UpdateError) as refused:
        store.stage(manifest, installer, keys, "0.1.0", 3)
    assert refused.value.code == "store"
    assert list(elsewhere.iterdir()) == []


def test_the_module_has_no_network_path():
    source = Path(local_update.__file__).read_text(encoding="utf-8") + Path(ed25519.__file__).read_text(
        encoding="utf-8")
    for name in ("socket", "urllib", "http.client", "requests", "httpx", "aiohttp", "websocket"):
        assert f"import {name}" not in source and f"from {name}" not in source


# ------------------------------------------------- weak trusted keys (I6)

IDENTITY = bytes([1]) + bytes(31)


def forged_envelope(payload: dict, public: bytes) -> bytes:
    """An 'unsigned' envelope: R = A (the identity), S = 0."""
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return json.dumps({"schema": 1, "payload": base64.b64encode(payload_bytes).decode(),
                       "signatures": [{"keyId": local_update.key_id(public),
                                       "signature": base64.b64encode(public + bytes(32)).decode()}]}).encode()


def test_the_identity_key_is_refused_by_the_key_file(tmp_path):
    entry = {"keyId": local_update.key_id(IDENTITY), "publicKey": base64.b64encode(IDENTITY).decode(),
             "owner": "attacker", "revoked": False}

    with pytest.raises(UpdateError) as refused:
        local_update.load_trusted_keys(write_keys(tmp_path, [entry]))
    assert refused.value.code == "key_file" and "weak" in str(refused.value)


def test_an_injected_weak_key_cannot_validate_an_unsigned_payload(tmp_path):
    installer = package(tmp_path)
    keys = {local_update.key_id(IDENTITY): local_update.TrustedKey(local_update.key_id(IDENTITY), IDENTITY,
                                                                   "attacker", False)}

    with pytest.raises(UpdateError) as refused:
        verify(forged_envelope(payload_for(installer), IDENTITY), installer, keys)
    assert refused.value.code == "weak_key"


# --------------------------------------------- store file ownership (I7)

import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

OUTSIDE = b"a file outside the update store that belongs to someone else"


def outside_file(tmp_path: Path) -> Path:
    path = tmp_path / "outside" / "precious.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(OUTSIDE)
    return path


def planted(store: LocalUpdateStore, name: str, target: Path) -> Path:
    """A hard link, planted in advance at a name inside the store, to a file outside it."""
    folder = store.root if name.startswith("update-state") else store.staged_dir
    folder.mkdir(parents=True, exist_ok=True)
    os.link(target, folder / name)
    return folder / name


@pytest.mark.parametrize("name", ["{sha}.part", "update-state.json.tmp"])
def test_hard_links_at_old_temporary_names_are_never_written_through(tmp_path, name):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    precious = outside_file(tmp_path)
    sha = hashlib.sha256(installer.read_bytes()).hexdigest()
    planted(store, name.format(sha=sha), precious)

    store.stage(manifest, installer, keys, "0.1.0", 3)

    assert precious.read_bytes() == OUTSIDE
    assert store.state()["phase"] == "staged"
    assert not list(store.root.rglob(".qronos-*.tmp"))  # no temporary left behind


@pytest.mark.parametrize("name", ["{sha}.json", "{sha}.exe", "update-state.json"])
def test_a_hard_linked_destination_is_refused_and_untouched(tmp_path, name):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    precious = outside_file(tmp_path)
    sha = hashlib.sha256(installer.read_bytes()).hexdigest()
    planted(store, name.format(sha=sha), precious)

    with pytest.raises(UpdateError) as refused:
        store.stage(manifest, installer, keys, "0.1.0", 3)

    assert refused.value.code == "store" and "hard link" in str(refused.value)
    assert precious.read_bytes() == OUTSIDE
    assert not list(store.root.rglob(".qronos-*.tmp"))


def test_handoff_refuses_a_staged_file_that_gained_a_hard_link(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    update = store.stage(manifest, installer, keys, "0.1.0", 3)
    os.link(store.staged_dir / f"{update.package_sha256}.exe", tmp_path / "second-name.exe")

    with pytest.raises(UpdateError, match="hard link"):
        store.handoff(keys, "0.1.0", 3)
    assert store.state()["phase"] == "staged"


def test_a_failed_copy_leaves_no_temporary_and_no_state(tmp_path, monkeypatch):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    real_verify = local_update.verify_update

    def verify_then_change(*args, **kwargs):
        update = real_verify(*args, **kwargs)
        installer.write_bytes(installer.read_bytes()[:-1] + b"Z")  # swapped after the check
        return update

    monkeypatch.setattr(local_update, "verify_update", verify_then_change)

    with pytest.raises(UpdateError) as refused:
        store.stage(manifest, installer, keys, "0.1.0", 3)
    assert refused.value.code == "package_mismatch"
    assert not list(store.root.rglob(".qronos-*.tmp")) and not list(store.staged_dir.glob("*.exe"))
    assert store.state()["phase"] == "idle"


def test_a_concurrent_floor_change_is_detected(tmp_path, monkeypatch):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    other = LocalUpdateStore(store.root)
    real_verify = local_update.verify_update

    def verify_while_another_process_confirms(*args, **kwargs):
        update = real_verify(*args, **kwargs)
        state = other.state()
        state["securityFloor"] = "0.1.9"
        with other._locked():
            other._save(state)
        return update

    monkeypatch.setattr(local_update, "verify_update", verify_while_another_process_confirms)

    with pytest.raises(UpdateError) as refused:
        store.stage(manifest, installer, keys, "0.1.0", 3)
    assert refused.value.code == "busy"


LOCK_HOLDER = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core.local_update import LocalUpdateStore
store = LocalUpdateStore(Path(sys.argv[2]))
with store._locked():
    Path(sys.argv[3]).write_text("held")
    deadline = time.time() + 30
    while not Path(sys.argv[4]).exists() and time.time() < deadline:
        time.sleep(0.05)
"""


def test_another_process_holding_the_lock_makes_a_change_refuse_as_busy(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    store.lock_timeout = 0.5
    ready, release = tmp_path / "ready", tmp_path / "release"
    source_root = Path(local_update.__file__).resolve().parent.parent
    holder = subprocess.Popen([sys.executable, "-B", "-c", LOCK_HOLDER, str(source_root), str(store.root),
                               str(ready), str(release)])
    try:
        deadline = time.time() + 30
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        with pytest.raises(UpdateError) as refused:
            store.stage(manifest, installer, keys, "0.1.0", 3)
        assert refused.value.code == "busy"
    finally:
        release.write_text("go")
        holder.wait(timeout=30)
    store.stage(manifest, installer, keys, "0.1.0", 3)  # once released, it proceeds
    assert store.state()["phase"] == "staged"


def _too_long_history(state: dict) -> None:
    state["history"] = state["history"] * 51


def full_state(tmp_path: Path) -> tuple[LocalUpdateStore, dict]:
    """A store whose state has a staged record, a previous record and one history entry."""
    store, manifest, installer, keys = stage_fixture(tmp_path)
    store.stage(manifest, installer, keys, "0.1.0", 3)
    store.confirm("0.2.0")
    store.stage(manifest, installer, keys, "0.2.0", 3)  # a repair: staged again
    state = json.loads(store.state_path.read_text(encoding="utf-8"))
    assert state["staged"] and state["previous"] and len(state["history"]) == 1
    return store, state


def _set(*path_and_value):
    *path, value = path_and_value

    def damage(state: dict) -> None:
        target = state
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return damage


# Wrong values, and above all wrong TYPES in every field: lists and dicts are unhashable
# and used to escape as TypeError from a frozenset membership test.
STATE_DAMAGE = {
    "phase without a staged record": _set("staged", None),
    "history not a list": _set("history", "many"),
    "history entry missing fields": _set("history", [{"kind": "upgrade"}]),
    "too long history": _too_long_history,
    "unknown field": _set("extra", True),
    "schema True": _set("schema", True),
    "schema 1.0": _set("schema", 1.0),
    "schema '1'": _set("schema", "1"),
    "schema 2": _set("schema", 2),
    "phase list": _set("phase", ["staged"]),
    "phase dict": _set("phase", {"staged": 1}),
    "phase unknown": _set("phase", "installing"),
    "floor number": _set("securityFloor", 7),
    "floor list": _set("securityFloor", ["0.2.0"]),
    "staged list": _set("staged", ["record"]),
    "staged kind list": _set("staged", "kind", ["upgrade"]),
    "staged kind dict": _set("staged", "kind", {"upgrade": True}),
    "staged kind unknown": _set("staged", "kind", "downgrade"),
    "staged version list": _set("staged", "version", ["0.2.0"]),
    "staged floor dict": _set("staged", "securityFloor", {}),
    "staged sha list": _set("staged", "packageSha256", ["0" * 64]),
    "staged key id number": _set("staged", "keyId", 7),
    "staged package file list": _set("staged", "packageFile", ["x.exe"]),
    "staged manifest file dict": _set("staged", "manifestFile", {}),
    "staged package name list": _set("staged", "packageName", []),
    "staged bytes True": _set("staged", "packageBytes", True),
    "staged bytes float": _set("staged", "packageBytes", 5.0),
    "staged bytes string": _set("staged", "packageBytes", "5"),
    "previous kind list": _set("previous", "kind", ["repair"]),
    "history kind dict": _set("history", 0, "kind", {"upgrade": 1}),
    "history from list": _set("history", 0, "fromVersion", ["0.1.0"]),
    "history entry list": _set("history", 0, ["upgrade"]),
}


@pytest.mark.parametrize("damage", STATE_DAMAGE.values(), ids=STATE_DAMAGE.keys())
def test_a_malformed_state_file_is_refused(tmp_path, damage):
    store, state = full_state(tmp_path)
    damage(state)
    store.state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(UpdateError) as refused:  # a TypeError here would fail the test
        store.state()
    assert refused.value.code == "store"


def test_the_full_state_used_for_damage_is_itself_accepted(tmp_path):
    store, state = full_state(tmp_path)
    store.state_path.write_text(json.dumps(state), encoding="utf-8")
    assert store.state() == state


def test_an_oversized_state_file_is_refused(tmp_path):
    store, *_ = stage_fixture(tmp_path)
    store.root.mkdir(parents=True)
    store.state_path.write_text(" " * (256 * 1024 + 1), encoding="utf-8")

    with pytest.raises(UpdateError, match="too large"):
        store.state()


def test_the_state_file_is_read_with_a_hard_bound_whatever_its_size(tmp_path, monkeypatch):
    """No stat-then-read: a file that grows after any size check is still read at most limit + 1 bytes."""
    store, *_ = stage_fixture(tmp_path)
    store.root.mkdir(parents=True)
    store.state_path.write_bytes(b" " * (4 * 1024 * 1024))
    requested = []
    real_fdopen = os.fdopen

    class Spy:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            requested.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(local_update.os, "fdopen", lambda *a, **k: Spy(real_fdopen(*a, **k)))
    with pytest.raises(UpdateError, match="too large"):
        store.state()
    assert requested == [256 * 1024 + 1]


def _swap_state_on_open(store: LocalUpdateStore, monkeypatch, times: int) -> list:
    """Replace the state file by a different file right before the store opens it."""
    real_open = os.open
    swaps = []

    def swapping_open(path, *args, **kwargs):
        if Path(path) == store.state_path and len(swaps) < times:
            replacement = store.root / f"replacement-{len(swaps)}"
            replacement.write_bytes(store.state_path.read_bytes())
            os.replace(replacement, store.state_path)
            swaps.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(local_update.os, "open", swapping_open)
    return swaps


def test_a_state_file_replaced_between_check_and_open_is_checked_again(tmp_path, monkeypatch):
    store, state = full_state(tmp_path)
    swaps = _swap_state_on_open(store, monkeypatch, times=1)

    assert store.state() == state  # an ordinary atomic save in between: the new file is checked and read
    assert len(swaps) == 1


def test_a_state_file_that_keeps_being_replaced_is_not_read(tmp_path, monkeypatch):
    store, _ = full_state(tmp_path)
    _swap_state_on_open(store, monkeypatch, times=99)

    with pytest.raises(UpdateError) as refused:
        store.state()
    assert refused.value.code == "busy"


def test_an_oversized_manifest_is_refused_by_a_bounded_read(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    manifest.write_bytes(manifest.read_bytes() + b" " * (64 * 1024))

    with pytest.raises(UpdateError, match="too large"):
        store.stage(manifest, installer, keys, "0.1.0", 3)
    assert not store.root.exists()


def test_history_is_capped(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    store.stage(manifest, installer, keys, "0.1.0", 3)
    store.confirm("0.2.0")
    state = json.loads(store.state_path.read_text(encoding="utf-8"))
    state["history"] = state["history"] * 50
    store.state_path.write_text(json.dumps(state), encoding="utf-8")
    store.stage(manifest, installer, keys, "0.2.0", 3)  # a repair of the same version

    assert store.confirm("0.2.0", replacement_evidence=True) == "repair"
    assert len(store.state()["history"]) == 50


def test_a_same_version_repair_is_never_confirmed_by_the_version_alone(tmp_path):
    """H-1: the same version running again does not prove the repair installer ran."""
    store, manifest, installer, keys = stage_fixture(tmp_path)
    store.stage(manifest, installer, keys, "0.1.0", 3)
    store.confirm("0.2.0")
    store.stage(manifest, installer, keys, "0.2.0", 3)  # a repair
    before = store.state()

    assert store.confirm("0.2.0") == "unconfirmed"
    assert store.state() == before  # still staged, no history entry, floor unchanged


def test_a_store_below_a_junctioned_ancestor_is_refused(tmp_path):
    winapi = pytest.importorskip("_winapi")
    real = tmp_path / "real-data"
    real.mkdir()
    winapi.CreateJunction(str(real), str(tmp_path / "data-link"))
    _, manifest, installer, keys = stage_fixture(tmp_path)
    store = LocalUpdateStore(tmp_path / "data-link" / "Updates")

    with pytest.raises(UpdateError, match="above the update store"):
        store.stage(manifest, installer, keys, "0.1.0", 3)
    assert not (real / "Updates").exists()


# ------------------------------------- concurrent readers and writers (I7-F1)
#
# On Windows a file that another handle has open without delete sharing cannot be
# replaced: os.replace raises PermissionError. Store readers take the store lock, so
# Qronos processes never collide; any other program holding a file open makes the
# change a typed UpdateError("busy") after a bounded retry, never a raw OSError.


def _temporaries(store: LocalUpdateStore) -> list[str]:
    return [path.name for folder in (store.root, store.staged_dir) if folder.exists()
            for path in folder.iterdir() if path.name.endswith(".tmp")]


def test_a_program_holding_the_state_file_open_makes_a_save_busy_not_an_oserror(tmp_path):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    store.stage(manifest, installer, keys, "0.1.0", 3)
    before = store.state_path.read_bytes()
    store.replace_timeout = 0.3

    with open(store.state_path, "rb"):  # a reader outside Qronos: no delete sharing
        with pytest.raises(UpdateError) as refused:
            store.discard()

    assert refused.value.code == "busy"
    assert store.state_path.read_bytes() == before  # nothing changed, nothing claimed
    assert store.state()["phase"] == "staged"
    assert _temporaries(store) == []
    store.discard()  # once the other program lets go, the same change succeeds
    assert store.state()["phase"] == "idle"


def test_a_short_lived_reader_is_waited_for(tmp_path):
    import threading
    store, manifest, installer, keys = stage_fixture(tmp_path)
    store.stage(manifest, installer, keys, "0.1.0", 3)
    reader = open(store.state_path, "rb")
    threading.Timer(0.3, reader.close).start()

    store.discard()  # retried until the reader closed, well within replace_timeout

    assert reader.closed and store.state()["phase"] == "idle"


@pytest.mark.parametrize(("failure", "code"), [
    (PermissionError(13, "in use"), "busy"),
    (OSError(28, "no space left"), "store"),
])
def test_a_failed_replace_is_a_typed_refusal_and_changes_nothing(tmp_path, monkeypatch, failure, code):
    store, manifest, installer, keys = stage_fixture(tmp_path)
    store.stage(manifest, installer, keys, "0.1.0", 3)
    before = store.state_path.read_bytes()
    store.replace_timeout = 0.2
    attempts = []

    def failing_replace(source, destination):
        attempts.append(destination)
        raise failure

    monkeypatch.setattr(local_update.os, "replace", failing_replace)
    with pytest.raises(UpdateError) as refused:
        store.discard()

    assert refused.value.code == code
    assert len(attempts) >= (2 if code == "busy" else 1) and (code == "busy" or len(attempts) == 1)
    assert store.state_path.read_bytes() == before
    assert _temporaries(store) == []


def test_a_failed_write_is_a_typed_refusal_and_leaves_no_temporary(tmp_path, monkeypatch):
    store, manifest, installer, keys = stage_fixture(tmp_path)

    def failing_fsync(descriptor):
        raise OSError(28, "no space left")

    monkeypatch.setattr(local_update.os, "fsync", failing_fsync)
    with pytest.raises(UpdateError) as refused:
        store.stage(manifest, installer, keys, "0.1.0", 3)

    assert refused.value.code == "store"
    assert _temporaries(store) == [] and not store.state_path.exists()


def test_reading_the_state_waits_for_the_store_lock(tmp_path):
    """state() takes the writers' lock, so it never holds the file open during a save."""
    store, manifest, installer, keys = stage_fixture(tmp_path)
    store.stage(manifest, installer, keys, "0.1.0", 3)
    store.lock_timeout = 0.5
    ready, release = tmp_path / "ready", tmp_path / "release"
    source_root = Path(local_update.__file__).resolve().parent.parent
    holder = subprocess.Popen([sys.executable, "-B", "-c", LOCK_HOLDER, str(source_root), str(store.root),
                               str(ready), str(release)])
    try:
        deadline = time.time() + 30
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        with pytest.raises(UpdateError) as refused:
            store.state()
        assert refused.value.code == "busy"
    finally:
        release.write_text("go")
        holder.wait(timeout=30)
    assert store.state()["phase"] == "staged"


def test_reading_a_store_that_does_not_exist_creates_nothing(tmp_path):
    store, *_ = stage_fixture(tmp_path)
    assert store.state()["phase"] == "idle"
    assert not store.root.exists()


STAGER = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core import local_update
public = bytes.fromhex(sys.argv[5])
identifier = local_update.key_id(public)
keys = {identifier: local_update.TrustedKey(identifier, public, "fixture owner", False)}
store = local_update.LocalUpdateStore(Path(sys.argv[2]))
go = Path(sys.argv[6])
while not go.exists():
    time.sleep(0.01)
for _ in range(int(sys.argv[7])):
    try:
        if sys.argv[8] == "read":
            store.state()
            print("read", flush=True)
            continue
        store.stage(Path(sys.argv[3]), Path(sys.argv[4]), keys, "0.1.0", 3)
        print("staged", flush=True)
    except local_update.UpdateError as error:
        print("UpdateError:" + error.code, flush=True)
    except BaseException as error:
        print("UNTYPED:" + type(error).__name__ + ":" + str(error).replace(chr(10), " "), flush=True)
"""


def test_concurrent_stagers_and_readers_get_typed_outcomes_and_leave_one_valid_state(tmp_path):
    """Six real processes stage the same package eight times each while three others read
    the state 150 times each, all at once. Before readers took the store lock, a reader
    holding the state file open made a stager's os.replace raise PermissionError."""
    store, manifest, installer, keys = stage_fixture(tmp_path)
    public = next(iter(keys.values())).public_key
    source_root = Path(local_update.__file__).resolve().parent.parent
    go = tmp_path / "go"

    def start(count: int, mode: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-B", "-c", STAGER, str(source_root), str(store.root), str(manifest),
                                 str(installer), public.hex(), str(go), str(count), mode],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    processes = [start(8, "stage") for _ in range(6)] + [start(150, "read") for _ in range(3)]
    go.write_text("go")
    results = []
    for process in processes:
        output, errors = process.communicate(timeout=300)
        assert process.returncode == 0, errors
        results += output.split()

    assert len(results) == 6 * 8 + 3 * 150
    assert set(results) <= {"staged", "read", "UpdateError:busy"}, \
        [r for r in results if r not in ("staged", "read")]
    assert "staged" in results
    state = store.state()
    sha = hashlib.sha256(installer.read_bytes()).hexdigest()
    assert state["phase"] == "staged" and state["staged"]["packageSha256"] == sha
    assert sorted(path.name for path in store.staged_dir.iterdir()) == [f"{sha}.exe", f"{sha}.json"]
    assert _temporaries(store) == []
