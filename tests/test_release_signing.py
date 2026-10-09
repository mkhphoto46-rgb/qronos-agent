"""Owner key and signed manifest tooling (build_support/release_signing.py).

Safety of these fixtures: every key is synthetic and lives only under tmp_path
(outside every Git checkout). DPAPI protects synthetic bytes for the current
user. One test loosens a tmp key file's access list with the system icacls
(fixed System32 path) to prove it is then refused. No certificate store, trust
store, registry or network is touched; the preflight reads the repository's own
config.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

ICACLS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"

import pytest

from build_support import release_signing
from build_support.release_signing import SigningError
from core import ed25519, local_update, update_stores
from core.local_update import InstalledState

pytestmark = pytest.mark.skipif(os.name != "nt", reason="DPAPI and Windows access lists")


def fixed_random(secret: bytes):
    return lambda count: secret[:count]


def product(root: Path, version: str = "0.2.0", package: str | None = None, cargo: str | None = None) -> Path:
    (root / "desktop" / "src-tauri").mkdir(parents=True)
    (root / "desktop" / "src-tauri" / "tauri.conf.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (root / "desktop" / "package.json").write_text(json.dumps({"version": package or version}), encoding="utf-8")
    (root / "desktop" / "src-tauri" / "Cargo.toml").write_text(f'[package]\nversion = "{cargo or version}"\n',
                                                               encoding="utf-8")
    return root


@pytest.fixture
def owner(tmp_path):
    secret = os.urandom(32)
    folder = tmp_path / "owner-keys"
    folder.mkdir()
    key_file = folder / "qronos-owner.key"
    entry = release_signing.create_key(key_file, "Fixture Owner", random=fixed_random(secret))
    return key_file, secret, entry


def test_a_new_key_file_keeps_the_secret_protected_and_private(owner):
    key_file, secret, entry = owner
    raw = key_file.read_bytes()
    for form in (secret, secret.hex().encode(), base64.b64encode(secret)):
        assert form not in raw
    record = json.loads(raw)
    assert set(entry) == {"keyId", "publicKey", "owner", "revoked"}
    assert entry["publicKey"] == record["publicKey"] == base64.b64encode(ed25519.public_key(secret)).decode()
    release_signing.check_private_access(key_file)  # owner-only, protected access list
    assert release_signing._unlock(record) == bytearray(secret)


def test_the_printed_entry_is_accepted_by_the_trusted_key_loader(owner, tmp_path):
    _, _, entry = owner
    trust = tmp_path / "trusted.json"
    trust.write_text(json.dumps({"schema": 1, "keys": [entry]}), encoding="utf-8")
    keys = local_update.load_trusted_keys(trust)
    assert list(keys) == [entry["keyId"]]


def test_a_key_file_is_never_overwritten(owner):
    key_file, _, _ = owner
    before = key_file.read_bytes()
    with pytest.raises(SigningError, match="never overwritten"):
        release_signing.create_key(key_file, "Someone Else")
    assert key_file.read_bytes() == before


def test_a_key_inside_a_git_checkout_is_refused(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "keys").mkdir()
    with pytest.raises(SigningError, match="outside every Git checkout"):
        release_signing.create_key(checkout / "keys" / "owner.key", "Owner")
    assert not (checkout / "keys" / "owner.key").exists()


def test_a_key_below_a_junction_is_refused(tmp_path):
    winapi = pytest.importorskip("_winapi")
    real = tmp_path / "real"
    real.mkdir()
    winapi.CreateJunction(str(real), str(tmp_path / "linked"))
    with pytest.raises(SigningError, match="link or junction"):
        release_signing.create_key(tmp_path / "linked" / "owner.key", "Owner")
    assert list(real.iterdir()) == []


def test_a_key_whose_access_was_loosened_is_not_used(owner, tmp_path):
    key_file, _, _ = owner
    subprocess.run([str(ICACLS), str(key_file), "/grant", "*S-1-1-0:(R)"], check=True, capture_output=True)
    with pytest.raises(SigningError, match="accessible only to your Windows account"):
        release_signing.public_entry(key_file)


def test_a_hard_linked_key_file_is_not_used(owner, tmp_path):
    key_file, _, _ = owner
    os.link(key_file, tmp_path / "second-name.key")
    with pytest.raises(SigningError, match="another hard link"):
        release_signing.public_entry(key_file)


def test_a_failed_access_check_after_creation_leaves_no_key_file(tmp_path, monkeypatch):
    def broken(handle, sid):
        raise SigningError("fixture: access could not be confirmed")

    monkeypatch.setattr(release_signing, "_check_access_after_create", broken)
    with pytest.raises(SigningError, match="fixture"):
        release_signing.create_key(tmp_path / "owner.key", "Owner")
    assert not (tmp_path / "owner.key").exists()


def test_the_key_file_is_private_from_the_moment_it_exists(tmp_path, monkeypatch):
    """The access list is checked through the creating handle, before anything else can open the file."""
    seen = []

    def inspect(handle, sid):
        seen.append(release_signing._handle_sddl(handle))
        release_signing._check_handle(handle, sid)

    monkeypatch.setattr(release_signing, "_check_access_after_create", inspect)
    release_signing.create_key(tmp_path / "owner.key", "Owner")
    sid = release_signing.current_user_sid()
    assert seen[0].startswith(f"O:{sid}") and f"D:P(A;;FA;;;{sid})" in seen[0]


def test_no_external_program_is_a_trust_root():
    source = (Path(release_signing.__file__)).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert "subprocess" not in code and "whoami" not in code and "icacls" not in code


def test_signing_writes_a_manifest_qronos_accepts(owner, tmp_path):
    key_file, _, entry = owner
    root = product(tmp_path / "root")
    installer = tmp_path / "out" / "qronos-desktop_0.2.0_x64-setup.exe"
    installer.parent.mkdir()
    installer.write_bytes(b"MZ fixture release")

    manifest = release_signing.sign(key_file, installer, security_floor="0.1.0", notes="ملاحظات", root=root)

    keys = {entry["keyId"]: local_update.TrustedKey(entry["keyId"], base64.b64decode(entry["publicKey"]),
                                                    entry["owner"], False)}
    update = local_update.verify_update(manifest.read_bytes(), installer, keys,
                                        InstalledState("0.1.0", update_stores.current_profile()))
    assert update.kind == "upgrade" and update.package_sha256 == hashlib.sha256(b"MZ fixture release").hexdigest()
    assert update.payload["dataStores"]["settings"] == {"writes": 2, "reads": [1, 2]}
    with pytest.raises(SigningError, match="already exists"):
        release_signing.sign(key_file, installer, security_floor="0.1.0", root=root)


@pytest.mark.parametrize(("versions", "name", "message"), [
    ({"version": "0.2.0"}, "qronos-desktop_0.3.0_x64-setup.exe", "must be qronos-desktop_0.2.0"),
    ({"version": "0.2.0", "package": "0.2.1"}, "qronos-desktop_0.2.0_x64-setup.exe", "not consistent"),
    ({"version": "0.2.0", "cargo": "0.1.9"}, "qronos-desktop_0.2.0_x64-setup.exe", "not consistent"),
])
def test_the_signed_version_comes_from_the_product_version_authority(owner, tmp_path, versions, name, message):
    key_file, _, _ = owner
    root = product(tmp_path / "root", **versions)
    installer = tmp_path / name
    installer.write_bytes(b"MZ")
    with pytest.raises(SigningError, match=message):
        release_signing.sign(key_file, installer, security_floor="0.1.0", root=root)
    assert not installer.with_name(name + local_update.MANIFEST_SUFFIX).exists()


def test_the_cli_never_prints_the_private_key(tmp_path, capsys, monkeypatch):
    secret = os.urandom(32)
    monkeypatch.setattr(release_signing.os, "urandom", fixed_random(secret))
    folder = tmp_path / "keys"
    folder.mkdir()
    assert release_signing.main(["create-key", "--key-file", str(folder / "o.key"), "--owner", "Owner"]) == 0
    assert release_signing.main(["public-entry", "--key-file", str(folder / "o.key")]) == 0
    printed = capsys.readouterr().out
    for form in (secret.hex(), base64.b64encode(secret).decode(), secret.hex().upper()):
        assert form not in printed
    assert json.loads(printed.split("\n}\n")[0] + "\n}")["trustedKeyEntry"]["owner"] == "Owner"


def test_preflight_reports_the_closed_gates_with_actions():
    report = release_signing.preflight(signtool=lambda: None)
    gates = {gate["gate"]: gate for gate in report["gates"]}
    assert report["ok"] is False
    assert gates["version_authority"]["open"] is True
    assert gates["owner_update_key"]["open"] is False and "create-key" in gates["owner_update_key"]["detail"]
    assert gates["authenticode"]["open"] is False and "certificate authority" in gates["authenticode"]["detail"]


def test_preflight_needs_a_real_thumbprint_not_just_signtool(tmp_path):
    tool = tmp_path / "signtool.exe"
    report = release_signing.preflight(signtool=lambda: tool, certificate="not-a-thumbprint")
    authenticode = next(g for g in report["gates"] if g["gate"] == "authenticode")
    assert authenticode["open"] is False and "self-signed certificate is not a substitute" in authenticode["detail"]
