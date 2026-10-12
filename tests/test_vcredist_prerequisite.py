"""The pinned Microsoft Visual C++ 2015-2022 Redistributable (x64) prerequisite of the Qronos installer.

Nothing here installs, runs an installer or downloads. The hook is only compiled (Tauri's local NSIS) and its
archive inspected with 7-Zip; the real pinned redistributable is verified when it is already in .cache/vcredist/.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from build_support import build_desktop
from build_support import desktop_installer_verify as installer_verify
from build_support import desktop_prerequisites as prerequisites
from build_support.desktop_prerequisites import PrerequisiteError
from tests.test_desktop_installer import (
    MAKENSIS,
    FakeBuild,
    assert_quarantined,
    committed_pin,
    extracted_tree,
    fake_pin,
    sha256,
    vc_stand_in,
    wire,
)

REAL_VCREDIST = prerequisites.VCREDIST_CACHE_DIR / "vc_redist.x64.exe"


def vc_pin() -> dict:
    return prerequisites.load_vcredist_pin()


def microsoft_signer(pin: dict) -> dict:
    authenticode = pin["authenticode"]
    return {"thumbprintSha1": authenticode["signerCertificateSha1"], "subject": authenticode["signer"],
            "issuer": authenticode["issuer"]}


def test_the_committed_pin_is_microsofts_signed_x64_redistributable():
    pin = vc_pin()
    assert pin["fileName"] == "vc_redist.x64.exe" and pin["architecture"] == "x64"
    assert pin["url"].startswith("https://download.visualstudio.microsoft.com/download/pr/")
    assert f"/{pin['sha256'].upper()}/" in pin["url"]  # Microsoft's immutable URL names the file's SHA-256
    assert "aka.ms/vs/17/release/vc_redist.x64.exe" in pin["resolvedFrom"]
    assert pin["runtime"]["version"] == "14.44.35211.0" and (pin["runtime"]["major"], pin["runtime"]["minor"],
                                                             pin["runtime"]["build"]) == (14, 44, 35211)
    assert pin["bytes"] == 25635768
    assert pin["sha256"] == "cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b"
    assert pin["authenticode"]["signer"] == "Microsoft Corporation"
    assert pin["installArguments"] == "/install /quiet /norestart" and pin["successExitCodes"] == [0, 1638, 3010]
    assert "licensed Visual Studio users" in pin["licence"]["condition"]
    gates = " ".join(pin["ownerGates"]).lower()
    assert "licensed visual studio" in gates or "visual studio 2022 licence" in gates
    assert "unsigned" in gates and "uac" in gates


@pytest.mark.parametrize("change", [
    {"fileName": "VC_redist.x86.exe"},
    {"architecture": "arm64"},
    {"url": "https://aka.ms/vs/17/release/vc_redist.x64.exe"},
    {"url": "https://download.visualstudio.microsoft.com/download/pr/x/" + "0" * 64 + "/VC_redist.x64.exe"},
    {"sha256": "CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B"},
    {"bytes": 0},
    {"runtime": {"version": "15.0.0.0", "major": 15, "minor": 0, "build": 0}},
    {"runtime": {"version": "14.44.35211.0", "major": 14, "minor": 43, "build": 35211}},
    {"authenticode": {"signer": "Someone Else", "issuer": "x", "signerCertificateSha1": "0" * 40}},
    {"installArguments": "/install /passive"},
    {"successExitCodes": [0, 1, 3010]},
    {"ownerGates": []},
    {"licence": {}},
    {"waiver": True},
])
def test_a_malformed_vcredist_pin_is_refused(tmp_path, change):
    path = tmp_path / "pin.json"
    path.write_text(json.dumps({**vc_pin(), **change}), encoding="utf-8")
    with pytest.raises(PrerequisiteError):
        prerequisites.load_vcredist_pin(path)


def test_a_cached_redistributable_must_match_size_hash_and_microsofts_signer(tmp_path):
    file, pin = vc_stand_in(tmp_path)
    good = prerequisites.verify_vcredist_installer(file, pin, trust=lambda path: microsoft_signer(pin))
    assert good["sha256"] == pin["sha256"] and good["runtimeVersion"] == "14.44.35211.0"
    other = {**microsoft_signer(pin), "thumbprintSha1": "0" * 40}
    with pytest.raises(PrerequisiteError, match="VC\\+\\+ redistributable is signed by"):
        prerequisites.verify_vcredist_installer(file, pin, trust=lambda path: other)
    with pytest.raises(PrerequisiteError, match="SHA-256"):
        prerequisites.verify_vcredist_installer(file, {**pin, "sha256": "0" * 64}, trust=lambda path: microsoft_signer(pin))
    with pytest.raises(PrerequisiteError, match="must be named"):
        renamed = file.with_name("other.exe")
        shutil.copyfile(file, renamed)
        prerequisites.verify_vcredist_installer(renamed, pin, trust=lambda path: microsoft_signer(pin))


@pytest.mark.skipif(not REAL_VCREDIST.is_file(), reason="The pinned redistributable is not in .cache/vcredist/.")
def test_the_real_cached_redistributable_verifies_and_a_tampered_copy_does_not(tmp_path):
    evidence = prerequisites.verify_vcredist_installer(REAL_VCREDIST, vc_pin())  # real WinVerifyTrust, offline
    assert evidence["sha256"] == vc_pin()["sha256"] and "Microsoft Corporation" in evidence["authenticode"]
    tampered = tmp_path / "vc_redist.x64.exe"
    data = bytearray(REAL_VCREDIST.read_bytes())
    data[-1] ^= 0xFF
    tampered.write_bytes(bytes(data))
    with pytest.raises(PrerequisiteError):
        prerequisites.verify_vcredist_installer(tampered, vc_pin())


def test_a_hook_wrapper_without_the_redistributable_is_refused(tmp_path):
    pin = committed_pin()
    wrapper = prerequisites.hook_wrapper(tmp_path / pin["fileName"], pin, owned_manifest=tmp_path / "m.txt",
                                         vcredist=vc_stand_in(tmp_path))
    prerequisites.verify_hook_wrapper(wrapper)
    stripped = "\n".join(line for line in wrapper.splitlines() if "QRONOS_VCREDIST_" not in line)
    with pytest.raises(PrerequisiteError, match="QRONOS_VCREDIST_INSTALLER"):
        prerequisites.verify_hook_wrapper(stripped)


def _preinstall(hook: str) -> str:
    body = hook[hook.index("!macro NSIS_HOOK_PREINSTALL"):]
    return body[:body.index("!macroend")]


def test_the_redistributable_runs_after_webview2_and_before_anything_of_the_previous_install_changes():
    hook = prerequisites.HOOK_PATH.read_text(encoding="utf-8")
    pre = _preinstall(hook)
    order = [pre.index("!insertmacro QRONOS_WEBVIEW2_RUN_INSTALLER"),
             pre.index("!insertmacro QRONOS_VCREDIST_SATISFIED_TO_R9"),
             pre.index("!insertmacro QRONOS_VCREDIST_RUN_INSTALLER"),
             pre.rindex("!insertmacro QRONOS_VCREDIST_SATISFIED_TO_R9"),  # checked again after the run
             pre.index("CheckIfAppIsRunning"), pre.index("Call QronosTxnBegin")]
    assert order == sorted(order) and order[1] != order[3]
    assert "Abort" not in pre.replace("!insertmacro QRONOS_FAIL", "")  # every stop goes through QRONOS_FAIL
    for register in ("$R4", "$R5", "$R6"):
        assert f"Push {register}" in pre and f"Pop {register}" in pre


def test_the_run_accepts_only_the_pinned_exit_codes_and_clears_errors_first():
    hook = prerequisites.HOOK_PATH.read_text(encoding="utf-8")
    run = hook[hook.index("!macro QRONOS_VCREDIST_RUN_INSTALLER"):]
    run = run[:run.index("!macroend")]
    order = [run.index(step) for step in ('StrCpy $R8 ""', "ClearErrors", "ExecWait", "${If} ${Errors}")]
    assert order == sorted(order)
    assert "/install /quiet /norestart" in run and "http" not in run.lower()
    assert "${If} $R8 <> 0" in run and "${AndIf} $R8 <> 1638" in run and "${AndIf} $R8 <> 3010" in run
    # 3010 (installed; a restart finishes it) is reported, and is still followed by the registry check, like 0.
    pre = _preinstall(hook)
    run_at, restart_at = pre.index("!insertmacro QRONOS_VCREDIST_RUN_INSTALLER"), pre.index("${If} $R8 = 3010")
    assert run_at < restart_at < pre.rindex("!insertmacro QRONOS_VCREDIST_SATISFIED_TO_R9")
    assert "QRONOS_FAIL" not in pre[restart_at:pre.index("${EndIf}", restart_at)]
    detect = hook[hook.index("!macro QRONOS_VCREDIST_SATISFIED_TO_R9"):]
    detect = detect[:detect.index("!macroend")]
    assert "QRONOS_VCREDIST_READ_VIEW 64" in detect and "QRONOS_VCREDIST_READ_VIEW 32" in detect
    assert "SetRegView lastused" in detect


@pytest.mark.skipif(os.name != "nt", reason="NSIS compiles on Windows here.")
def test_the_hook_does_not_compile_without_the_verified_redistributable(tmp_path):
    if MAKENSIS is None or not MAKENSIS.is_file():
        pytest.skip("Tauri's NSIS compiler is not installed on this machine.")
    payload = tmp_path / "wv2.exe"
    payload.write_bytes(b"MZ")
    script = tmp_path / "probe.nsi"
    script.write_text("\n".join((
        "Unicode true", 'Name "probe"', f'OutFile "{tmp_path / "probe.exe"}"', "RequestExecutionLevel user",
        f'!define QRONOS_WEBVIEW2_INSTALLER "{payload}"', '!define QRONOS_WEBVIEW2_VERSION "1.2.3.4"',
        f'!include "{prerequisites.HOOK_PATH}"', "Section", "SectionEnd", "")), encoding="utf-8")
    result = subprocess.run([str(MAKENSIS), "/V2", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "Visual C++ redistributable was not provided" in result.stdout + result.stderr
    assert not (tmp_path / "probe.exe").exists()


@pytest.mark.skipif(os.name != "nt", reason="The desktop installer is built on Windows only.")
def test_a_bad_cached_redistributable_stops_the_build_before_anything_is_built(tmp_path, monkeypatch):
    fake = FakeBuild(tmp_path, committed_pin())
    backend = wire(tmp_path, monkeypatch, fake)

    def refuse(path, pin):
        raise PrerequisiteError("The cached VC++ redistributable SHA-256 x does not match the pin.")

    monkeypatch.setattr(build_desktop, "verify_vcredist_installer", refuse)
    with pytest.raises(PrerequisiteError, match="VC\\+\\+ redistributable"):
        build_desktop.build(backend, target_dir=tmp_path / "t")
    assert fake.calls == ["prerequisite"] and not (tmp_path / "runtime").exists()


@pytest.mark.skipif(os.name != "nt", reason="The desktop installer is built on Windows only.")
def test_a_built_installer_with_other_redistributable_bytes_is_refused_and_quarantined(tmp_path, monkeypatch):
    fake = FakeBuild(tmp_path, committed_pin())
    fake.embedded_vc_sha = "0" * 64
    backend = wire(tmp_path, monkeypatch, fake)
    with pytest.raises(PrerequisiteError, match="embedded VC\\+\\+ redistributable SHA-256"):
        build_desktop.build(backend, target_dir=tmp_path / "t", work_root=tmp_path / "w")
    assert "extracted" not in fake.calls
    assert_quarantined(tmp_path)


@pytest.mark.parametrize("entries, message", [
    ({}, "exactly once"),
    ({prerequisites.VCREDIST_EMBEDDED_PATH: 5}, "bytes; the pin says"),
    ({prerequisites.VCREDIST_EMBEDDED_PATH: None, "other\\vc_redist.x64.exe": 1}, "exactly once"),
])
def test_an_installer_without_exactly_the_pinned_redistributable_is_refused(entries, message):
    with pytest.raises(PrerequisiteError, match=message):
        prerequisites.verify_embedded_vcredist(entries, vc_pin())


def test_the_extracted_installer_and_its_provenance_carry_the_redistributable(tmp_path):
    root, pin = extracted_tree(tmp_path)
    data = b"MZ stand-in VC++ redistributable"
    vc = {**vc_pin(), "bytes": len(data), "sha256": sha256(data)}
    with pytest.raises(PrerequisiteError, match="does not contain the VC\\+\\+ redistributable"):
        installer_verify.verify_extracted(root, pin, vcredist_pin=vc)
    (root / "$PLUGINSDIR" / "vc_redist.x64.exe").write_bytes(b"MZ other bytes of the same kind")
    with pytest.raises(PrerequisiteError, match="VC\\+\\+ redistributable SHA-256"):
        installer_verify.verify_extracted(root, pin, vcredist_pin=vc)
    (root / "$PLUGINSDIR" / "vc_redist.x64.exe").write_bytes(data)
    extracted = installer_verify.verify_extracted(root, pin, vcredist_pin=vc)
    assert extracted["vcredistInstaller"] == {"path": "$PLUGINSDIR/vc_redist.x64.exe", "bytes": len(data),
                                              "sha256": sha256(data)}
    setup = tmp_path / "qronos-desktop_0.1.0_x64-setup.exe"
    setup.write_bytes(b"MZ installer")
    record = installer_verify.provenance_record(setup=setup, root=build_desktop.ROOT, pin=pin, vcredist_pin=vc,
                                                extracted=extracted, backend_source="x", purpose="packaging-check",
                                                tauri_cli="2.11.4")
    assert record["vcredist"]["runtimeVersion"] == "14.44.35211.0"
    assert record["vcredist"]["embeddedAt"] == "$PLUGINSDIR/vc_redist.x64.exe"
    assert set(vc["ownerGates"]) <= set(record["ownerGates"])
    assert any("Visual C++ runtime" in item for item in record["notVerifiedHere"])
    assert fake_pin(b"x")["fileName"] == pin["fileName"]  # the WebView2 pin is unchanged by this
