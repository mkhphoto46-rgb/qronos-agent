"""Small, fail-closed HTTP boundary for Qronos's local model sidecars."""

from __future__ import annotations

import http.client
import json as json_module
import math
import urllib.error
import urllib.parse
import urllib.request

import psutil


class LocalIpcError(RuntimeError):
    """A local request failed; the message never contains server/user data."""


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
