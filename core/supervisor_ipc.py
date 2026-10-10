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
    Waiting for an earlier send's turn counts against the same deadline (a
    send that times out there wrote nothing and leaves the channel open).
    Closing never waits on a send. Every timeout must be a finite number of
    seconds; infinity, NaN and non-numbers are refused.

    **Closed messages.** One JSON object per message, at most
    ``MAX_MESSAGE_BYTES``, with a ``kind`` from a closed table and exactly
    the fields that kind declares.

Nothing here runs a command, opens a dialog or touches a device.
"""

from __future__ import annotations

import hmac
import json
import math
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
#: Longest wait any channel call accepts; also keeps millisecond waits in range.
MAX_TIMEOUT_SECONDS = 3600.0
KEY_BYTES = 32
HANDSHAKE_BYTES = 128
#: How often an interruptible wait looks at its stop signal.
_WAIT_SLICE_SECONDS = 0.05
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0

_PIPE_PREFIX = "\\\\.\\pipe\\qronos-"
_PIPE_NAME = re.compile(r"^\\\\\.\\pipe\\qronos-[a-z][a-z-]{0,23}-[0-9a-f]{32}$")
_PURPOSE = re.compile(r"^[a-z][a-z-]{0,23}$")
_FIELD_TYPES = {"int": int, "float": (int, float), "str": str, "bool": bool, "object": dict}

_CHALLENGE = b"QIPC1-CHALLENGE:"
_RESPONSE = b"QIPC1-RESPONSE:"
_CONFIRM = b"QIPC1-CONFIRM:"
_KEY = b"QIPC1-KEY:"

PeerPid = Callable[[object], Optional[int]]


class ChannelError(RuntimeError):
    """The channel could not be used safely; nothing from it is trusted."""


class ChannelTimeout(ChannelError):
    """The peer did not answer within the deadline."""


class _NothingWritten(ChannelTimeout):
    """Out of time before any byte was written; the channel is still whole."""


class ChannelCancelled(ChannelError):
    """The wait was interrupted on purpose (the owner is stopping); nobody was accepted."""


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


def _deadline(timeout: object) -> float:
    """
    The deadline ``timeout`` seconds from now.

    The timeout must be a real, finite number of seconds no larger than
    ``MAX_TIMEOUT_SECONDS``: infinity, NaN, booleans and other types are
    refused rather than turned into a wait without an end. A negative value
    (a deadline already passed) means no time at all.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(timeout) or timeout > MAX_TIMEOUT_SECONDS:
        raise ValueError(f"A channel timeout must be a finite number of seconds up to {MAX_TIMEOUT_SECONDS:g}.")

    return time.monotonic() + max(0.0, float(timeout))


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _receive_raw(connection, deadline: float, limit: int,
                 interrupted: Optional[Callable[[], bool]] = None) -> bytes:
    """
    One whole message, or an error, within the deadline.

    With ``interrupted``, the wait is taken in short slices and ends with
    ``ChannelCancelled`` as soon as it returns True. Polling does not consume
    anything, so a message arriving meanwhile is not lost.
    """
    try:
        while True:
            if interrupted is not None and interrupted():
                raise ChannelCancelled("Stopped while waiting for the peer.")
            remaining = _remaining(deadline)
            if remaining <= 0:
                raise ChannelTimeout("The peer did not answer in time.")
            if connection.poll(remaining if interrupted is None else min(remaining, _WAIT_SLICE_SECONDS)):
                break
            if interrupted is None:
                raise ChannelTimeout("The peer did not answer in time.")
        return connection.recv_bytes(limit)
    except ChannelError:
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
        raise _NothingWritten("No time left to send; nothing was written.")

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


def _server_handshake(connection, key: bytes, deadline: float,
                      interrupted: Optional[Callable[[], bool]] = None) -> None:
    challenge = secrets.token_bytes(32)
    _send_raw(connection, _CHALLENGE + challenge, deadline)
    reply = _receive_raw(connection, deadline, HANDSHAKE_BYTES, interrupted)

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
        """
        Send one message within ``timeout``.

        The whole call shares the one deadline, including waiting for an
        earlier send on this channel to finish. A send that runs out of time
        while waiting its turn has written nothing and leaves the channel
        open; one whose write timed out closes it.
        """
        deadline = _deadline(timeout)
        self._send.validate({"kind": kind, **fields})
        data = encode_message(kind, fields)

        if not self._send_lock.acquire(timeout=max(0.0, _remaining(deadline))):
            raise _NothingWritten("An earlier send still holds the channel; nothing was written.")

        try:
            with self._state_lock:
                if self._closed:
                    raise ChannelError("The channel is closed.")
                self._sending = True

            try:
                _send_raw(self._connection, data, deadline, self._register)
            except _NothingWritten:
                raise
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
        finally:
            self._send_lock.release()

    def _register(self, overlapped) -> bool:
        with self._state_lock:
            if self._closed:
                return False
            self._pending = overlapped
            return True

    def receive(self, timeout: float, interrupted: Optional[Callable[[], bool]] = None) -> dict:
        """The next message within ``timeout``; ``ChannelCancelled`` once ``interrupted`` returns True."""
        data = _receive_raw(self._connection, _deadline(timeout), MAX_MESSAGE_BYTES, interrupted)
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
        self._interrupted = threading.Event()
        self.refused_peers = 0

    def accept(
        self,
        timeout: float,
        expected_pid: Optional[int] = None,
        peer_pid: Optional[PeerPid] = None,
        release_key: bool = False,
        release_gate: Optional[Callable[[], bool]] = None,
    ) -> Channel:
        """
        The expected, authenticated peer within ``timeout``, or an error.

        ``expected_pid`` is required: a connection from any other process, or
        one whose process id cannot be read, is closed before anything is read
        from it, and waiting continues until the deadline. ``peer_pid`` stands
        in for the Win32 query in tests.

        With ``release_key``, the session key itself is sent to that exact
        peer — only after its process id was checked, before anything else —
        for a peer that could not be given the key any other way without
        putting it on a command line (``connect_for_key``).

        ``interrupt()`` ends the whole call promptly with ``ChannelCancelled``:
        the wait for a peer, and the handshake after it. ``release_gate`` is
        asked once, immediately before the key would be sent; False closes
        the connection unanswered and raises ``ChannelCancelled``. It is the
        owner's own serialisation point between "release the key" and
        "cancel" (see ``core.action_broker_client.Cancellation``), so a
        cancellation that won it never sees a key leave.
        """
        deadline = _deadline(timeout)

        while True:
            if self._interrupted.is_set():
                raise ChannelCancelled("Stopped while waiting for the peer.")

            connection = self._accept_raw(_remaining(deadline))

            if connection is None:
                if self._interrupted.is_set():
                    raise ChannelCancelled("Stopped while waiting for the peer.")
                if self.refused_peers:
                    raise PeerRejected("Only processes other than the launched one connected.")
                raise ChannelTimeout("No peer connected in time.")

            if not self._is_expected(connection, expected_pid, peer_pid):
                self.refused_peers += 1
                connection.close()
                continue

            try:
                if self._interrupted.is_set():
                    raise ChannelCancelled("Stopped before the peer was answered.")
                if release_key:
                    if release_gate is not None and not release_gate():
                        raise ChannelCancelled("Stopped before the key was released; nothing was sent.")
                    _send_raw(connection, _KEY + self.key, deadline)
                _server_handshake(connection, self.key, deadline, self._interrupted.is_set)
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

        # Wait in short slices so ``interrupt`` ends the wait promptly. The
        # slices are this thread's own waits: nothing extra connects to the
        # pipe until the wait is over, so a peer arriving meanwhile is not lost.
        until = time.monotonic() + remaining
        while True:
            if done.wait(max(0.0, min(0.05, until - time.monotonic()))):
                if "error" in result:
                    raise ChannelError("The listener failed.")
                return result["connection"]
            if self._interrupted.is_set() or time.monotonic() >= until:
                break

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

    def interrupt(self) -> None:
        """End a wait in ``accept`` promptly, from any thread; later accepts refuse."""
        self._interrupted.set()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._listener.close()
        except OSError:
            pass


def _open_checked(address: str, expected_server_pid: Optional[int], peer_pid: Optional[PeerPid], deadline: float):
    connection = _open_pipe(address, deadline)

    if expected_server_pid is None:
        connection.close()
        raise PeerRejected("The expected server process must be named.")

    from core.pipe_peer import server_pid

    actual = (peer_pid or server_pid)(connection)

    if actual is None or actual != expected_server_pid:
        connection.close()
        raise PeerRejected("The pipe is not served by the expected process.")

    return connection


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
    deadline = _deadline(timeout)
    connection = _open_checked(address, expected_server_pid, peer_pid, deadline)

    try:
        _client_handshake(connection, key, deadline)
    except ChannelError:
        connection.close()
        raise

    return Channel(connection, receive, send)


def connect_for_key(
    address: str,
    receive: MessageSchema,
    send: MessageSchema,
    expected_server_pid: Optional[int] = None,
    peer_pid: Optional[PeerPid] = None,
    timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> tuple[Channel, bytes]:
    """
    Connect to a server that releases the session key on the pipe.

    For a peer started with only public identifiers on its command line (the
    elevated Action Broker): the server's process id is checked first, and
    only then is the first message read — the key — followed by the same
    mutual handshake under it. The server sends the key only after checking
    this process's id (``ChannelServer.accept(release_key=True)``), so the
    key never appears on a command line, in the environment or in a log.
    Returns the channel and the key.
    """
    _require_sessions_supported()
    address = require_local_address(address)
    deadline = _deadline(timeout)
    connection = _open_checked(address, expected_server_pid, peer_pid, deadline)

    try:
        message = _receive_raw(connection, deadline, HANDSHAKE_BYTES)
        if len(message) != len(_KEY) + KEY_BYTES or not message.startswith(_KEY):
            raise ChannelError("The server did not release a session key.")
        key = message[len(_KEY):]
        _client_handshake(connection, key, deadline)
    except ChannelError:
        connection.close()
        raise

    return Channel(connection, receive, send), key


def hmac_sha256(key: bytes, data: bytes) -> str:
    return hmac.new(_require_key(key), data, "sha256").hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
