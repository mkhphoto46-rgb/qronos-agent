"""Small, fail-closed HTTP boundary for Qronos's local model sidecars."""

from __future__ import annotations

import http.client
import json as json_module
import math
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

import psutil
import requests as _requests
from urllib3.connection import HTTPConnection as _HttpConnection
from urllib3.connectionpool import HTTPConnectionPool as _HttpPool


MAX_MODEL_RESPONSE_BYTES = 8 * 1024 * 1024
_MODEL_ROUTES = frozenset({
    "/api/version", "/api/tags", "/api/ps", "/api/chat", "/api/generate",
    "/api/embed", "/api/show", "/api/delete",
})


class LocalIpcError(RuntimeError):
    """A local request failed; the message never contains server/user data."""


def _deadline_adapter(deadline: float, budget: dict):
    """Enforce elapsed deadlines even if a peer drips HTTP headers/body bytes.

    requests' ordinary timeout is an inactivity timeout. SocketIO uses
    recv_into, so decreasing that timeout before every read prevents a slow
    local peer from holding a model request open indefinitely.
    """
    class DeadlineSocket(socket.socket):
        def __init__(self, original, request_deadline):
            idle_timeout = original.gettimeout()
            super().__init__(original.family, original.type, original.proto,
                             fileno=original.detach())
            self.deadline = request_deadline
            self.settimeout(idle_timeout)

        def _arm(self):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("Local model elapsed deadline exceeded.")
            self.settimeout(min(self.gettimeout() or remaining, remaining))

        def recv_into(self, *args):
            self._arm()
            return super().recv_into(*args)

        def sendall(self, *args, **kwargs):
            self._arm()
            return super().sendall(*args, **kwargs)

    class DeadlineConnection(_HttpConnection):
        def request(self, *args, **kwargs):
            self.request_deadline = min(deadline, time.monotonic() + float(self.timeout))
            budget["deadline"] = self.request_deadline
            if isinstance(self.sock, DeadlineSocket):
                self.sock.deadline = self.request_deadline
            return super().request(*args, **kwargs)

        def connect(self):
            super().connect()
            self.sock = DeadlineSocket(self.sock, self.request_deadline)

    class DeadlinePool(_HttpPool):
        ConnectionCls = DeadlineConnection

    class DeadlineAdapter(_requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = self.poolmanager.pool_classes_by_scheme.copy()
            self.poolmanager.pool_classes_by_scheme["http"] = DeadlinePool

    return DeadlineAdapter()


def local_model_base_url(value: str) -> str:
    """Primary Ollama may be user-owned, but its origin must be explicit loopback."""
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme != "http" or host not in {"127.0.0.1", "localhost", "::1"}
            or (port is not None and not 1 <= port <= 65535)
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Local model IPC requires an explicit loopback HTTP endpoint.") from None
    host = "127.0.0.1" if host == "localhost" else host
    authority = f"[{host}]" if host == "::1" else host
    return f"http://{authority}" + (f":{port}" if port is not None else "")


def _model_request_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    origin = local_model_base_url(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")))
    if parsed.path not in _MODEL_ROUTES or parsed.query or parsed.fragment:
        raise ValueError("Local model IPC requires a reviewed fixed API route.")
    return origin + parsed.path


class LocalModelRequests:
    """Narrow requests-compatible seam, with all environment trust disabled.

    Kept separate from sidecar ownership: an explicitly installed, user-owned
    Ollama service remains usable. Only these reviewed local API routes exist.
    """

    RequestException = _requests.RequestException
    ConnectionError = _requests.ConnectionError
    Timeout = _requests.Timeout
    HTTPError = _requests.HTTPError

    def get(self, url: str, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._request("POST", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self._request("DELETE", url, **kwargs)

    @staticmethod
    def _read_response(response):
        if not 200 <= response.status_code < 300:
            safe = _requests.Response()
            safe.status_code = response.status_code
            safe.url = response.url
            safe._content = b""
            raise _requests.HTTPError("Local model HTTP request failed.", response=safe)
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > MAX_MODEL_RESPONSE_BYTES:
                raise _requests.RequestException("Local model response exceeded its limit.")
            chunks.append(chunk)
        response._content = b"".join(chunks)
        response._content_consumed = True
        return response

    @staticmethod
    def _assert_local_inference(session, url, payload, timeout):
        model = payload.get("model") if isinstance(payload, dict) else None
        if (
            not isinstance(model, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}", model)
            or "://" in model or model.lower().endswith(("-cloud", ":cloud"))
        ):
            raise PermissionError("Only installed local model inference is permitted.")
        parsed = urllib.parse.urlsplit(url)
        origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        with session.post(origin + "/api/show", json={"model": model},
                          timeout=min(timeout, 5.0), allow_redirects=False, stream=True) as response:
            LocalModelRequests._read_response(response)
            try:
                metadata = response.json()
            except ValueError:
                raise _requests.RequestException("Local model provenance metadata is invalid.") from None
        if not isinstance(metadata, dict):
            raise _requests.RequestException("Local model provenance metadata is invalid.")
        if any(metadata.get(key) is not None and not isinstance(metadata[key], str)
               for key in ("remote_host", "remote_model")):
            raise _requests.RequestException("Local model provenance metadata is invalid.")
        if metadata.get("remote_host") or metadata.get("remote_model"):
            raise PermissionError("Remote model inference through local Ollama is disabled.")
        # Cloud aliases must not pass merely because their displayed name looks
        # local. A real local model exposes parsed tensor/model metadata.
        if not isinstance(metadata.get("model_info"), dict) or not metadata["model_info"]:
            raise _requests.RequestException("Local model provenance could not be established.")

    @staticmethod
    def _request(method: str, url: str, *, timeout: float, json=None,
                 allow_redirects: bool = False, proxies=None):
        url = _model_request_url(url)
        timeout = local_timeout(timeout)
        if allow_redirects:
            raise ValueError("Local model redirects are refused.")
        deadline = time.monotonic() + timeout
        budget = {"deadline": deadline}
        try:
            with _requests.Session() as session:
                # Disables environment/system proxies, netrc credentials and
                # environment certificate configuration before request preparation.
                session.trust_env = False
                session.mount("http://", _deadline_adapter(deadline, budget))
                if urllib.parse.urlsplit(url).path in {"/api/chat", "/api/embed", "/api/generate"}:
                    LocalModelRequests._assert_local_inference(session, url, json, timeout)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _requests.Timeout("Local model request timed out.")
                with session.request(method, url, json=json, timeout=remaining,
                                     allow_redirects=False, stream=True) as response:
                    return LocalModelRequests._read_response(response)
        except _requests.HTTPError:
            raise
        except _requests.Timeout:
            raise _requests.Timeout("Local model request timed out.") from None
        except _requests.ConnectionError:
            if time.monotonic() >= budget["deadline"]:
                raise _requests.Timeout("Local model request timed out.") from None
            raise _requests.ConnectionError("Local model connection failed.") from None
        except _requests.RequestException:
            raise _requests.RequestException("Local model request failed.") from None


requests_client = LocalModelRequests()


def local_port(port: int) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Local IPC port must be an integer between 1 and 65535.")
    return port


def local_host(host: str) -> str:
    if not isinstance(host, str) or host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Local IPC host must be 127.0.0.1 or localhost.")
    # Do not depend on DNS/hosts-file resolution of localhost.
    return "127.0.0.1"


def local_timeout(timeout: float) -> float:
    value = float(timeout)
    if not math.isfinite(value) or not 0 < value <= 300:
        raise ValueError("Local IPC timeout must be finite and within 300 seconds.")
    return value


def local_base_url(base_url: str, *, required_port: int | None = None) -> str:
    try:
        parsed = urllib.parse.urlsplit(base_url)
        port = local_port(parsed.port)
        host = local_host(parsed.hostname)
        if (
            parsed.scheme != "http" or parsed.username is not None
            or parsed.password is not None or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment
            or (required_port is not None and port != required_port)
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Local IPC endpoint must be a bounded loopback HTTP origin.") from None
    return f"http://{host}:{port}"


def _local_request_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    base = local_base_url(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")))
    if parsed.query or parsed.fragment or not parsed.path.startswith("/"):
        raise ValueError("Local IPC request must use a fixed local path.")
    return base + parsed.path


def request_json(method: str, url: str, *, timeout: float, json=None, owner=None):
    """JSON HTTP without redirects, environment proxies/netrc, or raw failures."""
    url = _local_request_url(url)
    timeout = local_timeout(timeout)
    if method not in {"GET", "POST"}:
        raise ValueError("Unsupported local IPC method.")
    body = None if json is None else json_module.dumps(json).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method=method, headers={"Content-Type": "application/json"},
    )
    try:
        raw = request_bytes(request, timeout=timeout, owner=owner)
        return json_module.loads(raw.decode("utf-8"))
    except Exception:
        raise LocalIpcError("Local sidecar JSON request failed.") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LocalIpcError("Local sidecar redirects are refused.")


def request_bytes(request: urllib.request.Request, *, timeout: float, owner=None) -> bytes:
    # Rebuild only the URL: local_host canonicalizes localhost to numeric IPv4.
    request.full_url = _local_request_url(request.full_url)
    timeout = local_timeout(timeout)
    # Verify the *accepted connection* belongs to the captured process before
    # HTTP headers or sensitive audio/text leave the client socket. A listener
    # check alone has a check-to-connect race when that process exits.
    class OwnedConnection(http.client.HTTPConnection):
        def connect(self):
            super().connect()
            if owner is not None and not process_owns_connection(owner, self.sock):
                self.close()
                raise LocalIpcError("Local sidecar connection ownership is unavailable.")

    class OwnedHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(OwnedConnection, req)

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(), OwnedHandler(),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise LocalIpcError("Local sidecar returned a non-success response.")
            return response.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        raise LocalIpcError("Local sidecar binary request failed.") from None
    except Exception:
        raise LocalIpcError("Local sidecar binary request failed.") from None


def process_owns_listener(process, port: int) -> bool:
    """Prove a still-live captured Popen/psutil process owns this loopback port.

    A pid file, executable name, successful TCP connect, or HTTP response is not
    proof. Inspection denied by the OS therefore means unavailable, not owned.
    """
    try:
        port = local_port(port)
        if hasattr(process, "poll") and process.poll() is not None:
            return False
        proc = process if isinstance(process, psutil.Process) else psutil.Process(process.pid)
        if not proc.is_running():
            return False
        connections = proc.net_connections(kind="tcp")
        owned = any(
            conn.status == psutil.CONN_LISTEN
            and conn.laddr.ip == "127.0.0.1" and conn.laddr.port == port
            for conn in connections
        )
        return owned and proc.is_running() and (
            not hasattr(process, "poll") or process.poll() is None
        )
    except (psutil.Error, OSError, ValueError, AttributeError):
        return False


def process_owns_connection(process, sock) -> bool:
    """Verify the peer's accepted TCP connection without sending HTTP data."""
    try:
        if hasattr(process, "poll") and process.poll() is not None:
            return False
        proc = process if isinstance(process, psutil.Process) else psutil.Process(process.pid)
        if not proc.is_running():
            return False
        peer = sock.getpeername()
        client = sock.getsockname()
        if peer[0] != "127.0.0.1" or client[0] != "127.0.0.1":
            return False
        owned = any(
            conn.status == psutil.CONN_ESTABLISHED
            and tuple(conn.laddr) == peer and tuple(conn.raddr) == client
            for conn in proc.net_connections(kind="tcp")
        )
        return owned and proc.is_running() and (
            not hasattr(process, "poll") or process.poll() is None
        )
    except (psutil.Error, OSError, ValueError, AttributeError):
        return False
