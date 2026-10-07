from unittest.mock import patch
import ast
from pathlib import Path

import pytest

from core.web_providers import requests_transport
from core.computer_executor import (
    _default_gui_action, KNOWN_APPS, ComputerOperation,
)


def test_legacy_web_transport_refuses_before_network_io():
    with patch("requests.request") as request:
        with pytest.raises(PermissionError, match="browser adapter"):
            requests_transport("GET", "https://example.com")
        request.assert_not_called()


def test_ungrounded_paint_input_is_disabled():
    with pytest.raises(RuntimeError, match="grounding"):
        _default_gui_action(KNOWN_APPS["paint"], ComputerOperation.DRAW_BASIC_HOUSE)


def test_product_network_imports_require_an_explicit_reviewed_exception():
    # This inventory requires review of new direct-network paths. Behavioral
    # tests still prove the actual boundary for each hardened sidecar.
    exceptions = {
        "core.local_ipc": "Bounded loopback HTTP; sidecar ownership or local-model provenance; no proxy/redirects.",
        "core.memory_runtime": "Owned CPU sidecar lifecycle and fixed loopback port probe.",
        "core.whisper_server_runtime": "Owned STT sidecar request objects and loopback readiness.",
        "core.chatterbox_runtime": "Owned TTS sidecar request objects and loopback port allocation.",
        "core.ollama_controller": "Reviewed local Ollama inference controller.",
        "core.user_memory": "Local embedding adapter through reviewed environment-free HTTP boundary.",
        "core.ollama_models": "Local model metadata/deletion; registry downloads refused before IO.",
        "core.web_providers": "Disabled legacy transport; test proves refusal before requests IO.",
        "core.link_client": "Explicit authenticated/encrypted Device Link transport exception.",
        "core.link_server": "Permission-controlled Device Link listener and wake sockets.",
        "core.link_transport": "Authenticated/encrypted Device Link TLS transport.",
        "core.link_pairing": "Local Device Link address discovery; no inference or web requests.",
    }
    root = Path(__file__).resolve().parent.parent
    risky_roots = {"requests", "socket", "aiohttp", "httpx", "websocket", "websockets"}
    undeclared = []
    for package in ("core", "security"):
        for path in (root / package).rglob("*.py"):
            module = ".".join(path.relative_to(root).with_suffix("").parts)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imports = [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
                else:
                    continue
                for name in imports:
                    if (name.split(".")[0] in risky_roots or name in {"urllib", "urllib.request", "http.client"}) and module not in exceptions:
                        undeclared.append((module, name))
    assert not undeclared, f"Undeclared product network paths: {undeclared}"


def test_hardened_sidecars_cannot_bypass_the_local_http_boundary():
    root = Path(__file__).resolve().parent.parent
    for filename in ("memory_runtime.py", "whisper_server_runtime.py", "chatterbox_runtime.py"):
        tree = ast.parse((root / "core" / filename).read_text(encoding="utf-8"))
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(tree) if isinstance(node, ast.Call)
        }
        forbidden = {"requests.get", "requests.post", "requests.request", "requests.Session", "urllib.request.urlopen", "urllib.request.build_opener"}
        assert not calls & forbidden, f"{filename} bypassed reviewed local IPC: {calls & forbidden}"
