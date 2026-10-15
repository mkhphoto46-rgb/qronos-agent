"""Real loopback HTTP cancellation, with no models or user applications."""

import json
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from core import local_ipc
from core import ollama_controller
from core.inference_cancellation import (
    LocalInferenceCancellation, LocalInferenceCancelled,
    current_inference_cancellation, inference_cancellation_scope,
)
from core.ollama_controller import OllamaController


@pytest.fixture
def slow_daemon(monkeypatch):
    state = {"path": "/api/chat", "phase": "headers", "received": [],
             "blocked": threading.Event(), "closed": threading.Event(),
             "body_started": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["received"].append((self.path, json.loads(raw)))
            payload = ({"model_info": {"general.architecture": "fixture"}}
                       if self.path == "/api/show" else {"message": {"content": "normal answer"}})
            body = json.dumps(payload).encode()
            try:
                if self.path != state["path"]:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if state["phase"] == "body":
                    # Publish the phase before flushing bytes: a fast client
                    # can start its body read as soon as headers arrive.
                    state["body_started"].set()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body[:2])
                    self.wfile.flush()
                state["blocked"].set()
                # A silent generation/headers/body peer detects client EOF.
                # Stop must cause it before the client's 10-second timeout.
                self.connection.settimeout(3)
                if self.connection.recv(1) == b"":
                    state["closed"].set()
            except (ConnectionResetError, BrokenPipeError):
                state["closed"].set()
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    readiness = Mock(return_value=False)
    monkeypatch.setattr(OllamaController, "ensure_ready", readiness)
    state["readiness"] = readiness
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def start_worker(operation):
    result = {}

    def run():
        try:
            result["answer"] = operation()
        except BaseException as exc:
            result["exception"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def assert_cancelled(thread, result, token, reason="user_stop"):
    thread.join(timeout=0.75)
    assert not thread.is_alive(), "Cancelled HTTP request did not promptly finish"
    assert "answer" not in result
    assert isinstance(result["exception"], LocalInferenceCancelled)
    assert result["exception"].reason == reason == token.reason
    formatted = "".join(traceback.format_exception(result["exception"]))
    assert "PRIVATE_FIXTURE" not in formatted


@pytest.mark.parametrize("path", ["/api/show", "/api/chat"])
@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("reason", ["user_stop", "runtime_paused", "runtime_shutdown", "preempted"])
def test_cancel_closes_provenance_or_inference_connection(slow_daemon, path, phase, reason):
    state = slow_daemon
    state.update(path=path, phase=phase)
    token = LocalInferenceCancellation()
    controller = OllamaController(state["url"])
    private = "PRIVATE_FIXTURE"
    thread, result = start_worker(lambda: controller.chat(
        "local:4b", prompt=private, timeout_seconds=10, cancellation=token))
    assert state["blocked"].wait(2)
    started = time.monotonic()
    assert token.cancel(reason)
    assert time.monotonic() - started < 0.5
    assert not token._aborters  # Synchronous socket shutdown, before returning.
    assert state["closed"].wait(0.75), "Synthetic daemon did not observe client EOF"
    assert_cancelled(thread, result, token, reason)
    state["readiness"].assert_not_called()
    paths = [route for route, _ in state["received"]]
    assert paths == (["/api/show"] if path == "/api/show" else ["/api/show", "/api/chat"])
    assert all(route != "/api/generate" for route in paths)


def test_already_cancelled_token_sends_no_provenance_or_prompt(slow_daemon):
    token = LocalInferenceCancellation()
    token.cancel()
    with pytest.raises(LocalInferenceCancelled):
        OllamaController(slow_daemon["url"]).chat("local:4b", prompt="private", cancellation=token)
    assert slow_daemon["received"] == []


def test_cancel_between_provenance_and_payload_never_sends_prompt(slow_daemon, monkeypatch):
    verified, proceed = threading.Event(), threading.Event()
    original = local_ipc.LocalModelRequests._assert_local_inference

    def gated(*args):
        original(*args)
        verified.set()
        assert proceed.wait(2)

    monkeypatch.setattr(local_ipc.LocalModelRequests, "_assert_local_inference", staticmethod(gated))
    token = LocalInferenceCancellation()
    thread, result = start_worker(lambda: OllamaController(slow_daemon["url"]).chat(
        "local:4b", prompt="private", cancellation=token))
    assert verified.wait(2)
    token.cancel()
    proceed.set()
    assert_cancelled(thread, result, token)
    assert [route for route, _ in slow_daemon["received"]] == ["/api/show"]


def test_connect_socket_is_registered_before_connect_can_wait(slow_daemon, monkeypatch):
    connecting, proceed = threading.Event(), threading.Event()
    sockets = []
    original = local_ipc._DeadlineSocket.connect

    def gated(sock, address):
        sockets.append(sock)
        connecting.set()
        assert proceed.wait(2)
        return original(sock, address)

    monkeypatch.setattr(local_ipc._DeadlineSocket, "connect", gated)
    token = LocalInferenceCancellation()
    thread, result = start_worker(lambda: OllamaController(slow_daemon["url"]).chat(
        "local:4b", prompt="private", cancellation=token))
    assert connecting.wait(2)
    token.cancel()
    assert sockets[0].fileno() == -1
    proceed.set()
    assert_cancelled(thread, result, token)
    assert slow_daemon["received"] == []


def test_real_pending_loopback_connect_can_be_cancelled(monkeypatch):
    # Exhaust a real loopback listen backlog without accepting clients.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    fillers = []
    try:
        for _ in range(32):
            sock = socket.socket()
            sock.setblocking(False)
            sock.connect_ex(listener.getsockname())
            fillers.append(sock)
        pending = threading.Event()
        token = LocalInferenceCancellation()
        original = local_ipc.select.select

        def observe_pending(*args):
            result = original(*args)
            if not result[1] and not result[2]:
                pending.set()
            return result

        monkeypatch.setattr(local_ipc.select, "select", observe_pending)

        def connect():
            with inference_cancellation_scope(token):
                return local_ipc.requests_client.get(
                    f"http://127.0.0.1:{listener.getsockname()[1]}/api/version", timeout=10)

        thread, result = start_worker(connect)
        assert pending.wait(2), "Loopback connect did not encounter the saturated backlog"
        token.cancel()
        assert_cancelled(thread, result, token)
    finally:
        for sock in fillers:
            sock.close()
        listener.close()


def test_old_token_cannot_close_new_request_on_same_controller(slow_daemon):
    controller = OllamaController(slow_daemon["url"])
    old = LocalInferenceCancellation()
    thread, result = start_worker(lambda: controller.chat("local:4b", prompt="old", cancellation=old))
    assert slow_daemon["blocked"].wait(2)
    old.cancel()
    assert_cancelled(thread, result, old)
    # The client worker and server EOF observer finish independently. Drain
    # the old connection before reusing these events for the next request.
    assert slow_daemon["closed"].wait(0.75)
    slow_daemon["blocked"].clear()
    slow_daemon["closed"].clear()
    current = LocalInferenceCancellation()
    thread, result = start_worker(lambda: controller.chat("local:4b", prompt="new", cancellation=current))
    assert slow_daemon["blocked"].wait(2)
    assert not old.cancel("runtime_shutdown")
    assert not slow_daemon["closed"].wait(0.1)
    assert thread.is_alive() and not current.cancelled
    current.cancel()
    assert_cancelled(thread, result, current)


def test_late_buffered_answer_is_refused_and_scope_is_restored(slow_daemon, monkeypatch):
    slow_daemon["path"] = "/unused"
    token = LocalInferenceCancellation()
    original = local_ipc.requests_client.post

    def late(*args, **kwargs):
        response = original(*args, **kwargs)
        token.cancel()
        return response

    monkeypatch.setattr(local_ipc.requests_client, "post", late)
    with pytest.raises(LocalInferenceCancelled):
        OllamaController(slow_daemon["url"]).chat("local:4b", prompt="private", cancellation=token)
    assert current_inference_cancellation() is None
    assert not token._aborters


def test_outer_worker_scope_applies_to_chat_and_normal_result_is_unchanged(slow_daemon):
    slow_daemon["path"] = "/unused"
    token = LocalInferenceCancellation()
    with inference_cancellation_scope(token):
        assert OllamaController(slow_daemon["url"]).chat("local:4b", prompt="normal") == "normal answer"
    assert not token._aborters and current_inference_cancellation() is None
    assert token.cancel()
    # A fresh future turn on the same shared controller remains usable.
    assert OllamaController(slow_daemon["url"]).chat("local:4b", prompt="next") == "normal answer"


def test_owned_sidecar_body_cancellation_preserves_peer_ownership_check(slow_daemon, monkeypatch):
    slow_daemon.update(path="/api/chat", phase="body")
    token = LocalInferenceCancellation()
    owner = object()
    ownership = Mock(return_value=True)
    monkeypatch.setattr(local_ipc, "process_owns_connection", ownership)

    def request():
        with inference_cancellation_scope(token):
            return local_ipc.request_json("POST", slow_daemon["url"] + "/api/chat",
                                          timeout=10, json={"fixture": True}, owner=owner)

    thread, result = start_worker(request)
    assert slow_daemon["blocked"].wait(2)
    token.cancel()
    assert slow_daemon["closed"].wait(0.75)
    assert_cancelled(thread, result, token)
    assert ownership.call_args.args[0] is owner


def test_http10_body_socket_is_closed_before_cancel_returns(slow_daemon, monkeypatch):
    slow_daemon["phase"] = "body"
    reading, sockets = threading.Event(), []
    original = local_ipc._DeadlineSocket.recv_into

    def observe_body(sock, *args):
        if slow_daemon["body_started"].is_set() and sock._closed and sock.fileno() != -1:
            sockets.append(sock)
            reading.set()
        return original(sock, *args)

    monkeypatch.setattr(local_ipc._DeadlineSocket, "recv_into", observe_body)
    token = LocalInferenceCancellation()
    thread, result = start_worker(lambda: OllamaController(slow_daemon["url"]).chat(
        "local:4b", prompt="private", cancellation=token))
    assert reading.wait(2), "Fixture did not reach HTTP/1.0 response body ownership"
    assert slow_daemon["blocked"].wait(2), "Fixture did not flush its partial body"
    assert token._aborters
    token.cancel()
    assert all(sock.fileno() == -1 for sock in sockets)
    assert slow_daemon["closed"].wait(0.75)
    assert_cancelled(thread, result, token)


def test_reason_is_fixed_vocabulary_and_first_cancellation_wins():
    token = LocalInferenceCancellation()
    with pytest.raises(ValueError, match="Unsupported"):
        token.cancel("PRIVATE_FIXTURE")
    assert not token.cancelled
    assert token.cancel("runtime_paused")
    assert not token.cancel("user_stop")
    assert token.reason == "runtime_paused"


def test_cancel_interrupts_bootstrap_lock_wait_without_starting_any_process(monkeypatch):
    # The start below is faked; auto-start is set here so the test does not depend on the parent environment.
    monkeypatch.setenv(ollama_controller.OLLAMA_AUTO_START_ENV, "1")
    lock = threading.Lock()
    lock.acquire()
    monkeypatch.setattr(ollama_controller, "_BOOTSTRAP_LOCK", lock)
    controller = OllamaController()
    probed = threading.Event()
    monkeypatch.setattr(controller, "_probe", lambda *args: probed.set() or False)
    start = Mock()
    monkeypatch.setattr(controller, "_start_local_server", start)
    token = LocalInferenceCancellation()

    def ready():
        with inference_cancellation_scope(token):
            return controller.ensure_ready()

    try:
        thread, result = start_worker(ready)
        assert probed.wait(2)
        token.cancel()
        assert_cancelled(thread, result, token)
        start.assert_not_called()
    finally:
        lock.release()


def test_cancel_interrupts_bootstrap_poll_wait_with_fake_process(monkeypatch):
    # The start below is faked; auto-start is set here so the test does not depend on the parent environment.
    monkeypatch.setenv(ollama_controller.OLLAMA_AUTO_START_ENV, "1")
    controller = OllamaController()
    monkeypatch.setattr(controller, "_probe", Mock(return_value=False))
    monkeypatch.setattr(controller, "_resolve_local_executable", lambda: "synthetic-local-executable")
    started = threading.Event()
    monkeypatch.setattr(controller, "_start_local_server", lambda *args: started.set())
    token = LocalInferenceCancellation()

    def ready():
        with inference_cancellation_scope(token):
            return controller.ensure_ready()

    thread, result = start_worker(ready)
    assert started.wait(2)
    token.cancel()
    assert_cancelled(thread, result, token)
