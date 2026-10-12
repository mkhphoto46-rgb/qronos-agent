"""OS-enforced limits for decoder children: memory, process count, teardown.

A timeout and an output cap do not bound what a native decoder allocates
before it prints anything, and killing one process does not kill anything it
started. So on Windows every decoder child runs inside its own Job Object:

* the child is created suspended and assigned to the job before its first
  instruction runs, so there is no window outside the limits;
* ``ProcessMemoryLimit`` caps committed memory per process;
* ``ActiveProcessLimit`` (default 1) stops it starting further processes;
* ``KILL_ON_JOB_CLOSE`` plus an explicit terminate tears down the child and
  any descendant at once, so no survivor holds the output pipes open.

If the job cannot be created or the child cannot be assigned, the child is
killed and the call fails closed. This is resource containment, not a
security sandbox: the decoder still runs as the user and can read what the
user can read. Stronger isolation is a separate deployment decision.

Elsewhere (CI on Linux) the child gets ``RLIMIT_AS`` and its own session so
the whole process group can be killed; the process-count limit is Windows-only.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess


@dataclass(frozen=True)
class ChildLimits:
    memory_bytes: int = 2 * 1024**3
    max_processes: int = 1


DECODER_LIMITS = ChildLimits()


class ContainmentUnavailable(OSError):
    """The OS refused to set up limits; the child must not run."""


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
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

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _Accounting(ctypes.Structure):
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

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry))
    _kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry))
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    _kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4

    _EXTENDED_LIMITS = 9
    _BASIC_ACCOUNTING = 1
    _LIMIT_ACTIVE_PROCESS = 0x00000008
    _LIMIT_PROCESS_MEMORY = 0x00000100
    _LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    _LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _SNAPSHOT_THREADS = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    CREATE_SUSPENDED = 0x00000004

    def _error(what: str) -> ContainmentUnavailable:
        return ContainmentUnavailable(ctypes.get_last_error(), f"{what} failed")

    class Containment:
        """One job per child. Use :meth:`spawn_flags`, then :meth:`adopt` right after Popen."""

        def __init__(self, limits: ChildLimits) -> None:
            self.limits = limits
            self._job = _kernel32.CreateJobObjectW(None, None)
            if not self._job:
                raise _error("CreateJobObject")
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = (
                _LIMIT_PROCESS_MEMORY | _LIMIT_ACTIVE_PROCESS | _LIMIT_KILL_ON_JOB_CLOSE
                | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            )
            info.BasicLimitInformation.ActiveProcessLimit = max(1, int(limits.max_processes))
            info.ProcessMemoryLimit = int(limits.memory_bytes)
            if not _kernel32.SetInformationJobObject(self._job, _EXTENDED_LIMITS, ctypes.byref(info),
                                                     ctypes.sizeof(info)):
                self.close()
                raise _error("SetInformationJobObject")

        @staticmethod
        def spawn_flags() -> int:
            return CREATE_SUSPENDED

        def adopt(self, child: subprocess.Popen) -> None:
            """Put the suspended child in the job, then let it run."""
            if not _kernel32.AssignProcessToJobObject(self._job, int(child._handle)):  # noqa: SLF001
                raise _error("AssignProcessToJobObject")
            self._resume(child.pid)

        @staticmethod
        def _resume(pid: int) -> None:
            snapshot = _kernel32.CreateToolhelp32Snapshot(_SNAPSHOT_THREADS, 0)
            if not snapshot or snapshot == _INVALID_HANDLE:
                raise _error("CreateToolhelp32Snapshot")
            resumed = 0
            try:
                entry = _ThreadEntry()
                entry.dwSize = ctypes.sizeof(entry)
                more = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
                while more:
                    if entry.th32OwnerProcessID == pid:
                        thread = _kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                        if thread:
                            try:
                                if _kernel32.ResumeThread(thread) != 0xFFFFFFFF:
                                    resumed += 1
                            finally:
                                _kernel32.CloseHandle(thread)
                    more = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            finally:
                _kernel32.CloseHandle(snapshot)
            if not resumed:
                raise _error("ResumeThread")

        def active_processes(self) -> int:
            info = _Accounting()
            if not self._job or not _kernel32.QueryInformationJobObject(
                    self._job, _BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None):
                return 0
            return int(info.ActiveProcesses)

        def peak_memory(self) -> int:
            info = _ExtendedLimits()
            if not self._job or not _kernel32.QueryInformationJobObject(
                    self._job, _EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info), None):
                return 0
            return int(info.PeakProcessMemoryUsed)

        def terminate(self) -> None:
            if self._job:
                _kernel32.TerminateJobObject(self._job, 1)

        @staticmethod
        def creation_time(child: subprocess.Popen) -> int:
            """The child's creation FILETIME from its own handle (0 if unreadable): with the pid, its identity."""
            times = [wintypes.FILETIME() for _ in range(4)]
            if not _kernel32.GetProcessTimes(int(child._handle), *(ctypes.byref(t) for t in times)):  # noqa: SLF001
                return 0
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

        def exit_proven(self, child: subprocess.Popen) -> bool:
            """True only when the child's own handle reports exit AND the job holds no process at all.

            Read while the job handle is still open: after close the job cannot be asked.
            """
            if child.poll() is None or not self._job:
                return False
            info = _Accounting()
            if not _kernel32.QueryInformationJobObject(
                    self._job, _BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None):
                return False
            return int(info.ActiveProcesses) == 0

        def close(self) -> None:
            if self._job:
                _kernel32.CloseHandle(self._job)  # KILL_ON_JOB_CLOSE ends any survivor
                self._job = None

else:  # pragma: no cover - exercised on non-Windows CI only
    import resource
    import signal

    class Containment:
        def __init__(self, limits: ChildLimits) -> None:
            self.limits = limits
            self._pid: int | None = None

        @staticmethod
        def spawn_flags() -> int:
            return 0

        def preexec(self):
            memory = int(self.limits.memory_bytes)

            def apply() -> None:
                resource.setrlimit(resource.RLIMIT_AS, (memory, memory))

            return apply

        def adopt(self, child: subprocess.Popen) -> None:
            self._pid = child.pid

        def active_processes(self) -> int:
            return 0

        def peak_memory(self) -> int:
            return 0

        @staticmethod
        def creation_time(child: subprocess.Popen) -> int:
            return 0

        def exit_proven(self, child: subprocess.Popen) -> bool:
            if child.poll() is None or self._pid is None:
                return False
            try:
                os.killpg(self._pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            return False

        def terminate(self) -> None:
            if self._pid is not None:
                try:
                    os.killpg(self._pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        def close(self) -> None:
            self.terminate()
