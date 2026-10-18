from __future__ import annotations

import json
import os
import threading
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import psutil
import pytest

from core import local_ipc
from core.chatterbox_runtime import ChatterboxRuntime, VoiceUnavailable
from core.memory_runtime import MemorySidecarManager, SidecarEmbeddingProvider
from core.user_memory import EmbeddingUnavailable
from core.whisper_server_runtime import WhisperServerRuntime
from core.windows_sidecar_guard import WindowsSidecarGuard, SidecarGuardUnavailable, WAIT_TIMEOUT


class FakeProcess:
    def __init__(self, pid=1234, dead=False):
        self.pid = pid
        self.dead = dead
        self.terminated = self.killed = False

    def poll(self):
        return 0 if self.dead else None

    def terminate(self):
        self.terminated = True
        self.dead = True

    def kill(self):
        self.killed = True
        self.dead = True

    def wait(self, timeout):
        return 0


@pytest.fixture
def server():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.read_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.respond()

        def respond(self):
            hits.append((self.path, self.headers.get("Authorization")))
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", "/target")
                self.end_headers()
            elif self.path == "/public-redirect":
                self.send_response(302)
                self.send_header("Location", "https://example.com/private")
                self.end_headers()
            elif self.path == "/error":
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"PRIVATE-PROMPT-AND-TOKEN")
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"version":"local","text":"local transcript"}')

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", hits, httpd.server_port
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize("base", [
    "https://127.0.0.1:11435", "http://example.com:11435", "http://0.0.0.0:11435",
    "http://127.0.0.1:11434", "http://127.0.0.1:0", "http://user:pass@127.0.0.1:11435",
    "http://127.0.0.1:11435/api", "http://127.0.0.1:11435?secret=1",
    "http://127.0.0.1:11435#secret", "http://localhost:11435@public.example:11435",
])
def test_memory_rejects_unbounded_endpoints_before_io(base, tmp_path, monkeypatch):
    io_boundary = Mock(side_effect=AssertionError("unexpected IO"))
    monkeypatch.setattr("core.memory_runtime.request_json", io_boundary)
    with pytest.raises(ValueError):
        MemorySidecarManager(base_url=base, project_root=tmp_path)
    with pytest.raises(ValueError):
        SidecarEmbeddingProvider(base_url=base)
    io_boundary.assert_not_called()


@pytest.mark.parametrize("runtime_type", [WhisperServerRuntime, ChatterboxRuntime])
@pytest.mark.parametrize("host", ["0.0.0.0", "example.com", "::", "127.1", "127.0.0.1.evil"])
def test_voice_rejects_non_loopback_hosts(runtime_type, host):
    with pytest.raises(ValueError):
        runtime_type(host=host)


@pytest.mark.parametrize("port", [False, 0, -1, 65536, 8178.5, "8178"])
def test_ports_are_strict_integers(port, tmp_path):
    for runtime_type in (WhisperServerRuntime, ChatterboxRuntime):
        with pytest.raises(ValueError):
            runtime_type(port=port)
    with pytest.raises(ValueError):
        MemorySidecarManager(port=port, project_root=tmp_path)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1, 0, 301])
def test_timeouts_cannot_disable_bounds(timeout):
    with pytest.raises(ValueError):
        SidecarEmbeddingProvider(timeout_seconds=timeout)
    with pytest.raises(ValueError):
        WhisperServerRuntime(request_timeout_seconds=timeout)
    with pytest.raises(ValueError):
        ChatterboxRuntime(request_timeout=timeout)


@pytest.mark.parametrize("transport", ["json", "bytes"])
def test_real_local_requests_bypass_proxy_and_netrc(server, monkeypatch, tmp_path, transport):
    base, hits, _ = server
    proxy = "http://127.0.0.1:1"
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(variable, proxy)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    netrc = tmp_path / "private.netrc"
    netrc.write_text("machine 127.0.0.1 login private password PRIVATE-PROMPT-AND-TOKEN")
    monkeypatch.setenv("NETRC", str(netrc))
    # Prove localhost is canonicalized rather than resolved by the host OS.
    url = base.replace("127.0.0.1", "localhost") + "/ok"
    if transport == "json":
        assert local_ipc.request_json("GET", url, timeout=1)["version"] == "local"
    else:
        assert b"local" in local_ipc.request_bytes(urllib.request.Request(url), timeout=1)
    assert hits == [("/ok", None)]


@pytest.mark.parametrize("transport", ["json", "bytes"])
@pytest.mark.parametrize("path", ["/redirect", "/public-redirect", "/error"])
def test_redirects_and_server_errors_are_sanitized(server, transport, path):
    base, hits, _ = server
    private = "PRIVATE-PROMPT-AND-TOKEN"
    with pytest.raises(local_ipc.LocalIpcError) as caught:
        if transport == "json":
            local_ipc.request_json("POST", base + path, timeout=1, json={"input": private})
        else:
            local_ipc.request_bytes(urllib.request.Request(base + path, data=private.encode()), timeout=1)
    formatted = "".join(traceback.format_exception(caught.value))
    assert "PRIVATE-PROMPT-AND-TOKEN" not in formatted
    assert "example.com" not in formatted
    assert hits == [(path, None)]  # No second request, even to another local path.
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("transport", ["json", "bytes"])
def test_public_request_is_rejected_before_transport(monkeypatch, transport):
    boundary = Mock(side_effect=AssertionError("network IO"))
    monkeypatch.setattr(local_ipc.urllib.request, "build_opener", boundary)
    with pytest.raises(ValueError):
        if transport == "json":
            local_ipc.request_json("GET", "http://example.com:80/path", timeout=1)
        else:
            local_ipc.request_bytes(urllib.request.Request("http://example.com:80/path"), timeout=1)
    boundary.assert_not_called()


def test_real_listener_ownership_requires_captured_live_process(server):
    _, _, port = server
    assert local_ipc.process_owns_listener(FakeProcess(pid=os.getpid()), port)
    assert not local_ipc.process_owns_listener(FakeProcess(pid=os.getpid(), dead=True), port)
    assert not local_ipc.process_owns_listener(FakeProcess(pid=2**30), port)


@pytest.mark.parametrize("transport", ["json", "bytes"])
def test_connection_owner_is_verified_before_private_request_bytes(server, transport):
    base, hits, _ = server
    owner = FakeProcess(pid=2**30)  # The HTTP server belongs to this test process, not owner.
    request = urllib.request.Request(base + "/ok", data=b"private audio")
    with pytest.raises(local_ipc.LocalIpcError):
        if transport == "json":
            local_ipc.request_json("POST", base + "/ok", timeout=1, json={"input": "private"}, owner=owner)
        else:
            local_ipc.request_bytes(request, timeout=1, owner=owner)
    assert hits == []  # TCP was accepted, but no HTTP method/headers/body reached it.


@pytest.mark.parametrize("transport", ["json", "bytes"])
def test_real_owned_connection_preserves_local_inference(server, transport):
    base, hits, _ = server
    owner = FakeProcess(pid=os.getpid())
    if transport == "json":
        assert local_ipc.request_json("POST", base + "/ok", timeout=1, json={"input": "local"}, owner=owner)["version"] == "local"
    else:
        assert b"local" in local_ipc.request_bytes(urllib.request.Request(base + "/ok", data=b"local"), timeout=1, owner=owner)
    assert hits == [("/ok", None)]


def test_listener_takeover_during_connect_is_refused_before_send(server, monkeypatch):
    base, hits, port = server
    owner = FakeProcess(pid=os.getpid())
    assert local_ipc.process_owns_listener(owner, port)
    # Represents expiry/exit or takeover after the preflight listener check.
    connection_check = Mock(return_value=False)
    monkeypatch.setattr(local_ipc, "process_owns_connection", connection_check)
    with pytest.raises(local_ipc.LocalIpcError):
        local_ipc.request_bytes(urllib.request.Request(base + "/ok", data=b"private"), timeout=1, owner=owner)
    connection_check.assert_called_once()
    assert hits == []


@pytest.mark.parametrize("reused, listening, denied", [(True, True, False), (False, False, False), (False, True, True)])
def test_listener_proof_fails_closed_for_reuse_wrong_port_or_access_denial(monkeypatch, reused, listening, denied):
    class Identity:
        def __init__(self, pid):
            self.pid = pid

        def is_running(self):
            return not reused

        def net_connections(self, kind):
            if denied:
                raise psutil.AccessDenied(self.pid)
            return [SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(ip="127.0.0.1", port=8000 if listening else 8001))]

    monkeypatch.setattr(local_ipc.psutil, "Process", Identity)
    assert not local_ipc.process_owns_listener(FakeProcess(), 8000)


def test_pid_file_and_ollama_name_never_authorize_shutdown_or_unload(tmp_path, monkeypatch):
    pid_file = tmp_path / "runtime/temp/qronos_memory_sidecar_owner.json"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text(json.dumps({"pid": 1234, "host": "127.0.0.1:11435"}))
    process_reader = Mock(side_effect=AssertionError("must not adopt persisted PID"))
    requests = Mock(side_effect=AssertionError("must not unload unowned server"))
    monkeypatch.setattr("core.memory_runtime.psutil.Process", process_reader)
    monkeypatch.setattr("core.memory_runtime.request_json", requests)
    manager = MemorySidecarManager(project_root=tmp_path)
    manager.unload_owned_model()
    manager.shutdown()
    assert not manager.owns_server
    process_reader.assert_not_called()
    requests.assert_not_called()
    assert pid_file.exists()  # Do not delete another owner's metadata.


def test_memory_reused_identity_cannot_unload_or_kill(tmp_path, monkeypatch):
    manager = MemorySidecarManager(project_root=tmp_path)
    identity = Mock()
    identity.is_running.return_value = False
    manager._identity = identity
    manager._process = FakeProcess()
    manager._owned = True
    monkeypatch.setattr("core.memory_runtime.process_owns_listener", Mock(return_value=False))
    request = Mock(side_effect=AssertionError("must not unload"))
    monkeypatch.setattr("core.memory_runtime.request_json", request)
    manager.unload_owned_model()
    manager.shutdown()
    request.assert_not_called()
    identity.terminate.assert_not_called()
    identity.kill.assert_not_called()


def test_memory_live_identity_termination_ignores_tampered_pid_file(tmp_path):
    manager = MemorySidecarManager(project_root=tmp_path)
    identity = Mock()
    identity.is_running.return_value = True
    manager._identity = identity
    manager._process = FakeProcess()
    manager._owned = True
    manager._write_pid_file(999999)
    manager.shutdown()
    identity.terminate.assert_called_once_with()
    identity.wait.assert_called_once_with(timeout=2.0)


def test_memory_embedding_requires_ownership_and_normalizes_valid_vectors(monkeypatch):
    request = Mock(return_value={"embeddings": [[3, 4]]})
    monkeypatch.setattr("core.memory_runtime.request_json", request)
    provider = SidecarEmbeddingProvider()
    with pytest.raises(EmbeddingUnavailable):
        provider.embed(["PRIVATE-PROMPT-AND-TOKEN"])
    request.assert_not_called()
    provider = SidecarEmbeddingProvider(owner_check=lambda: True)
    np.testing.assert_allclose(provider.embed(["local text"]), [[0.6, 0.8]])
    assert request.call_args.kwargs["json"]["options"] == {"num_gpu": 0}


@pytest.mark.parametrize("payload", [{"embeddings": [["PRIVATE-PROMPT-AND-TOKEN"]]}, {"embeddings": [[float("nan")]]}, []])
def test_invalid_embedding_response_never_leaks_server_content(monkeypatch, payload):
    monkeypatch.setattr("core.memory_runtime.request_json", Mock(return_value=payload))
    provider = SidecarEmbeddingProvider(owner_check=lambda: True)
    with pytest.raises(EmbeddingUnavailable) as caught:
        provider.embed(["local"])
    assert "PRIVATE-PROMPT-AND-TOKEN" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("runtime_type", [WhisperServerRuntime, ChatterboxRuntime])
def test_voice_request_refuses_dead_or_wrong_listener(tmp_path, monkeypatch, runtime_type):
    prefix = "core.whisper_server_runtime" if runtime_type is WhisperServerRuntime else "core.chatterbox_runtime"
    runtime = runtime_type()
    request = Mock(side_effect=AssertionError("must not send private audio/text"))
    monkeypatch.setattr(prefix + ".request_bytes", request)
    monkeypatch.setattr(prefix + ".process_owns_listener", Mock(return_value=False))
    if runtime_type is WhisperServerRuntime:
        monkeypatch.setattr(runtime, "prepare", lambda: None)
        audio = tmp_path / "local.wav"
        audio.write_bytes(b"PRIVATE-PROMPT-AND-TOKEN")
        with pytest.raises(RuntimeError, match="owns"):
            runtime.transcribe_file(audio)
    else:
        runtime._active_port = 8000
        with pytest.raises(VoiceUnavailable, match="owns"):
            runtime._request_speech("PRIVATE-PROMPT-AND-TOKEN")
    request.assert_not_called()


@pytest.mark.parametrize("runtime_type", [WhisperServerRuntime, ChatterboxRuntime])
def test_voice_network_errors_suppress_private_body_and_cause(tmp_path, monkeypatch, runtime_type):
    prefix = "core.whisper_server_runtime" if runtime_type is WhisperServerRuntime else "core.chatterbox_runtime"
    runtime = runtime_type()
    monkeypatch.setattr(prefix + ".process_owns_listener", Mock(return_value=True))
    monkeypatch.setattr(prefix + ".request_bytes", Mock(side_effect=RuntimeError("PRIVATE-PROMPT-AND-TOKEN")))
    with pytest.raises(RuntimeError) as caught:
        if runtime_type is WhisperServerRuntime:
            monkeypatch.setattr(runtime, "prepare", lambda: None)
            audio = tmp_path / "local.wav"
            audio.write_bytes(b"audio")
            runtime.transcribe_file(audio)
        else:
            runtime._active_port = 8000
            runtime._request_speech("private")
    assert "PRIVATE-PROMPT-AND-TOKEN" not in "".join(traceback.format_exception(caught.value))


def test_whisper_transcript_json_error_is_sanitized(tmp_path, monkeypatch):
    runtime = WhisperServerRuntime()
    monkeypatch.setattr(runtime, "prepare", lambda: None)
    monkeypatch.setattr("core.whisper_server_runtime.process_owns_listener", Mock(return_value=True))
    monkeypatch.setattr("core.whisper_server_runtime.request_bytes", Mock(return_value=b"PRIVATE-PROMPT-AND-TOKEN"))
    audio = tmp_path / "local.wav"
    audio.write_bytes(b"audio")
    with pytest.raises(RuntimeError) as caught:
        runtime.transcribe_file(audio)
    assert "PRIVATE-PROMPT-AND-TOKEN" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("observed_pid, wait_result", [(9999, WAIT_TIMEOUT), (1234, 0), (0, WAIT_TIMEOUT)])
def test_job_binding_rejects_wrong_or_dead_captured_handle(monkeypatch, observed_pid, wait_result):
    kernel = Mock()
    kernel.GetProcessId.return_value = observed_pid
    kernel.WaitForSingleObject.return_value = wait_result
    monkeypatch.setattr("core.windows_sidecar_guard._kernel32", lambda: kernel)
    guard = WindowsSidecarGuard("Local.Test.Sidecar")
    guard._job_handle = 100
    try:
        with pytest.raises(SidecarGuardUnavailable):
            guard.bind_process(1234, process_handle=200)
        kernel.AssignProcessToJobObject.assert_not_called()
        kernel.OpenProcess.assert_not_called()  # Never reopen a possibly recycled PID.
    finally:
        guard.close()


def test_job_binding_uses_only_exact_captured_handle(monkeypatch):
    kernel = Mock()
    kernel.GetProcessId.return_value = 1234
    kernel.WaitForSingleObject.return_value = WAIT_TIMEOUT
    kernel.AssignProcessToJobObject.return_value = True
    monkeypatch.setattr("core.windows_sidecar_guard._kernel32", lambda: kernel)
    guard = WindowsSidecarGuard("Local.Test.Sidecar")
    guard._job_handle = 100
    try:
        guard.bind_process(1234, process_handle=200)
        kernel.AssignProcessToJobObject.assert_called_once_with(100, 200)
        kernel.OpenProcess.assert_not_called()
        kernel.CloseHandle.assert_not_called()  # Popen owns the child handle.
    finally:
        guard.close()


@pytest.mark.parametrize("diagnostic_failure", [False, True])
def test_memory_launch_captures_identity_and_binds_child_lifetime(tmp_path, monkeypatch, diagnostic_failure):
    child = FakeProcess()
    child._handle = 200
    identity = Mock()
    identity.is_running.return_value = True
    identity.create_time.return_value = 1700000000.5
    guard = Mock()
    guard.spawn_contained.return_value = child
    popen = Mock(return_value=child)
    manager = MemorySidecarManager(project_root=tmp_path, sidecar_guard_factory=lambda: guard)
    monkeypatch.setattr(manager, "_api_ready", Mock(side_effect=[False, True]))
    monkeypatch.setattr(manager, "_port_open", lambda: False)
    monkeypatch.setattr(manager, "_ollama_executable", lambda: "owned-ollama")
    monkeypatch.setattr("core.memory_runtime.subprocess.Popen", popen)
    monkeypatch.setattr("core.memory_runtime.psutil.Process", Mock(return_value=identity))
    monkeypatch.setattr("core.memory_runtime.process_owns_listener", Mock(return_value=True))
    if diagnostic_failure:
        monkeypatch.setattr(manager, "_write_pid_file", Mock(side_effect=PermissionError("diagnostic read-only")))
    assert manager.ensure_server()
    assert manager.owned_process is identity
    identity.create_time.assert_called_once_with()
    if os.name == "nt":
        # The child's lifetime is bound by creating it INSIDE the guard's Job (suspended, assigned,
        # resumed), never by binding an already running Popen.
        (argv,), options = guard.spawn_contained.call_args
        assert argv == ["owned-ollama", "serve"] and options["env"]["OLLAMA_HOST"] == "127.0.0.1:11435"
        popen.assert_not_called()
        guard.bind_process.assert_not_called()
        lifecycle = manager.lifecycle
        assert (lifecycle.generation, lifecycle.server_pid, lifecycle.server_create_time) == (1, child.pid,
                                                                                             1700000000.5)
    manager.shutdown()
    identity.terminate.assert_called_once_with()


def test_memory_unload_uses_captured_owner_and_cannot_reload_external_service(tmp_path, monkeypatch):
    manager = MemorySidecarManager(project_root=tmp_path)
    identity = Mock()
    manager._identity = identity
    manager._process = FakeProcess()
    manager._owned = True
    monkeypatch.setattr("core.memory_runtime.process_owns_listener", Mock(return_value=True))
    monkeypatch.setattr(manager, "_api_ready", lambda: True)
    request = Mock(return_value={})
    monkeypatch.setattr("core.memory_runtime.request_json", request)
    manager.unload_owned_model()
    assert request.call_args.kwargs["owner"] is identity
    assert request.call_args.kwargs["json"]["keep_alive"] == "0s"
    manager._process.dead = True
    request.reset_mock()
    assert not manager.warm_model()
    request.assert_not_called()
    identity.is_running.return_value = False
    manager.shutdown()


@pytest.mark.parametrize("runtime_type", [WhisperServerRuntime, ChatterboxRuntime])
def test_eviction_remains_available_during_an_owned_inference_request(tmp_path, monkeypatch, runtime_type):
    runtime = runtime_type()
    process = FakeProcess()
    runtime._process = process
    started = threading.Event()
    finish = threading.Event()
    released = threading.Event()
    errors = []
    prefix = "core.whisper_server_runtime" if runtime_type is WhisperServerRuntime else "core.chatterbox_runtime"
    monkeypatch.setattr(prefix + ".process_owns_listener", Mock(return_value=True))

    def blocked_request(request, *, timeout, owner):
        assert owner is process
        started.set()
        assert finish.wait(timeout=3)
        return b'{"text":"local"}' if runtime_type is WhisperServerRuntime else b"a" * 1001

    monkeypatch.setattr(prefix + ".request_bytes", blocked_request)
    if runtime_type is WhisperServerRuntime:
        monkeypatch.setattr(runtime, "prepare", lambda: None)
        audio = tmp_path / "local.wav"
        audio.write_bytes(b"audio")
        inference = lambda: runtime.transcribe_file(audio)
        evict = runtime.shutdown
    else:
        runtime._active_port = 8000
        inference = lambda: runtime._request_speech("local")
        evict = runtime.emergency_release

    def infer():
        try:
            inference()
        except Exception as exc:
            errors.append(exc)

    def release():
        try:
            evict()
            released.set()
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=infer)
    worker.start()
    assert started.wait(timeout=2)
    releaser = threading.Thread(target=release)
    releaser.start()
    try:
        assert released.wait(timeout=1), "Eviction must not wait for inference's network deadline."
        assert process.terminated or process.killed
    finally:
        finish.set()
        worker.join(timeout=3)
        releaser.join(timeout=3)
    assert not errors
