"""Production and development Content Security Policies of the desktop webview.

The locked tauri 2.11.5 (manager/mod.rs) and tauri-codegen 2.6.3 (context.rs)
inject `app.security.csp` into release builds and `devCsp`, when present, into
`tauri dev`. The Vite dev server origins therefore belong only in devCsp.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
VITE_CONFIG = ROOT / "desktop" / "vite.config.ts"


def directives(policy: str) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for part in policy.split(";"):
        tokens = part.split()
        if tokens:
            assert tokens[0] not in parsed, f"duplicate directive {tokens[0]}"
            parsed[tokens[0]] = tokens[1:]
    return parsed


def security() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))["app"]["security"]


def dev_server_origins() -> set[str]:
    port = re.search(r"port:\s*(\d+)", VITE_CONFIG.read_text(encoding="utf-8")).group(1)
    return {f"http://localhost:{port}", f"ws://localhost:{port}"}


def is_loopback(source: str) -> bool:
    host = (urlsplit(source).hostname or "") if "://" in source else ""
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost") and host not in {
        "ipc.localhost", "asset.localhost"}


def test_the_production_policy_allows_no_dev_server_or_loopback_origin():
    production = directives(security()["csp"])

    for name, sources in production.items():
        for source in sources:
            assert not is_loopback(source), f"{name} allows {source} in release builds"
    assert production["connect-src"] == ["'self'", "ipc:", "http://ipc.localhost"]
    assert production["script-src"] == ["'self'"] and production["default-src"] == ["'self'"]
    assert not any("'unsafe-eval'" in sources for sources in production.values())


def test_the_dev_policy_only_adds_the_vite_dev_server_to_connect_src():
    production, development = directives(security()["csp"]), directives(security()["devCsp"])

    assert set(development) == set(production)
    for name in production:
        if name != "connect-src":
            assert development[name] == production[name], f"devCsp changes {name}"
    added = set(development["connect-src"]) - set(production["connect-src"])
    assert added == dev_server_origins()
    assert set(production["connect-src"]) <= set(development["connect-src"])


@pytest.mark.parametrize("source", ["http://localhost:1420", "ws://localhost:1420", "http://127.0.0.1:8080",
                                    "ws://[::1]:9000", "http://evil.localhost"])
def test_a_loopback_source_is_recognised(source):
    assert is_loopback(source)


@pytest.mark.parametrize("source", ["'self'", "ipc:", "http://ipc.localhost", "http://asset.localhost", "asset:",
                                    "data:", "blob:"])
def test_tauri_protocol_sources_are_not_mistaken_for_loopback(source):
    assert not is_loopback(source)
