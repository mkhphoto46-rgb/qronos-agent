"""A stand-in for the NSIS installer's side of the update hand-over (round-7 contract B-D, G). Fixture only.

Run by owned tests as a base-interpreter child (tests/fixtures/child_python.py), never as an installer:
it changes no program files and touches only the tmp data root and receipts folder it is given. The
fence name is the test's unique private mutex, never the production one.

    python -B -I update_installer_child.py REPO FENCE_NAME DATA_ROOT MODE GO_FILE REPORT_FILE

MODE is a comma list:
- update      the guard handed over through standard input / output (otherwise: a manual install);
- receipt     write the installation receipt after GO_FILE appears (a completed installation);
- grandchild  start a long-lived grandchild WITH handle inheritance before waiting for GO_FILE;
- silent      never acknowledge (the guard then keeps the exclusion until this process exits);
- keep_guard  skip waiting for the guard process to end (the guard is the test process itself).
REPORT_FILE receives one JSON object: the steps taken, or the reason for refusing.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

WAIT_OBJECT_0, WAIT_ABANDONED, WAIT_TIMEOUT = 0, 0x80, 0x102
HANDLE_FLAG_INHERIT = 0x1


def _parent_pid() -> int:
    """This process's parent id as Windows records it (Toolhelp snapshot), never as a record claims."""
    class Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    try:
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32ProcessID == os.getpid():
                return int(entry.th32ParentProcessID)
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return -1


def main() -> int:
    repo, fence_name, data_root, mode, go_file, report_file = sys.argv[1:7]
    modes = set(mode.split(","))
    sys.path.insert(0, repo)
    from core import store_admission

    report: dict = {"steps": []}

    def done(reason: str | None, code: int) -> int:
        report["refused"] = reason
        Path(report_file).write_text(json.dumps(report), encoding="utf-8")
        return code

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetHandleInformation.restype = wintypes.BOOL
    base = ctypes.WinDLL("kernelbase", use_last_error=True)  # CompareObjectHandles (Windows 10+) lives here
    base.CompareObjectHandles.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    base.CompareObjectHandles.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    record: dict[str, str] = {}
    if "update" in modes:
        data = os.read(0, 1024)  # blocks until the guard has duplicated the fence and written the record (B6)
        while len(data) <= 1023:  # the NSIS installer's bound (qronos-update-fence.nsh)
            more = os.read(0, 1024 - len(data))
            if not more:
                break
            data += more
        os.close(0)  # C0: the record channel is gone before anything else happens
        lines = data.decode("utf-8", "replace").split("\n")
        if len(data) > 1023 or not lines or lines[0] != "QRONOS-UPDATE-FENCE 1":
            return done("bad_record", 3)
        for line in lines[1:]:
            if "=" in line:
                key, value = line.split("=", 1)
                record[key] = value
        if set(record) != {"nonce", "fence", "receipts", "version", "package", "manifest", "guard"} \
                or len(record["nonce"]) != 64:
            return done("bad_record", 3)
        # R-B4: the record is not authority. The receipts folder must be the one this installer derives
        # itself from the data root, and the guard must be its own parent process.
        own_receipts = os.path.normcase(os.path.abspath(os.path.join(data_root, "Updates", "receipts")))
        if os.path.normcase(os.path.abspath(record["receipts"])) != own_receipts:
            return done("foreign_receipts_path", 3)
        if int(record["guard"].split(":")[0]) != _parent_pid():
            return done("guard_is_not_parent", 3)
        duplicated = int(record["fence"])
        flags = wintypes.DWORD()
        if not kernel.GetHandleInformation(duplicated, ctypes.byref(flags)):
            return done("no_fence_handle", 3)
        report["fenceInheritable"] = bool(flags.value & HANDLE_FLAG_INHERIT)
        report["steps"].append("record")

    fence = kernel.CreateMutexW(None, False, fence_name)  # NULL attributes: not inheritable
    if not fence:
        return done("fence_unavailable", 3)
    state = kernel.WaitForSingleObject(fence, 0)  # C1: never a long wait on a fence another process owns
    owned = state in (WAIT_OBJECT_0, WAIT_ABANDONED)
    if "update" in modes and not base.CompareObjectHandles(fence, int(record["fence"])):
        return done("fence_mismatch", 3)  # always checked: the handed handle must be THIS fence
    if not owned and "update" not in modes:
        return done("another_update_running", 4)
    report["steps"].append("fence_owned_at_start" if owned else "fence_owned_by_guard")

    if "update" in modes:
        if "silent" not in modes:
            try:
                os.write(1, f"QRONOS-UPDATE-ACK 1 {record['nonce']}\n".encode("ascii"))
                report["steps"].append("ack")
            except OSError:
                report["steps"].append("ack_undelivered")  # the guard is gone; G4
        os.close(1)
        if "silent" in modes:  # an installer without the protocol: it runs under the guard's exclusion
            report["steps"].append("under_guard")
            Path(report_file).write_text(json.dumps({**report, "refused": None, "installing": True}),
                                         encoding="utf-8")
            deadline = time.monotonic() + 120
            while not os.path.exists(go_file) and time.monotonic() < deadline:
                time.sleep(0.05)
            report["steps"].append("installed")
            return done(None, 0)
        if not owned:
            state = kernel.WaitForSingleObject(fence, 60_000)
            if state not in (WAIT_OBJECT_0, WAIT_ABANDONED):
                return done("fence_not_released", 5)
            report["steps"].append("fence_taken_over")

    admission = None
    deadline = time.monotonic() + 30
    while admission is None:
        try:
            admission = store_admission.admit_exclusive(Path(data_root))
        except Exception as error:  # writers_active: retried briefly, never waited on indefinitely
            if "update" not in modes or time.monotonic() > deadline:
                return done("writers_active:" + type(error).__name__, 6)
            time.sleep(0.05)
    report["steps"].append("admission")

    if "update" in modes and "keep_guard" not in modes:
        pid, created = (int(x) for x in record["guard"].split(":"))
        guard = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if guard:
            times = [wintypes.FILETIME() for _ in range(4)]
            if kernel.GetProcessTimes(guard, *[ctypes.byref(t) for t in times]) \
                    and ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) == created:
                if kernel.WaitForSingleObject(guard, 60_000) != WAIT_OBJECT_0:
                    return done("guard_still_running", 7)
            kernel.CloseHandle(guard)
        report["steps"].append("guard_gone")

    Path(report_file).write_text(json.dumps({**report, "refused": None, "installing": True}), encoding="utf-8")
    if "grandchild" in modes:
        code = ("import os, sys, time\n"
                "deadline = time.monotonic() + 120\n"
                "while not os.path.exists(sys.argv[1]) and time.monotonic() < deadline:\n"
                "    time.sleep(0.05)\n")
        grandchild = subprocess.Popen([sys.executable, "-B", "-I", "-c", code, go_file + ".grandchild"], close_fds=False)
        report["grandchildPid"] = grandchild.pid
        Path(report_file).write_text(json.dumps({**report, "refused": None, "installing": True}), encoding="utf-8")
    deadline = time.monotonic() + 120
    while not os.path.exists(go_file) and time.monotonic() < deadline:
        time.sleep(0.05)
    if "receipt" in modes and "update" in modes:
        receipt = Path(record["receipts"]) / f"{os.getpid()}-{time.monotonic_ns()}.receipt"
        with open(receipt, "x", encoding="ascii", newline="") as stream:  # CREATE_NEW, never over a file
            stream.write(f"QRONOS-INSTALL-RECEIPT 1\r\nnonce={record['nonce']}\r\npackage={record['package']}\r\n"
                         f"manifest={record['manifest']}\r\nversion={record['version']}\r\n"
                         "result=installed\r\n")
        report["steps"].append("receipt")
    report["steps"].append("installed")
    del admission  # released when this process exits anyway
    return done(None, 0)


if __name__ == "__main__":
    raise SystemExit(main())
