"""Build a Tauri installer with a verified standalone Qronos backend.

This build-time tool requires a prepared Python bundle, the locked desktop
dependencies and the pinned offline WebView2 installer in .cache/wv2/. It
never packages local models, voice binaries or user state, and it never
downloads anything (see docs/desktop-offline-installer.md).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
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
from build_support.desktop_prerequisites import (
    CACHE_DIR,
    PrerequisiteError,
    extract_and_hash_payload,
    find_seven_zip,
    list_installer,
    load_pin,
    verify_embedded_payload,
    verify_generated_script,
    verify_installer,
    write_hook_wrapper,
)

TAURI_CONFIG = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
#: Each build writes its notices, hook wrapper and overlay into a new folder here (ignored by Git).
DESKTOP_BUILDS_ROOT = ROOT / "runtime" / "package" / "desktop-builds"

#: Only NSIS carries the offline WebView2 hook, and notices are collected for WINDOWS_TARGET only.
SUPPORTED_BUNDLES = ("nsis",)
HOOK_SETTING = "installer-hooks/webview2-offline.nsh"


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
    if bundle.get("createUpdaterArtifacts") or "updater" in config.get("plugins", {}):
        raise ValueError("Updater artifacts would make Tauri fall back to the WebView2 download bootstrapper; "
                         "authenticated updates need their own reviewed design first.")
    if windows.get("minimumWebview2Version") or nsis.get("minimumWebview2Version"):
        raise ValueError("A minimum WebView2 version makes the installer contact Microsoft's updater.")


def _target_dir(environment: dict[str, str]) -> Path:
    configured = environment.get("CARGO_TARGET_DIR")
    return Path(configured) if configured else ROOT / "desktop" / "src-tauri" / "target"


def build(backend: Path, *, bundles: str = "nsis", webview2_installer: Path | None = None,
          target_dir: Path | None = None) -> dict[str, Any]:
    require_windows_x64(bundles)
    config = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))
    check_tauri_config(config)
    # Everything that can fail quickly is checked before the long build starts.
    pin = load_pin()
    installer = (webview2_installer or CACHE_DIR / pin["fileName"]).absolute()
    prerequisite = verify_installer(installer, pin)
    seven_zip = find_seven_zip()
    backend = backend.resolve(strict=True)
    verify_manifest(backend)
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise ValueError("Node.js/npm is required on the build machine.")

    # A new folder per build: nothing is deleted, and it stays as evidence.
    DESKTOP_BUILDS_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="b-", dir=DESKTOP_BUILDS_ROOT))
    notices = work / "notices"
    collect_desktop_notices(notices)
    verify_notice_output(notices)
    wrapper = write_hook_wrapper(work, installer, pin)
    overlay = work / "tauri-overlay.json"
    overlay.write_text(json.dumps({
        "bundle": {
            "resources": {
                backend.as_posix() + "/": "qronos-runtime/",
                notices.as_posix() + "/": NOTICE_RESOURCE_TARGET,
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
    started = time.time()
    subprocess.run([npm, "run", "tauri", "--", "build", "--config", str(overlay),
                    "--bundles", bundles, "--", "--locked"], cwd=ROOT / "desktop", env=environment, check=True)

    # The installer is checked as built, not assumed from configuration.
    release = _target_dir(environment) / "release"
    verify_generated_script((release / "nsis" / "x64" / "installer.nsi").read_text(encoding="utf-8"), wrapper)
    setup = release / "bundle" / "nsis" / f"{config['productName']}_{config['version']}_x64-setup.exe"
    if not setup.is_file() or setup.stat().st_mtime < started:
        raise PrerequisiteError(f"This build did not produce {setup.name}.")
    entries = list_installer(setup, seven_zip)
    verify_embedded_payload(entries, pin)
    embedded = extract_and_hash_payload(setup, seven_zip, work / "payload")
    if embedded != pin["sha256"]:
        raise PrerequisiteError(f"The embedded WebView2 installer SHA-256 {embedded} does not match the pin.")
    return {"installer": setup, "work": work, "prerequisite": prerequisite, "entries": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True, help="Verified onedir runtime produced by build_runtime.")
    parser.add_argument("--bundles", default="nsis", choices=SUPPORTED_BUNDLES)
    parser.add_argument("--webview2-installer", type=Path, default=None,
                        help="Pinned offline WebView2 installer (default: .cache/wv2/<pinned file name>).")
    parser.add_argument("--target-dir", type=Path, default=None,
                        help="Short Cargo target directory, to stay clear of Windows MAX_PATH.")
    arguments = parser.parse_args()
    result = build(arguments.backend, bundles=arguments.bundles, webview2_installer=arguments.webview2_installer,
                   target_dir=arguments.target_dir)
    print(json.dumps({"installer": str(result["installer"]), "prerequisite": result["prerequisite"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
