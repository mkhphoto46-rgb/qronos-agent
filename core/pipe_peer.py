"""
Which process is on the other end of a Windows named pipe. Read-only.

Two Win32 calls, both queries: ``GetNamedPipeClientProcessId`` and
``GetNamedPipeServerProcessId``. They take the pipe handle this process
already holds and report a process id; they change nothing. The supervisor
and the runtime use them to check that the process which connected is the
exact child they launched, and that the server a child connected to is the
process that launched it.

Elsewhere, or when a handle is not a named pipe, the answer is None and the
caller decides what an unverifiable peer means (the callers refuse).
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Callable, Optional

#: ``BOOL GetNamedPipe{Client,Server}ProcessId(HANDLE, PULONG)``.
PeerCall = Callable[[object, object], int]


def _kernel32_call(name: str) -> Optional[PeerCall]:
    if sys.platform != "win32":
        return None

    try:
        function = getattr(ctypes.WinDLL("kernel32", use_last_error=True), name)
    except (OSError, AttributeError):
        return None

    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    function.restype = wintypes.BOOL
    return function


def _pipe_handle(connection: object) -> Optional[int]:
    """The pipe handle of a ``multiprocessing.connection.PipeConnection``."""
    handle = getattr(connection, "_handle", None)
    return handle if isinstance(handle, int) and handle > 0 else None


def _peer_pid(connection: object, name: str, call: Optional[PeerCall]) -> Optional[int]:
    function = call if call is not None else _kernel32_call(name)
    handle = _pipe_handle(connection)

    if function is None or handle is None:
        return None

    pid = wintypes.ULONG(0)

    try:
        succeeded = function(wintypes.HANDLE(handle), ctypes.byref(pid))
    except Exception:
        return None

    return int(pid.value) if succeeded and pid.value > 0 else None


def client_pid(connection: object, call: Optional[PeerCall] = None) -> Optional[int]:
    """The process id of the client connected to a pipe this process serves."""
    return _peer_pid(connection, "GetNamedPipeClientProcessId", call)


def server_pid(connection: object, call: Optional[PeerCall] = None) -> Optional[int]:
    """The process id of the server of a pipe this process connected to."""
    return _peer_pid(connection, "GetNamedPipeServerProcessId", call)
