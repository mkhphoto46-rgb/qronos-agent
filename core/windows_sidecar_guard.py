from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes


WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class SidecarAlreadyRunning(RuntimeError):
    """Another Qronos process already owns this sidecar singleton."""


class SidecarGuardUnavailable(RuntimeError):
    """Windows could not establish safe lifetime ownership for a sidecar."""


class SidecarUpdateInProgress(SidecarGuardUnavailable):
    """The update fence exists: no sidecar starts while an update or its installer runs."""


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

        kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
        kernel32.GetProcessId.restype = wintypes.DWORD

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

    The mutex is OWNED by a holder thread (``core.instance_ownership``): a
    wait-acquisition, never "created" or "already existed", released on that
    same thread exactly once. ``WAIT_ABANDONED`` is ownership after a dead
    owner, but the dead owner may have been stopped by an update, so after ANY
    acquisition the update fence is checked: while it exists the mutex is
    released again and no sidecar starts (``SidecarUpdateInProgress``).
    """

    def __init__(self, mutex_name: str) -> None:
        if not mutex_name.strip():
            raise ValueError("mutex_name must not be empty")

        self.mutex_name = mutex_name
        self._owned = None
        self._job_handle = None
        self._closed = False
        #: True only if this guard's mutex release was confirmed by Windows.
        self.release_confirmed = False
        self._lock = threading.RLock()

    @classmethod
    def acquire(cls, mutex_name: str, *, api=None, fence_exists=None) -> "WindowsSidecarGuard":
        guard = cls(mutex_name)
        try:
            guard._acquire_mutex(api=api, fence_exists=fence_exists)
            guard._create_kill_on_close_job()
            return guard
        except Exception:
            guard.close()
            raise

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def abandoned(self) -> bool:
        """True when this guard's mutex was acquired from an owner that died holding it."""
        owned = self._owned
        return bool(owned is not None and owned.abandoned)

    def _acquire_mutex(self, *, api=None, fence_exists=None) -> None:
        from core.instance_ownership import AlreadyOwned, OwnershipError, UpdateInProgress, acquire_unless_updating

        options = {"retry_seconds": 0.0, "api": api}
        if fence_exists is not None:
            options["fence_exists"] = fence_exists
        try:
            self._owned = acquire_unless_updating(self.mutex_name, **options)
        except AlreadyOwned as error:
            raise SidecarAlreadyRunning("Another Qronos process already owns this sidecar.") from error
        except UpdateInProgress as error:
            raise SidecarUpdateInProgress(
                "An update is being installed; the sidecar does not start until it ends."
            ) from error
        except OwnershipError as error:
            raise SidecarGuardUnavailable(
                f"Could not own Qronos sidecar mutex {self.mutex_name!r}: {error}"
            ) from error

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

    def bind_process(self, pid: int, *, process_handle) -> None:
        """Bind the exact child handle returned by Popen, never reopen its PID."""
        if type(pid) is not int or pid <= 0:
            raise ValueError("pid must be a positive integer")
        if process_handle is None or int(process_handle) <= 0:
            raise SidecarGuardUnavailable("The captured child process handle is unavailable.")

        with self._lock:
            if self._closed or not self._job_handle:
                raise SidecarGuardUnavailable("Qronos sidecar Job Object is not available.")
            kernel32 = _kernel32()
            if (
                int(kernel32.GetProcessId(process_handle)) != pid
                or kernel32.WaitForSingleObject(process_handle, 0) != WAIT_TIMEOUT
            ):
                raise SidecarGuardUnavailable("The captured sidecar process is no longer valid.")
            if not kernel32.AssignProcessToJobObject(self._job_handle, process_handle):
                _raise_last_error("Could not bind the captured Qronos sidecar to its lifetime job")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return

            self._closed = True
            job = self._job_handle
            owned = self._owned
            self._job_handle = None
            self._owned = None

        # Close the job first. If a sidecar survived normal shutdown, this is
        # the final fail-safe that terminates it before singleton ownership is
        # released to another Qronos process.
        if job and os.name == "nt":
            _kernel32().CloseHandle(job)

        if owned is not None:
            # On its holder thread, once. An unconfirmed native release is recorded,
            # never reported as clean; Windows releases it when this process exits.
            self.release_confirmed = owned.release()

    def __enter__(self) -> "WindowsSidecarGuard":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
