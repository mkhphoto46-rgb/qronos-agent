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
import subprocess
from pathlib import Path

import pytest

from build_support import build_desktop
from build_support import desktop_installer_verify as installer_verify
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

    evidence = prerequisites.verify_installer(cached, fake_pin(data), trust=trusted.append)

    assert trusted == [cached]
    assert evidence["sha256"] == sha256(data) and evidence["runtimeVersion"] == "154.0.4258.62"

    with pytest.raises(PrerequisiteError, match="bytes; the pin says"):
        prerequisites.verify_installer(cached, {**fake_pin(data), "bytes": len(data) + 1}, trust=trusted.append)
    cached.write_bytes(data[:-1] + b"X")  # same size, one byte changed
    with pytest.raises(PrerequisiteError, match="does not match the pin"):
        prerequisites.verify_installer(cached, fake_pin(data), trust=trusted.append)

    cached.write_bytes(data)

    def untrusted(path):
        raise PrerequisiteError("no valid Authenticode signature")

    with pytest.raises(PrerequisiteError, match="Authenticode"):
        prerequisites.verify_installer(cached, fake_pin(data), trust=untrusted)


def test_a_misnamed_missing_or_linked_installer_is_refused(tmp_path):
    data = b"MZ pinned installer bytes"
    real = tmp_path / "real"
    real.mkdir()
    (real / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").write_bytes(data)
    (real / "other.exe").write_bytes(data)

    with pytest.raises(PrerequisiteError, match="must be named"):
        prerequisites.verify_installer(real / "other.exe", fake_pin(data), trust=lambda path: None)
    with pytest.raises(PrerequisiteError, match="missing"):
        prerequisites.verify_installer(tmp_path / "absent" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
                                       fake_pin(data), trust=lambda path: None)
    junction(real, tmp_path / "linked")
    with pytest.raises(PrerequisiteError, match="link"):
        prerequisites.verify_installer(tmp_path / "linked" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
                                       fake_pin(data), trust=lambda path: None)


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
    tampered = tmp_path / pin["fileName"]
    data = bytearray(REAL_INSTALLER.read_bytes())
    data[len(data) // 2] ^= 0xFF
    tampered.write_bytes(bytes(data))
    with pytest.raises(PrerequisiteError, match="no valid Authenticode signature"):
        prerequisites.winverifytrust(tampered)  # the signature itself fails, not only the pin
    with pytest.raises(PrerequisiteError, match="does not match the pin"):
        prerequisites.verify_installer(tampered, pin)


# ------------------------------------------------------- the NSIS hook


def test_the_hook_wrapper_names_the_verified_file_and_refuses_unsafe_paths(tmp_path):
    pin = committed_pin()
    wrapper = prerequisites.write_hook_wrapper(tmp_path, tmp_path / pin["fileName"], pin)

    text = wrapper.read_text(encoding="utf-8")
    assert f'!define QRONOS_WEBVIEW2_INSTALLER "{tmp_path / pin["fileName"]}"' in text
    assert f'!define QRONOS_WEBVIEW2_VERSION "{pin["runtime"]["version"]}"' in text
    assert f'!include "{prerequisites.HOOK_PATH}"' in text
    with pytest.raises(FileExistsError):
        prerequisites.write_hook_wrapper(tmp_path, tmp_path / pin["fileName"], pin)
    for unsafe in ('a"b', "a$b", "a\nb", "a;b"):
        with pytest.raises(PrerequisiteError, match="NSIS"):
            prerequisites.hook_wrapper(tmp_path / unsafe / pin["fileName"], pin)


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

    result = compile_hook(tmp_path, prerequisites.hook_wrapper(payload, pin))

    assert result.returncode == 0, result.stdout + result.stderr
    tool = seven_zip()
    entries = prerequisites.list_installer(tmp_path / "probe.exe", tool)
    assert EMBEDDED_PATH in entries  # 7-Zip may not know the size of a solid stream's last entry
    prerequisites.verify_embedded_payload(entries, pin)
    assert prerequisites.extract_and_hash_payload(tmp_path / "probe.exe", tool, tmp_path / "out") == pin["sha256"]
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


@pytest.mark.parametrize("change", [
    lambda c: c["bundle"].pop("windows"),  # Tauri's default is the download bootstrapper
    lambda c: c["bundle"]["windows"].update(webviewInstallMode={"type": "downloadBootstrapper"}),
    lambda c: c["bundle"]["windows"].update(webviewInstallMode={"type": "embedBootstrapper"}),
    lambda c: c["bundle"]["windows"].update(webviewInstallMode={"type": "offlineInstaller"}),
    lambda c: c["bundle"]["windows"].update(webviewInstallMode={"type": "fixedRuntime", "path": "wv2/"}),
    lambda c: c["bundle"].update(targets="all"),
    lambda c: c["bundle"].update(targets=["nsis", "msi"]),
    lambda c: c["bundle"]["windows"]["nsis"].pop("installerHooks"),
    lambda c: c["bundle"].update(createUpdaterArtifacts=True),
    lambda c: c.setdefault("plugins", {}).update(updater={"pubkey": "x"}),
    lambda c: c["bundle"]["windows"].update(minimumWebview2Version="120.0.0.0"),
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


# ----------------------------------------------------------- wiring


class FakeBuild:
    """Stands in for npm/Tauri/7-Zip; records order and writes what Tauri would."""

    def __init__(self, tmp_path: Path, pin: dict, *, embedded_sha: str | None = None,
                 mode: str = "") -> None:
        self.tmp_path, self.pin, self.calls = tmp_path, pin, []
        self.embedded_sha = embedded_sha or pin["sha256"]
        self.mode = mode
        self.overlays: list[dict] = []
        self.records: list[dict] = []

    def run(self, command, **kwargs):
        self.calls.append("tauri")
        assert command[command.index("--bundles") + 1] == "nsis"
        overlay = json.loads(Path(command[command.index("--config") + 1]).read_text(encoding="utf-8"))
        self.overlays.append(overlay)
        release = Path(kwargs["env"]["CARGO_TARGET_DIR"]) / "release"
        (release / "nsis" / "x64").mkdir(parents=True, exist_ok=True)
        wrapper = overlay["bundle"]["windows"]["nsis"]["installerHooks"]
        (release / "nsis" / "x64" / "installer.nsi").write_text(
            GOOD_SCRIPT.format(wrapper=wrapper).replace('INSTALLWEBVIEW2MODE ""', f'INSTALLWEBVIEW2MODE "{self.mode}"'),
            encoding="utf-8")
        (release / "bundle" / "nsis").mkdir(parents=True, exist_ok=True)
        (release / "bundle" / "nsis" / "qronos-desktop_0.1.0_x64-setup.exe").write_bytes(b"MZ installer")
        return subprocess.CompletedProcess(command, 0)


def wire(tmp_path: Path, monkeypatch, fake: FakeBuild) -> Path:
    backend = tmp_path / "qronos-runtime"
    backend.mkdir(exist_ok=True)
    monkeypatch.setattr(build_desktop, "DESKTOP_BUILDS_ROOT", tmp_path / "runtime/package/desktop-builds")
    monkeypatch.setattr(build_desktop, "verify_installer",
                        lambda path, pin: fake.calls.append("prerequisite") or {"sha256": pin["sha256"]})
    monkeypatch.setattr(build_desktop, "find_seven_zip", lambda: tmp_path / "7z.exe")
    monkeypatch.setattr(build_desktop, "verify_manifest", lambda path: fake.calls.append("backend"))
    monkeypatch.setattr(build_desktop.shutil, "which", lambda name: "npm")

    def collect(out_dir):
        fake.calls.append("notices")
        out_dir.mkdir()
        (out_dir / "THIRD_PARTY_NOTICES-desktop.txt").write_text("notices", encoding="utf-8")

    monkeypatch.setattr(build_desktop, "collect_desktop_notices", collect)
    monkeypatch.setattr(build_desktop, "verify_notice_output", lambda path: fake.calls.append("notices-verified"))
    monkeypatch.setattr(build_desktop.subprocess, "run", fake.run)
    monkeypatch.setattr(build_desktop, "list_installer",
                        lambda setup, tool: fake.calls.append("listed") or {EMBEDDED_PATH: fake.pin["bytes"]})
    monkeypatch.setattr(build_desktop, "extract_and_hash_payload",
                        lambda setup, tool, scratch: fake.calls.append("payload-hashed") or fake.embedded_sha)

    def extract(setup, tool, destination):
        fake.calls.append("extracted")
        assert setup.parent == destination.parent  # the kept copy, beside its evidence
        destination.mkdir()
        return destination

    monkeypatch.setattr(build_desktop, "extract_installer", extract)
    monkeypatch.setattr(build_desktop, "verify_extracted",
                        lambda root, pin: fake.calls.append("extracted-verified") or {"root": root})

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

    assert fake.calls == ["prerequisite", "backend", "notices", "notices-verified", "tauri", "listed",
                          "payload-hashed", "extracted", "extracted-verified", "provenance"] * 2
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
        build_desktop.build(backend, target_dir=tmp_path / "t")


# ------------------------------------------------ the extracted installer


def extracted_tree(tmp_path: Path) -> tuple[Path, dict]:
    """A synthetic extracted installer built with the real runtime-manifest and notice writers."""
    from build_support import build_runtime
    from build_support import desktop_notices as notices

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
    build_runtime.write_manifest(runtime)
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "LICENSE").write_text("Permission is hereby granted, free of charge, to any person.\n"
                                     "THE SOFTWARE IS PROVIDED AS IS.\n", encoding="utf-8")
    component = notices.Component("rust", "demo", "1.0.0", "MIT", None,
                                  "registry+https://github.com/rust-lang/crates.io-index", "sum", package.resolve())
    files = notices.check_coverage([component], {})
    notices.write_notices([component], files, {}, root / "third-party-notices", notices.WINDOWS_TARGET, ())
    return root, pin


def test_an_extracted_installer_is_verified_byte_for_byte(tmp_path):
    root, pin = extracted_tree(tmp_path)

    evidence = installer_verify.verify_extracted(root, pin)

    assert evidence["topLevel"] == sorted(installer_verify.EXPECTED_TOP_LEVEL)
    assert evidence["webview2Installer"]["sha256"] == pin["sha256"]
    assert evidence["runtimeFiles"] == 2 and evidence["desktopNoticeFiles"] >= 3
    assert evidence["plugins"] == ["MicrosoftEdgeWebView2RuntimeInstallerX64.exe", "System.dll"]


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
