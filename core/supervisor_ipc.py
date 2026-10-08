"""
The authenticated local channel between Qronos processes.

Used by the Supervisor and the user runtime, and by the runtime and the
Elevated Action Broker. It is deliberately small, and every wait in it has a
deadline:

    **Local only, Windows only.** Sessions run over Windows named pipes
    (``\\\\.\\pipe\\qronos-…``). A host/port address is refused before
    anything is opened, so this can never become a network listener. Pipe
    names carry 128 random bits and the first instance is created before the
    peer is launched, so another process can neither guess the name in
    advance nor take it over. The supervisor and broker are Windows
    components; off Windows a session is refused rather than run over a
    stream transport whose reads could not be bounded (see below).

    **Identity first.** The server reads the connecting process id from the
    pipe (``core/pipe_peer.py``) before reading a single byte from it. A
    process other than the one launched, or one whose id cannot be read, is
    closed at once and the server keeps waiting — within the same deadline —
    for the right one, so an impostor cannot block the real peer. A client
    can check the server's process id the same way.

    **Mutually authenticated, with deadlines.** Each side proves it holds the
    per-session key with an HMAC-SHA256 challenge-response in both
    directions. ``multiprocessing.connection``'s own authkey handshake is NOT
    used: it reads without a timeout, so a peer that connects and never
    answers would park the waiting thread forever — reproduced on this
    machine, where ``accept(timeout=1)`` did not return within 8 seconds.
    Here every read is preceded by ``poll(remaining)``, and Windows
    message-mode pipes deliver each message whole, so a read that ``poll``
    has cleared completes at once: a partial payload cannot stall it, and an
    oversized message is refused by its length without being read.

    **Bounded sends.** ``multiprocessing``'s ``send_bytes`` waits for its
    ``WriteFile`` with no timeout. One message below the 8 KiB pipe buffer
    fits an empty buffer, but repeated sends to a peer that has stopped
    reading fill it, and the next write then waits forever. So sends here use
    the same overlapped ``WriteFile`` with a deadline: if the write has not
    completed when the deadline passes, it is cancelled (``CancelIoEx``), the
    cancellation is collected, the channel is closed as broken — a cancelled
    write may have left a partial message — and ``ChannelTimeout`` is raised.
    Closing never waits on a send.

    **Closed messages.** One JSON object per message, at most
    ``MAX_MESSAGE_BYTES``, with a ``kind`` from a closed table and exactly
    the fields that kind declares.

Nothing here runs a command, opens a dialog or touches a device.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

PROTOCOL_VERSION = 1
#: Every message this protocol has is far smaller. Being below the 8 KiB
#: pipe buffer only means one message fits an empty buffer; it does NOT bound
#: a send — that is what the send deadline is for.
MAX_MESSAGE_BYTES = 4096
DEFAULT_SEND_TIMEOUT_SECONDS = 5.0
KEY_BYTES = 32
HANDSHAKE_BYTES = 128
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0

_PIPE_PREFIX = "\\\\.\\pipe\\qronos-"
_PIPE_NAME = re.compile(r"^\\\\\.\\pipe\\qronos-[a-z][a-z-]{0,23}-[0-9a-f]{32}$")
_PURPOSE = re.compile(r"^[a-z][a-z-]{0,23}$")
_FIELD_TYPES = {"int": int, "float": (int, float), "str": str, "bool": bool, "object": dict}

_CHALLENGE = b"QIPC1-CHALLENGE:"
_RESPONSE = b"QIPC1-RESPONSE:"
_CONFIRM = b"QIPC1-CONFIRM:"

PeerPid = Callable[[object], Optional[int]]


class ChannelError(RuntimeError):
    """The channel could not be used safely; nothing from it is trusted."""


class ChannelTimeout(ChannelError):
    """The peer did not answer within the deadline."""


class PeerRejected(ChannelError):
    """The peer is not the process this channel was opened for."""


@dataclass(frozen=True)
class MessageSchema:
    """Kinds one side may receive, each with its exact fields and types."""

    kinds: Mapping[str, Mapping[str, str]]

    def __post_init__(self) -> None:
        for kind, fields in self.kinds.items():
            if not re.fullmatch(r"[a-z][a-z_]{0,31}", kind):
                raise ValueError(f"Invalid message kind {kind!r}.")
            for name, type_name in fields.items():
                if name == "kind" or type_name not in _FIELD_TYPES:
                    raise ValueError(f"Invalid field {name!r} for {kind}.")

    def validate(self, message: object) -> dict:
        if not isinstance(message, dict) or not isinstance(message.get("kind"), str):
            raise ChannelError("A message must be an object with a kind.")

        fields = self.kinds.get(message["kind"])

        if fields is None:
            raise ChannelError("Unexpected message kind.")

        if set(message) != {"kind", *fields}:
            raise ChannelError("A message has missing or unknown fields.")

        for name, type_name in fields.items():
            value = message[name]
            expected = _FIELD_TYPES[type_name]

            # bool is an int in Python; an int field must not accept True.
            if type_name in {"int", "float"} and isinstance(value, bool):
                raise ChannelError(f"Field {name} has the wrong type.")

            if not isinstance(value, expected):
                raise ChannelError(f"Field {name} has the wrong type.")

        return message


def encode_message(kind: str, fields: Mapping[str, Any]) -> bytes:
    data = json.dumps({"kind": kind, **fields}, separators=(",", ":"), allow_nan=False, sort_keys=True).encode("utf-8")

    if len(data) > MAX_MESSAGE_BYTES:
        raise ChannelError("A message is larger than the channel allows.")

    return data


def decode_message(data: bytes, schema: MessageSchema) -> dict:
    if len(data) > MAX_MESSAGE_BYTES:
        raise ChannelError("A message is larger than the channel allows.")

    def refuse_constant(value: str) -> None:
        raise ChannelError("Non-finite numbers are not allowed.")

    try:
        message = json.loads(data.decode("utf-8"), parse_constant=refuse_constant)
    except (UnicodeDecodeError, ValueError) as error:
        raise ChannelError("A message is not valid JSON.") from error

    return schema.validate(message)


# ----------------------------------------------------------------- addresses


def new_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def new_address(purpose: str) -> str:
    """A fresh, unguessable local address for one session."""
    if not _PURPOSE.fullmatch(purpose):
        raise ValueError("Invalid channel purpose.")

    token = secrets.token_hex(16)

    if sys.platform == "win32":
        return f"{_PIPE_PREFIX}{purpose}-{token}"

    directory = tempfile.mkdtemp(prefix="qronos-ipc-")
    os.chmod(directory, 0o700)
    return str(Path(directory) / f"{purpose}-{token}.sock")


def require_local_address(address: object) -> str:
    """Refuse anything that is not one of our local pipe or socket addresses."""
    if not isinstance(address, str):
        raise ChannelError("Only local pipe or socket addresses are allowed.")

    if sys.platform == "win32":
        if not _PIPE_NAME.fullmatch(address):
            raise ChannelError("Not a Qronos named pipe address.")
        return address

    path = Path(address)

    if not path.is_absolute() or not path.name.endswith(".sock") or not path.parent.name.startswith("qronos-ipc-"):
        raise ChannelError("Not a Qronos local socket address.")

    return address


def _require_sessions_supported() -> None:
    if sys.platform != "win32":
        raise ChannelError(
            "Qronos IPC sessions run over Windows named pipes only; a stream "
            "socket's reads could not be bounded by a deadline."
        )


def _require_key(key: object) -> bytes:
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise ChannelError("A channel key must be exactly 32 bytes.")
    return key


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _receive_raw(connection, deadline: float, limit: int) -> bytes:
    """One whole message, or an error, within the deadline."""
    remaining = _remaining(deadline)

    try:
        if remaining <= 0 or not connection.poll(remaining):
            raise ChannelTimeout("The peer did not answer in time.")
        return connection.recv_bytes(limit)
    except ChannelTimeout:
        raise
    except (OSError, EOFError, ValueError) as error:
        raise ChannelError("The peer is gone or sent an oversized message.") from error


def _send_raw(connection, data: bytes, deadline: float, register: Optional[Callable[[object], bool]] = None) -> None:
    """
    Write one whole message within the deadline, or cancel it and fail.

    Mirrors ``PipeConnection._send_bytes`` (overlapped ``WriteFile``, wait on
    its event, collect the result) except that the wait is bounded. A write
    that is still pending at the deadline is cancelled and its cancellation
    collected before ``ChannelTimeout`` is raised; the caller must then treat
    the channel as broken. ``register`` is handed a pending write so that a
    concurrent close can cancel it; if it returns False the channel is already
    closing and the write is cancelled here.
    """
    import _winapi

    handle = getattr(connection, "_handle", None)

    if not isinstance(handle, int) or handle <= 0:
        raise ChannelError("The channel is closed.")

    remaining_ms = int(_remaining(deadline) * 1000)

    if remaining_ms <= 0:
        raise ChannelTimeout("No time left to send.")

    try:
        overlapped, error = _winapi.WriteFile(handle, data, overlapped=True)
    except OSError as failure:
        raise ChannelError("The peer is gone.") from failure

    try:
        if error == _winapi.ERROR_IO_PENDING:
            if register is not None and not register(overlapped):
                overlapped.cancel()

            waited = _winapi.WaitForMultipleObjects([overlapped.event], False, remaining_ms)

            if waited != _winapi.WAIT_OBJECT_0:
                overlapped.cancel()
                try:
                    overlapped.GetOverlappedResult(True)  # collect the cancellation
                except OSError:
                    pass
                raise ChannelTimeout("The peer stopped reading; the send was cancelled.")

        written, error = overlapped.GetOverlappedResult(True)
    except ChannelTimeout:
        raise
    except OSError as failure:
        raise ChannelError("The peer is gone.") from failure

    if error != 0 or written != len(data):
        raise ChannelError("The message was not written whole.")


def _open_pipe(address: str, deadline: float):
    """
    Open a Qronos named pipe as a client within the deadline.

    The same calls as ``multiprocessing.connection.PipeClient`` (wait in
    slices for a free instance, open it overlapped, switch to message read
    mode), except that the wait honours the caller's deadline instead of
    multiprocessing's fixed 20 seconds: a busy pipe must not hold the caller
    beyond its own timeout.
    """
    import _winapi
    from multiprocessing.connection import PipeConnection

    while True:
        remaining_ms = int(_remaining(deadline) * 1000)

        if remaining_ms <= 0:
            raise ChannelTimeout("The pipe stayed busy past the deadline.")

        try:
            _winapi.WaitNamedPipe(address, max(1, min(remaining_ms, 250)))
            handle = _winapi.CreateFile(
                address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, _winapi.NULL,
                _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL,
            )
        except OSError as error:
            if error.winerror in (_winapi.ERROR_SEM_TIMEOUT, _winapi.ERROR_PIPE_BUSY):
                continue
            raise ChannelError("Could not connect.") from error
        break

    try:
        _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
    except OSError as error:
        _winapi.CloseHandle(handle)
        raise ChannelError("Could not connect.") from error

    return PipeConnection(handle)


def _tag(key: bytes, role: bytes, challenge: bytes) -> bytes:
    return hmac.new(key, role + challenge, "sha256").digest()


def _server_handshake(connection, key: bytes, deadline: float) -> None:
    challenge = secrets.token_bytes(32)
    _send_raw(connection, _CHALLENGE + challenge, deadline)
    reply = _receive_raw(connection, deadline, HANDSHAKE_BYTES)

    if len(reply) != len(_RESPONSE) + 64 or not reply.startswith(_RESPONSE):
        raise ChannelError("The peer failed authentication.")

    proof, theirs = reply[len(_RESPONSE):len(_RESPONSE) + 32], reply[len(_RESPONSE) + 32:]

    if not hmac.compare_digest(proof, _tag(key, b"client", challenge)):
        raise ChannelError("The peer failed authentication.")

    _send_raw(connection, _CONFIRM + _tag(key, b"server", theirs), deadline)


def _client_handshake(connection, key: bytes, deadline: float) -> None:
    challenge = _receive_raw(connection, deadline, HANDSHAKE_BYTES)

    if len(challenge) != len(_CHALLENGE) + 32 or not challenge.startswith(_CHALLENGE):
        raise ChannelError("The server failed authentication.")

    ours = secrets.token_bytes(32)
    _send_raw(connection, _RESPONSE + _tag(key, b"client", challenge[len(_CHALLENGE):]) + ours, deadline)
    confirm = _receive_raw(connection, deadline, HANDSHAKE_BYTES)

    if len(confirm) != len(_CONFIRM) + 32 or not confirm.startswith(_CONFIRM) \
            or not hmac.compare_digest(confirm[len(_CONFIRM):], _tag(key, b"server", ours)):
        raise ChannelError("The server failed authentication.")


# ------------------------------------------------------------------ channel


class Channel:
    """One authenticated connection with a closed receive schema."""

    def __init__(self, connection, receive: MessageSchema, send: MessageSchema) -> None:
        self._connection = connection
        self._receive = receive
        self._send = send
        self._send_lock = threading.Lock()
        # Guards _closed, _sending and _pending. Never held while waiting.
        self._state_lock = threading.Lock()
        self._closed = False
        self._sending = False
        self._pending = None

    @property
    def connection(self):
        return self._connection

    def send(self, kind: str, timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS, **fields: Any) -> None:
        """Send one message within ``timeout``; a timed-out channel is closed."""
        self._send.validate({"kind": kind, **fields})
        data = encode_message(kind, fields)
        deadline = time.monotonic() + max(0.0, timeout)

        with self._send_lock:
            with self._state_lock:
                if self._closed:
                    raise ChannelError("The channel is closed.")
                self._sending = True

            try:
                _send_raw(self._connection, data, deadline, self._register)
            except ChannelTimeout:
                # A cancelled write may have left part of a message in the
                # pipe; nothing more may be sent or trusted on this channel.
                with self._state_lock:
                    self._closed = True
                raise
            finally:
                with self._state_lock:
                    self._sending, self._pending = False, None
                    release = self._closed

                if release:
                    self._release()

    def _register(self, overlapped) -> bool:
        with self._state_lock:
            if self._closed:
                return False
            self._pending = overlapped
            return True

    def receive(self, timeout: float) -> dict:
        data = _receive_raw(self._connection, time.monotonic() + max(0.0, timeout), MAX_MESSAGE_BYTES)
        return decode_message(data, self._receive)

    def close(self) -> None:
        """
        Close without waiting on a send.

        With no send in progress the pipe is closed here. Otherwise the
        pending write is cancelled and the sending thread, which wakes at
        once, closes the pipe as it leaves — so the handle is never closed
        under a write that still uses it.
        """
        with self._state_lock:
            if self._closed:
                return
            self._closed = True

            if self._sending:
                if self._pending is not None:
                    self._pending.cancel()
                return

        self._release()

    def _release(self) -> None:
        try:
            self._connection.close()
        except OSError:
            pass


class ChannelServer:
    """
    Listens on one fresh address for exactly one identified, authenticated peer.

    The listener exists before the peer is launched, which is what makes the
    name impossible for anyone else to claim first.
    """

    def __init__(self, purpose: str, key: bytes, receive: MessageSchema, send: MessageSchema) -> None:
        _require_sessions_supported()
        self.key = _require_key(key)
        self.address = require_local_address(new_address(purpose))
        self._receive = receive
        self._send = send
        # No authkey: multiprocessing's handshake reads without a deadline.
        self._listener = Listener(self.address, family="AF_PIPE")
        self._closed = False
        self.refused_peers = 0

    def accept(self, timeout: float, expected_pid: Optional[int] = None, peer_pid: Optional[PeerPid] = None) -> Channel:
        """
        The expected, authenticated peer within ``timeout``, or an error.

        ``expected_pid`` is required: a connection from any other process, or
        one whose process id cannot be read, is closed before anything is read
        from it, and waiting continues until the deadline. ``peer_pid`` stands
        in for the Win32 query in tests.
        """
        deadline = time.monotonic() + max(0.0, timeout)

        while True:
            connection = self._accept_raw(_remaining(deadline))

            if connection is None:
                if self.refused_peers:
                    raise PeerRejected("Only processes other than the launched one connected.")
                raise ChannelTimeout("No peer connected in time.")

            if not self._is_expected(connection, expected_pid, peer_pid):
                self.refused_peers += 1
                connection.close()
                continue

            try:
                _server_handshake(connection, self.key, deadline)
            except ChannelError:
                connection.close()
                raise

            return Channel(connection, self._receive, self._send)

    @staticmethod
    def _is_expected(connection, expected_pid: Optional[int], peer_pid: Optional[PeerPid]) -> bool:
        if expected_pid is None:
            return False

        from core.pipe_peer import client_pid

        actual = (peer_pid or client_pid)(connection)
        return actual is not None and actual == expected_pid

    def _accept_raw(self, remaining: float):
        """The next connected pipe client within ``remaining`` seconds, or None."""
        if remaining <= 0:
            return None

        result: dict[str, object] = {}
        done = threading.Event()

        def wait_for_peer() -> None:
            try:
                result["connection"] = self._listener.accept()
            except BaseException as error:  # noqa: BLE001 - reported below
                result["error"] = error
            finally:
                done.set()

        threading.Thread(target=wait_for_peer, name="qronos-ipc-accept", daemon=True).start()

        if done.wait(remaining):
            if "error" in result:
                raise ChannelError("The listener failed.")
            return result["connection"]

        # Wake the waiting accept with a connection that never handshakes;
        # opening it is itself bounded.
        try:
            _open_pipe(self.address, time.monotonic() + 2.0).close()
        except Exception:
            pass

        done.wait(2.0)
        late = result.get("connection")
        if late is not None:
            late.close()
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._listener.close()
        except OSError:
            pass


def connect(
    address: str,
    key: bytes,
    receive: MessageSchema,
    send: MessageSchema,
    expected_server_pid: Optional[int] = None,
    peer_pid: Optional[PeerPid] = None,
    timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> Channel:
    """
    Connect, check the server's process id, authenticate — all within ``timeout``.

    ``expected_server_pid`` is required. Opening the pipe, the identity check
    and the handshake share the one deadline.
    """
    _require_sessions_supported()
    address = require_local_address(address)
    key = _require_key(key)
    deadline = time.monotonic() + max(0.0, timeout)
    connection = _open_pipe(address, deadline)

    if expected_server_pid is None:
        connection.close()
        raise PeerRejected("The expected server process must be named.")

    from core.pipe_peer import server_pid

    actual = (peer_pid or server_pid)(connection)

    if actual is None or actual != expected_server_pid:
        connection.close()
        raise PeerRejected("The pipe is not served by the expected process.")

    try:
        _client_handshake(connection, key, deadline)
    except ChannelError:
        connection.close()
        raise

    return Channel(connection, receive, send)


def hmac_sha256(key: bytes, data: bytes) -> str:
    return hmac.new(_require_key(key), data, "sha256").hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
