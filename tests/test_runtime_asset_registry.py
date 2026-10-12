"""The reviewed runtime asset registry and its read-only verifier (build_support/runtime_assets.py).

Synthetic files under tmp_path only; no real model, binary or download.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from build_support import runtime_assets
from core.runtime_preflight import ASSET_GROUPS

ROOT = Path(__file__).resolve().parent.parent


def entry(path: str, data: bytes | None = None, **extra) -> dict:
    if data is None:
        return {"path": path, "status": extra.pop("status", "unverified"), **extra}
    return {"path": path, "status": "verified", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "source": "https://example.invalid/official", "evidence": "fixture", **extra}


def test_the_registry_covers_every_configured_asset_and_vouches_only_with_an_identity():
    assets = runtime_assets.load_registry()
    paths = {item["path"] for item in assets}
    configured = {path for group in ASSET_GROUPS.values() for path in group}
    assert configured <= paths, sorted(configured - paths)  # no configured asset escapes review
    for item in assets:
        if item["status"] == "verified":
            assert len(item["sha256"]) == 64 and item["bytes"] > 0 and item["source"].startswith("https://")
        else:
            assert "sha256" not in item and "bytes" not in item  # no guessed or local-only hash is a claim
        assert item.get("licence"), item["path"]


@pytest.mark.parametrize("bad", [
    {"path": "a.bin", "status": "verified", "bytes": 1, "sha256": "x" * 64, "source": "https://x", "evidence": "e"},
    {"path": "a.bin", "status": "verified", "bytes": 1, "sha256": "a" * 64, "source": "http://x", "evidence": "e"},
    {"path": "a.bin", "status": "unverified", "sha256": "a" * 64},
    {"path": "../outside.bin", "status": "missing"},
    {"path": "a.bin", "status": "probably_fine"},
])
def test_a_malformed_registry_is_refused(tmp_path, bad):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema": 1, "assets": [bad]}), encoding="utf-8")
    with pytest.raises(runtime_assets.RegistryError):
        runtime_assets.load_registry(registry)


def test_each_state_is_reported_and_only_an_exact_file_is_verified(tmp_path):
    good, other = b"official bytes", b"official bytez"
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "good.bin").write_bytes(good)
    (tmp_path / "models" / "swapped.bin").write_bytes(other)
    (tmp_path / "models" / "local.bin").write_bytes(b"something local")
    os.mkdir(tmp_path / "models" / "folder.bin")
    assets = [entry("models/good.bin", good), entry("models/swapped.bin", good), entry("models/local.bin"),
              entry("models/absent.bin", good), entry("models/folder.bin", good)]
    report = runtime_assets.check(tmp_path, assets)
    states = {item["path"]: item["state"] for item in report["assets"]}
    assert states == {"models/good.bin": "verified", "models/swapped.bin": "mismatch",
                      "models/local.bin": "unverified", "models/absent.bin": "missing",
                      "models/folder.bin": "refused"}
    assert report["allVerified"] is False


def test_the_command_is_a_release_gate_and_reads_only(tmp_path):
    def run(*arguments):
        return subprocess.run([sys.executable, "-B", "-m", "build_support.runtime_assets", "--root", str(tmp_path),
                               *arguments], cwd=ROOT, capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    done = run()
    report = json.loads(done.stdout)
    assert done.returncode == 0 and report["counts"]["missing"] == len(report["assets"])  # an empty profile
    assert run("--require-verified").returncode == 1
    assert list(tmp_path.iterdir()) == []  # nothing was created
