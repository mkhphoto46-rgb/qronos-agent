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
