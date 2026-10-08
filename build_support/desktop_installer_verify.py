"""Verify a built Qronos NSIS installer by extracting it, and record its provenance.

The installer is opened with 7-Zip as an archive; nothing is installed or run.
After extraction:

* the embedded WebView2 installer has exactly the pinned SHA-256;
* no NSIS download plug-in is present;
* ``qronos-runtime/`` matches its runtime manifest file-for-file and byte-for-byte;
* ``third-party-notices/`` matches its own notice manifest the same way;
* the top level holds only the files Qronos expects to ship.

The provenance record ties the installer's SHA-256 to the desktop source commit,
the runtime and notice manifest hashes, the WebView2 pin and the build tools.
The runtime bundle does not record its own source, so the backend source is the
builder's statement and is labelled as such.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from build_support.build_runtime import verify_manifest
from build_support.desktop_notices import MANIFEST as NOTICE_MANIFEST, NoticeError, verify_notice_output
from build_support.desktop_prerequisites import (
    DOWNLOAD_PLUGINS,
    PrerequisiteError,
    sha256_file,
)

PLUGINS_DIR = "$PLUGINSDIR"
MAIN_EXECUTABLE = "qronos-desktop.exe"
#: Everything an extracted Qronos installer may contain at its top level.
EXPECTED_TOP_LEVEL = frozenset({PLUGINS_DIR, MAIN_EXECUTABLE, "uninstall.exe", "qronos-runtime",
                                "third-party-notices"})


def extract_installer(setup: Path, seven_zip: Path, destination: Path,
                      runner: Callable[..., Any] = subprocess.run) -> Path:
    """Extract every file of the installer into a new folder (the installer is not run)."""
    destination.mkdir(parents=True, exist_ok=False)
    result = runner([str(seven_zip), "x", "-y", f"-o{destination}", str(setup)],
                    capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise PrerequisiteError(f"7-Zip could not extract the installer (exit {result.returncode}).")
    return destination


def verify_extracted(root: Path, pin: dict[str, Any]) -> dict[str, Any]:
    """Byte-level checks of an extracted installer; returns the evidence."""
    top_level = sorted(entry.name for entry in root.iterdir())
    unexpected = sorted(set(top_level) - EXPECTED_TOP_LEVEL)
    if unexpected:
        raise PrerequisiteError(f"The installer contains unexpected top-level items: {', '.join(unexpected)}.")
    missing = sorted(EXPECTED_TOP_LEVEL - set(top_level))
    if missing:
        raise PrerequisiteError(f"The installer is missing: {', '.join(missing)}.")

    plugins = root / PLUGINS_DIR
    downloaders = sorted(entry.name for entry in plugins.iterdir() if entry.name.lower() in DOWNLOAD_PLUGINS)
    if downloaders:
        raise PrerequisiteError(f"The installer contains an NSIS download plug-in: {', '.join(downloaders)}.")
    payload = plugins / pin["fileName"]
    if not payload.is_file():
        raise PrerequisiteError("The installer does not contain the WebView2 installer.")
    payload_sha = sha256_file(payload)
    if payload.stat().st_size != pin["bytes"] or payload_sha != pin["sha256"]:
        raise PrerequisiteError(f"The embedded WebView2 installer SHA-256 {payload_sha} does not match the pin.")

    runtime = root / "qronos-runtime"
    try:
        verify_manifest(runtime)
    except ValueError as error:
        raise PrerequisiteError(f"The installer's qronos-runtime does not match its manifest: {error}") from error
    notices = root / "third-party-notices"
    try:
        notice_manifest = verify_notice_output(notices)
    except (NoticeError, OSError, ValueError) as error:
        raise PrerequisiteError(f"The installer's third-party-notices do not match their manifest: {error}") from error
    runtime_manifest = runtime / "runtime-manifest.json"
    return {
        "topLevel": top_level,
        "plugins": sorted(entry.name for entry in plugins.iterdir()),
        "webview2Installer": {"path": f"{PLUGINS_DIR}/{pin['fileName']}", "bytes": pin["bytes"],
                              "sha256": payload_sha},
        "mainExecutableSha256": sha256_file(root / MAIN_EXECUTABLE),
        "runtimeManifestSha256": sha256_file(runtime_manifest),
        "runtimeFiles": len(json.loads(runtime_manifest.read_text(encoding="utf-8"))["files"]),
        "desktopNoticeManifestSha256": sha256_file(notices / NOTICE_MANIFEST),
        "desktopNoticeFiles": len(notice_manifest["files"]),
    }


def _command_output(command: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False)
    except OSError:
        return "unavailable"
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def source_state(root: Path) -> dict[str, Any]:
    """The desktop source commit and whether any tracked content differs from it or any file is untracked.

    Content is compared after Git's line-ending normalisation: the Tauri CLI rewrites
    desktop/src-tauri/Cargo.toml with LF endings, which `git status` reports as modified
    under core.autocrlf even though the content is the committed content.
    """
    commit = _command_output(["git", "rev-parse", "HEAD"], root)
    changed = _command_output(["git", "diff", "--name-only", "HEAD", "--"], root)
    untracked = _command_output(["git", "ls-files", "--others", "--exclude-standard"], root)
    unknown = "unavailable" in (commit, changed, untracked)
    changed_paths = sorted(set(changed.splitlines()) | set(untracked.splitlines())) if not unknown else []
    return {"commit": commit, "clean": not unknown and not changed_paths, "changedPaths": changed_paths}


def provenance_record(*, setup: Path, root: Path, pin: dict[str, Any], extracted: dict[str, Any],
                      backend_source: str, purpose: str, tauri_cli: str) -> dict[str, Any]:
    source = source_state(root)
    return {
        "schema": 1,
        "purpose": purpose,
        "installer": {"fileName": setup.name, "bytes": setup.stat().st_size, "sha256": sha256_file(setup),
                      "signed": False},
        "desktopSource": source,
        "attributableToDesktopSource": source["clean"] and source["commit"] != "unavailable",
        "backend": {
            "sourceAsStatedByBuilder": backend_source,
            "sourceVerifiedFromBundle": False,
            "runtimeManifestSha256": extracted["runtimeManifestSha256"],
            "runtimeFiles": extracted["runtimeFiles"],
        },
        "desktopNotices": {"manifestSha256": extracted["desktopNoticeManifestSha256"],
                           "files": extracted["desktopNoticeFiles"]},
        "webview2": {"runtimeVersion": pin["runtime"]["version"], "installerSha256": pin["sha256"],
                     "installerBytes": pin["bytes"], "url": pin["url"], "embeddedAt": extracted["webview2Installer"]["path"]},
        "extracted": {"topLevel": extracted["topLevel"], "plugins": extracted["plugins"],
                      "mainExecutableSha256": extracted["mainExecutableSha256"]},
        "toolchain": {
            "tauriCli": tauri_cli,
            "rustc": _command_output(["rustc", "-V"], root),
            "cargo": _command_output(["cargo", "-V"], root),
            "node": _command_output(["node", "--version"], root),
        },
        "ownerGates": pin["ownerGates"],
        "notVerifiedHere": [
            "installation on a clean Windows machine (offline, with and without WebView2, standard user)",
            "Authenticode signing of this installer",
        ],
    }


def write_record(path: Path, record: dict[str, Any]) -> None:
    with open(path, "x", encoding="utf-8", newline="\n") as handle:  # never replaces a record
        handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
