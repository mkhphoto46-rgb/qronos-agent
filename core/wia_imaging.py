"""Scanners through Windows Image Acquisition 2.0 (WIA), local driver only (Device Hub, H1/H2).

Which devices: WIA 2.0 serves SCANNERS (the SDK's stireg.h: "1 - StiDeviceTypeScanner (still image scanner: STI,
WIA 1.0 or WIA 2.0); 2 - StiDeviceTypeDigitalCamera (still digital camera, WIA 1.0); 3 - StiDeviceTypeStreamingVideo
(... webcam ... no longer STI/WIA compliant)"). So a scanner can be used here; a WIA camera, a webcam or a phone is
listed as not supported, never captured.

How: the IWiaDevMgr2 COM interface (wia_lh.h, Windows SDK 10.0.26100), in process:

* listing (read-only): EnumDeviceInfo(WIA_DEVINFO_ENUM_LOCAL) -> each device's IWiaPropertyStorage::ReadMultiple of
  its id, type, name, vendor and description;
* one scan: GetImageDlg(WIA_DEVICE_DIALOG_USE_COMMON_UI, device id, ..., a fresh private staging folder, ...) shows
  Windows' own WIA dialog; the person drives the scan there (nothing is captured in the background);
* the one staged image is then checked and re-encoded by :func:`finalize_scan` to the person's exact new file.

There is no helper process, shell, vendor SDK, network or service call; nothing is installed.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import os
import shutil
import stat
import tempfile
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_STAGED_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_PIXELS = 400_000_000  # a full A4 page at 1200 dpi in colour is about 140 megapixels
FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
STI_SCANNER, STI_CAMERA, STI_VIDEO = 1, 2, 3
KIND_OF_TYPE = {STI_SCANNER: "scanner", STI_CAMERA: "camera", STI_VIDEO: "video"}


class ImagingError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


@dataclass(frozen=True)
class ImagingDevice:
    wia_id: str
    name: str
    vendor: str
    description: str
    kind: str  # scanner | camera | video | other

    @property
    def device_id(self) -> str:
        return "img-" + hashlib.sha256(self.wia_id.casefold().encode("utf-8")).hexdigest()[:20]


def kind_of(dev_type: int) -> str:
    """GET_STIDEVICE_TYPE(WIA_DIP_DEV_TYPE) is its high word (sti.h)."""
    return KIND_OF_TYPE.get((int(dev_type) >> 16) & 0xFFFF, "other")


# ------------------------------------------------------------------ the exact new file (pure)


def _is_link(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    return stat.S_ISLNK(os.lstat(path).st_mode) or bool(attributes & 0x400)


def check_destination(destination: Path) -> str:
    """The image format of an exact NEW destination in an existing, non-linked folder, or ImagingError."""
    destination = Path(destination)
    fmt = FORMATS.get(destination.suffix.casefold())
    if not destination.is_absolute() or fmt is None:
        raise ImagingError("bad_destination", "an absolute .png, .jpg or .jpeg path is required")
    if os.path.lexists(destination):
        raise ImagingError("destination_exists", "the destination already exists; nothing is overwritten")
    parent = destination.parent
    if not parent.is_dir() or _is_link(parent) or any(_is_link(p) for p in parent.parents if p.exists()):
        raise ImagingError("bad_destination", "the folder must exist and must not be (or be inside) a link")
    return fmt


def finalize_scan(staged: list[str], staging: Path, destination: Path) -> dict[str, Any]:
    """Exactly one staged image inside ``staging`` -> decoded, bounded, re-encoded to the exact new destination."""
    from PIL import Image

    fmt = check_destination(destination)
    staging = Path(staging).resolve()
    present = [p for p in staging.iterdir()] if staging.is_dir() else []
    if len(staged) != 1 or len(present) != 1:
        raise ImagingError("not_one_image", f"the scan produced {len(staged)} reported / {len(present)} staged files")
    source = Path(staged[0])
    try:
        resolved = source.resolve(strict=True)
    except OSError:
        raise ImagingError("not_one_image", "the reported file is missing") from None
    if resolved.parent != staging or resolved != present[0].resolve() or _is_link(source) or not resolved.is_file():
        raise ImagingError("not_one_image", "the reported file is not the one staged file")
    size = resolved.stat().st_size
    if not 0 < size <= MAX_STAGED_BYTES:
        raise ImagingError("too_large" if size else "empty_image", "the scanned file size is out of bounds")
    data = resolved.read_bytes()
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # Pillow raises DecompressionBombError beyond twice this; refused below
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            source_format = str(image.format or "")
            if image.width * image.height > MAX_PIXELS:
                raise ImagingError("too_large", "the image has too many pixels")
            image.load()
            mode = image.mode
            if fmt == "JPEG" and mode not in ("RGB", "L"):
                image = image.convert("RGB")
            elif fmt == "PNG" and mode not in ("RGB", "RGBA", "L", "LA", "1", "P", "I;16"):
                image = image.convert("RGBA" if "A" in mode else "RGB")
            out = io.BytesIO()
            image.save(out, format=fmt, **({"quality": 95} if fmt == "JPEG" else {}))  # no metadata is copied
            width, height = image.width, image.height
    except ImagingError:
        raise
    except Image.DecompressionBombError:
        raise ImagingError("too_large", "the image has too many pixels") from None
    except Exception as error:  # noqa: BLE001 - not an image Pillow can read safely: refused, nothing written
        raise ImagingError("not_an_image", type(error).__name__) from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous
    encoded = out.getvalue()
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ImagingError("too_large", "the encoded image is too large")
    try:
        with open(destination, "xb") as handle:  # exclusive create: never overwrite
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise ImagingError("destination_exists", "the destination appeared meanwhile; nothing was overwritten") from None
    return {"path": str(destination), "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded),
            "width": width, "height": height, "format": fmt, "scannedFormat": source_format}


def new_staging() -> Path:
    """A fresh private folder for one scan (removed by :func:`remove_staging`)."""
    return Path(tempfile.mkdtemp(prefix="qronos-scan-"))


def remove_staging(staging: Path) -> None:
    staging = Path(staging)
    if not staging.name.startswith("qronos-scan-") or not staging.is_dir() or _is_link(staging):
        return
    if any(_is_link(p) for p in staging.rglob("*")):
        return  # never follow a link out of the staging folder; left for the person to inspect
    shutil.rmtree(staging)


# ------------------------------------------------------------------ Windows (WIA 2.0 COM, vtables from wia_lh.h)

CLSID_WIA_DEV_MGR2 = "b6c292bc-7c88-41ee-8b54-8ec92617e599"
IID_IWIA_DEV_MGR2 = "79c07cf1-cbdd-41ee-8ec3-f00080cada7a"
WIA_DEVINFO_ENUM_LOCAL = 0x10
WIA_DEVICE_DIALOG_USE_COMMON_UI = 0x4  # GetImageDlg documents only 0 or this flag (system UI preferred)
WIA_DIP_DEV_ID, WIA_DIP_VEND_DESC, WIA_DIP_DEV_DESC, WIA_DIP_DEV_TYPE, WIA_DIP_DEV_NAME = 2, 3, 4, 5, 7
_MGR_ENUM_DEVICE_INFO, _MGR_GET_IMAGE_DLG = 3, 10   # IWiaDevMgr2 (header order)
_ENUM_NEXT = 3                                      # IEnumWIA_DEV_INFO::Next
_PROPS_READ_MULTIPLE = 3                            # IWiaPropertyStorage::ReadMultiple
PRSPEC_PROPID = 1
VT_I4, VT_BSTR = 3, 8
S_FALSE = 1


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class _PROPSPEC(ctypes.Structure):
    _fields_ = [("ulKind", wintypes.ULONG), ("propid", ctypes.c_size_t)]  # union {PROPID; LPOLESTR}


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("value", ctypes.c_size_t), ("pad", ctypes.c_size_t)]  # 24 bytes on x64


def _call(obj, slot: int, *args, argtypes: tuple = ()) -> int:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return int(ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])(obj, *args))


def _release(obj) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)


class WiaComApi:
    """COM bindings, the STA apartment and the WIA manager (tests override ``_manager`` with a fake)."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise ImagingError("unavailable", "scanning through WIA is only available on Windows")
        self._ole = ctypes.WinDLL("ole32")
        self._oleaut = ctypes.WinDLL("oleaut32")
        self._ole.CoInitializeEx.argtypes, self._ole.CoInitializeEx.restype = [ctypes.c_void_p, wintypes.DWORD], ctypes.c_long
        self._ole.CoUninitialize.argtypes, self._ole.CoUninitialize.restype = [], None
        self._ole.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        self._ole.CoCreateInstance.restype = ctypes.c_long
        self._ole.PropVariantClear.argtypes, self._ole.PropVariantClear.restype = [ctypes.POINTER(_PROPVARIANT)], ctypes.c_long
        self._ole.CoTaskMemFree.argtypes, self._ole.CoTaskMemFree.restype = [ctypes.c_void_p], None
        self._oleaut.SysAllocString.argtypes, self._oleaut.SysAllocString.restype = [wintypes.LPCWSTR], ctypes.c_void_p
        self._oleaut.SysFreeString.argtypes, self._oleaut.SysFreeString.restype = [ctypes.c_void_p], None

    def _apartment(self, work, *args):
        hr = self._ole.CoInitializeEx(None, 0x2)  # STA: the WIA dialog is UI
        if hr < 0 and (hr & 0xFFFFFFFF) != 0x80010106:
            raise ImagingError("native_refused", f"CoInitializeEx failed (0x{hr & 0xFFFFFFFF:08X})")
        try:
            return work(*args)
        finally:
            if hr >= 0:
                self._ole.CoUninitialize()

    def _manager(self) -> ctypes.c_void_p:
        out = ctypes.c_void_p()
        hr = self._ole.CoCreateInstance(ctypes.byref(_GUID.parse(CLSID_WIA_DEV_MGR2)), None, 0x4,  # CLSCTX_LOCAL_SERVER
                                        ctypes.byref(_GUID.parse(IID_IWIA_DEV_MGR2)), ctypes.byref(out))
        if hr < 0:
            raise ImagingError("wia_unavailable", f"the WIA service did not answer (0x{hr & 0xFFFFFFFF:08X})")
        return out


def _list_with(api: WiaComApi) -> list[ImagingDevice]:
    manager = api._manager()
    enum = ctypes.c_void_p()
    try:
        hr = _call(manager, _MGR_ENUM_DEVICE_INFO, WIA_DEVINFO_ENUM_LOCAL, ctypes.byref(enum),
                   argtypes=(ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)))
        if hr < 0:
            raise ImagingError("wia_unavailable", f"EnumDeviceInfo failed (0x{hr & 0xFFFFFFFF:08X})")
        devices = []
        for _ in range(64):
            storage, fetched = ctypes.c_void_p(), wintypes.ULONG(0)
            hr = _call(enum, _ENUM_NEXT, 1, ctypes.byref(storage), ctypes.byref(fetched),
                       argtypes=(wintypes.ULONG, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)))
            if hr != 0 or fetched.value != 1:
                break
            try:
                devices.append(_read_with(api, storage))
            finally:
                _release(storage)
        return devices
    finally:
        _release(enum)
        _release(manager)


def _read_with(api: WiaComApi, storage) -> ImagingDevice:
    ids = (WIA_DIP_DEV_ID, WIA_DIP_DEV_TYPE, WIA_DIP_DEV_NAME, WIA_DIP_VEND_DESC, WIA_DIP_DEV_DESC)
    specs = (_PROPSPEC * len(ids))(*[_PROPSPEC(PRSPEC_PROPID, pid) for pid in ids])
    values = (_PROPVARIANT * len(ids))()
    hr = _call(storage, _PROPS_READ_MULTIPLE, len(ids), specs, values,
               argtypes=(wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p))
    try:
        if hr < 0:
            raise ImagingError("wia_unavailable", f"ReadMultiple failed (0x{hr & 0xFFFFFFFF:08X})")
        read = []
        for value in values:
            if value.vt == VT_BSTR and value.value:
                read.append(ctypes.wstring_at(value.value)[:512])
            elif value.vt == VT_I4:
                read.append(ctypes.c_int32(value.value & 0xFFFFFFFF).value)
            else:
                read.append(None)
        wia_id, dev_type, name, vendor, description = read
        return ImagingDevice(str(wia_id or ""), str(name or ""), str(vendor or ""), str(description or ""),
                             kind_of(int(dev_type or 0)))
    finally:
        for value in values:
            api._ole.PropVariantClear(ctypes.byref(value))


def _acquire_with(api: WiaComApi, wia_id: str, staging: Path) -> list[str]:
    manager = api._manager()
    device, folder, name = (api._oleaut.SysAllocString(wia_id), api._oleaut.SysAllocString(str(staging)),
                            api._oleaut.SysAllocString("qronos-scan"))
    count, paths, item = ctypes.c_long(0), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        hr = _call(manager, _MGR_GET_IMAGE_DLG, WIA_DEVICE_DIALOG_USE_COMMON_UI, device, None, folder, name,
                   ctypes.byref(count), ctypes.byref(paths), ctypes.byref(item),
                   argtypes=(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(ctypes.c_void_p)))
        files: list[str] = []
        if paths:
            array = ctypes.cast(paths, ctypes.POINTER(ctypes.c_void_p))
            for index in range(max(0, min(count.value, 16))):
                if array[index]:
                    files.append(ctypes.wstring_at(array[index]))
                    api._oleaut.SysFreeString(array[index])
            api._ole.CoTaskMemFree(paths)
        if hr == S_FALSE:
            return []  # cancelled in the dialog: nothing to keep
        if hr < 0:
            raise ImagingError("scan_failed", f"the WIA scan failed (0x{hr & 0xFFFFFFFF:08X})")
        return files
    finally:
        _release(item)
        for value in (device, folder, name):
            api._oleaut.SysFreeString(value)
        _release(manager)


class WinWia(WiaComApi):
    """The real WIA 2.0 manager of this PC; constructed only for a request (blocked in tests)."""

    def list_devices(self) -> list[ImagingDevice]:
        return self._apartment(_list_with, self)

    def acquire(self, wia_id: str, staging: Path) -> list[str]:
        """Windows' own WIA scan dialog for ONE image into ``staging``; [] when the person cancels."""
        return self._apartment(_acquire_with, self, wia_id, Path(staging))


def default_imaging() -> WinWia:
    return WinWia()
