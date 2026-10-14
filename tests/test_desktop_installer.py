"""The offline Windows installer: pinned WebView2 prerequisite, hook and wiring.

Nothing here installs, runs an installer or downloads. Where the local NSIS
compiler that Tauri uses and 7-Zip are present, the hook is compiled into a
small throwaway installer and its archive is inspected; the real pinned
installer is verified when it is already in .cache/wv2/.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from build_support import build_desktop
from build_support import desktop_installer_verify as installer_verify
from build_support import install_manifest
from build_support import desktop_prerequisites as prerequisites
from build_support.desktop_prerequisites import EMBEDDED_PATH, PrerequisiteError

# Captured before the autouse fixture isolates LOCALAPPDATA/USERPROFILE.
_LOCAL = os.environ.get("LOCALAPPDATA", "")
MAKENSIS = Path(_LOCAL, "tauri", "NSIS", "makensis.exe") if _LOCAL else None
REAL_INSTALLER = prerequisites.CACHE_DIR / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def committed_pin() -> dict:
    return prerequisites.load_pin()


def fake_pin(data: bytes) -> dict:
    """The committed pin, re-pointed at a small fixture so the checks can be exercised."""
    pin = committed_pin()
    return {**pin, "bytes": len(data), "sha256": sha256(data)}


def vc_stand_in(tmp_path: Path) -> tuple[Path, dict]:
    """A small stand-in for the pinned VC++ redistributable and the committed pin re-pointed at it."""
    data = b"MZ stand-in for the pinned VC++ redistributable\n" * 32
    path = tmp_path / "vcredist" / "vc_redist.x64.exe"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path, {**prerequisites.load_vcredist_pin(), "bytes": len(data), "sha256": sha256(data)}


def pinned_signer(pin: dict | None = None) -> dict:
    authenticode = (pin or committed_pin())["authenticode"]
    return {"thumbprintSha1": authenticode["signerCertificateSha1"], "subject": authenticode["signer"],
            "issuer": authenticode["issuer"]}


def trusted_as(identity: dict, seen: list | None = None):
    def trust(path: Path) -> dict:
        if seen is not None:
            seen.append(path)
        return identity
    return trust


def seven_zip() -> Path:
    try:
        return prerequisites.find_seven_zip()
    except PrerequisiteError:
        pytest.skip("7-Zip is not installed on this machine.")


def junction(target: Path, link: Path) -> None:
    winapi = pytest.importorskip("_winapi")
    winapi.CreateJunction(str(target), str(link))


# ------------------------------------------------------------- the pin


def test_the_committed_pin_is_the_x64_evergreen_standalone_installer():
    pin = committed_pin()

    assert pin["architecture"] == "x64" and pin["target"] == "x86_64-pc-windows-msvc"
    assert pin["fileName"] == "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    # The immutable delivery URL, not Microsoft's moving fwlink.
    assert pin["url"].startswith("https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/")
    assert "fwlink" not in pin["url"] and "fwlink" in pin["resolvedFrom"]
    assert pin["runtime"] == {**pin["runtime"], "appId": "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                              "version": "154.0.4258.62"}
    assert pin["bytes"] == 212358352
    assert pin["sha256"] == "ac22ecdc19c5b88b87f3fa752c00da9541653a8f5c0c5fc4a3b2b6ebe6591f69"
    gates = " ".join(pin["ownerGates"]).lower()
    assert "redistribution" in gates and "unsigned" in gates


@pytest.mark.parametrize("change", [
    {"target": "aarch64-pc-windows-msvc"},
    {"architecture": "arm64"},
    {"fileName": "MicrosoftEdgeWebview2Setup.exe"},
    {"url": "https://go.microsoft.com/fwlink/?linkid=2124701"},
    {"url": "http://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/x/"
            "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"},
    {"bytes": 0},
    {"bytes": True},
    {"sha256": "AC22ECDC19C5B88B87F3FA752C00DA9541653A8F5C0C5FC4A3B2B6EBE6591F69"},
    {"runtime": {"appId": "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}", "version": "latest"}},
    {"installArguments": "/install"},
    {"ownerGates": []},
    {"waiver": True},
])
def test_a_malformed_pin_is_refused(tmp_path, change):
    pin = {**committed_pin(), **change}
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(pin), encoding="utf-8")

    with pytest.raises(PrerequisiteError):
        prerequisites.load_pin(path)


# ------------------------------------------------ the cached installer


def test_a_cached_installer_must_match_size_hash_and_signature(tmp_path):
    data = b"MZ pinned installer bytes"
    cached = tmp_path / "wv2" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    cached.parent.mkdir()
    cached.write_bytes(data)
    trusted: list[Path] = []

    evidence = prerequisites.verify_installer(cached, fake_pin(data), trust=trusted_as(pinned_signer(), trusted))

    assert trusted == [cached]
    assert evidence["sha256"] == sha256(data) and evidence["runtimeVersion"] == "154.0.4258.62"

    with pytest.raises(PrerequisiteError, match="bytes; the pin says"):
        prerequisites.verify_installer(cached, {**fake_pin(data), "bytes": len(data) + 1}, trust=trusted_as(pinned_signer(), trusted))
    cached.write_bytes(data[:-1] + b"X")  # same size, one byte changed
    with pytest.raises(PrerequisiteError, match="does not match the pin"):
        prerequisites.verify_installer(cached, fake_pin(data), trust=trusted_as(pinned_signer(), trusted))

    cached.write_bytes(data)

    def untrusted(path):
        raise PrerequisiteError("no valid Authenticode signature")

    with pytest.raises(PrerequisiteError, match="Authenticode"):
        prerequisites.verify_installer(cached, fake_pin(data), trust=untrusted)


@pytest.mark.parametrize("field", ["thumbprintSha1", "subject", "issuer"])
def test_a_trusted_but_different_signer_is_refused(tmp_path, field):
    data = b"MZ pinned installer bytes"
    cached = tmp_path / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    cached.write_bytes(data)
    other = {**pinned_signer(), field: "0" * 40 if field == "thumbprintSha1" else "Somebody Else"}

    with pytest.raises(PrerequisiteError, match="not the pinned 'Microsoft Corporation' certificate"):
        prerequisites.verify_installer(cached, fake_pin(data), trust=trusted_as(other))
    # A thumbprint in another letter case is the same certificate.
    lower = {**pinned_signer(), "thumbprintSha1": pinned_signer()["thumbprintSha1"].lower()}
    prerequisites.verify_installer(cached, fake_pin(data), trust=trusted_as(lower))


@pytest.mark.skipif(os.name != "nt", reason="WinVerifyTrust is a Windows API.")
def test_a_validly_signed_non_microsoft_binary_is_refused(tmp_path):
    """A real Authenticode-valid file from another publisher, in place of the pinned installer."""
    source = Path(sys.executable)
    try:
        identity = prerequisites.winverifytrust(source)
    except PrerequisiteError:
        pytest.skip("The test interpreter is not Authenticode-signed here.")
    if identity["subject"] == "Microsoft Corporation":
        pytest.skip("The test interpreter is signed by Microsoft here.")
    stand_in = tmp_path / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    stand_in.write_bytes(source.read_bytes())
    pin = fake_pin(stand_in.read_bytes())  # the bytes match; only the signer differs

    assert prerequisites.winverifytrust(stand_in) == identity  # the signature itself is valid
    with pytest.raises(PrerequisiteError, match=f"signed by '{re.escape(identity['subject'])}'"):
        prerequisites.verify_installer(stand_in, pin)


@pytest.mark.parametrize("change", [{"signerCertificateSha1": "4028CAD6"}, {"signer": ""}, {"issuer": ""}])
def test_a_pin_without_a_complete_signer_identity_is_refused(tmp_path, change):
    pin = committed_pin()
    pin = {**pin, "authenticode": {**pin["authenticode"], **change}}
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(pin), encoding="utf-8")

    with pytest.raises(PrerequisiteError, match="signer, issuer and SHA-1 thumbprint"):
        prerequisites.load_pin(path)


def test_a_misnamed_missing_or_linked_installer_is_refused(tmp_path):
    data = b"MZ pinned installer bytes"
    real = tmp_path / "real"
    real.mkdir()
    (real / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").write_bytes(data)
    (real / "other.exe").write_bytes(data)

    with pytest.raises(PrerequisiteError, match="must be named"):
        prerequisites.verify_installer(real / "other.exe", fake_pin(data), trust=trusted_as(pinned_signer()))
    with pytest.raises(PrerequisiteError, match="missing"):
        prerequisites.verify_installer(tmp_path / "absent" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
                                       fake_pin(data), trust=trusted_as(pinned_signer()))
    junction(real, tmp_path / "linked")
    with pytest.raises(PrerequisiteError, match="link"):
        prerequisites.verify_installer(tmp_path / "linked" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
                                       fake_pin(data), trust=trusted_as(pinned_signer()))


@pytest.mark.skipif(os.name != "nt", reason="WinVerifyTrust is a Windows API.")
def test_windows_refuses_an_unsigned_file(tmp_path):
    unsigned = tmp_path / "unsigned.exe"
    unsigned.write_bytes(b"MZ" + b"\0" * 64)

    with pytest.raises(PrerequisiteError, match="no valid Authenticode signature"):
        prerequisites.winverifytrust(unsigned)


@pytest.mark.skipif(not REAL_INSTALLER.is_file(), reason="The pinned installer is not in .cache/wv2/.")
def test_the_real_cached_installer_verifies_and_a_tampered_copy_does_not(tmp_path):
    pin = committed_pin()

    evidence = prerequisites.verify_installer(REAL_INSTALLER, pin)

    assert evidence["sha256"] == pin["sha256"] and evidence["authenticode"].startswith("valid")
    assert prerequisites.winverifytrust(REAL_INSTALLER) == pinned_signer(pin)
    tampered = tmp_path / pin["fileName"]
    data = bytearray(REAL_INSTALLER.read_bytes())
    data[len(data) // 2] ^= 0xFF
    tampered.write_bytes(bytes(data))
    with pytest.raises(PrerequisiteError, match="no valid Authenticode signature"):
        prerequisites.winverifytrust(tampered)  # the signature itself fails, not only the pin
    with pytest.raises(PrerequisiteError, match="does not match the pin"):
        prerequisites.verify_installer(tampered, pin)


# ------------------------------------------------- build-tool preflight


def prepared_tools(tmp_path: Path) -> Path:
    tools = tmp_path / "tauri"
    for name in prerequisites.NSIS_REQUIRED_FILES:
        (tools / "NSIS" / name).parent.mkdir(parents=True, exist_ok=True)
        (tools / "NSIS" / name).write_bytes(b"tool")
    return tools


def test_a_prepared_nsis_tool_folder_passes(tmp_path, monkeypatch):
    tools = prepared_tools(tmp_path)
    plugin = tools / "NSIS" / prerequisites.NSIS_TAURI_UTILS
    monkeypatch.setattr(prerequisites, "NSIS_TAURI_UTILS_SHA1",
                        hashlib.sha1(plugin.read_bytes()).hexdigest().upper())

    evidence = prerequisites.verify_build_tools(tools, "2.11.4")

    assert evidence["nsisTauriUtilsSha1"] == prerequisites.NSIS_TAURI_UTILS_SHA1


@pytest.mark.parametrize("missing", ["makensis.exe", "Stubs/lzma_solid-x86-unicode", "Include/x64.nsh",
                                     "Plugins/x86-unicode/additional/nsis_tauri_utils.dll"])
def test_an_incomplete_nsis_tool_folder_is_refused_instead_of_downloaded(tmp_path, missing):
    tools = prepared_tools(tmp_path)
    (tools / "NSIS" / missing).unlink()

    with pytest.raises(PrerequisiteError, match=f"missing {re.escape(missing)}; the build would download"):
        prerequisites.verify_build_tools(tools, "2.11.4")


def test_a_wrong_nsis_tauri_utils_is_refused(tmp_path):
    tools = prepared_tools(tmp_path)  # the stand-in plug-in bytes do not have the pinned SHA-1

    with pytest.raises(PrerequisiteError, match="would download a replacement"):
        prerequisites.verify_build_tools(tools, "2.11.4")


def test_another_tauri_cli_version_needs_a_new_review(tmp_path):
    with pytest.raises(PrerequisiteError, match="reviewed against 2.11.4"):
        prerequisites.verify_build_tools(prepared_tools(tmp_path), "2.12.0")


def test_the_preflight_matches_the_locked_cli():
    lock = json.loads((build_desktop.ROOT / "desktop" / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"]["node_modules/@tauri-apps/cli"]["version"] == prerequisites.TAURI_CLI_VERSION


@pytest.mark.skipif(not (MAKENSIS and MAKENSIS.is_file()), reason="Tauri's NSIS folder is not prepared here.")
def test_this_machines_prepared_nsis_folder_passes():
    assert prerequisites.verify_build_tools(MAKENSIS.parent.parent, "2.11.4")["nsisTauriUtilsSha1"] == \
        prerequisites.NSIS_TAURI_UTILS_SHA1


@pytest.mark.skipif(os.name != "nt", reason="The desktop installer is built on Windows only.")
def test_unprepared_build_tools_stop_before_anything_is_built(tmp_path, monkeypatch):
    fake = FakeBuild(tmp_path, committed_pin())
    backend = wire(tmp_path, monkeypatch, fake)

    def refuse(tools, version):
        raise PrerequisiteError("the build would download NSIS from GitHub")

    monkeypatch.setattr(build_desktop, "verify_build_tools", refuse)

    with pytest.raises(PrerequisiteError, match="would download NSIS"):
        build_desktop.build(backend, target_dir=tmp_path / "t", work_root=tmp_path / "w")
    assert "tauri" not in fake.calls and not (tmp_path / "w").exists()


# ------------------------------------------------------- the NSIS hook


def test_the_hook_wrapper_names_the_verified_file_and_refuses_unsafe_paths(tmp_path):
    pin = committed_pin()
    manifest = tmp_path / "qronos-install-manifest.txt"
    vc = vc_stand_in(tmp_path)
    wrapper = prerequisites.write_hook_wrapper(tmp_path, tmp_path / pin["fileName"], pin, owned_manifest=manifest,
                                               vcredist=vc)

    text = wrapper.read_text(encoding="utf-8")
    assert f'!define QRONOS_VCREDIST_INSTALLER "{vc[0]}"' in text
    assert '!define QRONOS_VCREDIST_MAJOR "14"' in text and '!define QRONOS_VCREDIST_MINOR "44"' in text
    assert '!define QRONOS_VCREDIST_BUILD "35211"' in text
    prerequisites.verify_hook_wrapper(text)
    assert f'!define QRONOS_WEBVIEW2_INSTALLER "{tmp_path / pin["fileName"]}"' in text
    assert f'!define QRONOS_WEBVIEW2_VERSION "{pin["runtime"]["version"]}"' in text
    assert f'!define QRONOS_OWNED_MANIFEST "{manifest}"' in text
    assert f'!include "{prerequisites.HOOK_PATH}"' in text
    with pytest.raises(FileExistsError):
        prerequisites.write_hook_wrapper(tmp_path, tmp_path / pin["fileName"], pin, owned_manifest=manifest,
                                         vcredist=vc)
    for unsafe in ('a"b', "a$b", "a\nb", "a;b"):
        with pytest.raises(PrerequisiteError, match="NSIS"):
            prerequisites.hook_wrapper(tmp_path / unsafe / pin["fileName"], pin, owned_manifest=manifest, vcredist=vc)
        with pytest.raises(PrerequisiteError, match="NSIS"):
            prerequisites.hook_wrapper(tmp_path / pin["fileName"], pin, owned_manifest=tmp_path / unsafe / "m.txt",
                                       vcredist=vc)
        with pytest.raises(PrerequisiteError, match="NSIS"):
            prerequisites.hook_wrapper(tmp_path / pin["fileName"], pin, owned_manifest=manifest,
                                       vcredist=(tmp_path / unsafe / "vc_redist.x64.exe", vc[1]))


def compile_hook(tmp_path: Path, wrapper_text: str | None) -> subprocess.CompletedProcess:
    if MAKENSIS is None or not MAKENSIS.is_file():
        pytest.skip("Tauri's NSIS compiler is not installed on this machine.")
    script = tmp_path / "probe.nsi"
    include = ""
    if wrapper_text is not None:
        (tmp_path / "hooks.nsh").write_text(wrapper_text, encoding="utf-8")
        include = f'!include "{tmp_path / "hooks.nsh"}"'
    else:
        include = f'!include "{prerequisites.HOOK_PATH}"'
    script.write_text("\n".join((
        "Unicode true", "SetCompressor /SOLID lzma", 'Name "probe"', f'OutFile "{tmp_path / "probe.exe"}"', "RequestExecutionLevel user",
        'InstallDir "$TEMP\\qronos-probe"', include,
        "Section", "  !insertmacro NSIS_HOOK_PREINSTALL", "SectionEnd", "",
    )), encoding="utf-8")
    return subprocess.run([str(MAKENSIS), "/V2", str(script)], capture_output=True, text=True, check=False)


def test_the_hook_does_not_compile_without_the_verified_installer(tmp_path):
    result = compile_hook(tmp_path, None)

    assert result.returncode != 0
    assert "must be built with build_support.build_desktop" in result.stdout + result.stderr
    assert not (tmp_path / "probe.exe").exists()


def test_the_compiled_hook_embeds_exactly_the_pinned_bytes_and_no_downloader(tmp_path):
    data = b"MZ stand-in for the pinned WebView2 installer\n" * 64
    payload = tmp_path / "wv2" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    payload.parent.mkdir()
    payload.write_bytes(data)
    pin = fake_pin(data)
    manifest = tmp_path / "qronos-install-manifest.txt"  # the update transaction embeds the build's own list
    manifest.write_bytes(b"stand-in list (embedded under $PLUGINSDIR, never installed as a payload file)")

    vc_payload, vc_pin = vc_stand_in(tmp_path)
    result = compile_hook(tmp_path, prerequisites.hook_wrapper(payload, pin, owned_manifest=manifest,
                                                               vcredist=(vc_payload, vc_pin)))

    assert result.returncode == 0, result.stdout + result.stderr
    tool = seven_zip()
    entries = prerequisites.list_installer(tmp_path / "probe.exe", tool)
    assert EMBEDDED_PATH in entries  # 7-Zip may not know the size of a solid stream's last entry
    prerequisites.verify_embedded_payload(entries, pin)
    assert prerequisites.extract_and_hash_payload(tmp_path / "probe.exe", tool, tmp_path / "out") == pin["sha256"]
    prerequisites.verify_embedded_vcredist(entries, vc_pin)
    assert prerequisites.extract_and_hash_vcredist(tmp_path / "probe.exe", tool, tmp_path / "vc-out") == \
        vc_pin["sha256"]
    # The script runs the embedded file silently and never names a download URL.
    hook = prerequisites.HOOK_PATH.read_text(encoding="utf-8")
    assert "/silent /install" in hook and "NSISdl" not in hook and "http" not in hook.lower()


# --------------------------------------------- the generated Tauri script


GOOD_SCRIPT = """
!define INSTALLWEBVIEW2MODE ""
!define WEBVIEW2INSTALLERARGS "/silent"
!define WEBVIEW2BOOTSTRAPPERPATH ""
!define WEBVIEW2INSTALLERPATH ""
!define MINIMUMWEBVIEW2VERSION ""
!include "{wrapper}"
"""


def test_a_generated_script_without_download_paths_passes(tmp_path):
    wrapper = tmp_path / "qronos-installer-hooks.nsh"

    prerequisites.verify_generated_script(GOOD_SCRIPT.format(wrapper=wrapper), wrapper)


@pytest.mark.parametrize(("old", "new", "message"), [
    ('INSTALLWEBVIEW2MODE ""', 'INSTALLWEBVIEW2MODE "downloadBootstrapper"', "INSTALLWEBVIEW2MODE"),
    ('INSTALLWEBVIEW2MODE ""', 'INSTALLWEBVIEW2MODE "embedBootstrapper"', "INSTALLWEBVIEW2MODE"),
    ('INSTALLWEBVIEW2MODE ""', 'INSTALLWEBVIEW2MODE "offlineInstaller"', "INSTALLWEBVIEW2MODE"),
    ('WEBVIEW2BOOTSTRAPPERPATH ""', 'WEBVIEW2BOOTSTRAPPERPATH "C:\\t\\MicrosoftEdgeWebview2Setup.exe"',
     "WEBVIEW2BOOTSTRAPPERPATH"),
    ('MINIMUMWEBVIEW2VERSION ""', 'MINIMUMWEBVIEW2VERSION "120.0.0.0"', "MINIMUMWEBVIEW2VERSION"),
    ('!define INSTALLWEBVIEW2MODE ""\n', "", "does not define INSTALLWEBVIEW2MODE"),
    ("!include", "; !include", "does not include the verified WebView2 hook"),
])
def test_a_generated_script_with_a_download_path_is_refused(tmp_path, old, new, message):
    wrapper = tmp_path / "qronos-installer-hooks.nsh"
    script = GOOD_SCRIPT.format(wrapper=wrapper).replace(old, new)

    with pytest.raises(PrerequisiteError, match=message):
        prerequisites.verify_generated_script(script, wrapper)


# ----------------------------------------------- the built installer


@pytest.mark.parametrize(("entries", "message"), [
    ({}, "exactly once"),
    ({EMBEDDED_PATH: 5, "$TEMP\\MicrosoftEdgeWebView2RuntimeInstallerX64.exe": 5}, "exactly once"),
    ({EMBEDDED_PATH: 4}, "bytes; the pin says"),
    ({EMBEDDED_PATH: 5, "$PLUGINSDIR\\NSISdl.dll": 10}, "download plug-in"),
    ({EMBEDDED_PATH: 5, "$PLUGINSDIR\\INetC.dll": 10}, "download plug-in"),
])
def test_an_installer_without_exactly_the_pinned_payload_is_refused(entries, message):
    pin = {**committed_pin(), "bytes": 5}

    with pytest.raises(PrerequisiteError, match=message):
        prerequisites.verify_embedded_payload(entries, pin)


def test_the_previous_review_installer_would_be_refused():
    """The 7584 review installer embedded Tauri's download plug-in; recreate its relevant listing."""
    listing = {"$PLUGINSDIR\\System.dll": 12288, "$PLUGINSDIR\\NSISdl.dll": 15360,
               "qronos-desktop.exe": 1, "uninstall.exe": 1}

    with pytest.raises(PrerequisiteError, match="download plug-in"):
        prerequisites.verify_embedded_payload(listing, committed_pin())


# ---------------------------------------------------- configuration


def committed_config() -> dict:
    return json.loads(build_desktop.TAURI_CONFIG.read_text(encoding="utf-8"))


def test_the_committed_tauri_config_never_downloads_webview2():
    config = committed_config()

    build_desktop.check_tauri_config(config)
    windows = config["bundle"]["windows"]
    assert windows["webviewInstallMode"] == {"type": "skip"}
    assert (build_desktop.ROOT / "desktop" / "src-tauri" / windows["nsis"]["installerHooks"]).resolve() == \
        prerequisites.HOOK_PATH.resolve()
    assert (build_desktop.ROOT / "desktop" / "src-tauri" / windows["nsis"]["template"]).resolve() == \
        (prerequisites.TEMPLATE_DIR / "installer.nsi").resolve()


@pytest.mark.parametrize("change", [
    lambda c: c["bundle"].pop("windows"),  # Tauri's default is the download bootstrapper
    lambda c: c["bundle"]["windows"].update(webviewInstallMode={"type": "downloadBootstrapper"}),
    lambda c: c["bundle"]["windows"].update(webviewInstallMode={"type": "embedBootstrapper"}),
    lambda c: c["bundle"]["windows"].update(webviewInstallMode={"type": "offlineInstaller"}),
    lambda c: c["bundle"]["windows"].update(webviewInstallMode={"type": "fixedRuntime", "path": "wv2/"}),
    lambda c: c["bundle"].update(targets="all"),
    lambda c: c["bundle"].update(targets=["nsis", "msi"]),
    lambda c: c["bundle"]["windows"]["nsis"].pop("installerHooks"),
    lambda c: c["bundle"]["windows"]["nsis"].pop("template"),  # Tauri's stock template: unsafe order
    lambda c: c["bundle"]["windows"]["nsis"].update(template="installer-template/upstream/installer.nsi"),
    lambda c: c["bundle"].update(createUpdaterArtifacts=True),
    lambda c: c.setdefault("plugins", {}).update(updater={"pubkey": "x"}),
    lambda c: c["bundle"]["windows"].update(minimumWebview2Version="120.0.0.0"),
    lambda c: c.update(productName="Qronos"),  # per-user install folder = the user-data folder
    lambda c: c.update(productName=" qronos "),
    lambda c: c.update(productName="Qronos."),  # Windows opens %LOCALAPPDATA%\Qronos for this
    lambda c: c.update(productName="Qronos. ."),
    lambda c: c.update(identifier="qronos "),
    lambda c: c.update(productName="QRONOS~1"),
    lambda c: c.update(productName="NUL"),
    lambda c: c.update(identifier="com1.qronos"),
    lambda c: c.update(productName="qronos-desktop:stream"),
    lambda c: c.update(productName="..\\Qronos"),
    lambda c: c.update(identifier="QRONOS"),  # "delete app data" would remove the user-data folder
    lambda c: c["bundle"]["windows"]["nsis"].update(installMode="both"),
])
def test_a_config_that_could_download_webview2_is_refused(change):
    config = committed_config()
    change(config)

    with pytest.raises(ValueError):
        build_desktop.check_tauri_config(config)


@pytest.mark.parametrize(("bundles", "machine", "target"), [
    ("msi", "AMD64", None), ("deb", "AMD64", None), ("appimage", "AMD64", None), ("app", "AMD64", None),
    ("dmg", "AMD64", None), ("nsis", "ARM64", None), ("nsis", "AMD64", "aarch64-pc-windows-msvc"),
])
def test_only_a_windows_x64_nsis_installer_is_built(monkeypatch, bundles, machine, target):
    monkeypatch.setattr(build_desktop.platform, "machine", lambda: machine)
    monkeypatch.delenv("CARGO_BUILD_TARGET", raising=False)
    if target:
        monkeypatch.setenv("CARGO_BUILD_TARGET", target)

    with pytest.raises(ValueError, match="x86_64-pc-windows-msvc"):
        build_desktop.require_windows_x64(bundles)


@pytest.mark.parametrize("name", ["Qronos.", "Qronos ", " Qronos", "Qronos. . ", "QRONOS~1", "CON", "nul.txt",
                                  "COM1", "lpt9.log", "COM¹", "CONIN$", "a:b", "a/b", "a\\b", "a\tb", ".", "..",
                                  ""])
def test_names_windows_would_resolve_elsewhere_are_refused(name):
    assert build_desktop.folder_name_problem(name) is not None


@pytest.mark.parametrize("name", ["qronos-desktop", "com.qronos.agent", "Qronos Desktop", "con-tools", "nullable",
                                  "COM10", "Qronos.Agent"])
def test_ordinary_folder_names_are_accepted(name):
    assert build_desktop.folder_name_problem(name) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows name canonicalisation.")
@pytest.mark.parametrize("alias", ["Qronos.", "Qronos ", "Qronos. . "])
def test_windows_really_resolves_trailing_dot_and_space_aliases(tmp_path, alias):
    """Fixture evidence for I5: as the last path component (the way %LOCALAPPDATA%\\<productName>
    names the install folder) these aliases open the real folder, so they must be refused."""
    (tmp_path / "Qronos").mkdir()
    (tmp_path / "Qronos" / "state.txt").write_text("user data", encoding="utf-8")
    aliased = str(tmp_path) + "\\" + alias

    assert os.path.normcase(os.path.abspath(aliased)) == os.path.normcase(str(tmp_path / "Qronos"))
    assert os.path.isdir(aliased) and os.path.samefile(aliased, tmp_path / "Qronos")  # same file ID
    assert build_desktop.folder_name_problem(alias) is not None


def test_the_install_and_app_data_folders_are_not_the_user_data_folder():
    """The uninstaller never manages %LOCALAPPDATA%\\Qronos, where the backend keeps user data."""
    config = committed_config()
    backend_rs = (build_desktop.ROOT / "desktop" / "src-tauri" / "src" / "runtime" / "backend.rs").read_text(
        encoding="utf-8")
    config_py = (build_desktop.ROOT / "core" / "config.py").read_text(encoding="utf-8")

    assert 'local.join("Qronos")' in backend_rs and '/ "Qronos"' in config_py  # the data folder name
    assert config["productName"].casefold() != "qronos" and config["identifier"].casefold() != "qronos"
    build_desktop.check_tauri_config(config)
    # Nothing in the hooks removes recursively or by wildcard. The only removals are the
    # hook's own temporary payloads (WebView2 and the VC++ redistributable), files and folders the previous
    # installation's manifest lists, the manifest itself and the install folder when it is empty.
    hooks = (prerequisites.HOOK_PATH.read_text(encoding="utf-8")
             + (prerequisites.HOOK_PATH.parent / "qronos-install-root.nsh").read_text(encoding="utf-8"))
    removals = {line.strip() for line in hooks.splitlines() if line.strip().lower().startswith(("rmdir", "delete"))}
    assert removals == {'RMDir "$INSTDIR"', 'Delete "$PLUGINSDIR\\${QRONOS_WEBVIEW2_FILE}"',
                        'Delete "$PLUGINSDIR\\${QRONOS_VCREDIST_FILE}"',
                        'Delete "$INSTDIR\\$QronosPath"', 'RMDir "$INSTDIR\\$QronosPath"',
                        'Delete "$INSTDIR\\${QRONOS_MANIFEST}"'}


# ----------------------------------------------------------- wiring


class FakeBuild:
    """Stands in for npm/Tauri/7-Zip; records order and writes what Tauri would."""

    def __init__(self, tmp_path: Path, pin: dict, *, embedded_sha: str | None = None,
                 mode: str = "") -> None:
        self.tmp_path, self.pin, self.calls = tmp_path, pin, []
        self.embedded_sha = embedded_sha or pin["sha256"]
        self.vc_pin = prerequisites.load_vcredist_pin()
        self.embedded_vc_sha = self.vc_pin["sha256"]
        self.mode = mode
        self.overlays: list[dict] = []
        self.records: list[dict] = []
        self.install_manifests: list = []

    def run(self, command, **kwargs):
        self.calls.append("tauri")
        assert command[command.index("--bundles") + 1] == "nsis"
        assert command[-2:] == ["--locked", "--offline"]  # Cargo may not fetch crates during the build
        overlay = json.loads(Path(command[command.index("--config") + 1]).read_text(encoding="utf-8"))
        self.overlays.append(overlay)
        release = Path(kwargs["env"]["CARGO_TARGET_DIR"]) / "release"
        (release / "nsis" / "x64").mkdir(parents=True, exist_ok=True)
        wrapper = overlay["bundle"]["windows"]["nsis"]["installerHooks"]
        (release / "nsis" / "x64" / "installer.nsi").write_text(rendered_template(wrapper, self.mode),
                                                                encoding="utf-8-sig")
        (release / "bundle" / "nsis").mkdir(parents=True, exist_ok=True)
        (release / "bundle" / "nsis" / "qronos-desktop_0.1.0_x64-setup.exe").write_bytes(b"MZ installer")
        return subprocess.CompletedProcess(command, 0)


def rendered_template(wrapper: str, mode: str = "") -> str:
    """The committed Qronos template rendered the way Tauri does (tests/fixtures/nsis_template.py)."""
    from tests.fixtures.nsis_template import render

    template = (prerequisites.TEMPLATE_DIR / "installer.nsi").read_text(encoding="utf-8")
    context = {name: "" for name in re.findall(r"\{\{(\w+)\}\}", template)}
    context.update({"compression": "lzma", "languages": ["English"], "language_files": [], "resources_dirs": [],
                    "resources": {}, "binaries": {}, "file_associations": [], "deep_link_protocols": [],
                    "resources_ancestors": [], "installer_hooks": str(wrapper), "install_webview2_mode": mode,
                    "version": "0.1.0", "product_name": "qronos-desktop"})
    return render(template, context)


def wire(tmp_path: Path, monkeypatch, fake: FakeBuild) -> Path:
    backend = tmp_path / "qronos-runtime"
    backend.mkdir(exist_ok=True)
    (backend / "runtime-manifest.json").write_text(json.dumps({"files": [{"path": "qronos-runtime.exe"}]}),
                                                   encoding="utf-8")
    monkeypatch.setattr(build_desktop, "DESKTOP_BUILDS_ROOT", tmp_path / "runtime/package/desktop-builds")
    monkeypatch.setattr(build_desktop, "verify_installer",
                        lambda path, pin: fake.calls.append("prerequisite") or {"sha256": pin["sha256"]})
    monkeypatch.setattr(build_desktop, "verify_vcredist_installer",
                        lambda path, pin: fake.calls.append("vcredist") or {"sha256": pin["sha256"]})
    monkeypatch.setattr(build_desktop, "find_seven_zip", lambda: tmp_path / "7z.exe")
    monkeypatch.setattr(build_desktop, "tauri_tools_dir", lambda: tmp_path / "tools")
    monkeypatch.setattr(build_desktop, "verify_build_tools",
                        lambda tools, version: {"nsisToolDir": str(tools), "version": version})
    monkeypatch.setattr(build_desktop, "verify_manifest", lambda path: fake.calls.append("backend"))
    monkeypatch.setattr(build_desktop.shutil, "which", lambda name: "npm")

    def collect(out_dir):
        fake.calls.append("notices")
        out_dir.mkdir()
        (out_dir / "THIRD_PARTY_NOTICES-desktop.txt").write_text("notices", encoding="utf-8")
        (out_dir / "third-party-manifest-desktop.json").write_text(
            json.dumps({"files": [{"path": "THIRD_PARTY_NOTICES-desktop.txt"}]}), encoding="utf-8")

    monkeypatch.setattr(build_desktop, "collect_desktop_notices", collect)
    monkeypatch.setattr(build_desktop, "verify_notice_output", lambda path: fake.calls.append("notices-verified"))
    monkeypatch.setattr(build_desktop.subprocess, "run", fake.run)
    monkeypatch.setattr(build_desktop, "list_installer",
                        lambda setup, tool: fake.calls.append("listed") or {
                            EMBEDDED_PATH: fake.pin["bytes"],
                            prerequisites.VCREDIST_EMBEDDED_PATH: fake.vc_pin["bytes"]})
    monkeypatch.setattr(build_desktop, "extract_and_hash_payload",
                        lambda setup, tool, scratch: fake.calls.append("payload-hashed") or fake.embedded_sha)
    monkeypatch.setattr(build_desktop, "extract_and_hash_vcredist",
                        lambda setup, tool, scratch: fake.calls.append("vcredist-hashed") or fake.embedded_vc_sha)

    def extract(setup, tool, destination):
        fake.calls.append("extracted")
        assert setup.parent == destination.parent  # the kept copy, beside its evidence
        destination.mkdir()
        return destination

    monkeypatch.setattr(build_desktop, "extract_installer", extract)
    def verify_extracted(root, pin, install_manifest=None, vcredist_pin=None):
        assert vcredist_pin == fake.vc_pin  # the build always checks the embedded VC++ bytes too
        fake.calls.append("extracted-verified")
        fake.install_manifests.append(install_manifest)
        return {"root": root}

    monkeypatch.setattr(build_desktop, "verify_extracted", verify_extracted)

    def record(**kwargs):
        fake.calls.append("provenance")
        fake.records.append(kwargs)
        return {"installer": {"sha256": "x"}, "attributableToDesktopSource": False}

    monkeypatch.setattr(build_desktop, "provenance_record", record)
    monkeypatch.delenv("CARGO_BUILD_TARGET", raising=False)
    return backend


@pytest.mark.skipif(os.name != "nt", reason="The desktop installer is built on Windows only.")
def test_a_build_verifies_the_prerequisite_first_and_the_installer_last(tmp_path, monkeypatch):
    fake = FakeBuild(tmp_path, committed_pin())
    backend = wire(tmp_path, monkeypatch, fake)

    first = build_desktop.build(backend, target_dir=tmp_path / "t", work_root=tmp_path / "w",
                                backend_source="7e12e2b (builder statement)")
    second = build_desktop.build(backend, target_dir=tmp_path / "t", work_root=tmp_path / "w")

    assert fake.calls == ["prerequisite", "vcredist", "backend", "notices", "notices-verified", "tauri", "listed",
                          "payload-hashed", "vcredist-hashed", "extracted", "extracted-verified", "provenance"] * 2
    assert first["work"] != second["work"] and first["work"].parent == second["work"].parent == tmp_path / "w"
    # Each build keeps its own installer copy and provenance record; nothing is overwritten.
    for result in (first, second):
        assert result["installer"] == result["work"] / "qronos-desktop_0.1.0_x64-setup.exe"
        assert result["installer"].read_bytes() == b"MZ installer"
        assert json.loads((result["work"] / "installer-provenance.json").read_text(encoding="utf-8")) ==             {"installer": {"sha256": "x"}, "attributableToDesktopSource": False}
    assert fake.records[0]["backend_source"] == "7e12e2b (builder statement)"
    assert fake.records[1]["backend_source"] == "not stated" and fake.records[0]["purpose"] == "packaging-check"
    overlay = fake.overlays[0]
    resources = overlay["bundle"]["resources"]
    assert resources[backend.resolve().as_posix() + "/"] == "qronos-runtime/"
    assert resources[(first["work"] / "notices").as_posix() + "/"] == "third-party-notices/"
    wrapper = Path(overlay["bundle"]["windows"]["nsis"]["installerHooks"])
    assert wrapper.parent == first["work"]
    ownership = first["work"] / "qronos-install-manifest.txt"
    assert resources[ownership.as_posix()] == "qronos-install-manifest.txt"
    assert fake.install_manifests[0] == ownership.read_bytes()
    assert install_manifest.parse(ownership.read_bytes())[0] == [
        "qronos-desktop.exe", "qronos-install-manifest.txt", "qronos-runtime\\qronos-runtime.exe",
        "qronos-runtime\\runtime-manifest.json", "third-party-notices\\third-party-manifest-desktop.json",
        "third-party-notices\\THIRD_PARTY_NOTICES-desktop.txt", "uninstall.exe"]  # case-insensitive order
    assert str(build_desktop.CACHE_DIR / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe") in \
        wrapper.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="The desktop installer is built on Windows only.")
def test_a_bad_cached_prerequisite_stops_before_anything_is_built(tmp_path, monkeypatch):
    fake = FakeBuild(tmp_path, committed_pin())
    backend = wire(tmp_path, monkeypatch, fake)

    def refuse(path, pin):
        raise PrerequisiteError("does not match the pin")

    monkeypatch.setattr(build_desktop, "verify_installer", refuse)

    with pytest.raises(PrerequisiteError):
        build_desktop.build(backend, target_dir=tmp_path / "t")
    assert fake.calls == []
    assert not (tmp_path / "runtime").exists()


@pytest.mark.skipif(os.name != "nt", reason="The desktop installer is built on Windows only.")
@pytest.mark.parametrize(("mode", "embedded", "message"), [
    ("downloadBootstrapper", None, "INSTALLWEBVIEW2MODE"),
    ("", "0" * 64, "does not match the pin"),
])
def test_a_built_installer_that_differs_from_the_pin_is_refused(tmp_path, monkeypatch, mode, embedded, message):
    fake = FakeBuild(tmp_path, committed_pin(), embedded_sha=embedded, mode=mode)
    backend = wire(tmp_path, monkeypatch, fake)

    with pytest.raises(PrerequisiteError, match=message):
        build_desktop.build(backend, target_dir=tmp_path / "t", work_root=tmp_path / "w")
    assert_quarantined(tmp_path)


CANONICAL = Path("t") / "release" / "bundle" / "nsis" / "qronos-desktop_0.1.0_x64-setup.exe"


def assert_quarantined(tmp_path: Path) -> None:
    """No unmarked installer anywhere: not at Tauri's output path, not in the build folder."""
    assert not (tmp_path / CANONICAL).exists()
    (work,) = (tmp_path / "w").iterdir()
    assert not (work / CANONICAL.name).exists()
    assert (work / (CANONICAL.name + ".UNVERIFIED")).read_bytes() == b"MZ installer"
    assert not (work / "installer-provenance.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="The desktop installer is built on Windows only.")
def test_a_provenance_failure_quarantines_the_installer(tmp_path, monkeypatch):
    fake = FakeBuild(tmp_path, committed_pin())
    backend = wire(tmp_path, monkeypatch, fake)

    def refuse(**kwargs):
        raise OSError("git is unavailable")

    monkeypatch.setattr(build_desktop, "provenance_record", refuse)

    with pytest.raises(OSError):
        build_desktop.build(backend, target_dir=tmp_path / "t", work_root=tmp_path / "w")
    assert_quarantined(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="The desktop installer is built on Windows only.")
def test_an_extraction_failure_quarantines_the_installer(tmp_path, monkeypatch):
    fake = FakeBuild(tmp_path, committed_pin())
    backend = wire(tmp_path, monkeypatch, fake)

    def refuse(root, pin, install_manifest=None, vcredist_pin=None):
        raise PrerequisiteError("qronos-runtime does not match its manifest")

    monkeypatch.setattr(build_desktop, "verify_extracted", refuse)

    with pytest.raises(PrerequisiteError, match="does not match its manifest"):
        build_desktop.build(backend, target_dir=tmp_path / "t", work_root=tmp_path / "w")
    assert_quarantined(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="The desktop installer is built on Windows only.")
def test_a_tauri_failure_after_bundling_quarantines_the_output(tmp_path, monkeypatch):
    fake = FakeBuild(tmp_path, committed_pin())
    backend = wire(tmp_path, monkeypatch, fake)

    def run_then_fail(command, **kwargs):
        fake.run(command, **kwargs)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(build_desktop.subprocess, "run", run_then_fail)

    with pytest.raises(subprocess.CalledProcessError):
        build_desktop.build(backend, target_dir=tmp_path / "t", work_root=tmp_path / "w")
    assert not (tmp_path / CANONICAL).exists()
    assert (tmp_path / (str(CANONICAL) + ".UNVERIFIED")).read_bytes() == b"MZ installer"


@pytest.mark.skipif(os.name != "nt", reason="The desktop installer is built on Windows only.")
def test_a_stale_installer_at_the_output_path_is_quarantined_first(tmp_path, monkeypatch):
    fake = FakeBuild(tmp_path, committed_pin())
    backend = wire(tmp_path, monkeypatch, fake)
    stale = tmp_path / CANONICAL
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"MZ from a manual tauri build")
    (stale.parent / (stale.name + ".UNVERIFIED")).write_bytes(b"MZ older refusal")

    result = build_desktop.build(backend, target_dir=tmp_path / "t", work_root=tmp_path / "w")

    assert not stale.exists()
    assert (stale.parent / (stale.name + ".UNVERIFIED")).read_bytes() == b"MZ older refusal"  # never overwritten
    assert (stale.parent / (stale.name + ".1.UNVERIFIED")).read_bytes() == b"MZ from a manual tauri build"
    assert result["installer"].read_bytes() == b"MZ installer"
    assert (result["work"] / "installer-provenance.json").is_file()


# ------------------------------------------------ the extracted installer


def extracted_tree(tmp_path: Path, *, desktop_licence: str = "repo", runtime_licence: str = "repo",
                   artwork: str = "repo") -> tuple[Path, dict]:
    """A synthetic extracted installer built with the real runtime-manifest and notice writers.

    Both carry Qronos's own LICENSE and NOTICE ("repo"); "none" leaves them out, and "other-notice" ships a
    different (still valid) NOTICE, with every manifest consistent either way."""
    from build_support import build_runtime, project_licence
    from build_support import desktop_notices as notices

    licence = project_licence.read()
    variants = {"repo": licence, "none": None,
                "other-notice": {**licence, "NOTICE": licence["NOTICE"] + b"Another valid line.\n"}}

    root = tmp_path / "x"
    payload = b"MZ stand-in for the pinned WebView2 installer"
    pin = fake_pin(payload)
    (root / "$PLUGINSDIR").mkdir(parents=True)
    (root / "$PLUGINSDIR" / pin["fileName"]).write_bytes(payload)
    (root / "$PLUGINSDIR" / "System.dll").write_bytes(b"MZ nsis plug-in")
    (root / "qronos-desktop.exe").write_bytes(b"MZ desktop")
    (root / "uninstall.exe").write_bytes(b"MZ uninstaller")
    runtime = root / "qronos-runtime"
    (runtime / "_internal").mkdir(parents=True)
    (runtime / "qronos-runtime.exe").write_bytes(b"MZ runtime")
    (runtime / "_internal" / "python313.dll").write_bytes(b"MZ python")
    if variants[runtime_licence] is not None:
        (runtime / "third-party-notices" / "qronos").mkdir(parents=True)
        for name, data in variants[runtime_licence].items():
            (runtime / "third-party-notices" / "qronos" / name).write_bytes(data)
    build_runtime.write_manifest(runtime)
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "LICENSE").write_text("Permission is hereby granted, free of charge, to any person.\n"
                                     "THE SOFTWARE IS PROVIDED AS IS.\n", encoding="utf-8")
    component = notices.Component("rust", "demo", "1.0.0", "MIT", None,
                                  "registry+https://github.com/rust-lang/crates.io-index", "sum", package.resolve())
    files = notices.check_coverage([component], {})
    notices.write_notices([component], files, {}, root / "third-party-notices", notices.WINDOWS_TARGET, (),
                          project=variants[desktop_licence],
                          artwork=notices.load_artwork() if artwork == "repo" else None)
    install_manifest.write(root, install_manifest.payload_files(runtime, root / "third-party-notices",
                                                                "qronos-desktop"))
    return root, pin


def test_an_extracted_installer_is_verified_byte_for_byte(tmp_path):
    root, pin = extracted_tree(tmp_path)

    evidence = installer_verify.verify_extracted(root, pin)

    assert evidence["topLevel"] == sorted(installer_verify.EXPECTED_TOP_LEVEL)
    assert evidence["webview2Installer"]["sha256"] == pin["sha256"]
    assert evidence["runtimeFiles"] == 4 and evidence["desktopNoticeFiles"] >= 5
    assert evidence["plugins"] == ["MicrosoftEdgeWebView2RuntimeInstallerX64.exe", "System.dll"]
    assert evidence["projectLicence"]["license"] == "Apache-2.0"
    assert evidence["projectLicence"]["files"]["LICENSE"] == {
        "bytes": 11358, "sha256": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"}
    assert evidence["projectLicence"]["locations"] == ["qronos-runtime/third-party-notices/qronos",
                                                       "third-party-notices/qronos"]
    assert evidence["thirdPartyArtworkAttributed"] == 19


def test_an_installer_whose_notices_do_not_attribute_the_template_artwork_is_refused(tmp_path):
    # Every manifest is consistent; only the artwork attribution is missing.
    root, pin = extracted_tree(tmp_path, artwork="none")

    with pytest.raises(PrerequisiteError, match="do not attribute exactly the third-party artwork"):
        installer_verify.verify_extracted(root, pin)


@pytest.mark.parametrize(("desktop", "runtime", "message"), [
    ("none", "repo", "does not carry Qronos's own LICENSE and NOTICE"),
    ("repo", "none", "does not carry Qronos's own LICENSE and NOTICE"),
    ("repo", "other-notice", "copies of Qronos's LICENSE and NOTICE differ"),
])
def test_an_installer_without_the_project_licence_in_both_distributions_is_refused(tmp_path, desktop, runtime,
                                                                                    message):
    # Every manifest is consistent here, so only the licence check can refuse.
    root, pin = extracted_tree(tmp_path, desktop_licence=desktop, runtime_licence=runtime)

    with pytest.raises(PrerequisiteError, match=message):
        installer_verify.verify_extracted(root, pin)


@pytest.mark.parametrize(("damage", "message"), [
    (lambda root: (root / "models").mkdir(), "unexpected top-level items: models"),
    (lambda root: (root / "Qwen.gguf").write_bytes(b"weights"), "unexpected top-level items: Qwen.gguf"),
    (lambda root: (root / "$PLUGINSDIR" / "NSISdl.dll").write_bytes(b"MZ"), "download plug-in"),
    (lambda root: (root / "$PLUGINSDIR" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").write_bytes(b"MZ other"),
     "does not match the pin"),
    (lambda root: (root / "$PLUGINSDIR" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").unlink(),
     "does not contain the WebView2 installer"),
    (lambda root: (root / "qronos-runtime" / "_internal" / "python313.dll").write_bytes(b"MZ swapped"),
     "qronos-runtime does not match its manifest"),
    (lambda root: (root / "qronos-runtime" / "_internal" / "extra.pyd").write_bytes(b"MZ"),
     "qronos-runtime does not match its manifest"),
    (lambda root: (root / "third-party-notices" / "licenses" / "rust" / "demo" / "1.0.0" / "LICENSE")
     .write_bytes(b"edited"), "does not match its manifest"),
])
def test_a_damaged_or_unexpected_extracted_installer_is_refused(tmp_path, damage, message):
    root, pin = extracted_tree(tmp_path)
    damage(root)

    with pytest.raises(PrerequisiteError, match=message):
        installer_verify.verify_extracted(root, pin)


def test_a_missing_notice_resource_is_refused(tmp_path):
    root, pin = extracted_tree(tmp_path)
    for path in sorted((root / "third-party-notices").rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    (root / "third-party-notices").rmdir()

    with pytest.raises(PrerequisiteError, match="missing: third-party-notices"):
        installer_verify.verify_extracted(root, pin)


def test_provenance_states_what_was_and_was_not_verified(tmp_path):
    root, pin = extracted_tree(tmp_path)
    setup = tmp_path / "qronos-desktop_0.1.0_x64-setup.exe"
    setup.write_bytes(b"MZ installer")
    extracted = installer_verify.verify_extracted(root, pin)

    record = installer_verify.provenance_record(setup=setup, root=build_desktop.ROOT, pin=pin, extracted=extracted,
                                                backend_source="7e12e2b", purpose="packaging-check",
                                                tauri_cli="2.11.4")

    assert record["installer"] == {"fileName": setup.name, "bytes": 12, "sha256": sha256(b"MZ installer"),
                                   "signed": False}
    assert record["projectLicence"] == extracted["projectLicence"]
    assert record["projectLicence"]["license"] == "Apache-2.0"
    assert record["backend"]["sourceAsStatedByBuilder"] == "7e12e2b"
    assert record["backend"]["sourceVerifiedFromBundle"] is False
    assert len(record["desktopSource"]["commit"]) == 40
    assert record["attributableToDesktopSource"] == record["desktopSource"]["clean"]
    assert record["webview2"]["runtimeVersion"] == "154.0.4258.62"
    assert any("clean Windows machine" in item for item in record["notVerifiedHere"])
    installer_verify.write_record(tmp_path / "p.json", record)
    with pytest.raises(FileExistsError):
        installer_verify.write_record(tmp_path / "p.json", record)


def test_source_state_ignores_line_ending_rewrites_but_not_real_changes(tmp_path):
    """Tauri's CLI rewrites Cargo.toml with LF endings; only real content or untracked files count."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "core.autocrlf=true", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "core.autocrlf", "true")
    (repo / "Cargo.toml").write_bytes(b"[package]\r\nname = \"demo\"\r\n")
    git("add", "Cargo.toml")
    git("commit", "-q", "-m", "fixture")

    (repo / "Cargo.toml").write_bytes(b"[package]\nname = \"demo\"\n")  # line endings only
    state = installer_verify.source_state(repo)
    assert state["clean"] is True and state["changedPaths"] == [] and len(state["commit"]) == 40

    (repo / "Cargo.toml").write_bytes(b"[package]\nname = \"other\"\n")
    assert installer_verify.source_state(repo)["changedPaths"] == ["Cargo.toml"]

    (repo / "Cargo.toml").write_bytes(b"[package]\r\nname = \"demo\"\r\n")
    (repo / "extra.rs").write_text("fn main() {}\n", encoding="utf-8")
    state = installer_verify.source_state(repo)
    assert state["clean"] is False and state["changedPaths"] == ["extra.rs"]


def test_source_state_outside_a_repository_is_not_clean(tmp_path):
    state = installer_verify.source_state(tmp_path)

    assert state["clean"] is False and state["commit"] == "unavailable"


# ------------------------------------- the hook's launch handling (I4)
#
# A throwaway NSIS probe, compiled into tmp_path, runs only the hook's
# QRONOS_WEBVIEW2_RUN_INSTALLER macro against a stand-in payload. It is not the
# Qronos installer: it installs nothing, reads no registry value and runs no
# Microsoft installer. Each stand-in is either not an executable at all or a
# tiny compiled program whose only action is to set its exit code.


def makensis(script: Path) -> None:
    if MAKENSIS is None or not MAKENSIS.is_file():
        pytest.skip("Tauri's NSIS compiler is not installed on this machine.")
    result = subprocess.run([str(MAKENSIS), "/V1", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def exit_code_stub(tmp_path: Path, code: int) -> Path:
    script = tmp_path / f"stub{code}.nsi"
    stub = tmp_path / f"stub{code}" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    stub.parent.mkdir()
    script.write_text("\n".join(("Unicode true", 'Name "stub"', f'OutFile "{stub}"', "RequestExecutionLevel user",
                                 "SilentInstall silent", "Section", f"  SetErrorLevel {code}", "SectionEnd", "")),
                      encoding="utf-8")
    makensis(script)
    return stub


def run_probe(tmp_path: Path, payload: Path) -> tuple[str, Path]:
    install_dir = tmp_path / "instdir"
    result_file = tmp_path / "result.txt"
    script = tmp_path / "probe.nsi"
    script.write_text("\n".join((
        "Unicode true", "SetCompressor /SOLID lzma", 'Name "probe"', f'OutFile "{tmp_path / "probe.exe"}"',
        "RequestExecutionLevel user", "SilentInstall silent", f'InstallDir "{install_dir}"',
        f'!define QRONOS_WEBVIEW2_INSTALLER "{payload}"', '!define QRONOS_WEBVIEW2_VERSION "1.2.3.4"',
        f'!define QRONOS_VCREDIST_INSTALLER "{payload}"', '!define QRONOS_VCREDIST_VERSION "14.0.0.0"',
        '!define QRONOS_VCREDIST_MAJOR "14"', '!define QRONOS_VCREDIST_MINOR "0"', '!define QRONOS_VCREDIST_BUILD "0"',
        f'!define QRONOS_PROBE_RESULT "{result_file}"', f'!include "{prerequisites.HOOK_PATH}"',
        "Section", "  !insertmacro QRONOS_WEBVIEW2_RUN_INSTALLER",  # before SetOutPath (Qronos template)
        '  FileOpen $R7 "${QRONOS_PROBE_RESULT}" w', '  FileWrite $R7 "ran, exit code $R8"', "  FileClose $R7",
        "SectionEnd", "")), encoding="utf-8")
    makensis(script)
    subprocess.run([str(tmp_path / "probe.exe")], check=False, timeout=120)
    return result_file.read_text(encoding="utf-16" if result_file.read_bytes()[:2] == b"\xff\xfe" else "utf-8",
                                 errors="replace").lstrip("\ufeff"), install_dir


@pytest.mark.skipif(os.name != "nt", reason="NSIS probes run on Windows only.")
def test_an_installer_that_cannot_start_is_reported_as_such(tmp_path):
    payload = tmp_path / "payload" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    payload.parent.mkdir()
    payload.write_text("not a program", encoding="utf-8")

    message, install_dir = run_probe(tmp_path, payload)

    assert message == ("The Microsoft Edge WebView2 Runtime installer could not be started. "
                       "Qronos was not installed.")
    assert not install_dir.exists()  # nothing was created: the Qronos template runs this before SetOutPath


@pytest.mark.skipif(os.name != "nt", reason="NSIS probes run on Windows only.")
def test_a_failing_installer_reports_its_exit_code(tmp_path):
    message, install_dir = run_probe(tmp_path, exit_code_stub(tmp_path, 7))

    assert message == ("The Microsoft Edge WebView2 Runtime could not be installed (exit code 7). "
                       "Qronos was not installed.")
    assert not install_dir.exists()


@pytest.mark.skipif(os.name != "nt", reason="NSIS probes run on Windows only.")
def test_a_successful_run_continues_to_the_registry_check(tmp_path):
    message, install_dir = run_probe(tmp_path, exit_code_stub(tmp_path, 0))

    assert message == "ran, exit code 0"
    assert not install_dir.exists()  # the install folder is created only later, by the template's SetOutPath


@pytest.mark.skipif(os.name != "nt", reason="NSIS probes run on Windows only.")
def test_a_failure_never_removes_a_folder_that_holds_files(tmp_path):
    install_dir = tmp_path / "instdir"
    install_dir.mkdir()
    (install_dir / "keep.txt").write_text("user file", encoding="utf-8")
    payload = tmp_path / "payload" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    payload.parent.mkdir()
    payload.write_text("not a program", encoding="utf-8")

    message, _ = run_probe(tmp_path, payload)

    assert "could not be started" in message
    assert (install_dir / "keep.txt").read_text(encoding="utf-8") == "user file"


def test_the_hook_clears_errors_and_the_exit_code_before_running_the_installer():
    hook = prerequisites.HOOK_PATH.read_text(encoding="utf-8")
    run = hook[hook.index("!macro QRONOS_WEBVIEW2_RUN_INSTALLER"):]
    run = run[:run.index("!macroend")]
    order = [run.index(step) for step in ('StrCpy $R8 ""', "ClearErrors", "ExecWait", "${If} ${Errors}")]
    assert order == sorted(order)
    preinstall = hook[hook.index("!macro NSIS_HOOK_PREINSTALL"):]
    # The registry is checked again after the installer reports success.
    assert preinstall.index("QRONOS_WEBVIEW2_RUN_INSTALLER") < preinstall.rindex("QRONOS_WEBVIEW2_VERSION_TO_R9")
    assert "Abort" not in preinstall.replace("!insertmacro QRONOS_FAIL", "")  # every stop goes through QRONOS_FAIL


def test_the_hooks_never_change_the_output_path():
    """A SetOutPath in a hook makes archive readers (7-Zip) place every later File instruction under
    that path; a packaging-check build at 0fc6fca was refused for exactly this ("$TEMP" at the top)."""
    for hook in (prerequisites.HOOK_PATH, prerequisites.HOOK_PATH.parent / "qronos-install-root.nsh"):
        instructions = [line.strip() for line in hook.read_text(encoding="utf-8").splitlines()
                        if line.strip() and not line.strip().startswith(";")]
        assert not [line for line in instructions if line.lower().startswith("setoutpath")], hook.name
