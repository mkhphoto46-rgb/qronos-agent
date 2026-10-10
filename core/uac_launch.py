"""
Start the bundled Action Broker elevated, through the normal UAC prompt.

``ShellExecuteExW`` with the ``runas`` verb is the documented way for a
standard process to ask Windows for an elevated one: Windows shows the UAC
consent (or credential) prompt, and the person can refuse. Nothing here
avoids, pre-answers or suppresses that prompt; Qronos never bypasses UAC.

Only one program is ever started — the bundled Qronos runtime executable — and
only with the broker's fixed arguments (``broker_arguments``), each checked
against a strict pattern so no quoting, shell syntax or extra word can reach
the command line. The returned handle is used only to learn the process id
(``GetProcessId``), wait for it (``WaitForSingleObject``) and read its exit
code (``GetExitCodeProcess``); it is never used to change the process.

The Win32 functions are injectable; tests inspect what would be asked and
never call Windows. Elevated launch is acceptance-tested later, live.
"""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

from core.action_broker_client import broker_arguments

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SW_HIDE = 0
ERROR_CANCELLED = 1223
WAIT_OBJECT_0 = 0
_ARGUMENT = re.compile(r"^[A-Za-z0-9\\.:_-]{1,128}$")


class UacDeclined(RuntimeError):
    """The person declined the UAC prompt; nothing was started."""


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def command_parameters(arguments: list[str]) -> str:
    """The broker's arguments as a command line; refuses anything unusual."""
    for argument in arguments:
        if not isinstance(argument, str) or not _ARGUMENT.fullmatch(argument):
            raise ValueError("A broker argument contains characters that are never passed.")
    return " ".join(arguments)


def _real_win32_calls():
    """
    The real shell32/kernel32 functions. The one place they are resolved, so
    the test suite's side-effect boundary (tests/conftest.py) can refuse it.
    """
    if sys.platform != "win32":
        raise OSError("Elevation through UAC is only available on Windows.")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessId.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return (shell32.ShellExecuteExW, kernel32.GetProcessId, kernel32.WaitForSingleObject,
            kernel32.GetExitCodeProcess, kernel32.CloseHandle, ctypes.get_last_error)


class Win32:
    """The four Win32 calls used, resolved lazily; injectable for tests."""

    def __init__(
        self,
        shell_execute_ex: Optional[Callable[[object], int]] = None,
        get_process_id: Optional[Callable[[object], int]] = None,
        wait_for_single_object: Optional[Callable[[object, int], int]] = None,
        get_exit_code_process: Optional[Callable[[object, object], int]] = None,
        close_handle: Optional[Callable[[object], int]] = None,
        last_error: Optional[Callable[[], int]] = None,
    ) -> None:
        if shell_execute_ex is None:
            (shell_execute_ex, get_process_id, wait_for_single_object,
             get_exit_code_process, close_handle, last_error) = _real_win32_calls()
        self.shell_execute_ex = shell_execute_ex
        self.get_process_id = get_process_id
        self.wait_for_single_object = wait_for_single_object
        self.get_exit_code_process = get_exit_code_process
        self.close_handle = close_handle
        self.last_error = last_error or (lambda: 0)


class ElevatedBroker:
    """The started broker: its id, a bounded wait, and its exit code."""

    def __init__(self, handle: object, pid: int, win32: Win32) -> None:
        self._handle = handle
        self.pid = pid
        self._win32 = win32

    def wait(self, timeout: float) -> Optional[int]:
        if self._handle is None:
            return None
        milliseconds = max(0, int(timeout * 1000))
        if self._win32.wait_for_single_object(self._handle, milliseconds) != WAIT_OBJECT_0:
            return None
        code = wintypes.DWORD(0)
        ok = self._win32.get_exit_code_process(self._handle, ctypes.byref(code))
        self.close()
        return int(code.value) if ok else None

    def close(self) -> None:
        """Release the process handle (it is never used to change the process)."""
        if self._handle is not None:
            self._win32.close_handle(self._handle)
            self._handle = None


class UacBrokerLauncher:
    """Launches the bundled broker elevated, with the normal UAC prompt."""

    def __init__(self, executable: Path, win32: Optional[Win32] = None) -> None:
        executable = Path(executable)
        if not executable.is_absolute() or executable.suffix.lower() != ".exe":
            raise ValueError("The broker must be the bundled .exe, by absolute path.")
        self._executable = executable
        self._win32 = win32

    def launch(self, address: str, runtime_pid: int) -> ElevatedBroker:
        win32 = self._win32 or Win32()
        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
        info.lpVerb = "runas"
        info.lpFile = str(self._executable)
        info.lpParameters = command_parameters(broker_arguments(address, runtime_pid))
        info.lpDirectory = str(self._executable.parent)
        info.nShow = SW_HIDE

        if not win32.shell_execute_ex(ctypes.byref(info)):
            if win32.last_error() == ERROR_CANCELLED:
                raise UacDeclined("The UAC prompt was declined.")
            raise OSError("Windows could not start the Action Broker.")

        if not info.hProcess:
            raise OSError("Windows started the broker without a process handle.")

        pid = int(win32.get_process_id(info.hProcess))
        if pid <= 0:
            win32.close_handle(info.hProcess)
            raise OSError("Windows did not report the broker's process id.")

        return ElevatedBroker(info.hProcess, pid, win32)
