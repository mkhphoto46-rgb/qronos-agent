from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes


WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class SidecarAlreadyRunning(RuntimeError):
    """Another Qronos process already owns this sidecar singleton."""


class SidecarGuardUnavailable(RuntimeError):
    """Windows could not establish safe lifetime ownership for a sidecar."""


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_kernel32_lock = threading.Lock()
_kernel32_instance = None


def _kernel32():
    global _kernel32_instance

    if os.name != "nt":
        raise SidecarGuardUnavailable(
            "Windows sidecar lifetime guard is available on Windows only."
        )

    with _kernel32_lock:
        if _kernel32_instance is not None:
            return _kernel32_instance

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        kernel32.WaitForSingleObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD

        kernel32.ReleaseMutex.argtypes = [
            wintypes.HANDLE,
        ]
        kernel32.ReleaseMutex.restype = wintypes.BOOL

        kernel32.CreateJobObjectW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE

        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL

        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE

        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

        kernel32.CloseHandle.argtypes = [
            wintypes.HANDLE,
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL

        _kernel32_instance = kernel32
        return kernel32


def _raise_last_error(prefix: str) -> None:
    code = ctypes.get_last_error()
    raise SidecarGuardUnavailable(
        f"{prefix} (Windows error {code})."
    )


class WindowsSidecarGuard:
    """
    Cross-process singleton + parent-death lifetime guard.

    The named mutex prevents two Qronos parent processes from owning the same
    heavy sidecar concurrently.

    The Job Object uses JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. Its handle is owned
    only by the parent Qronos process. If that parent exits or crashes, Windows
    closes the handle and terminates every sidecar assigned to the job.
    """

    def __init__(self, mutex_name: str) -> None:
        if not mutex_name.strip():
            raise ValueError("mutex_name must not be empty")

        self.mutex_name = mutex_name
        self._mutex_handle = None
        self._job_handle = None
        self._closed = False
        self._lock = threading.RLock()

    @classmethod
    def acquire(cls, mutex_name: str) -> "WindowsSidecarGuard":
        guard = cls(mutex_name)
        try:
            guard._acquire_mutex()
            guard._create_kill_on_close_job()
            return guard
        except Exception:
            guard.close()
            raise

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _acquire_mutex(self) -> None:
        kernel32 = _kernel32()

        handle = kernel32.CreateMutexW(
            None,
            False,
            self.mutex_name,
        )
        if not handle:
            _raise_last_error(
                f"Could not create Qronos sidecar mutex {self.mutex_name!r}"
            )

        wait_result = kernel32.WaitForSingleObject(
            handle,
            0,
        )

        if wait_result in {
            WAIT_OBJECT_0,
            WAIT_ABANDONED,
        }:
            self._mutex_handle = handle
            return

        kernel32.CloseHandle(handle)

        if wait_result == WAIT_TIMEOUT:
            raise SidecarAlreadyRunning(
                "Another Qronos process already owns this sidecar."
            )

        raise SidecarGuardUnavailable(
            "Windows returned an unexpected mutex wait result "
            f"0x{int(wait_result):08X}."
        )

    def _create_kill_on_close_job(self) -> None:
        kernel32 = _kernel32()

        job = kernel32.CreateJobObjectW(
            None,
            None,
        )
        if not job:
            _raise_last_error(
                "Could not create Qronos sidecar Job Object"
            )

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )

        ok = kernel32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )

        if not ok:
            kernel32.CloseHandle(job)
            _raise_last_error(
                "Could not enable KILL_ON_JOB_CLOSE for Qronos sidecar"
            )

        self._job_handle = job

    def bind_process(self, pid: int) -> None:
        if int(pid) <= 0:
            raise ValueError("pid must be greater than zero")

        with self._lock:
            if self._closed:
                raise SidecarGuardUnavailable(
                    "Cannot bind a process to a closed sidecar guard."
                )

            job = self._job_handle

        if not job:
            raise SidecarGuardUnavailable(
                "Qronos sidecar Job Object is not available."
            )

        kernel32 = _kernel32()

        process_handle = kernel32.OpenProcess(
            PROCESS_TERMINATE
            | PROCESS_SET_QUOTA
            | PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(pid),
        )
        if not process_handle:
            _raise_last_error(
                f"Could not open Qronos sidecar process PID {pid}"
            )

        try:
            ok = kernel32.AssignProcessToJobObject(
                job,
                process_handle,
            )
            if not ok:
                _raise_last_error(
                    f"Could not bind Qronos sidecar PID {pid} "
                    "to the parent-death Job Object"
                )
        finally:
            kernel32.CloseHandle(process_handle)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return

            self._closed = True
            job = self._job_handle
            mutex = self._mutex_handle
            self._job_handle = None
            self._mutex_handle = None

        if os.name != "nt":
            return

        kernel32 = _kernel32()

        # Close the job first. If a sidecar survived normal shutdown, this is
        # the final fail-safe that terminates it before singleton ownership is
        # released to another Qronos process.
        if job:
            kernel32.CloseHandle(job)

        if mutex:
            try:
                kernel32.ReleaseMutex(mutex)
            finally:
                kernel32.CloseHandle(mutex)

    def __enter__(self) -> "WindowsSidecarGuard":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
