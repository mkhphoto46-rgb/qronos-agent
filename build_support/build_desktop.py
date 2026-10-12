"""Build a Tauri installer with a verified standalone Qronos backend.

This build-time tool requires a prepared Python bundle, the locked desktop
dependencies, the pinned offline WebView2 installer in .cache/wv2/, the Cargo
crates already fetched and Tauri's NSIS tool folder already prepared (one-time
developer steps in docs/offline-windows-installer.md). It never packages local
models, voice binaries or user state. It runs Cargo with --offline and refuses
to start when the NSIS tools are incomplete, so the build itself downloads
nothing; the finished installer never downloads anything either.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import shlex
import subprocess
import tempfile
import time
from typing import Any

from build_support.build_runtime import ROOT, verify_manifest
from build_support.desktop_notices import (
    NOTICE_RESOURCE_TARGET,
    WINDOWS_TARGET,
    collect_desktop_notices,
    verify_notice_output,
)
from build_support import install_manifest
from build_support.desktop_installer_verify import (
    extract_installer,
    provenance_record,
    verify_extracted,
    write_record,
)
from build_support.desktop_prerequisites import (
    CACHE_DIR,
    PrerequisiteError,
    extract_and_hash_payload,
    find_seven_zip,
    list_installer,
    load_pin,
    verify_embedded_payload,
    TEMPLATE_SETTING,
    verify_generated_order,
    verify_generated_script,
    verify_hook_wrapper,
    verify_installer,
    verify_template_source,
    tauri_tools_dir,
    verify_build_tools,
    write_hook_wrapper,
)

TAURI_CONFIG = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
#: Each build writes its notices, hook wrapper and overlay into a new folder here (ignored by Git).
DESKTOP_BUILDS_ROOT = ROOT / "runtime" / "package" / "desktop-builds"

#: Only NSIS carries the offline WebView2 hook, and notices are collected for WINDOWS_TARGET only.
SUPPORTED_BUNDLES = ("nsis",)
#: Why an installer was built; recorded in its provenance.
PURPOSES = ("packaging-check", "review-candidate")
HOOK_SETTING = "installer-hooks/webview2-offline.nsh"
#: The per-user data folder name under %LOCALAPPDATA% (core/config.py, runtime/backend.rs).
USER_DATA_FOLDER = "Qronos"


_FOLDER_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_DEVICE_NAMES = frozenset({"con", "prn", "aux", "nul", "conin$", "conout$",
                           *(f"{port}{n}" for port in ("com", "lpt") for n in (*"123456789", "¹", "²",
                                                                                 "³"))})


def folder_name_problem(name: str) -> str | None:
    """Why a configuration value cannot be used as one canonical Windows folder name, if it cannot.

    Windows drops trailing dots and spaces ("Qronos." and "Qronos " open "Qronos"),
    resolves 8.3 short names ("QRONOS~1"), treats device names as devices even with
    an extension ("NUL.txt") and reads ":" as an alternate data stream. Each would
    let a harmless-looking name reach another folder, so each is refused.
    """
    if not isinstance(name, str) or not name or name in (".", ".."):
        return "is empty or a relative path element"
    if _FOLDER_FORBIDDEN.search(name):
        return "contains a path separator, ':' or another character Windows does not allow in a name"
    if name != name.strip(" ") or name.endswith("."):
        return "starts or ends with a space or ends with a dot, which Windows removes"
    if re.search(r"~\d", name):
        return "looks like an 8.3 short name, which Windows resolves to another folder"
    if name.split(".")[0].rstrip(" ").casefold() in _DEVICE_NAMES:
        return "is a Windows device name"
    return None


def require_windows_x64(bundles: str) -> None:
    """Refuse any build whose installer would not match the Windows x64 inventory and prerequisite."""
    if bundles not in SUPPORTED_BUNDLES:
        raise ValueError(f"Only {', '.join(SUPPORTED_BUNDLES)} installers are supported: the offline WebView2 "
                         f"prerequisite and third-party notices exist for {WINDOWS_TARGET} NSIS only.")
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise ValueError(f"The desktop installer must be built on Windows x64 ({WINDOWS_TARGET}).")
    if os.environ.get("CARGO_BUILD_TARGET", WINDOWS_TARGET) != WINDOWS_TARGET:
        raise ValueError(f"CARGO_BUILD_TARGET must be unset or {WINDOWS_TARGET}.")


def check_tauri_config(config: dict[str, Any]) -> None:
    """The committed configuration keeps every WebView2 download path off.

    tauri-bundler 2.9.4 switches the updater variant of an NSIS installer to the
    download bootstrapper, and a minimum WebView2 version makes the installer
    call Microsoft's updater, so both are refused as well.
    """
    bundle = config.get("bundle", {})
    windows = bundle.get("windows", {})
    nsis = windows.get("nsis", {})
    if windows.get("webviewInstallMode") != {"type": "skip"}:
        raise ValueError("tauri.conf.json must set bundle.windows.webviewInstallMode to {\"type\": \"skip\"}; "
                         "the offline WebView2 prerequisite is installed by the Qronos NSIS hook.")
    if bundle.get("targets") != list(SUPPORTED_BUNDLES):
        raise ValueError("tauri.conf.json must build only the NSIS target.")
    if nsis.get("installerHooks") != HOOK_SETTING:
        raise ValueError(f"tauri.conf.json must use {HOOK_SETTING} as the NSIS installer hook.")
    if nsis.get("template") != TEMPLATE_SETTING:
        raise ValueError(f"tauri.conf.json must use the pinned Qronos template {TEMPLATE_SETTING}: Tauri's stock "
                         "template creates the install folder and can run a previous uninstaller before the "
                         "Qronos update fence (installer-template/PROVENANCE.md).")
    if bundle.get("createUpdaterArtifacts") or "updater" in config.get("plugins", {}):
        raise ValueError("Updater artifacts would make Tauri fall back to the WebView2 download bootstrapper; "
                         "authenticated updates need their own reviewed design first.")
    if windows.get("minimumWebview2Version") or nsis.get("minimumWebview2Version"):
        raise ValueError("A minimum WebView2 version makes the installer contact Microsoft's updater.")
    # The per-user install folder is %LOCALAPPDATA%\<productName>, and "Delete the
    # application data" removes %APPDATA%\<identifier> and %LOCALAPPDATA%\<identifier>.
    # Neither may be the folder that holds Qronos user data.
    for setting, value in (("productName", config.get("productName", "")),
                           ("identifier", config.get("identifier", ""))):
        problem = folder_name_problem(value)
        if problem:
            raise ValueError(f"tauri.conf.json {setting} {value!r} {problem}; it names an install or app-data folder.")
        if value.casefold() == USER_DATA_FOLDER.casefold():
            raise ValueError(f"tauri.conf.json {setting} {value!r} would place installer-managed files in the "
                             f"%LOCALAPPDATA%\\{USER_DATA_FOLDER} user-data folder.")
    if nsis.get("installMode", "currentUser") not in ("currentUser", "perMachine"):
        raise ValueError("The NSIS install mode must be currentUser or perMachine; the reviewed upgrade and "
                         "uninstall paths cover only those.")


def _target_dir(environment: dict[str, str]) -> Path:
    configured = environment.get("CARGO_TARGET_DIR")
    return Path(configured) if configured else ROOT / "desktop" / "src-tauri" / "target"


#: Suffix of an installer that did not pass every check; it must never be used.
UNVERIFIED_SUFFIX = ".UNVERIFIED"


def quarantine(path: Path) -> Path:
    """Rename a refused installer so it can never pass for verified output. Nothing is deleted."""
    target = path.with_name(path.name + UNVERIFIED_SUFFIX)
    number = 1
    while target.exists():
        target = path.with_name(f"{path.name}.{number}{UNVERIFIED_SUFFIX}")
        number += 1
    os.replace(path, target)
    return target


def _tauri_cli_version() -> str:
    lock = json.loads((ROOT / "desktop" / "package-lock.json").read_text(encoding="utf-8"))
    return lock["packages"]["node_modules/@tauri-apps/cli"]["version"]


def build(backend: Path, *, bundles: str = "nsis", webview2_installer: Path | None = None,
          target_dir: Path | None = None, work_root: Path | None = None,
          backend_source: str = "not stated", purpose: str = "packaging-check") -> dict[str, Any]:
    require_windows_x64(bundles)
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {', '.join(PURPOSES)}.")
    config = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))
    check_tauri_config(config)
    verify_template_source()
    # Everything that can fail quickly is checked before the long build starts.
    pin = load_pin()
    installer = (webview2_installer or CACHE_DIR / pin["fileName"]).absolute()
    prerequisite = verify_installer(installer, pin)
    seven_zip = find_seven_zip()
    build_tools = verify_build_tools(tauri_tools_dir(), _tauri_cli_version())
    backend = backend.resolve(strict=True)
    verify_manifest(backend)
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise ValueError("Node.js/npm is required on the build machine.")

    # A new folder per build: nothing is deleted, and it stays as evidence. A short
    # work root keeps the deepest notice paths below the Windows MAX_PATH limit.
    builds_root = (work_root or DESKTOP_BUILDS_ROOT).absolute()
    builds_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="b-", dir=builds_root))
    notices = work / "notices"
    collect_desktop_notices(notices)
    verify_notice_output(notices)
    # Every file this installation will own, so a later upgrade or uninstall removes
    # exactly these and never a user's file (installer-hooks/qronos-install-root.nsh). The
    # update transaction claims exactly these paths (installer-hooks/qronos-update-transaction.nsh).
    ownership = install_manifest.write(work, install_manifest.payload_files(backend, notices, config["productName"]))
    wrapper = write_hook_wrapper(work, installer, pin, owned_manifest=ownership)
    verify_hook_wrapper(wrapper.read_text(encoding="utf-8"))
    overlay = work / "tauri-overlay.json"
    overlay.write_text(json.dumps({
        "bundle": {
            "resources": {
                backend.as_posix() + "/": "qronos-runtime/",
                notices.as_posix() + "/": NOTICE_RESOURCE_TARGET,
                ownership.as_posix(): install_manifest.FILE_NAME,
            },
            "windows": {"nsis": {"installerHooks": str(wrapper)}},
        },
    }, indent=2) + "\n", encoding="utf-8")

    environment = dict(os.environ)
    if target_dir is not None:
        environment["CARGO_TARGET_DIR"] = str(target_dir.absolute())
    existing = environment.get("CARGO_ENCODED_RUSTFLAGS")
    flags = existing.split("\x1f") if existing else shlex.split(environment.get("RUSTFLAGS", ""))
    flags.extend((f"--remap-path-prefix={ROOT}=qronos-source",
                  f"--remap-path-prefix={Path.home()}=qronos-build-home"))
    environment["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
    release = _target_dir(environment) / "release"
    canonical = release / "bundle" / "nsis" / f"{config['productName']}_{config['version']}_x64-setup.exe"
    # An installer left at Tauri's output path by an earlier or manual build must not be
    # mistaken for this build's verified output.
    if canonical.exists():
        quarantine(canonical)
    started = time.time()
    try:
        subprocess.run([npm, "run", "tauri", "--", "build", "--config", str(overlay),
                        "--bundles", bundles, "--", "--locked", "--offline"], cwd=ROOT / "desktop", env=environment,
                       check=True)
    except BaseException:
        if canonical.exists():
            quarantine(canonical)
        raise
    if not canonical.is_file() or canonical.stat().st_mtime < started:
        raise PrerequisiteError(f"This build did not produce {canonical.name}.")
    # Only this build's own folder ever holds the installer: verified beside its
    # provenance, or renamed *.UNVERIFIED if any check below refuses it.
    kept = work / canonical.name
    shutil.move(str(canonical), str(kept))
    try:
        # The installer is checked as built, not assumed from configuration.
        generated = (release / "nsis" / "x64" / "installer.nsi").read_text(encoding="utf-8")
        verify_generated_script(generated, wrapper)
        verify_generated_order(generated)  # the REAL rendered order, not the template file
        entries = list_installer(kept, seven_zip)
        verify_embedded_payload(entries, pin)
        embedded = extract_and_hash_payload(kept, seven_zip, work / "payload")
        if embedded != pin["sha256"]:
            raise PrerequisiteError(f"The embedded WebView2 installer SHA-256 {embedded} does not match the pin.")
        extracted = verify_extracted(extract_installer(kept, seven_zip, work / "x"), pin,
                                     install_manifest=ownership.read_bytes())
        record = provenance_record(setup=kept, root=ROOT, pin=pin, extracted=extracted,
                                   backend_source=backend_source, purpose=purpose, tauri_cli=_tauri_cli_version())
        write_record(work / "installer-provenance.json", record)
    except BaseException:
        quarantine(kept)
        raise
    return {"installer": kept, "work": work, "prerequisite": prerequisite, "entries": entries,
            "provenance": record, "buildTools": build_tools}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True, help="Verified onedir runtime produced by build_runtime.")
    parser.add_argument("--bundles", default="nsis", choices=SUPPORTED_BUNDLES)
    parser.add_argument("--webview2-installer", type=Path, default=None,
                        help="Pinned offline WebView2 installer (default: .cache/wv2/<pinned file name>).")
    parser.add_argument("--target-dir", type=Path, default=None,
                        help="Short Cargo target directory, to stay clear of Windows MAX_PATH.")
    parser.add_argument("--work-root", type=Path, default=None,
                        help="Short folder for this build's notices, extraction and provenance "
                             "(default: runtime/package/desktop-builds).")
    parser.add_argument("--backend-source", default="not stated",
                        help="The commit the backend bundle was built from, as known to the builder.")
    parser.add_argument("--purpose", default="packaging-check", choices=PURPOSES)
    arguments = parser.parse_args()
    result = build(arguments.backend, bundles=arguments.bundles, webview2_installer=arguments.webview2_installer,
                   target_dir=arguments.target_dir, work_root=arguments.work_root,
                   backend_source=arguments.backend_source, purpose=arguments.purpose)
    print(json.dumps({"installer": str(result["installer"]), "work": str(result["work"]),
                      "sha256": result["provenance"]["installer"]["sha256"],
                      "attributableToDesktopSource": result["provenance"]["attributableToDesktopSource"]},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
