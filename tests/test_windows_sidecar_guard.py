from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from ctypes import wintypes
from pathlib import Path

from core.windows_sidecar_guard import (
    SidecarAlreadyRunning,
    WindowsSidecarGuard,
)


@unittest.skipUnless(
    os.name == "nt",
    "Windows Job Object tests run on Windows only.",
)
class TestWindowsSidecarGuard(unittest.TestCase):
    def _unique_mutex(self, suffix: str) -> str:
        return (
            "Local\\Qronos.Test."
            + suffix
            + "."
            + uuid.uuid4().hex
        )

    @staticmethod
    def _process_alive(pid: int) -> bool:
        kernel32 = ctypes.WinDLL(
            "kernel32",
            use_last_error=True,
        )
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102

        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [
            wintypes.HANDLE,
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION
            | SYNCHRONIZE,
            False,
            int(pid),
        )
        if not handle:
            return False

        try:
            return (
                kernel32.WaitForSingleObject(
                    handle,
                    0,
                )
                == WAIT_TIMEOUT
            )
        finally:
            kernel32.CloseHandle(handle)

    def test_second_process_cannot_acquire_same_mutex(self) -> None:
        mutex_name = self._unique_mutex(
            "singleton"
        )
        guard = WindowsSidecarGuard.acquire(
            mutex_name
        )

        script = (
            "from core.windows_sidecar_guard import "
            "WindowsSidecarGuard, SidecarAlreadyRunning;"
            f"name={mutex_name!r};"
            "import sys;"
            "\ntry:\n"
            " g=WindowsSidecarGuard.acquire(name)\n"
            "except SidecarAlreadyRunning:\n"
            " raise SystemExit(0)\n"
            "else:\n"
            " g.close(); raise SystemExit(3)\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(
                    Path(__file__).resolve().parent.parent
                ),
                timeout=10.0,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
            )
        finally:
            guard.close()

    def test_closing_job_guard_terminates_bound_child(self) -> None:
        mutex_name = self._unique_mutex(
            "close"
        )
        guard = WindowsSidecarGuard.acquire(
            mutex_name
        )
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ]
        )

        try:
            guard.bind_process(child.pid)
            guard.close()
            child.wait(timeout=5.0)
            self.assertIsNotNone(
                child.returncode
            )
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5.0)

    def test_parent_exit_kills_bound_child(self) -> None:
        mutex_name = self._unique_mutex(
            "parentdeath"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            pid_file = Path(temp_dir) / "child.pid"
            project_root = (
                Path(__file__).resolve().parent.parent
            )

            parent_script = (
                "import os, subprocess, sys, time;"
                "from pathlib import Path;"
                "from core.windows_sidecar_guard import WindowsSidecarGuard;"
                f"g=WindowsSidecarGuard.acquire({mutex_name!r});"
                "p=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(60)']);"
                "g.bind_process(p.pid);"
                f"Path({str(pid_file)!r}).write_text(str(p.pid),encoding='utf-8');"
                "time.sleep(0.15);"
                "os._exit(0)"
            )

            parent = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    parent_script,
                ],
                cwd=str(project_root),
            )

            child_pid = None

            try:
                parent.wait(timeout=10.0)

                deadline = time.monotonic() + 5.0
                while (
                    not pid_file.exists()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)

                self.assertTrue(
                    pid_file.exists()
                )
                child_pid = int(
                    pid_file.read_text(
                        encoding="utf-8"
                    ).strip()
                )

                deadline = time.monotonic() + 8.0
                while (
                    self._process_alive(child_pid)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.10)

                self.assertFalse(
                    self._process_alive(child_pid),
                    "Bound sidecar survived parent process death.",
                )
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5.0)

                if (
                    child_pid is not None
                    and self._process_alive(child_pid)
                ):
                    subprocess.run(
                        [
                            "taskkill",
                            "/PID",
                            str(child_pid),
                            "/F",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )


if __name__ == "__main__":
    unittest.main()
