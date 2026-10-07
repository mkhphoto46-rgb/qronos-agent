"""Build a Tauri installer with a verified standalone Qronos backend.

This build-time tool requires a prepared Python bundle and the locked desktop
dependencies. It never packages local models, voice binaries or user state.
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

from build_support.build_runtime import ROOT, verify_manifest
from build_support.desktop_notices import (
    NOTICE_RESOURCE_TARGET,
    WINDOWS_TARGET,
    collect_desktop_notices,
    verify_notice_output,
)

#: Each build writes its notices into a new directory under here (ignored by Git).
DESKTOP_NOTICES_ROOT = ROOT / "runtime" / "package" / "desktop-notices"

#: The notice inventory is collected for WINDOWS_TARGET, so only its installers are built.
SUPPORTED_BUNDLES = ("nsis", "msi")


def require_windows_x64(bundles: str) -> None:
    """Refuse any build whose installer would not match the Windows x64 notice inventory."""
    if bundles not in SUPPORTED_BUNDLES:
        raise ValueError(f"Only {', '.join(SUPPORTED_BUNDLES)} installers are supported: third-party "
                         f"notices are collected for {WINDOWS_TARGET} only.")
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise ValueError(f"The desktop installer must be built on Windows x64 ({WINDOWS_TARGET}).")
    if os.environ.get("CARGO_BUILD_TARGET", WINDOWS_TARGET) != WINDOWS_TARGET:
        raise ValueError(f"CARGO_BUILD_TARGET must be unset or {WINDOWS_TARGET}.")


def build(backend: Path, *, bundles: str = "nsis") -> None:
    require_windows_x64(bundles)
    backend = backend.resolve(strict=True)
    verify_manifest(backend)
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise ValueError("Node.js/npm is required on the build machine.")
    # Before anything is bundled: an incomplete notice set stops the build. Every
    # build gets a new directory, so nothing is ever deleted; it stays as evidence.
    DESKTOP_NOTICES_ROOT.mkdir(parents=True, exist_ok=True)
    notices = Path(tempfile.mkdtemp(prefix="build-", dir=DESKTOP_NOTICES_ROOT))
    collect_desktop_notices(notices)
    verify_notice_output(notices)
    configuration = ROOT / "runtime" / "package" / "tauri-runtime.json"
    configuration.parent.mkdir(parents=True, exist_ok=True)
    configuration.write_text(json.dumps({
        "bundle": {"resources": {
            backend.as_posix() + "/": "qronos-runtime/",
            notices.as_posix() + "/": NOTICE_RESOURCE_TARGET,
        }},
    }, indent=2) + "\n", encoding="utf-8")
    environment = dict(os.environ)
    existing = environment.get("CARGO_ENCODED_RUSTFLAGS")
    flags = existing.split("\x1f") if existing else shlex.split(environment.get("RUSTFLAGS", ""))
    flags.extend((f"--remap-path-prefix={ROOT}=qronos-source",
                  f"--remap-path-prefix={Path.home()}=qronos-build-home"))
    environment["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
    subprocess.run([npm, "run", "tauri", "--", "build", "--config", str(configuration),
                    "--bundles", bundles, "--", "--locked"], cwd=ROOT / "desktop", env=environment, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True, help="Verified onedir runtime produced by build_runtime.")
    parser.add_argument("--bundles", default="nsis", choices=SUPPORTED_BUNDLES)
    arguments = parser.parse_args()
    build(arguments.backend, bundles=arguments.bundles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
