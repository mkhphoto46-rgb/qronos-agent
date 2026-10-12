"""Check a runtime asset folder against the reviewed asset registry (build_support/runtime_assets.json).

Read only: it stats and hashes files, opens nothing else, follows no link, starts nothing and downloads nothing.

    python -m build_support.runtime_assets --root "<Local AppData>\\Qronos\\RuntimeAssets"
    python -m build_support.runtime_assets --root ... --require-verified   # release gate: exit 1 unless all verified

Each registry entry is reported as one of:
- ``verified``: present, and its size and SHA-256 equal the reviewed official identity;
- ``mismatch``: present, but not the reviewed file (never used as if it were);
- ``unverified``: the registry has no official identity for it (present or not, nothing is vouched for);
- ``missing``: configured but absent;
- ``refused``: a link or reparse point, or not a regular file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

REGISTRY = Path(__file__).resolve().parent / "runtime_assets.json"
STATUSES = ("verified", "unverified", "missing")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RegistryError(ValueError):
    """The registry itself is not well formed; nothing is checked."""


def load_registry(path: Path = REGISTRY) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("assets"), list):
        raise RegistryError("unsupported runtime asset registry")
    seen: set[str] = set()
    for entry in data["assets"]:
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative or relative in seen or relative.startswith(("/", "\\")) \
                or ".." in Path(relative).parts:
            raise RegistryError(f"bad or duplicate asset path: {relative!r}")
        seen.add(relative)
        status = entry.get("status")
        if status not in STATUSES:
            raise RegistryError(f"{relative}: unknown status {status!r}")
        if status == "verified":
            if not (isinstance(entry.get("bytes"), int) and entry["bytes"] > 0
                    and isinstance(entry.get("sha256"), str) and _SHA256.fullmatch(entry["sha256"])
                    and str(entry.get("source", "")).startswith("https://") and entry.get("evidence")):
                raise RegistryError(f"{relative}: a verified entry needs bytes, sha256, an https source and evidence")
        elif "sha256" in entry or "bytes" in entry:
            raise RegistryError(f"{relative}: only a verified entry may carry an identity")
    return data["assets"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check(root: Path, assets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The state of every registry entry under ``root``."""
    assets = load_registry() if assets is None else assets
    results = []
    for entry in assets:
        path = Path(root) / Path(entry["path"])
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            results.append({"path": entry["path"], "state": "missing"})
            continue
        except OSError:
            results.append({"path": entry["path"], "state": "refused"})
            continue
        attributes = getattr(info, "st_file_attributes", 0)
        if not stat.S_ISREG(info.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            results.append({"path": entry["path"], "state": "refused"})
            continue
        if entry["status"] != "verified":
            results.append({"path": entry["path"], "state": "unverified", "bytes": info.st_size})
            continue
        same = info.st_size == entry["bytes"] and _sha256(path) == entry["sha256"]
        results.append({"path": entry["path"], "state": "verified" if same else "mismatch", "bytes": info.st_size})
    counts = {state: sum(1 for item in results if item["state"] == state)
              for state in ("verified", "mismatch", "unverified", "missing", "refused")}
    return {"schemaVersion": 1, "root": str(root), "assets": results, "counts": counts,
            "allVerified": counts["verified"] == len(results)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="The runtime asset folder to check.")
    parser.add_argument("--require-verified", action="store_true",
                        help="Exit 1 unless every registry entry is present and verified (release gate).")
    arguments = parser.parse_args(argv)
    report = check(arguments.root)
    sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    if any(report["counts"][state] for state in ("mismatch", "refused")):
        return 2
    return 1 if arguments.require_verified and not report["allVerified"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
