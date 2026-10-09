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
from tests.fixtures.owned_junction import owned_junction

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
    real = tmp_path / "real"
    real.mkdir()
    with owned_junction(real, tmp_path / "linked") as linked:
        with pytest.raises(SigningError, match="link or junction"):
            release_signing.create_key(linked / "owner.key", "Owner")
    assert list(real.iterdir()) == []
    assert not os.path.lexists(tmp_path / "linked")  # F4: the fixture's junction is gone, its target kept


def test_the_junction_fixture_removes_only_its_own_junction(tmp_path):
    """F4: cleanup removes the link alone, and refuses anything that is no longer its junction."""
    real = tmp_path / "real"
    (real / "kept").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="test body failed"):
        with owned_junction(real, tmp_path / "linked"):
            raise RuntimeError("test body failed")  # cleanup still runs
    assert not os.path.lexists(tmp_path / "linked") and (real / "kept").is_dir()

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(AssertionError, match="no longer this fixture's junction"):
        with owned_junction(real, tmp_path / "swapped"):
            os.rmdir(tmp_path / "swapped")  # the link is replaced by a plain folder holding data
            (tmp_path / "swapped").mkdir()
            (tmp_path / "swapped" / "data").write_text("not the fixture's", encoding="utf-8")
    assert (tmp_path / "swapped" / "data").is_file() and (real / "kept").is_dir()
    with pytest.raises(AssertionError, match="already exists"):
        with owned_junction(real, other):
            pass
    assert other.is_dir()


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


@pytest.mark.parametrize("certificate", ["not-a-thumbprint", "0" * 40, "A1B2" * 10])
def test_no_thumbprint_or_signtool_opens_the_authenticode_gate(tmp_path, certificate):
    """S-1: readiness needs a reviewed sign-and-verify of a built artefact; none exists, so it stays closed."""
    tool = tmp_path / "signtool.exe"
    report = release_signing.preflight(signtool=lambda: tool, certificate=certificate)
    authenticode = next(g for g in report["gates"] if g["gate"] == "authenticode")
    assert authenticode["open"] is False
    assert "not used as evidence" in authenticode["detail"]
    assert "self-signed certificate is never a substitute" in authenticode["detail"]


def test_signtool_is_never_taken_from_path():
    source = Path(release_signing.__file__).read_text(encoding="utf-8")
    assert "shutil.which" not in source and "Windows Kits" in source


def test_the_owner_key_gate_does_not_claim_custody_was_checked(tmp_path):
    root = product(tmp_path / "root")
    (root / "config").mkdir()
    public = ed25519.public_key(os.urandom(32))
    entry = {"keyId": local_update.key_id(public), "publicKey": base64.b64encode(public).decode(), "owner": "O",
             "revoked": False}
    (root / "config" / "update_trusted_keys.json").write_text(json.dumps({"schema": 1, "keys": [entry]}),
                                                              encoding="utf-8")
    gate = next(g for g in release_signing.preflight(root, signtool=lambda: None)["gates"]
                if g["gate"] == "owner_update_key")
    assert gate["open"] is True and "custody of the private key file is not checked" in gate["detail"]


def test_a_failed_cleanup_after_create_is_reported_with_the_path(tmp_path, monkeypatch):
    """S-2: if the half-written key file cannot be removed, the error says so and where."""
    win = release_signing._Win32.get()

    def broken_check(handle, sid):
        raise SigningError("fixture: access check failed")

    monkeypatch.setattr(release_signing, "_check_access_after_create", broken_check)
    monkeypatch.setattr(win, "SetFileInformationByHandle", lambda *args: 0)  # deletion refused
    key_file = tmp_path / "owner.key"
    with pytest.raises(SigningError) as refused:
        release_signing.create_key(key_file, "Fixture Owner", random=fixed_random(os.urandom(32)))
    try:
        assert "could not be removed" in str(refused.value) and str(key_file) in str(refused.value)
        assert isinstance(refused.value.__context__, SigningError)  # the original failure is kept
    finally:
        if key_file.exists():
            os.remove(key_file)  # the synthetic fixture file


def test_a_key_file_reached_through_another_final_path_is_not_used(owner, monkeypatch):
    """S-3: the handle that is read must be the intended file, not one an ancestor swap redirected to."""
    key_file, _, _ = owner
    monkeypatch.setattr(release_signing, "_final_path", lambda handle: "C:\\somewhere\\else\\owner.key")
    with pytest.raises(SigningError, match="not at the intended path"):
        release_signing.public_entry(key_file)


def test_create_and_read_check_the_real_final_path(owner):
    key_file, _, entry = owner
    assert release_signing.public_entry(key_file) == entry  # the real GetFinalPathNameByHandleW agrees


def _rewrite_public_key(key_file: Path, public: bytes) -> None:
    """A same-account edit of the key file's public fields (the owner's account may write its own file)."""
    record = json.loads(key_file.read_text(encoding="utf-8"))
    record["publicKey"] = base64.b64encode(public).decode()
    record["keyId"] = local_update.key_id(public)
    with open(key_file, "w", encoding="utf-8") as stream:  # keeps the protected access list
        stream.write(json.dumps(record, indent=1) + "\n")


def test_public_entry_refuses_a_foreign_public_key_in_the_key_file(owner, capsys):
    """F1: the printed entry must be the key this file can actually sign with, proved by unlocking it."""
    key_file, _, entry = owner
    foreign = ed25519.public_key(os.urandom(32))
    _rewrite_public_key(key_file, foreign)
    with pytest.raises(SigningError, match="does not unlock to its own public key"):
        release_signing.public_entry(key_file)
    assert release_signing.main(["public-entry", "--key-file", str(key_file)]) == 1
    printed = capsys.readouterr().out
    assert json.loads(printed)["ok"] is False and "trustedKeyEntry" not in printed
    assert base64.b64encode(foreign).decode() not in printed and local_update.key_id(foreign) not in printed


def test_public_entry_refuses_a_weak_public_key_even_with_a_matching_id(owner):
    key_file, _, _ = owner
    _rewrite_public_key(key_file, bytes(32))  # small-order point, a weak key
    with pytest.raises(SigningError, match="not an acceptable Ed25519 key"):
        release_signing.public_entry(key_file)


def test_public_entry_wipes_the_unlocked_secret(owner, monkeypatch):
    key_file, secret, entry = owner
    unlocked = []
    real = release_signing._unlock

    def recording(record):
        value = real(record)
        assert value == bytearray(secret)  # the real DPAPI unlock on this account
        unlocked.append(value)
        return value

    monkeypatch.setattr(release_signing, "_unlock", recording)
    assert release_signing.public_entry(key_file) == entry
    assert unlocked and unlocked[0] == bytearray(32)  # zeroed before the entry was returned


def test_public_entry_refuses_a_key_file_another_account_protected(owner, monkeypatch):
    """A protected blob that does not unlock here (DPAPI refuses) prints no entry."""
    key_file, _, _ = owner

    def refused(data, protect):
        raise SigningError("Windows could not unlock the owner key (DPAPI).")

    monkeypatch.setattr(release_signing, "_dpapi", refused)
    with pytest.raises(SigningError, match="DPAPI"):
        release_signing.public_entry(key_file)
