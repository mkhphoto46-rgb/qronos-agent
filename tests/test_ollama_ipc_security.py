from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import traceback
from unittest.mock import Mock

import pytest
import requests

from core.brain_runtime import BrainMessage, BrainMessageRole
from core.ollama_controller import OllamaController


@pytest.mark.parametrize("url", (
    "http://192.0.2.10:11434", "https://example.com", "http://127.0.0.1.example.com",
    "http://localhost.example.com", "http://user:password@127.0.0.1:11434",
    "http://127.0.0.1:11434/api/chat", "http://127.0.0.1:11434?token=private",
    "http://127.0.0.1:11434#private", "http://127.0.0.1:99999", "http://127.0.0.1:0",
    "ftp://127.0.0.1:11434", "http://2130706433:11434", "http://127.1:11434",
))
def test_remote_or_ambiguous_endpoint_is_rejected_before_io(url, monkeypatch):
    post, get = Mock(), Mock()
    monkeypatch.setattr("core.ollama_controller.requests.post", post)
    monkeypatch.setattr("core.ollama_controller.requests.get", get)
    with pytest.raises(ValueError, match="loopback") as caught:
        OllamaController(url)
    assert url not in str(caught.value)
    get.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("url,expected", (
    ("http://localhost:11434/", "http://127.0.0.1:11434"),
    ("http://127.0.0.1:12345", "http://127.0.0.1:12345"),
    ("http://[::1]:11434", "http://[::1]:11434"),
))
def test_explicit_loopback_endpoint_is_accepted(url, expected):
    assert OllamaController(url).base_url == expected


def test_reconfiguration_cannot_switch_to_remote_endpoint():
    runtime = OllamaController()
    with pytest.raises(ValueError, match="loopback"):
        runtime.base_url = "http://example.com"
    assert runtime.base_url == "http://127.0.0.1:11434"


@pytest.mark.parametrize("failure", ("connection", "timeout", "http", "request", "invalid_json"))
def test_error_and_formatted_traceback_contain_only_safe_request_metadata(failure, monkeypatch):
    private = "PRIVATE_PROMPT_RESPONSE_SECRET"
    response = requests.Response()
    response.status_code = 500 if failure == "http" else 200
    response._content = (json.dumps({"error": private}) if failure == "http" else private).encode()
    response.url = f"http://127.0.0.1:11434/private?token={private}"
    errors = {
        "connection": requests.ConnectionError(private),
        "timeout": requests.Timeout(private),
        "request": requests.RequestException(private),
    }
    post = Mock(return_value=response, side_effect=errors.get(failure))
    monkeypatch.setattr("core.ollama_controller.requests.post", post)
    monkeypatch.setattr(OllamaController, "ensure_ready", lambda *args, **kwargs: False)
    with pytest.raises((RuntimeError, TimeoutError)) as caught:
        OllamaController().chat(
            model_name="qwen3-vl:4b-instruct", timeout_seconds=17,
            messages=[BrainMessage(BrainMessageRole.USER, private)],
        )
    message = str(caught.value)
    assert "model=qwen3-vl:4b-instruct" in message
    assert "endpoint=/api/chat" in message
    assert "timeout=17s" in message
    assert "messages=1; images=0" in message
    assert private not in message
    assert caught.value.__suppress_context__
    # Format the exception chain without this test's own source frames.
    assert private not in "".join(traceback.format_exception(type(caught.value), caught.value, None))
    if failure == "http":
        assert "HTTP 500" in message
        assert "not installed" not in message


def test_invalid_model_identifier_is_redacted_in_errors(monkeypatch):
    monkeypatch.setattr("core.ollama_controller.requests.post", Mock(side_effect=requests.RequestException()))
    with pytest.raises(RuntimeError) as caught:
        OllamaController().chat(model_name="private\nAuthorization: secret", prompt="secret")
    assert "<invalid model identifier>" in str(caught.value)
    assert "private" not in str(caught.value)
    assert "Authorization" not in str(caught.value)


@pytest.mark.parametrize("deadline", (float("nan"), float("inf"), float("-inf"), 0, -1))
def test_invalid_deadline_is_rejected_before_io(deadline, monkeypatch):
    post = Mock()
    monkeypatch.setattr("core.ollama_controller.requests.post", post)
    with pytest.raises(ValueError):
        OllamaController().chat("qwen3:4b", prompt="test", timeout_seconds=deadline)
    post.assert_not_called()


def test_local_ipc_never_follows_redirects_or_environment_proxies(monkeypatch):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.redirect()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.redirect()

        def redirect(self):
            received.append(self.path)
            self.send_response(307)
            self.send_header("Location", "/must-not-be-followed")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    # No public endpoint or unrelated user service is contacted by this test.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    runtime = OllamaController(f"http://127.0.0.1:{server.server_port}")
    try:
        assert runtime.health_check() is False
        with pytest.raises(RuntimeError):
            runtime.list_running_models()
        with pytest.raises(RuntimeError, match="installed-model inspection is unavailable"):
            runtime.installed_model_names()
        with pytest.raises(RuntimeError):
            runtime.stop_model("qwen3:4b")
        with pytest.raises(RuntimeError, match="HTTP 307"):
            runtime.chat("qwen3:4b", prompt="private", timeout_seconds=1)
        assert received == ["/api/version", "/api/ps", "/api/tags", "/api/show", "/api/show"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("models,expected", (
    ([], ()),
    ([{"name": "qwen3:4b"}, {"name": "qwen3:14b"}, {"name": "qwen3:4b"}], ("qwen3:14b", "qwen3:4b")),
))
def test_installed_model_metadata_is_read_only_and_bounded(models, expected, monkeypatch):
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({"models": models}).encode()
    get = Mock(return_value=response)
    start, post = Mock(), Mock()
    monkeypatch.setattr("core.ollama_controller.requests.get", get)
    monkeypatch.setattr("core.ollama_controller.requests.post", post)
    monkeypatch.setattr(OllamaController, "ensure_ready", start)
    assert OllamaController().installed_model_names() == expected
    get.assert_called_once_with(
        "http://127.0.0.1:11434/api/tags", timeout=3,
        allow_redirects=False, proxies={"http": "", "https": ""},
    )
    start.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("data", (
    {}, [], {"models": None}, {"models": [None]}, {"models": [{}]},
    {"models": [{"name": 1}]}, {"models": [{"name": " "}]},
    {"models": [{"name": "private\nsecret"}]}, {"models": [{"name": "a" * 257}]},
))
def test_malformed_installed_model_metadata_is_not_treated_as_no_models(data, monkeypatch):
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(data).encode()
    monkeypatch.setattr("core.ollama_controller.requests.get", Mock(return_value=response))
    with pytest.raises(RuntimeError, match="invalid.*metadata") as caught:
        OllamaController().installed_model_names()
    assert "private" not in str(caught.value)


def test_unavailable_model_server_is_distinct_from_no_installed_models(monkeypatch):
    monkeypatch.setattr("core.ollama_controller.requests.get", Mock(side_effect=requests.ConnectionError("private")))
    with pytest.raises(RuntimeError, match="inspection is unavailable") as caught:
        OllamaController().installed_model_names()
    assert "private" not in str(caught.value)
