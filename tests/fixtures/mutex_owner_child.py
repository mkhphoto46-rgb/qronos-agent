"""A competing owner of one PRIVATE named mutex, for ownership fixtures only.

Run by owned tests as a base-interpreter child (tests/fixtures/child_python.py) with -B:

    python -B mutex_owner_child.py NAME MODE READY_FILE GO_FILE

It wait-acquires NAME (never a product name), writes READY_FILE with the wait
result, then:
- MODE "hold":    keeps ownership until GO_FILE appears (at most 60 s), releases it and exits 0;
- MODE "abandon": ends the process at once while owning it, so Windows abandons the mutex.
It imports nothing from Qronos and touches no file but its two tmp files.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes


def main() -> int:
    name, mode, ready, go = sys.argv[1:5]
    if not name.startswith("Local\\QronosR9Test-"):
        return 9  # only private test names
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, name)
    result = kernel.WaitForSingleObject(handle, 5000)
    with open(ready + ".tmp", "w", encoding="ascii") as stream:
        stream.write(str(int(result)))
    os.replace(ready + ".tmp", ready)  # published whole: the parent never reads a half-written file
    if result not in (0, 0x80):
        return 3
    if mode == "abandon":
        os._exit(0)  # the owning thread ends without releasing: abandoned
    deadline = time.monotonic() + 60
    while not os.path.exists(go) and time.monotonic() < deadline:
        time.sleep(0.02)
    kernel.ReleaseMutex(handle)
    kernel.CloseHandle(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
