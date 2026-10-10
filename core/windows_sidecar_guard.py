from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes


WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
CREATE_SUSPENDED = 0x00000004
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

#: tree_state() values. "tree_exited" is a proof; anything else keeps ownership.
TREE_RUNNING = "running"
TREE_EXITED = "tree_exited"
TREE_UNKNOWN = "unknown"


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


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
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

        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL

        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL

        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD

        _kernel32_instance = kernel32
        return kernel32


def _resume_only_thread(kernel32, pid: int) -> None:
    """A process created suspended has exactly one thread: resume exactly that one, or fail."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        _raise_last_error("Could not list the new sidecar's thread")
    threads = []
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if int(entry.th32OwnerProcessID) == int(pid):
                threads.append(int(entry.th32ThreadID))
            more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if len(threads) != 1:
        raise SidecarGuardUnavailable("The new sidecar process did not have exactly one suspended thread.")
    thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, threads[0])
    if not thread:
        _raise_last_error("Could not open the new sidecar's thread")
    try:
        if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
            _raise_last_error("Could not resume the new sidecar")
    finally:
        kernel32.CloseHandle(thread)


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
        # The whole-tree proof (spawn_contained / tree_state). One contained tree per guard.
        self._contained_from_start = False
        self._tree_exit_proven = False
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

    # ------------------------------------------------------------------ whole-contained tree proof

    def spawn_contained(self, argv, **popen_options):
        """Start ``argv`` so that it and every process it ever starts belong to this guard's Job.

        The process is created suspended, assigned to the Job through the exact Popen handle and only then is its
        single thread resumed: containment holds from its first instruction. The Job permits no breakaway. If it
        cannot be made a member or resumed, the never-run process is ended through its own handle and reaped.
        One contained tree per guard; its lifetime proof is :meth:`tree_state`.
        """
        import subprocess

        flags = int(popen_options.pop("creationflags", 0)) | CREATE_SUSPENDED
        with self._lock:
            if self._closed or not self._job_handle:
                raise SidecarGuardUnavailable("Qronos sidecar Job Object is not available.")
            if self._contained_from_start:
                raise SidecarGuardUnavailable("This sidecar guard already contains its process tree.")
            kernel32 = _kernel32()
            process = subprocess.Popen(argv, creationflags=flags, **popen_options)
            try:
                handle = getattr(process, "_handle", None)
                if handle is None or int(kernel32.GetProcessId(int(handle))) != process.pid:
                    raise SidecarGuardUnavailable("The new sidecar process handle is not its own.")
                if not kernel32.AssignProcessToJobObject(self._job_handle, int(handle)):
                    _raise_last_error("Could not contain the new Qronos sidecar in its lifetime job")
                _resume_only_thread(kernel32, process.pid)
            except BaseException:
                try:
                    process.kill()  # TerminateProcess on its own handle; it never ran
                    process.wait(timeout=5.0)
                except Exception:
                    pass
                raise
            self._contained_from_start = True
            return process

    def active_processes(self) -> int | None:
        """Live processes in this Job (nested Jobs included), or None when Windows cannot say."""
        with self._lock:
            return self._active_processes_locked()

    def _active_processes_locked(self) -> int | None:
        if self._closed or not self._job_handle:
            return None
        info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        try:
            ok = _kernel32().QueryInformationJobObject(
                self._job_handle, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
                ctypes.byref(info), ctypes.sizeof(info), None,
            )
        except Exception:
            return None
        return int(info.ActiveProcesses) if ok else None

    def tree_state(self) -> str:
        """``tree_exited`` only from observation: contained from its first instruction, the Job handle still held,
        and ActiveProcesses == 0 seen (then sticky). A query failure, a closed or lost handle before that
        observation, or a guard that never contained its process is ``unknown``; live members are ``running``.
        """
        with self._lock:
            if self._tree_exit_proven:
                return TREE_EXITED
            if not self._contained_from_start:
                return TREE_UNKNOWN
            count = self._active_processes_locked()
            if count is None:
                return TREE_UNKNOWN
            if count == 0:
                self._tree_exit_proven = True
                return TREE_EXITED
            return TREE_RUNNING

    def wait_tree_exit(self, bound: float) -> bool:
        """Observe (never cause) the tree's end within ``bound`` seconds."""
        deadline = time.monotonic() + max(0.0, float(bound))
        while True:
            state = self.tree_state()
            if state == TREE_EXITED:
                return True
            if state == TREE_UNKNOWN or time.monotonic() >= deadline:
                return False
            time.sleep(0.025)

    def end_contained_tree(self, bound: float) -> bool:
        """The owned backstop: end THIS Job's members (only the contained tree can be in it), then observe.

        Issuing the terminate is not the proof; True only once ActiveProcesses == 0 was observed within ``bound``.
        """
        with self._lock:
            if self._tree_exit_proven:
                return True
            if not self._contained_from_start or self._closed or not self._job_handle:
                return False
            _kernel32().TerminateJobObject(self._job_handle, 1)
        return self.wait_tree_exit(bound)

    def breakaway_permitted(self) -> bool | None:
        """Whether this Job lets a member leave it (expected False); None when Windows cannot say."""
        with self._lock:
            if self._closed or not self._job_handle:
                return None
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            ok = _kernel32().QueryInformationJobObject(
                self._job_handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info), ctypes.sizeof(info), None,
            )
            if not ok:
                return None
            flags = int(info.BasicLimitInformation.LimitFlags)
            return bool(flags & (JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK))

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
