"""Authenticode build step (build_support/authenticode_release.py) and the preflight gate it opens.

Safety of these fixtures: signtool is NEVER run. Every sign/verify goes through a
synthetic runner that records the command and edits only a tmp_path file. Trust
answers and certificate facts are fakes, except:
- the certificate parser is exercised on small DER certificates built here in
  memory (self-signed or issued by an unknown issuer, with a junk signature) -
  read-only crypt32 calls, offline chain building, nothing added to any store;
- the real Windows SDK signtool.exe, when present, is only CHECKED (its path and
  its Microsoft signature, offline WinVerifyTrust), never executed.
No certificate store is written, no time-stamp or network call is made.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from build_support import authenticode_release as authenticode
from build_support import desktop_installer_verify, release_signing
from build_support.authenticode_release import CODE_SIGNING_EKU, AuthenticodeError
from build_support.desktop_prerequisites import PrerequisiteError
from tests.fixtures.owned_junction import owned_junction

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Authenticode tooling is Windows-only")

THUMB = "AB" * 20
MICROSOFT = {"subject": "Microsoft Corporation", "thumbprintSha1": "CD" * 20, "issuer": "Microsoft Code Signing PCA"}


def good_facts(**changes) -> dict:
    facts = {"sha1": THUMB, "subject": "CN=Owner Ltd", "issuer": "CN=Public CA", "notBefore": 1, "notAfter": 2,
             "timeValidity": 0, "selfIssued": False, "selfSigned": False, "enhancedKeyUsage": [CODE_SIGNING_EKU],
             "chain": [{"subject": "CN=Owner Ltd", "sha1": THUMB}, {"subject": "CN=Public Root", "sha1": "EF" * 20}],
             "chainPolicyError": 0, "rootStore": "local_machine"}
    facts.update(changes)
    return facts


# ------------------------------------------------------------- certificate rules


def test_a_public_code_signing_certificate_passes():
    assert authenticode.certificate_problems(good_facts(), THUMB) == []


@pytest.mark.parametrize(("changes", "problem"), [
    ({"sha1": "00" * 20}, "not the configured thumbprint"),
    ({"timeValidity": 1}, "not valid now"),
    ({"timeValidity": -1}, "not valid now"),
    ({"enhancedKeyUsage": []}, "Code Signing usage"),
    ({"enhancedKeyUsage": ["1.3.6.1.5.5.7.3.1"]}, "Code Signing usage"),
    ({"selfSigned": True}, "self-signed"),
    ({"selfIssued": True}, "self-signed"),
    ({"chain": [{"subject": "CN=Owner Ltd", "sha1": THUMB}]}, "no certificate chain"),
    ({"chainPolicyError": 0x800B0109}, "Authenticode chain policy"),
    ({"rootStore": "current_user_only"}, "only the current user added it"),
    ({"rootStore": "none"}, "not in the local machine's trusted root stores"),
])
def test_each_certificate_rule_refuses(changes, problem):
    problems = authenticode.certificate_problems(good_facts(**changes), THUMB)
    assert any(problem in item for item in problems), problems


# -------------------------------------------- real crypt32 on synthetic DER


def _der(tag: int, content: bytes) -> bytes:
    size = len(content)
    if size < 0x80:
        length = bytes([size])
    else:
        raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + content


def _oid(text: str) -> bytes:
    parts = [int(p) for p in text.split(".")]
    body = bytes([40 * parts[0] + parts[1]])
    for value in parts[2:]:
        chunk = [value & 0x7F]
        value >>= 7
        while value:
            chunk.insert(0, 0x80 | (value & 0x7F))
            value >>= 7
        body += bytes(chunk)
    return _der(0x06, body)


def _name(common: str) -> bytes:
    return _der(0x30, _der(0x31, _der(0x30, _oid("2.5.4.3") + _der(0x0C, common.encode()))))


def _utc(offset_days: float) -> bytes:
    return _der(0x17, time.strftime("%y%m%d%H%M%SZ", time.gmtime(time.time() + offset_days * 86400)).encode())


def synthetic_certificate(subject: str, issuer: str, *, days: tuple[float, float] = (-1, 30),
                          usage: str = CODE_SIGNING_EKU) -> bytes:
    """A structurally valid X.509 v3 certificate with a junk signature (no key exists for it anywhere)."""
    algorithm = _der(0x30, _oid("1.2.840.113549.1.1.11") + _der(0x05, b""))
    modulus = b"\x00\xc0" + os.urandom(255)
    key = _der(0x30, _der(0x30, _oid("1.2.840.113549.1.1.1") + _der(0x05, b""))
               + _der(0x03, b"\x00" + _der(0x30, _der(0x02, modulus) + _der(0x02, b"\x01\x00\x01"))))
    eku = _der(0x30, _oid("2.5.29.37") + _der(0x04, _der(0x30, _oid(usage))))
    tbs = _der(0x30, _der(0xA0, _der(0x02, b"\x02")) + _der(0x02, b"\x01" + os.urandom(8)) + algorithm
               + _name(issuer) + _der(0x30, _utc(days[0]) + _utc(days[1])) + _name(subject) + key
               + _der(0xA3, _der(0x30, eku)))
    return _der(0x30, tbs + algorithm + _der(0x03, b"\x00" + os.urandom(256)))


def test_windows_reads_a_self_signed_certificate_and_it_is_refused():
    der = synthetic_certificate("Qronos Review Fixture", "Qronos Review Fixture")
    facts = authenticode.certificate_facts(der)
    assert facts["sha1"] == hashlib.sha1(der).hexdigest().upper()
    assert facts["enhancedKeyUsage"] == [CODE_SIGNING_EKU] and facts["timeValidity"] == 0
    assert facts["selfIssued"] is True and facts["rootStore"] == "none" and facts["chainPolicyError"] != 0
    problems = authenticode.certificate_problems(facts, facts["sha1"])
    assert any("self-signed" in p for p in problems) and any("root" in p for p in problems)


def test_windows_reads_an_expired_certificate_from_an_unknown_issuer_and_it_is_refused():
    der = synthetic_certificate("Qronos Review Leaf", "Qronos Unknown Issuer", days=(-30, -1),
                                usage="1.3.6.1.5.5.7.3.1")
    facts = authenticode.certificate_facts(der)
    assert facts["selfIssued"] is False and facts["timeValidity"] == 1
    assert facts["enhancedKeyUsage"] == ["1.3.6.1.5.5.7.3.1"]
    problems = " ".join(authenticode.certificate_problems(facts, facts["sha1"]))
    for expected in ("not valid now", "Code Signing usage", "no certificate chain", "chain policy"):
        assert expected in problems


def test_a_file_that_is_not_a_certificate_is_refused():
    with pytest.raises(AuthenticodeError, match="not a DER X.509 certificate"):
        authenticode.certificate_facts(b"not a certificate")


# ------------------------------------------------------------------ config


def write_config(tmp_path: Path, **changes) -> Path:
    certificate = tmp_path / "owner.cer"
    if not certificate.exists():
        certificate.write_bytes(b"0\x82public certificate bytes")
    config = {"schema": 1, "signtool": str(tmp_path / "sdk" / "signtool.exe"), "certificate": str(certificate),
              "thumbprintSha1": THUMB, "timestampUrl": "https://timestamp.example.test/rfc3161"}
    config.update(changes)
    path = tmp_path / "authenticode.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


@pytest.mark.parametrize("changes", [
    {"timestampUrl": "http://timestamp.example.test"}, {"timestampUrl": "https://x.test/a b"},
    {"thumbprintSha1": "not-hex"}, {"thumbprintSha1": "AB" * 19}, {"schema": 2}, {"extra": "x"},
])
def test_a_malformed_config_is_refused(tmp_path, changes):
    with pytest.raises(AuthenticodeError):
        authenticode.load_config(write_config(tmp_path, **changes))


def test_a_config_missing_a_field_is_refused(tmp_path):
    path = write_config(tmp_path)
    config = json.loads(path.read_text(encoding="utf-8"))
    del config["timestampUrl"]
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(AuthenticodeError, match="needs exactly"):
        authenticode.load_config(path)


def test_environment_settings_are_not_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("QRONOS_AUTHENTICODE_THUMBPRINT", "CD" * 20)
    assert authenticode.load_config(write_config(tmp_path))["thumbprintSha1"] == THUMB


@pytest.mark.parametrize(("name", "content"), [("owner.pfx", b"0\x82pkcs12"),
                                               ("owner.cer", b"-----BEGIN PRIVATE KEY-----")])
def test_private_key_material_is_never_accepted_as_the_certificate(tmp_path, name, content):
    (tmp_path / name).write_bytes(content)
    config = authenticode.load_config(write_config(tmp_path, certificate=str(tmp_path / name)))
    with pytest.raises(AuthenticodeError, match="PUBLIC certificate"):
        authenticode.check_certificate(config, facts=lambda der: good_facts())


# ------------------------------------------------------------------ signtool


def sdk(tmp_path: Path, version: str = "10.0.26100.0") -> Path:
    tool = tmp_path / "pf86" / "Windows Kits" / "10" / "bin" / version / "x64" / "signtool.exe"
    tool.parent.mkdir(parents=True)
    tool.write_bytes(b"MZ stand-in, never run")
    return tool


def test_signtool_from_the_sdk_folder_signed_by_microsoft_passes(tmp_path):
    tool = sdk(tmp_path)
    found = authenticode.check_signtool(tool, program_files=lambda: tmp_path / "pf86", trust=lambda p: MICROSOFT)
    assert found["path"] == str(tool) and found["signer"] == "Microsoft Corporation"


@pytest.mark.parametrize("where", ["path_copy", "x86", "odd_version", "renamed"])
def test_signtool_anywhere_else_is_refused(tmp_path, where):
    if where == "path_copy":
        tool = tmp_path / "tools" / "signtool.exe"
        tool.parent.mkdir()
        tool.write_bytes(b"MZ")
    else:
        tool = sdk(tmp_path, "latest" if where == "odd_version" else "10.0.26100.0")
        if where == "x86":
            tool = tool.parent.parent / "x86" / "signtool.exe"
            tool.parent.mkdir()
            tool.write_bytes(b"MZ")
        elif where == "renamed":
            tool = tool.rename(tool.with_name("signtool2.exe"))
    with pytest.raises(AuthenticodeError, match="signtool must be"):
        authenticode.check_signtool(tool, program_files=lambda: tmp_path / "pf86", trust=lambda p: MICROSOFT)


def test_signtool_not_signed_by_microsoft_is_refused(tmp_path):
    tool = sdk(tmp_path)
    with pytest.raises(AuthenticodeError, match="not Microsoft Corporation"):
        authenticode.check_signtool(tool, program_files=lambda: tmp_path / "pf86",
                                    trust=lambda p: {**MICROSOFT, "subject": "Someone Else"})

    def unsigned(path):
        raise PrerequisiteError("no signature")

    with pytest.raises(AuthenticodeError, match="no valid Authenticode signature"):
        authenticode.check_signtool(tool, program_files=lambda: tmp_path / "pf86", trust=unsigned)


def test_signtool_reached_through_a_junction_is_refused(tmp_path):
    tool = sdk(tmp_path)
    with owned_junction(tool.parent, tmp_path / "pf86" / "Windows Kits" / "10" / "bin" / "10.0.1.0") as linked:
        with pytest.raises(AuthenticodeError, match="link or junction"):
            authenticode.check_signtool(linked / "signtool.exe", program_files=lambda: tmp_path / "pf86",
                                        trust=lambda p: MICROSOFT)


def test_this_machines_sdk_signtool_is_found_by_the_known_folder_and_is_microsoft_signed():
    """Read-only: the real SDK signtool, if installed, is checked (never run)."""
    candidates = sorted((authenticode.program_files_x86() / "Windows Kits" / "10" / "bin").glob("10.*/x64/signtool.exe"))
    if not candidates:
        pytest.skip("No Windows SDK signtool on this machine.")
    found = authenticode.check_signtool(candidates[-1])
    assert found["signer"] == "Microsoft Corporation" and len(found["sha256"]) == 64


# ------------------------------------------------------------- sign and verify


def product(root: Path, version: str = "0.2.0") -> Path:
    (root / "desktop" / "src-tauri").mkdir(parents=True)
    (root / "desktop" / "src-tauri" / "tauri.conf.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (root / "desktop" / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (root / "desktop" / "src-tauri" / "Cargo.toml").write_text(f'[package]\nversion = "{version}"\n',
                                                               encoding="utf-8")
    return root


class FakeSigntool:
    """Records commands; 'sign' appends bytes to the tmp artefact, as a real signature would change it."""

    def __init__(self, *, sign_exit: int = 0, verify_exit: int = 0, changes_file: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.sign_exit, self.verify_exit, self.changes_file = sign_exit, verify_exit, changes_file

    def __call__(self, argv: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(list(argv))
        assert timeout > 0
        if argv[1] == "sign":
            if self.sign_exit == 0 and self.changes_file:
                with open(argv[-1], "ab") as handle:
                    handle.write(b"[synthetic signature]")
            return self.sign_exit, "sign output"
        return self.verify_exit, "verify output"


@pytest.fixture
def release(tmp_path, monkeypatch):
    root = product(tmp_path / "root")
    artefact = tmp_path / "out" / "qronos-desktop_0.2.0_x64-setup.exe"
    artefact.parent.mkdir()
    artefact.write_bytes(b"MZ unsigned fixture installer")
    provenance = tmp_path / "out" / "installer-provenance.json"
    provenance.write_text(json.dumps({
        "installer": {"fileName": artefact.name, "bytes": artefact.stat().st_size,
                      "sha256": hashlib.sha256(artefact.read_bytes()).hexdigest(), "signed": False},
        "attributableToDesktopSource": True, "desktopSource": {"commit": "c" * 40, "clean": True}}), encoding="utf-8")
    monkeypatch.setattr(desktop_installer_verify, "source_state",
                        lambda path: {"commit": "c" * 40, "clean": True, "changedPaths": []})
    config = authenticode.load_config(write_config(tmp_path))
    options = {"root": root, "tool": lambda path: {"path": "C:\\SDK\\signtool.exe", "sha256": "0" * 64,
                                                    "signer": "Microsoft Corporation"},
               "facts": lambda der: good_facts(), "trust": lambda path: {**MICROSOFT, "thumbprintSha1": THUMB,
                                                                         "subject": "Owner Ltd"}}
    return config, artefact, provenance, options


def test_signing_signs_the_exact_installer_verifies_all_signatures_and_records_it(release):
    config, artefact, provenance, options = release
    unsigned = hashlib.sha256(artefact.read_bytes()).hexdigest()
    signtool = FakeSigntool()
    record = authenticode.sign(config, artefact, provenance, runner=signtool, **options)
    sign, verify = signtool.calls
    assert sign[1:] == ["sign", "/fd", "sha256", "/sha1", THUMB, "/tr", config["timestampUrl"], "/td", "sha256",
                        str(artefact)]
    assert verify[1:] == ["verify", "/pa", "/all", "/tw", "/v", str(artefact)]
    written = json.loads(record.read_text(encoding="utf-8"))
    assert written["unsignedSha256"] == unsigned
    assert written["signedSha256"] == hashlib.sha256(artefact.read_bytes()).hexdigest() != unsigned
    assert written["build"]["sourceCommit"] == "c" * 40 and written["certificate"]["sha1"] == THUMB
    assert "certificate revocation (offline build check)" in written["notVerifiedHere"]
    with pytest.raises(AuthenticodeError, match="already exists"):
        authenticode.sign(config, artefact, provenance, runner=FakeSigntool(), **options)


@pytest.mark.parametrize(("fake", "message"), [
    (FakeSigntool(sign_exit=1), "signtool sign did not succeed"),
    (FakeSigntool(verify_exit=2), "exit 2"),  # e.g. not time-stamped (/tw)
    (FakeSigntool(verify_exit=1), "did not succeed"),
    (FakeSigntool(changes_file=False), "unchanged"),
])
def test_a_failed_or_unproven_signature_writes_no_provenance(release, fake, message):
    config, artefact, provenance, options = release
    with pytest.raises(AuthenticodeError, match=message):
        authenticode.sign(config, artefact, provenance, runner=fake, **options)
    assert not artefact.with_name(artefact.name + ".authenticode.json").exists()


def test_a_signature_by_another_certificate_is_refused(release):
    config, artefact, provenance, options = release
    options["trust"] = lambda path: {**MICROSOFT, "thumbprintSha1": "99" * 20}
    with pytest.raises(AuthenticodeError, match="not signed by the configured certificate"):
        authenticode.sign(config, artefact, provenance, runner=FakeSigntool(), **options)


@pytest.mark.parametrize("problem", ["other_bytes", "already_signed", "unattributable", "dirty_now", "other_commit"])
def test_signing_needs_the_exact_attributable_unsigned_build(release, monkeypatch, problem):
    config, artefact, provenance, options = release
    record = json.loads(provenance.read_text(encoding="utf-8"))
    if problem == "other_bytes":
        record["installer"]["sha256"] = "0" * 64
    elif problem == "already_signed":
        record["installer"]["signed"] = True
    elif problem == "unattributable":
        record["attributableToDesktopSource"] = False
    else:
        state = {"commit": "c" * 40, "clean": problem != "dirty_now", "changedPaths": []}
        if problem == "other_commit":
            state["commit"] = "d" * 40
        monkeypatch.setattr(desktop_installer_verify, "source_state", lambda path: state)
    provenance.write_text(json.dumps(record), encoding="utf-8")
    signtool = FakeSigntool()
    with pytest.raises(AuthenticodeError):
        authenticode.sign(config, artefact, provenance, runner=signtool, **options)
    assert signtool.calls == []  # refused before anything was signed


def test_a_refused_certificate_signs_nothing(release):
    config, artefact, provenance, options = release
    options["facts"] = lambda der: good_facts(selfSigned=True)
    signtool = FakeSigntool()
    with pytest.raises(AuthenticodeError, match="self-signed"):
        authenticode.sign(config, artefact, provenance, runner=signtool, **options)
    assert signtool.calls == []


def test_only_the_versioned_installer_name_is_signed(release, tmp_path):
    config, _, provenance, options = release
    other = tmp_path / "out" / "qronos-desktop_9.9.9_x64-setup.exe"
    other.write_bytes(b"MZ")
    with pytest.raises(AuthenticodeError, match="must be qronos-desktop_0.2.0"):
        authenticode.sign(config, other, provenance, runner=FakeSigntool(), **options)


# --------------------------------------------------------------- preflight gate


def test_the_gate_opens_only_on_a_successful_verification_now(release, tmp_path):
    config, artefact, _, options = release
    config_path = write_config(tmp_path)
    root = options["root"]
    (root / "config").mkdir()
    (root / "config" / "update_trusted_keys.json").write_text(
        release_signing.TRUSTED_KEYS.read_text(encoding="utf-8"), encoding="utf-8")

    def verified(loaded, path, root):
        return {"artifact": path.name, "sha256": "5" * 64, "signer": {"subject": "Owner Ltd", "thumbprintSha1": THUMB}}

    report = release_signing.preflight(root, authenticode_config=config_path, artifact=artefact,
                                       authenticode_verify=verified)
    gate = next(g for g in report["gates"] if g["gate"] == "authenticode")
    assert gate["open"] is True and "verified now" in gate["detail"] and report["ok"] is True

    def refused(loaded, path, root):
        raise AuthenticodeError("signtool verify /pa /all /tw did not succeed (exit 2)")

    report = release_signing.preflight(root, authenticode_config=config_path, artifact=artefact,
                                       authenticode_verify=refused)
    gate = next(g for g in report["gates"] if g["gate"] == "authenticode")
    assert gate["open"] is False and report["ok"] is False and "not used as evidence" in gate["detail"]


def test_a_provenance_file_alone_never_opens_the_gate(release, tmp_path):
    config, artefact, provenance, options = release
    authenticode.sign(config, artefact, provenance, runner=FakeSigntool(), **options)
    # Default checks, now: the stand-in config names no real SDK signtool, so verification fails closed.
    report = release_signing.preflight(options["root"], authenticode_config=write_config(tmp_path),
                                       artifact=artefact)
    assert next(g for g in report["gates"] if g["gate"] == "authenticode")["open"] is False


def test_the_cli_needs_both_the_config_and_the_artifact(tmp_path, capsys):
    assert release_signing.main(["preflight", "--artifact", str(tmp_path / "x.exe")]) == 1
    assert "both --authenticode-config and --artifact" in json.loads(capsys.readouterr().out)["error"]


def test_the_tool_never_searches_path_or_runs_a_shell():
    source = Path(authenticode.__file__).read_text(encoding="utf-8")
    assert "shutil.which" not in source and "shell=True" not in source and "os.environ" not in source
