from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import re
import uuid


class PackagedAppActivationError(RuntimeError):
    """Windows could not activate one exact packaged application."""


_AUMID_RE = re.compile(r"^[A-Za-z0-9._-]+![A-Za-z0-9._-]+$")

# Windows SDK values from shobjidl_core.h.
_CLSID_APPLICATION_ACTIVATION_MANAGER = "45BA127D-10A8-46EA-8AB7-56EA9078943C"
_IID_IAPPLICATION_ACTIVATION_MANAGER = "2E941141-7F97-4756-BA1D-9DECDE894A3D"

_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
_RPC_E_CHANGED_MODE = 0x80010106


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def _failed(hr: int) -> bool:
    return bool(ctypes.c_uint32(int(hr)).value & 0x80000000)


def _hr_hex(hr: int) -> str:
    return f"0x{ctypes.c_uint32(int(hr)).value:08X}"


def _validate_aumid(aumid: str) -> str:
    value = str(aumid or "").strip()
    if not value or len(value) > 256 or not _AUMID_RE.fullmatch(value):
        raise ValueError("A packaged-app AUMID must be one exact PackageFamily!Application id.")
    return value


def activate_application(aumid: str) -> int:
    """Activate one registered packaged app and return Windows' process id.

    This uses IApplicationActivationManager directly instead of routing through
    Explorer or a shell command. The AUMID is already part of a reviewed
    AppSpec; arbitrary arguments are intentionally not exposed here.
    """
    app_id = _validate_aumid(aumid)
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        raise PackagedAppActivationError(
            "Packaged application activation supports Windows only."
        )

    ole32 = ctypes.windll.ole32
    HRESULT = ctypes.c_long

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = HRESULT
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None

    init_hr = int(ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED))
    changed_mode = ctypes.c_uint32(init_hr).value == _RPC_E_CHANGED_MODE
    if _failed(init_hr) and not changed_mode:
        raise PackagedAppActivationError(
            f"COM initialization failed with HRESULT {_hr_hex(init_hr)}."
        )

    must_uninitialize = not changed_mode
    manager = ctypes.c_void_p()
    try:
        clsid = _GUID.parse(_CLSID_APPLICATION_ACTIVATION_MANAGER)
        iid = _GUID.parse(_IID_IAPPLICATION_ACTIVATION_MANAGER)
        hr = int(
            ole32.CoCreateInstance(
                ctypes.byref(clsid),
                None,
                _CLSCTX_INPROC_SERVER,
                ctypes.byref(iid),
                ctypes.byref(manager),
            )
        )
        if _failed(hr) or not manager.value:
            raise PackagedAppActivationError(
                "Windows ApplicationActivationManager was unavailable "
                f"(HRESULT {_hr_hex(hr)})."
            )

        prototype_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        vtable = ctypes.cast(
            manager,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ).contents
        activate = prototype_factory(
            HRESULT,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )(vtable[3])
        release = prototype_factory(
            wintypes.ULONG,
            ctypes.c_void_p,
        )(vtable[2])

        pid = wintypes.DWORD(0)
        try:
            hr = int(activate(manager, app_id, "", 0, ctypes.byref(pid)))
        finally:
            release(manager)
            manager = ctypes.c_void_p()

        if _failed(hr):
            raise PackagedAppActivationError(
                f"Windows could not activate {app_id!r} (HRESULT {_hr_hex(hr)})."
            )
        if int(pid.value) <= 0:
            raise PackagedAppActivationError(
                "Windows reported successful packaged-app activation without a process id."
            )
        return int(pid.value)
    finally:
        if manager.value:
            prototype_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
            vtable = ctypes.cast(
                manager,
                ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
            ).contents
            release = prototype_factory(wintypes.ULONG, ctypes.c_void_p)(vtable[2])
            release(manager)
        if must_uninitialize:
            ole32.CoUninitialize()
