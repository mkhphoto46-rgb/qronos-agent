"""Build a Tauri installer with a verified standalone Qronos backend.

This build-time tool requires a prepared Python bundle and the locked desktop
dependencies. It never packages local models, voice binaries or user state.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess

from build_support.build_runtime import ROOT, verify_manifest


def build(backend: Path, *, bundles: str = "nsis") -> None:
    backend = backend.resolve(strict=True)
    verify_manifest(backend)
    if os.name != "nt" and bundles == "nsis":
        raise ValueError("The Windows NSIS installer must be built on Windows.")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise ValueError("Node.js/npm is required on the build machine.")
    configuration = ROOT / "runtime" / "package" / "tauri-runtime.json"
    configuration.parent.mkdir(parents=True, exist_ok=True)
    configuration.write_text(json.dumps({
        "bundle": {"resources": {backend.as_posix() + "/": "qronos-runtime/"}},
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
    parser.add_argument("--bundles", default="nsis", choices=("nsis", "msi", "deb", "appimage", "app", "dmg"))
    arguments = parser.parse_args()
    build(arguments.backend, bundles=arguments.bundles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
