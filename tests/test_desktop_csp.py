"""Production and development Content Security Policies of the desktop webview.

The locked tauri 2.11.5 (manager/mod.rs) and tauri-codegen 2.6.3 (context.rs)
inject `app.security.csp` into release builds and `devCsp`, when present, into
`tauri dev`. The Vite dev server origins therefore belong only in devCsp.

The voice player fetches a spectrum sidecar through convertFileSrc, which the
locked tauri 2.11.5 (scripts/core.js) turns into http://asset.localhost/<path>
on Windows; the app's own origin is http://tauri.localhost. Tauri adds nonces
and hashes only to script-src and style-src (manager/mod.rs), never to
connect-src, so the asset origin must be listed there. The asset protocol itself
serves only the playback folder the desktop allows at runtime
(runtime/mod.rs allow_directory), and answers with the window origin as CORS.
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
    assert production["connect-src"] == ["'self'", "ipc:", "http://ipc.localhost", "asset:", "http://asset.localhost"]
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


# ------------------------------------------- CSP evaluation for real URLs


def allows(policy: str, directive: str, url: str, page_origin: str = "http://tauri.localhost") -> bool:
    """A small CSP source-list matcher: 'self', scheme sources and exact scheme+host sources."""
    sources = directives(policy).get(directive, directives(policy).get("default-src", []))
    target = urlsplit(url)
    for source in sources:
        if source == "'self'" and f"{target.scheme}://{target.netloc}" == page_origin:
            return True
        if source.endswith(":") and "//" not in source and target.scheme == source[:-1]:
            return True
        if "://" in source:
            expected = urlsplit(source)
            if (expected.scheme, expected.hostname, expected.port) == (target.scheme, target.hostname, target.port):
                return True
    return False


TAURI_CORE_SCRIPT = (Path.home() / ".cargo" / "registry" / "src" / "index.crates.io-1949cf8c6b5b557f"
                     / "tauri-2.11.5" / "scripts" / "core.js")


def asset_url_for(path: str) -> str:
    """What convertFileSrc returns on Windows with the default (http) scheme."""
    from urllib.parse import quote
    return f"http://asset.localhost/{quote(path, safe='')}"


@pytest.mark.skipif(not TAURI_CORE_SCRIPT.is_file(), reason="The locked tauri crate source is not in the Cargo cache.")
def test_the_locked_tauri_builds_asset_urls_this_way():
    script = TAURI_CORE_SCRIPT.read_text(encoding="utf-8")
    assert "${protocolScheme}://${protocol}.localhost/${path}" in script
    assert "osName === 'windows'" in script
    assert "useHttpsScheme" not in CONFIG.read_text(encoding="utf-8")  # so protocolScheme is "http"


def test_the_spectrum_sidecar_fetch_is_allowed_in_both_policies():
    url = asset_url_for(r"D:\QronosData\Playback\reply.spectrum.json")
    for name in ("csp", "devCsp"):
        policy = security()[name]
        assert allows(policy, "connect-src", url), name
        assert allows(policy, "media-src", url.replace(".spectrum.json", ".wav")), name  # unchanged audio path


@pytest.mark.parametrize("url", ["https://example.com/x", "http://localhost:1420/x", "http://127.0.0.1:11434/api",
                                 "http://evil.localhost/x", "ws://localhost:1421/"])
def test_production_connect_src_still_refuses_other_origins(url):
    assert not allows(security()["csp"], "connect-src", url)


def test_the_only_frontend_fetch_uses_a_convert_file_src_url():
    sources = list((ROOT / "desktop" / "src").rglob("*.ts")) + list((ROOT / "desktop" / "src").rglob("*.tsx"))
    calls = []
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for match in re.finditer(r"\bfetch\(\s*([A-Za-z_][A-Za-z0-9_]*)", text):
            calls.append((source.name, match.group(1), text))
    assert [(name, variable) for name, variable, _ in calls] == [("QronosVoicePlayer.ts", "sidecarUrl")]
    assert re.search(r"const\s+sidecarUrl\s*=\s*convertFileSrc\(", calls[0][2])


def test_the_previous_policy_blocked_the_sidecar_fetch():
    """Before this change the spectrum sidecar fetch could never succeed, in either build."""
    previous = ("default-src 'self'; media-src 'self' asset: http://asset.localhost data: blob:; "
                "connect-src 'self' ipc: http://ipc.localhost")
    url = asset_url_for(r"D:\QronosData\Playback\reply.spectrum.json")

    assert not allows(previous, "connect-src", url)
    assert allows(previous, "media-src", url)
