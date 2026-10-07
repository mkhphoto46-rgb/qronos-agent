from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os


class RuntimeAlreadyRunning(RuntimeError):
    pass


@dataclass
class RuntimeInstanceGuard:
    handle: int = 0

    @classmethod
    def acquire(
        cls,
    ) -> "RuntimeInstanceGuard":

        if os.name != "nt":
            return cls(
                handle=0
            )

        kernel32 = ctypes.windll.kernel32

        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]

        kernel32.CreateMutexW.restype = (
            wintypes.HANDLE
        )

        kernel32.GetLastError.restype = (
            wintypes.DWORD
        )

        kernel32.CloseHandle.argtypes = [
            wintypes.HANDLE
        ]

        kernel32.CloseHandle.restype = (
            wintypes.BOOL
        )

        handle = kernel32.CreateMutexW(
            None,
            False,
            "Local\\QronosAgentRuntimeBridge",
        )

        if not handle:
            raise OSError(
                "Windows could not create the Qronos runtime mutex."
            )

        ERROR_ALREADY_EXISTS = 183

        if (
            int(
                kernel32.GetLastError()
            )
            == ERROR_ALREADY_EXISTS
        ):
            kernel32.CloseHandle(
                handle
            )

            raise RuntimeAlreadyRunning(
                "Another Qronos runtime instance is already active."
            )

        return cls(
            handle=int(handle)
        )

    def close(
        self,
    ) -> None:

        if (
            os.name != "nt"
            or not self.handle
        ):
            self.handle = 0
            return

        try:
            ctypes.windll.kernel32.CloseHandle(
                wintypes.HANDLE(
                    self.handle
                )
            )
        finally:
            self.handle = 0
