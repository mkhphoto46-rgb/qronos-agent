from __future__ import annotations

import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import numpy as np
import pytest

from core import local_ipc
from core.ollama_controller import OllamaController
from core.ollama_models import OllamaModelCatalog
from core.user_memory import EmbeddingUnavailable, OllamaEmbeddingProvider


@pytest.fixture
def daemon(monkeypatch):
    state = {"overrides": {}, "received": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def do_DELETE(self):
            self.respond()

        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw) if raw else None
            state["received"].append((self.path, self.headers.get("Authorization"), body))
            payloads = {
                "/api/version": {"version": "local-fixture"},
                "/api/tags": {"models": [{"name": "bge-m3", "size": 123}, {"name": "local:4b", "size": 456}]},
                "/api/ps": {"models": [{"name": "local:4b", "size": 456, "context_length": 4096}]},
                "/api/show": {"model_info": {"general.architecture": "fixture"}, "details": {"family": "fixture"}},
                "/api/chat": {"message": {"content": "local answer"}},
                "/api/embed": {"embeddings": [[3, 4]]},
                "/api/generate": {},
                "/api/delete": {},
            }
            status, payload, location = state["overrides"].get(self.path, (200, payloads.get(self.path, {}), None))
            encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(encoded)))
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    monkeypatch.setattr(OllamaController, "ensure_ready", Mock(return_value=False))
    state["url"] = f"http://127.0.0.1:{httpd.server_port}"
    yield state
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


def assert_safe(exception):
    assert "PRIVATE_USER_PROMPT_TOKEN" not in str(exception)
    assert "PRIVATE_USER_PROMPT_TOKEN" not in "".join(traceback.format_exception(type(exception), exception, None))


def test_all_production_model_clients_bypass_proxies_and_netrc(daemon, monkeypatch, tmp_path):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    netrc = tmp_path / "private.netrc"
    netrc.write_text("machine 127.0.0.1 login private password PRIVATE_USER_PROMPT_TOKEN")
    monkeypatch.setenv("NETRC", str(netrc))
    url = daemon["url"].replace("127.0.0.1", "localhost")
    controller = OllamaController(url)
    catalog = OllamaModelCatalog(url)
    provider = OllamaEmbeddingProvider(base_url=url)
    assert controller.health_check()
    assert catalog.health_check()
    assert controller.installed_model_names() == ("bge-m3", "local:4b")
    assert controller.list_running_models()[0].context == 4096
    assert catalog.total_installed_bytes() == 579
    assert catalog.show_model("local:4b").family == "fixture"
    assert controller.chat("local:4b", prompt="private local text") == "local answer"
    controller.stop_model("local:4b")
    catalog.delete_model("local:4b")
    np.testing.assert_allclose(provider.embed(["private local text"]), [[0.6, 0.8]])
    assert all(authorization is None for _, authorization, _ in daemon["received"])
    paths = [path for path, _, _ in daemon["received"]]
    assert paths.count("/api/show") == 4  # Inspection + chat/unload/embedding provenance.
    assert "/api/pull" not in paths


@pytest.mark.parametrize("client,route", [
    ("catalog", "/api/tags"), ("embedding", "/api/tags"), ("brain", "/api/show"),
])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_real_redirects_never_reach_local_or_public_targets(daemon, client, route, status):
    daemon["overrides"][route] = (status, b"PRIVATE_USER_PROMPT_TOKEN", "https://example.com/private")
    private = "PRIVATE_USER_PROMPT_TOKEN"
    with pytest.raises((RuntimeError, EmbeddingUnavailable)) as caught:
        if client == "catalog":
            OllamaModelCatalog(daemon["url"]).list_installed_models()
        elif client == "embedding":
            OllamaEmbeddingProvider(base_url=daemon["url"]).embed([private])
        else:
            OllamaController(daemon["url"]).chat("local:4b", prompt=private)
    assert_safe(caught.value)
    assert [path for path, _, _ in daemon["received"]] == [route]
    assert all(private not in json.dumps(body) for _, _, body in daemon["received"])


@pytest.mark.parametrize("metadata", [
    {"remote_host": "https://example.com/private", "remote_model": "remote", "model_info": {"a": 1}},
    {"remote_model": "hidden-cloud-alias", "model_info": {"a": 1}},
])
@pytest.mark.parametrize("operation", ["chat", "embed", "unload"])
def test_remote_alias_provenance_blocks_sensitive_requests(daemon, metadata, operation):
    daemon["overrides"]["/api/show"] = (200, metadata, None)
    private = "PRIVATE_USER_PROMPT_TOKEN"
    with pytest.raises((PermissionError, EmbeddingUnavailable)) as caught:
        if operation == "chat":
            OllamaController(daemon["url"]).chat("local:4b", prompt=private)
        elif operation == "unload":
            OllamaController(daemon["url"]).stop_model("local:4b")
        else:
            OllamaEmbeddingProvider(base_url=daemon["url"]).embed([private])
    assert_safe(caught.value)
    assert [path for path, _, _ in daemon["received"]] in [["/api/show"], ["/api/tags", "/api/show"]]
    assert all(private not in json.dumps(body) for _, _, body in daemon["received"])


@pytest.mark.parametrize("metadata", [
    {}, [], {"model_info": []}, {"model_info": {}}, b"PRIVATE_USER_PROMPT_TOKEN",
    {"remote_host": False, "model_info": {"general.architecture": "fixture"}},
    {"remote_model": {}, "model_info": {"general.architecture": "fixture"}},
])
def test_missing_or_malformed_local_provenance_fails_before_prompt_send(daemon, metadata):
    daemon["overrides"]["/api/show"] = (200, metadata, None)
    private = "PRIVATE_USER_PROMPT_TOKEN"
    with pytest.raises(RuntimeError) as caught:
        OllamaController(daemon["url"]).chat("local:4b", prompt=private)
    assert_safe(caught.value)
    assert [path for path, _, _ in daemon["received"]] == ["/api/show"]


@pytest.mark.parametrize("name", ["gpt-oss:120b-cloud", "qwen3:cloud", "https://example.com/model"])
def test_explicit_cloud_or_url_model_cannot_reach_inference(daemon, name):
    with pytest.raises(PermissionError):
        OllamaController(daemon["url"]).chat(name, prompt="private")
    assert daemon["received"] == []


@pytest.mark.parametrize("metadata", [[], {"models": None}, {"models": 42}, {"models": [{"name": {"secret": "PRIVATE_USER_PROMPT_TOKEN"}}]}])
def test_catalog_invalid_metadata_does_not_become_empty_inventory(daemon, metadata):
    daemon["overrides"]["/api/tags"] = (200, metadata, None)
    with pytest.raises(RuntimeError) as caught:
        OllamaModelCatalog(daemon["url"]).list_installed_models()
    assert_safe(caught.value)


@pytest.mark.parametrize("payload", [[], {"models": 42}, {"models": [None]}, {"models": [{"name": "x", "context_length": "PRIVATE_USER_PROMPT_TOKEN"}]}])
def test_running_model_metadata_is_validated_and_sanitized(daemon, payload):
    daemon["overrides"]["/api/ps"] = (200, payload, None)
    with pytest.raises(RuntimeError) as caught:
        OllamaController(daemon["url"]).list_running_models()
    assert_safe(caught.value)


@pytest.mark.parametrize("payload", [[], {"message": []}, {"message": {"content": {"secret": "PRIVATE_USER_PROMPT_TOKEN"}}}, {"message": {"content": 0}}, {"message": {"thinking": False}}])
def test_brain_never_stringifies_malformed_message_payload(daemon, payload):
    daemon["overrides"]["/api/chat"] = (200, payload, None)
    with pytest.raises(RuntimeError) as caught:
        OllamaController(daemon["url"]).chat("local:4b", prompt="private")
    assert_safe(caught.value)


@pytest.mark.parametrize("payload", [{"embeddings": [["PRIVATE_USER_PROMPT_TOKEN"]]}, {"embeddings": [[float("nan")]]}, {"embeddings": [[], []]}, []])
def test_embedding_malformed_response_is_sanitized(daemon, payload):
    daemon["overrides"]["/api/embed"] = (200, payload, None)
    with pytest.raises(EmbeddingUnavailable) as caught:
        OllamaEmbeddingProvider(base_url=daemon["url"]).embed(["local"])
    assert_safe(caught.value)


def test_model_response_size_is_bounded(daemon, monkeypatch):
    monkeypatch.setattr(local_ipc, "MAX_MODEL_RESPONSE_BYTES", 64)
    daemon["overrides"]["/api/tags"] = (200, {"models": [{"name": "a" * 1000}]}, None)
    with pytest.raises(RuntimeError):
        OllamaModelCatalog(daemon["url"]).list_installed_models()


@pytest.mark.parametrize("origin", ["https://127.0.0.1:11434", "http://example.com:11434", "http://127.0.0.1:0", "http://user:private@127.0.0.1:11434", "http://127.0.0.1:11434/private", "http://127.0.0.1:11434?token=private"])
def test_remaining_clients_reject_remote_or_ambiguous_origins(origin):
    with pytest.raises(ValueError):
        OllamaModelCatalog(origin)
    with pytest.raises(ValueError):
        OllamaEmbeddingProvider(base_url=origin)


def test_catalog_reconfiguration_cannot_escape_loopback():
    catalog = OllamaModelCatalog()
    with pytest.raises(ValueError):
        catalog.base_url = "https://example.com/private"
    assert catalog.base_url == "http://127.0.0.1:11434"


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1, 301])
def test_primary_deadlines_are_explicit_and_finite(timeout):
    with pytest.raises(ValueError):
        OllamaEmbeddingProvider(timeout_seconds=timeout)
    with pytest.raises(ValueError):
        OllamaController().chat("local:4b", prompt="local", timeout_seconds=timeout)


def test_pull_is_terminal_even_with_adequate_storage_and_disguised_url(daemon):
    catalog = OllamaModelCatalog(daemon["url"])
    for name in ("local:4b", "https://example.com/weights", "public.registry/model"):
        with pytest.raises(PermissionError, match="Storage preflight does not authorize"):
            catalog.pull_model(name)
    assert daemon["received"] == []


def test_shared_http_seam_cannot_be_used_to_reenable_registry_pull(daemon):
    with pytest.raises(ValueError):
        local_ipc.requests_client.post(daemon["url"] + "/api/pull", json={"name": "x"}, timeout=1)
    assert daemon["received"] == []


def test_automatic_daemon_start_has_cloud_disabled_and_loopback_bind(monkeypatch):
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "0")
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    launch = Mock()
    monkeypatch.setattr("core.ollama_controller.subprocess.Popen", launch)
    OllamaController._start_local_server("approved-ollama")
    environment = launch.call_args.kwargs["env"]
    assert environment["OLLAMA_NO_CLOUD"] == "1"
    assert environment["OLLAMA_HOST"] == "127.0.0.1:11434"


@pytest.mark.parametrize("drip_headers", [True, False])
def test_slow_byte_drips_cannot_extend_the_elapsed_deadline(drip_headers):
    class DripHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            headers = b"HTTP/1.0 200 OK\r\nContent-Length: 100\r\n\r\n"
            body = b"x" * 100
            try:
                if not drip_headers:
                    self.wfile.write(headers)
                    data = body
                else:
                    data = headers + body
                for value in data:
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    time.sleep(0.01)  # Always below the normal 0.12s inactivity timeout.
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), DripHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(local_ipc.requests_client.Timeout):
            local_ipc.requests_client.get(f"http://127.0.0.1:{server.server_port}/api/version", timeout=0.12)
        assert time.monotonic() - started < 0.6
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
