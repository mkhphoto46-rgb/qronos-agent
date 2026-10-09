"""
Restart the Windows Print Spooler service: one fixed service, bounded waits.

The print workflow (``core/print_workflow.py``) stops with "The Windows print
spooler is not responding" when the spooler is stuck. Restarting that one
service is the standard recovery, and Windows lets only administrators do it,
so it is the one action the Elevated Action Broker performs
(``core/action_broker.py``, kind ``printer.spooler_restart``).

What this module does, and nothing near it:

    It opens exactly the service named ``Spooler`` — a constant, never a
    parameter — through the Service Control Manager, with only query, stop
    and start rights. No shell, ``sc.exe``, ``net.exe`` or PowerShell is
    started.

    It asks the service to stop and waits, within ``stop_timeout``, for it to
    report stopped; then asks it to start and waits, within
    ``start_timeout``, for it to report running. A step that does not finish
    in time raises ``SpoolerRecoveryFailed`` saying exactly how far it got;
    nothing is retried.

    If other services that depend on the spooler are running, Windows refuses
    the stop, and so does this: dependent services are never stopped. A
    paused service is not touched. The service's configuration (start type,
    account, recovery options) is never read or changed, and queued print
    jobs are left to the spooler, which keeps them across a restart.

The Win32 calls are injectable; tests never reach the real Service Control
Manager. A real restart is part of deferred live acceptance.
"""

from __future__ import annotations

import ctypes
import time
from typing import Any, Callable, Optional

SERVICE_NAME = "Spooler"
SERVICE_DISPLAY = "Windows Print Spooler service (Spooler)"

SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
SERVICE_CONTROL_STOP = 0x00000001

STOPPED = 1
START_PENDING = 2
STOP_PENDING = 3
RUNNING = 4
CONTINUE_PENDING = 5
PAUSE_PENDING = 6
PAUSED = 7

ERROR_DEPENDENT_SERVICES_RUNNING = 1051
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_NOT_ACTIVE = 1062

DEFAULT_STOP_TIMEOUT_SECONDS = 30.0
DEFAULT_START_TIMEOUT_SECONDS = 30.0
POLL_SECONDS = 0.25


class SpoolerRecoveryFailed(RuntimeError):
    """The restart did not complete; the message says how far it got."""


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "dwServiceType", "dwCurrentState", "dwControlsAccepted", "dwWin32ExitCode",
        "dwServiceSpecificExitCode", "dwCheckPoint", "dwWaitHint",
    )]


def _real_advapi32() -> tuple[Any, Callable[[], int]]:
    """
    The real advapi32 service functions. The one place they are resolved, so
    the test suite's side-effect boundary (tests/conftest.py) can refuse it.
    """
    from ctypes import wintypes

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    api.OpenSCManagerW.restype = ctypes.c_void_p
    api.OpenServiceW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD]
    api.OpenServiceW.restype = ctypes.c_void_p
    api.QueryServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(SERVICE_STATUS)]
    api.QueryServiceStatus.restype = wintypes.BOOL
    api.ControlService.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(SERVICE_STATUS)]
    api.ControlService.restype = wintypes.BOOL
    api.StartServiceW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    api.StartServiceW.restype = wintypes.BOOL
    api.CloseServiceHandle.argtypes = [ctypes.c_void_p]
    api.CloseServiceHandle.restype = wintypes.BOOL
    return api, ctypes.get_last_error


class Win32ServiceControl:
    """The six advapi32 calls used, resolved lazily; injectable for tests."""

    def __init__(self, advapi32: Any = None, last_error: Optional[Callable[[], int]] = None) -> None:
        self._api = advapi32
        self._last_error = last_error

    def _bind(self):
        if self._api is None:
            self._api, self._last_error = _real_advapi32()
        return self._api

    def _error(self) -> int:
        return int(self._last_error()) if self._last_error is not None else 0

    def open(self) -> tuple[Any, Any]:
        """The Spooler service, with query/stop/start rights only."""
        api = self._bind()
        manager = api.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
        if not manager:
            raise SpoolerRecoveryFailed(f"The Service Control Manager could not be opened (error {self._error()}).")
        service = api.OpenServiceW(manager, SERVICE_NAME, SERVICE_QUERY_STATUS | SERVICE_STOP | SERVICE_START)
        if not service:
            code = self._error()
            api.CloseServiceHandle(manager)
            raise SpoolerRecoveryFailed(f"The Print Spooler service could not be opened (error {code}).")
        return manager, service

    def state(self, service: Any) -> int:
        status = SERVICE_STATUS()
        if not self._api.QueryServiceStatus(service, ctypes.byref(status)):
            raise SpoolerRecoveryFailed(f"The Print Spooler status could not be read (error {self._error()}).")
        return int(status.dwCurrentState)

    def stop(self, service: Any) -> int:
        """0 when the stop was accepted, else the Win32 error code."""
        status = SERVICE_STATUS()
        return 0 if self._api.ControlService(service, SERVICE_CONTROL_STOP, ctypes.byref(status)) else self._error()

    def start(self, service: Any) -> int:
        """0 when the start was accepted, else the Win32 error code."""
        return 0 if self._api.StartServiceW(service, 0, None) else self._error()

    def close(self, handles: tuple[Any, Any]) -> None:
        for handle in reversed(handles):
            if handle:
                self._api.CloseServiceHandle(handle)


class SpoolerRecovery:
    """Stop, then start, the one Spooler service, each within its deadline."""

    def __init__(
        self,
        control: Optional[Win32ServiceControl] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS,
        start_timeout: float = DEFAULT_START_TIMEOUT_SECONDS,
    ) -> None:
        self._control = control if control is not None else Win32ServiceControl()
        self._clock = clock
        self._sleep = sleep
        self._stop_timeout = stop_timeout
        self._start_timeout = start_timeout

    def _wait(self, service: Any, wanted: int, timeout: float) -> bool:
        deadline = self._clock() + timeout
        while True:
            if self._control.state(service) == wanted:
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(POLL_SECONDS)

    def restart(self) -> str:
        handles = self._control.open()
        service = handles[1]
        try:
            state = self._control.state(service)

            if state in (PAUSED, PAUSE_PENDING, CONTINUE_PENDING):
                raise SpoolerRecoveryFailed("The Print Spooler is paused; it was not changed.")

            if state == START_PENDING and not self._wait(service, RUNNING, self._start_timeout):
                raise SpoolerRecoveryFailed("The Print Spooler is still starting; it was not changed.")

            if state in (RUNNING, START_PENDING):
                error = self._control.stop(service)
                if error == ERROR_DEPENDENT_SERVICES_RUNNING:
                    raise SpoolerRecoveryFailed(
                        "Other services depend on the Print Spooler and are running; nothing was stopped."
                    )
                if error not in (0, ERROR_SERVICE_NOT_ACTIVE):
                    raise SpoolerRecoveryFailed(f"Windows refused to stop the Print Spooler (error {error}).")

            if not self._wait(service, STOPPED, self._stop_timeout):
                raise SpoolerRecoveryFailed(
                    "The Print Spooler did not stop in time; nothing was started. Its state is uncertain."
                )

            error = self._control.start(service)
            if error not in (0, ERROR_SERVICE_ALREADY_RUNNING):
                raise SpoolerRecoveryFailed(
                    f"The Print Spooler stopped but Windows refused to start it (error {error}). It is not running."
                )

            if not self._wait(service, RUNNING, self._start_timeout):
                raise SpoolerRecoveryFailed(
                    "The Print Spooler stopped and was asked to start, but it is not running yet. "
                    "Its state is uncertain."
                )

            return "The Print Spooler service was restarted and is running."
        finally:
            self._control.close(handles)
