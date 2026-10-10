"""Authenticode build step (build_support/authenticode_release.py) and the preflight gate it opens.

Safety of these fixtures: signtool is NEVER run. Every sign/verify goes through a
synthetic runner that records the command and edits only a tmp_path file. Trust
answers and certificate facts are fakes, except:
- the certificate parser is exercised on small DER certificates built here in
  memory (self-signed or issued by an unknown issuer, with a junk signature) -
  read-only crypt32 calls, offline chain building, nothing added to any store;
- the real Windows SDK signtool.exe, when present, is only CHECKED (its path,
  its Microsoft signature and the chain Windows builds for it, offline
  WinVerifyTrust), never executed.
Anchor sets are written to tmp_path and read through the real validator; the
shipped config/authenticode_public_roots.json is only read.
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
LEAF = {"subject": "CN=Owner Ltd", "sha1": THUMB, "sha256": "a1" * 32}
PUBLIC_ROOT = {"subject": "CN=Reviewed Public Root", "sha1": "EF" * 20, "sha256": "e0" * 32}
ENTERPRISE_ROOT = {"subject": "CN=Contoso Enterprise Root", "sha1": "12" * 20, "sha256": "c0" * 32}
PROVENANCE = {"source": "fixture: a synthetic stand-in for a reviewed public root", "reviewedBy": "test",
              "reviewedAt": "2026-10-10", "evidence": "tests/test_authenticode_release.py"}


def anchor_entry(element: dict, **changes) -> dict:
    entry = {"subject": element["subject"], "sha256": element["sha256"], "sha1": element["sha1"],
             "provenance": dict(PROVENANCE)}
    entry.update(changes)
    return entry


def write_anchors(tmp_path: Path, entries: list, name: str = "anchors.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"schema": 1, "anchors": entries}), encoding="utf-8")
    return path


@pytest.fixture
def pinned(tmp_path) -> dict:
    """The fixture public root, pinned through the real validator."""
    return authenticode.load_anchors(write_anchors(tmp_path, [anchor_entry(PUBLIC_ROOT)], "pinned.json"))


def good_facts(**changes) -> dict:
    facts = {"sha1": THUMB, "subject": "CN=Owner Ltd", "issuer": "CN=Public CA", "notBefore": 1, "notAfter": 2,
             "timeValidity": 0, "selfIssued": False, "selfSigned": False, "enhancedKeyUsage": [CODE_SIGNING_EKU],
             "chain": [dict(LEAF), dict(PUBLIC_ROOT)], "chainPolicyError": 0, "simpleChains": 1}
    facts.update(changes)
    return facts


# ------------------------------------------------------------- public trust anchors


def test_the_shipped_anchor_set_is_empty_and_refuses_everything():
    shipped = json.loads(authenticode.ANCHORS_PATH.read_text(encoding="utf-8"))
    assert shipped["schema"] == 1 and shipped["anchors"] == []
    with pytest.raises(AuthenticodeError, match="No reviewed public trust anchor"):
        authenticode.load_anchors()


@pytest.mark.parametrize("damage", ["no_provenance", "empty_reviewer", "bad_date", "upper_sha256", "lower_sha1",
                                    "extra_field", "duplicate", "not_a_list", "wrong_schema"])
def test_an_incomplete_or_malformed_anchor_set_trusts_nothing(tmp_path, damage):
    entry = anchor_entry(PUBLIC_ROOT)
    entries = [entry]
    if damage == "no_provenance":
        del entry["provenance"]
    elif damage == "empty_reviewer":
        entry["provenance"]["reviewedBy"] = " "
    elif damage == "bad_date":
        entry["provenance"]["reviewedAt"] = "yesterday"
    elif damage == "upper_sha256":
        entry["sha256"] = entry["sha256"].upper()
    elif damage == "lower_sha1":
        entry["sha1"] = entry["sha1"].lower()
    elif damage == "extra_field":
        entry["trusted"] = True
    elif damage == "duplicate":
        entries = [entry, anchor_entry(PUBLIC_ROOT)]
    path = write_anchors(tmp_path, entries)
    if damage == "not_a_list":
        path.write_text(json.dumps({"schema": 1, "anchors": {"x": entry}}), encoding="utf-8")
    elif damage == "wrong_schema":
        path.write_text(json.dumps({"schema": 2, "anchors": entries}), encoding="utf-8")
    with pytest.raises(AuthenticodeError, match="nothing is trusted"):
        authenticode.load_anchors(path)


def test_only_the_exact_pinned_root_is_public_trust(pinned):
    assert authenticode.pinned_anchor([LEAF, PUBLIC_ROOT], pinned)["subject"] == PUBLIC_ROOT["subject"]
    assert authenticode.pinned_anchor([LEAF, ENTERPRISE_ROOT], pinned) is None  # Windows-trusted, not pinned
    assert authenticode.pinned_anchor([LEAF, {**PUBLIC_ROOT, "sha1": "00" * 20}], pinned) is None  # changed root
    assert authenticode.pinned_anchor([PUBLIC_ROOT, LEAF], pinned) is None  # the root is not the terminal element
    assert authenticode.pinned_anchor([], pinned) is None


# ------------------------------------------------------------- certificate rules


def test_a_public_code_signing_certificate_passes(pinned):
    assert authenticode.certificate_problems(good_facts(), THUMB, pinned) == []


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
    ({"simpleChains": 2}, "alternate"),
    # An otherwise valid code-signing chain to a root Windows trusts (enterprise), but not pinned:
    ({"chain": [dict(LEAF), dict(ENTERPRISE_ROOT)]}, "pinned public root"),
    # The same public subject with another key/DER (a changed or substituted root):
    ({"chain": [dict(LEAF), {**PUBLIC_ROOT, "sha256": "e1" * 32}]}, "pinned public root"),
    ({"chain": [dict(LEAF), {**PUBLIC_ROOT, "sha1": "EE" * 20}]}, "pinned public root"),
])
def test_each_certificate_rule_refuses(pinned, changes, problem):
    problems = authenticode.certificate_problems(good_facts(**changes), THUMB, pinned)
    assert any(problem in item for item in problems), problems


def test_an_enterprise_chain_passes_every_other_rule_and_is_still_refused(pinned):
    facts = good_facts(chain=[dict(LEAF), dict(ENTERPRISE_ROOT)])
    assert authenticode.certificate_problems(facts, THUMB, pinned) == [
        "the chain does not end in a reviewed, pinned public root (an enterprise, private or unlisted root is "
        "never public trust)"]


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
    assert facts["selfIssued"] is True and facts["chainPolicyError"] != 0 and "rootStore" not in facts
    assert facts["chain"] and facts["chain"][-1]["sha256"] == hashlib.sha256(der).hexdigest()
    problems = authenticode.certificate_problems(facts, facts["sha1"], {})
    assert any("self-signed" in p for p in problems) and any("pinned public root" in p for p in problems)


def test_windows_reads_an_expired_certificate_from_an_unknown_issuer_and_it_is_refused():
    der = synthetic_certificate("Qronos Review Leaf", "Qronos Unknown Issuer", days=(-30, -1),
                                usage="1.3.6.1.5.5.7.3.1")
    facts = authenticode.certificate_facts(der)
    assert facts["selfIssued"] is False and facts["timeValidity"] == 1
    assert facts["enhancedKeyUsage"] == ["1.3.6.1.5.5.7.3.1"]
    problems = " ".join(authenticode.certificate_problems(facts, facts["sha1"], {}))
    for expected in ("not valid now", "Code Signing usage", "no certificate chain", "chain policy",
                     "pinned public root"):
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
        authenticode.check_certificate(config, facts=lambda der: good_facts(), anchors={})


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


def test_the_chain_of_a_real_signed_file_is_read_from_windows_and_is_not_pinned(pinned):
    """Read-only: WinVerifyTrust (offline) on the SDK signtool.exe. Microsoft's root is not a pinned anchor."""
    candidates = sorted((authenticode.program_files_x86() / "Windows Kits" / "10" / "bin").glob("10.*/x64/signtool.exe"))
    if not candidates:
        pytest.skip("No Windows SDK signtool on this machine.")
    signatures = authenticode.signature_chains(candidates[-1])
    assert signatures[0]["index"] == 0 and len(signatures[0]["chain"]) >= 2
    for signature in signatures:
        root = signature["chain"][-1]
        assert len(root["sha256"]) == 64 and len(root["sha1"]) == 40
        assert authenticode.pinned_anchor(signature["chain"], pinned) is None


def test_a_file_without_a_signature_is_refused_by_windows(tmp_path):
    unsigned = tmp_path / "unsigned.exe"
    unsigned.write_bytes(b"MZ not signed")
    with pytest.raises(AuthenticodeError, match="primary signature"):
        authenticode.signature_chains(unsigned)


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


def signed_chains(*roots: dict, signer: str = THUMB):
    """What signature_chains would report: the primary signature, then secondary ones, each with its root."""
    return lambda path: [{"index": index, "chain": [{**LEAF, "sha1": signer if index == 0 else LEAF["sha1"]},
                                                    dict(root)]}
                         for index, root in enumerate(roots)]


@pytest.fixture
def release(tmp_path, monkeypatch, pinned):
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
               "facts": lambda der: good_facts(), "chains": signed_chains(PUBLIC_ROOT), "anchors": pinned}
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
    assert written["certificate"]["anchor"]["sha256"] == PUBLIC_ROOT["sha256"]
    assert written["certificate"]["anchor"]["provenance"] == PROVENANCE
    assert [s["root"]["sha256"] for s in written["signatures"]] == [PUBLIC_ROOT["sha256"]]
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
    options["chains"] = signed_chains(PUBLIC_ROOT, signer="99" * 20)
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


def test_an_enterprise_chain_signs_nothing(release):
    """Contract 1.4: an otherwise valid enterprise code-signing chain refuses before signtool is ever run."""
    config, artefact, provenance, options = release
    options["facts"] = lambda der: good_facts(chain=[dict(LEAF), dict(ENTERPRISE_ROOT)])
    signtool = FakeSigntool()
    with pytest.raises(AuthenticodeError, match="pinned public root"):
        authenticode.sign(config, artefact, provenance, runner=signtool, **options)
    assert signtool.calls == []


def test_with_the_shipped_empty_anchor_set_nothing_is_signed(release):
    config, artefact, provenance, options = release
    del options["anchors"]  # the repository's own config/authenticode_public_roots.json
    signtool = FakeSigntool()
    with pytest.raises(AuthenticodeError, match="No reviewed public trust anchor"):
        authenticode.sign(config, artefact, provenance, runner=signtool, **options)
    assert signtool.calls == []


@pytest.mark.parametrize("roots", [(ENTERPRISE_ROOT,), (PUBLIC_ROOT, ENTERPRISE_ROOT),
                                   ({**PUBLIC_ROOT, "sha256": "e1" * 32},)])
def test_the_current_artifact_must_end_in_a_pinned_root_for_every_signature(release, roots):
    """The certificate checked out, yet the file's ACTUAL signature chains (primary or secondary) do not all end
    in the pinned anchor: the current-artefact verification refuses."""
    config, artefact, _, options = release
    options["chains"] = signed_chains(*roots)
    with pytest.raises(AuthenticodeError, match="does not end in a reviewed, pinned public root"):
        authenticode.verify_signed(config, artefact, runner=FakeSigntool(), **{
            k: v for k, v in options.items() if k in ("root", "tool", "facts", "chains", "anchors")})


def test_the_current_artifact_verifies_with_the_exact_pinned_root(release):
    config, artefact, _, options = release
    result = authenticode.verify_signed(config, artefact, runner=FakeSigntool(), **{
        k: v for k, v in options.items() if k in ("root", "tool", "facts", "chains", "anchors")})
    assert result["signer"]["thumbprintSha1"] == THUMB and result["signatures"][0]["root"] == PUBLIC_ROOT


def test_the_release_gate_refuses_an_enterprise_signed_artifact_through_the_real_verifier(release, tmp_path):
    """preflight -> verify_signed with every check real except the file's chain (enterprise) and signtool."""
    config, artefact, _, options = release
    root = options["root"]
    (root / "config").mkdir()
    (root / "config" / "update_trusted_keys.json").write_text(
        release_signing.TRUSTED_KEYS.read_text(encoding="utf-8"), encoding="utf-8")

    def verify(loaded, path, root):
        return authenticode.verify_signed(loaded, path, root=root, runner=FakeSigntool(), tool=options["tool"],
                                          facts=options["facts"], chains=signed_chains(ENTERPRISE_ROOT),
                                          anchors=options["anchors"])

    report = release_signing.preflight(root, authenticode_config=write_config(tmp_path), artifact=artefact,
                                       authenticode_verify=verify)
    gate = next(g for g in report["gates"] if g["gate"] == "authenticode")
    assert gate["open"] is False and "pinned public root" in gate["detail"] and report["ok"] is False


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
        return authenticode.verify_signed(loaded, path, root=root, runner=FakeSigntool(), tool=options["tool"],
                                          facts=options["facts"], chains=options["chains"],
                                          anchors=options["anchors"])

    report = release_signing.preflight(root, authenticode_config=config_path, artifact=artefact,
                                       authenticode_verify=verified)
    gate = next(g for g in report["gates"] if g["gate"] == "authenticode")
    assert gate["open"] is True and "verified now" in gate["detail"] and "pinned public" in gate["detail"]
    assert report["ok"] is True

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
