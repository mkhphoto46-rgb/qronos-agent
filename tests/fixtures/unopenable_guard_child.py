"""An owned PRIVATE guard that stays ALIVE but that nobody can open (Media15 F15-1). Test fixture only.

Adapted from the independent reviewer's reproducer work/review_checks_media_r15_installer/probes/
unopenable_guard_child.py (sha256 f86e3b53...; that original stays untouched). Changes:
- the repository path and the installer argv come from the caller, so the full-template probe can be used too;
- the acknowledgement is recorded as received bytes;
- the lifetime is BOUNDED and tied to its exact controller (Root round 12). Nobody can open this process
  (that is the point), so a stop from outside cannot reach it. Therefore:
  - it opens its controller (pid and creation time given on the command line) BEFORE closing its own DACL;
  - it ends, after killing ONLY its own installer child through the handle it holds, as soon as the controller
    is gone, a stop file appears, or LIFETIME seconds have passed;
  - the acknowledgement is read on a thread, so no read can block that.

Run as an owned base-interpreter child (-B -I). It owns the private fence (MutexHold), gives ITS OWN process
object an empty DACL (standard-user openers get ERROR_ACCESS_DENIED), starts the installer as its own child,
duplicates the fence into it, writes a correct hand-over record (its own pid and creation time), reads the
acknowledgement until the installer closes its output (bounded), lets the fence go as launch() does and STAYS
ALIVE until <work>/guard.stop exists. Touches only its tmp work folder. No registry, UAC, SCM or production mutex.

Usage: python -B -I unopenable_guard_child.py REPO FENCE DATA_ROOT VERSION WORK ARGV_JSON CONTROLLER_PID:FILETIME
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

LIFETIME = 240.0  # seconds, whatever happens
started = time.monotonic()
repo, fence, data_root, version, work, argv_json, controller = sys.argv[1:8]
sys.path.insert(0, repo)
from core import update_service  # noqa: E402

import _winapi  # noqa: E402

work = Path(work)
argv = json.loads(argv_json)
report = {"guardPid": os.getpid()}

# The exact controller: opened now (while it must be alive) and checked by creation time.
controller_pid, controller_created = (int(part) for part in controller.split(":"))
try:
    controller_handle = _winapi.OpenProcess(0x00100000 | 0x1000, False, controller_pid)  # SYNCHRONIZE|QUERY_LIMITED
except OSError:
    raise SystemExit("the controller is already gone")
if update_service._process_creation_time(controller_handle) != controller_created:
    raise SystemExit("the pid no longer belongs to the controller")


def controller_gone() -> bool:
    return _winapi.WaitForSingleObject(controller_handle, 0) == _winapi.WAIT_OBJECT_0


def must_end() -> str | None:
    if controller_gone():
        return "controller gone"
    if time.monotonic() - started > LIFETIME:
        return "lifetime"
    if (work / "guard.stop").exists():
        return "stop file"
    return None


advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
advapi32.InitializeAcl.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
advapi32.SetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_void_p]
advapi32.SetSecurityInfo.restype = wintypes.DWORD

hold = update_service.MutexHold((fence,))
me = _winapi.GetCurrentProcess()
created = update_service._process_creation_time(me)
acl = ctypes.create_string_buffer(64)
assert advapi32.InitializeAcl(acl, 64, 2), ctypes.get_last_error()
report["setSecurityInfo"] = advapi32.SetSecurityInfo(kernel32.GetCurrentProcess(), 6, 4, None, None, acl, None)

process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
report["probePid"] = process.pid
(work / "guard.ready").write_text(json.dumps(report), encoding="utf-8")
ended = None
try:
    duplicated = _winapi.DuplicateHandle(me, hold.fence_handle(), int(process._handle), 0x00100000, False, 0)
    record = ("QRONOS-UPDATE-FENCE 1\n"
              f"nonce={os.urandom(32).hex()}\nfence={duplicated}\nreceipts={Path(data_root) / 'Updates' / 'receipts'}\n"
              f"version={version}\npackage={'a' * 64}\nmanifest={'b' * 64}\nguard={os.getpid()}:{created}\n").encode()
    try:
        process.stdin.write(record)
        process.stdin.close()
    except OSError:
        pass  # the child may not read at all (a stand-in); the bounded loop below still ends everything
    received: list[bytes] = []
    reader = threading.Thread(target=lambda: received.append(process.stdout.read()), daemon=True)
    reader.start()
    while reader.is_alive() and ended is None:
        reader.join(0.05)
        ended = must_end()
    report["ack"] = received[0].decode("ascii", "replace") if received else None
    hold.release()  # as launch() does after the ACK: the fence goes to the installer; this guard stays ALIVE
    while ended is None:
        if process.poll() is not None and "probeExit" not in report:
            report["probeExit"] = process.returncode
            report["guardAliveAtProbeExit"] = True
            (work / "guard.report").write_text(json.dumps(report), encoding="utf-8")
        time.sleep(0.05)
        ended = must_end()
finally:
    report["ended"] = ended
    if process.poll() is None:
        try:
            report["probeExit"] = process.wait(timeout=0 if ended == "controller gone" else 60)
        except subprocess.TimeoutExpired:
            process.kill()  # exactly its own installer child, by its own handle
            report["probeExit"] = process.wait(30)
            report["probeKilled"] = True
    report.setdefault("probeExit", process.returncode)
    (work / "guard.report").write_text(json.dumps(report), encoding="utf-8")
