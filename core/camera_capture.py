"""One still frame from a local camera/webcam (Device Hub, H1) through Windows Media Foundation.

What it does: lists what Windows itself lists as video capture devices, and for ONE approved request opens the one
camera that was approved, reads a few frames (the first ones are dropped in memory while the exposure settles), keeps
ONE frame and writes it to one exact NEW PNG/JPEG file. The camera is on only during that capture (at most
:data:`CAPTURE_TIMEOUT_S`); Windows shows its own camera indicator. No preview, no audio, no video, no recognition.

How: Media Foundation COM through ctypes (mfplat/mf/mfreadwrite/ole32; every slot, GUID and constant was read from the
Windows SDK 10.0.26100 headers - see tests/test_camera_capture.py). ``MFStartup`` runs with ``MFSTARTUP_LITE`` (no
socket library). The source reader converts the camera's own format to RGB32 with Windows' own video processing.
Nothing is loaded or called at import; the real backend :class:`WinCamera` is constructed only for a request (and is
blocked in tests by the live-device boundary).

Cancellation: the capture runs in a worker thread; the caller's ``should_stop`` (Pause/Stop/shutdown) and the deadline
are polled, and a stop calls ``IMFMediaSource::Shutdown`` (pending reads then fail with ``MF_E_SHUTDOWN``).
"""
from __future__ import annotations

import ctypes
import hashlib
import io
import os
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

CAPTURE_TIMEOUT_S = 10.0
JOIN_GRACE_S = 5.0
WARMUP_FRAMES = 4          # dropped in memory while the exposure settles; the next one is kept
MAX_READS = 120            # ReadSample calls per capture (stream ticks included)
MAX_WIDTH, MAX_HEIGHT = 7680, 4320
MAX_PIXELS = MAX_WIDTH * MAX_HEIGHT
MAX_CAMERAS = 32
DARK_MEAN = 2.0            # mean 8-bit luminance below this: "almost completely dark" (e.g. a covered lens)

# ------------------------------------------------------------------ SDK facts (mfobjects.h/mfidl.h/mfreadwrite.h/mfapi.h)

MF_VERSION = (0x0002 << 16) | 0x0070      # MF_SDK_VERSION (Win7+) << 16 | MF_API_VERSION
MFSTARTUP_LITE = 0x1                       # MFSTARTUP_NOSOCKET
COINIT_MULTITHREADED = 0x0
SLOTS = {
    ("IMFAttributes", "GetUINT32"): 7, ("IMFAttributes", "GetUINT64"): 8, ("IMFAttributes", "GetAllocatedString"): 13,
    ("IMFAttributes", "SetUINT32"): 21, ("IMFAttributes", "SetGUID"): 24,
    ("IMFActivate", "ActivateObject"): 33, ("IMFActivate", "ShutdownObject"): 34,
    ("IMFMediaSource", "Shutdown"): 12,
    ("IMFSourceReader", "SetStreamSelection"): 4, ("IMFSourceReader", "GetCurrentMediaType"): 6,
    ("IMFSourceReader", "SetCurrentMediaType"): 7, ("IMFSourceReader", "ReadSample"): 9,
    ("IMFSample", "ConvertToContiguousBuffer"): 41,
    ("IMFMediaBuffer", "Lock"): 3, ("IMFMediaBuffer", "Unlock"): 4,
    ("IMF2DBuffer", "Lock2D"): 3, ("IMF2DBuffer", "Unlock2D"): 4,
}
_QUERY_INTERFACE, _RELEASE = 0, 2
GUIDS = {
    "MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE": "c60ac5fe-252a-478f-a0ef-bc8fa5f7cad3",
    "MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID": "8ac3587a-4ae7-42d8-99e0-0a6013eef90f",
    "MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME": "60d0e559-52f8-4fa2-bbce-acdb34a8ec01",
    "MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK": "58f0aad8-22bf-4f8a-bb3d-d2c4978c6e2f",
    "MF_SOURCE_READER_ENABLE_ADVANCED_VIDEO_PROCESSING": "0f81da2c-b537-4672-a8b2-a681b17307a3",
    "MF_MT_MAJOR_TYPE": "48eba18e-f8c9-4687-bf11-0a74c9f96a8f",
    "MF_MT_SUBTYPE": "f7e34c9a-42e8-4714-b74b-cb29d72c35e5",
    "MF_MT_FRAME_SIZE": "1652c33d-d6b2-4012-b834-72030849a37d",
    "MF_MT_DEFAULT_STRIDE": "644b4e48-1e02-4516-b0eb-c01ca9d49ac6",
    "MFMediaType_Video": "73646976-0000-0010-8000-00aa00389b71",
    "MFVideoFormat_RGB32": "00000016-0000-0010-8000-00aa00389b71",   # DEFINE_MEDIATYPE_GUID(.., D3DFMT_X8R8G8B8 = 22)
}
IIDS = {"IMFMediaSource": "279a808d-aec7-40c8-9c6b-a6b492c78a66", "IMF2DBuffer": "7dc9d5f9-9ed9-44ec-9bbf-0600bb589fbb"}
READER = {"MF_SOURCE_READER_FIRST_VIDEO_STREAM": 0xFFFFFFFC, "MF_SOURCE_READER_ALL_STREAMS": 0xFFFFFFFE,
          "MF_SOURCE_READERF_ERROR": 0x1, "MF_SOURCE_READERF_ENDOFSTREAM": 0x2, "MF_SOURCE_READERF_STREAMTICK": 0x100}
HRESULTS = {"E_ACCESSDENIED": 0x80070005, "MF_E_SHUTDOWN": 0xC00D3E85, "MF_E_INVALIDMEDIATYPE": 0xC00D36B4,
            "MF_E_TOPO_CODEC_NOT_FOUND": 0xC00D5212, "MF_E_HW_MFT_FAILED_START_STREAMING": 0xC00D3704,
            "MF_E_VIDEO_RECORDING_DEVICE_INVALIDATED": 0xC00D3EA2, "MF_E_VIDEO_RECORDING_DEVICE_PREEMPTED": 0xC00D3EA3}
_HR_CODE = {HRESULTS["E_ACCESSDENIED"]: "camera_privacy_denied",
            HRESULTS["MF_E_HW_MFT_FAILED_START_STREAMING"]: "camera_busy",
            HRESULTS["MF_E_VIDEO_RECORDING_DEVICE_PREEMPTED"]: "camera_busy",
            HRESULTS["MF_E_VIDEO_RECORDING_DEVICE_INVALIDATED"]: "camera_gone"}
_FORMAT_HR = {HRESULTS["MF_E_INVALIDMEDIATYPE"], HRESULTS["MF_E_TOPO_CODEC_NOT_FOUND"]}


class CameraError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def _hr_error(hr: int, what: str, default: str = "camera_failed") -> CameraError:
    unsigned = hr & 0xFFFFFFFF
    return CameraError(_HR_CODE.get(unsigned, default), f"{what} failed (0x{unsigned:08X})")


@dataclass(frozen=True)
class CameraDevice:
    """One video capture device as Windows lists it; ``symbolic_link`` is its identity."""

    symbolic_link: str
    name: str

    @property
    def device_id(self) -> str:
        return "cam-" + hashlib.sha256(self.symbolic_link.casefold().encode("utf-8")).hexdigest()[:20]


@dataclass
class Frame:
    """One kept frame: top-down BGRX rows (``4 * width`` bytes each)."""

    width: int
    height: int
    bgrx: bytes
    frames_read: int


# ------------------------------------------------------------------ the file (no staging: the frame is in memory)

def save_frame(frame: Frame, destination: Path, check: Callable[[Path], str]) -> dict[str, Any]:
    """The kept frame as ONE exact new PNG/JPEG (no metadata), exclusive create; nothing is written on refusal.

    ``check`` is the Device Hub's destination rule (core.wia_imaging.check_destination: an exact absolute new
    .png/.jpg/.jpeg in an existing folder with no link on the way); it returns the format and runs right before
    the write."""
    from PIL import Image, ImageStat

    fmt = check(destination)
    if not (0 < frame.width <= MAX_WIDTH and 0 < frame.height <= MAX_HEIGHT) \
            or len(frame.bgrx) != 4 * frame.width * frame.height:
        raise CameraError("camera_bad_frame", "the frame does not have the size it reported")
    image = Image.frombuffer("RGB", (frame.width, frame.height), frame.bgrx, "raw", "BGRX", 0, 1).copy()
    dark = ImageStat.Stat(image.convert("L")).mean[0] < DARK_MEAN
    out = io.BytesIO()
    image.save(out, format=fmt, **({"quality": 95} if fmt == "JPEG" else {}))
    encoded = out.getvalue()
    try:
        with open(destination, "xb") as handle:  # exclusive create: never overwrite
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise CameraError("destination_exists", "the destination appeared meanwhile; nothing was overwritten") from None
    return {"path": str(destination), "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded),
            "width": frame.width, "height": frame.height, "format": fmt, "framesRead": frame.frames_read, "dark": dark}


# ------------------------------------------------------------------ COM plumbing

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, name: str) -> "_GUID":
        text = GUIDS.get(name) or IIDS[name]
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


def _call(obj, interface: str, method: str, *args, argtypes: tuple = ()) -> int:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    slot = SLOTS[(interface, method)]
    return int(ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])(obj, *args))


def _query(obj, iid: str) -> ctypes.c_void_p:
    out = ctypes.c_void_p()
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    hr = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
        vtable[_QUERY_INTERFACE])(obj, ctypes.byref(_GUID.of(iid)), ctypes.byref(out))
    return out if hr >= 0 and out else ctypes.c_void_p()


def _release(obj) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[_RELEASE])(obj)


class MfApi:
    """The Media Foundation entry points (tests replace this whole surface with fakes; no DLL is loaded then)."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise CameraError("unavailable", "camera capture through Media Foundation is only available on Windows")
        self._ole = ctypes.WinDLL("ole32")
        self._plat = ctypes.WinDLL("mfplat")
        self._mf = ctypes.WinDLL("mf")
        self._reader = ctypes.WinDLL("mfreadwrite")
        pv, ppv = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        for dll, name, argtypes in (
                (self._ole, "CoInitializeEx", [pv, wintypes.DWORD]), (self._ole, "CoUninitialize", []),
                (self._ole, "CoTaskMemFree", [pv]), (self._plat, "MFStartup", [wintypes.ULONG, wintypes.DWORD]),
                (self._plat, "MFShutdown", []), (self._plat, "MFCreateAttributes", [ppv, ctypes.c_uint32]),
                (self._plat, "MFCreateMediaType", [ppv]),
                (self._mf, "MFEnumDeviceSources", [pv, ctypes.POINTER(ppv), ctypes.POINTER(ctypes.c_uint32)]),
                (self._reader, "MFCreateSourceReaderFromMediaSource", [pv, pv, ppv])):
            function = getattr(dll, name)
            function.argtypes = argtypes
            function.restype = None if name in ("CoUninitialize", "CoTaskMemFree") else ctypes.c_long

    # each returns an HRESULT (or None) exactly as the SDK function does
    def co_initialize(self) -> int:
        return self._ole.CoInitializeEx(None, COINIT_MULTITHREADED)

    def co_uninitialize(self) -> None:
        self._ole.CoUninitialize()

    def task_free(self, pointer) -> None:
        self._ole.CoTaskMemFree(pointer)

    def startup(self) -> int:
        return self._plat.MFStartup(MF_VERSION, MFSTARTUP_LITE)

    def shutdown(self) -> int:
        return self._plat.MFShutdown()

    def create_attributes(self, out, count: int) -> int:
        return self._plat.MFCreateAttributes(out, count)

    def create_media_type(self, out) -> int:
        return self._plat.MFCreateMediaType(out)

    def enum_device_sources(self, attributes, out_array, out_count) -> int:
        return self._mf.MFEnumDeviceSources(attributes, out_array, out_count)

    def reader_from_source(self, source, attributes, out) -> int:
        return self._reader.MFCreateSourceReaderFromMediaSource(source, attributes, out)


def _session(api: MfApi, work: Callable[[], Any]) -> Any:
    hr = api.co_initialize()
    if hr < 0 and (hr & 0xFFFFFFFF) != 0x80010106:  # RPC_E_CHANGED_MODE: this thread already has COM
        raise CameraError("native_refused", f"CoInitializeEx failed (0x{hr & 0xFFFFFFFF:08X})")
    try:
        started = api.startup()
        if started < 0:
            raise _hr_error(started, "MFStartup", "camera_unavailable")
        try:
            return work()
        finally:
            api.shutdown()
    finally:
        if hr >= 0:
            api.co_uninitialize()


def _string(api: MfApi, attributes, key: str) -> str:
    text, length = ctypes.c_void_p(), ctypes.c_uint32(0)
    hr = _call(attributes, "IMFAttributes", "GetAllocatedString", ctypes.byref(_GUID.of(key)), ctypes.byref(text),
               ctypes.byref(length), argtypes=(ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
                                               ctypes.POINTER(ctypes.c_uint32)))
    if hr < 0 or not text:
        return ""
    try:
        return ctypes.wstring_at(text.value, min(length.value, 2048))
    finally:
        api.task_free(text)


def _enumerate(api: MfApi) -> list[tuple[Any, CameraDevice]]:
    """(activate, device) for every video capture device Windows lists; the caller releases the activates."""
    attributes = ctypes.c_void_p()
    hr = api.create_attributes(ctypes.byref(attributes), 1)
    if hr < 0:
        raise _hr_error(hr, "MFCreateAttributes", "camera_unavailable")
    array, count = ctypes.c_void_p(), ctypes.c_uint32(0)
    try:
        hr = _call(attributes, "IMFAttributes", "SetGUID", ctypes.byref(_GUID.of("MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE")),
                   ctypes.byref(_GUID.of("MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID")),
                   argtypes=(ctypes.POINTER(_GUID), ctypes.POINTER(_GUID)))
        if hr < 0:
            raise _hr_error(hr, "SetGUID(source type)")
        hr = api.enum_device_sources(attributes, ctypes.byref(array), ctypes.byref(count))
        if hr < 0:
            raise _hr_error(hr, "MFEnumDeviceSources", "camera_unavailable")
    finally:
        _release(attributes)
    found: list[tuple[Any, CameraDevice]] = []
    if array:
        pointers = ctypes.cast(array, ctypes.POINTER(ctypes.c_void_p))
        for index in range(count.value):
            activate = ctypes.c_void_p(pointers[index])
            if not activate:
                continue
            if len(found) >= MAX_CAMERAS:
                _release(activate)
                continue
            device = CameraDevice(_string(api, activate, "MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK"),
                                  _string(api, activate, "MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME")[:200])
            if device.symbolic_link:
                found.append((activate, device))
            else:
                _release(activate)
        api.task_free(array)
    return found


def _list_with(api: MfApi) -> list[CameraDevice]:
    def work():
        pairs = _enumerate(api)
        for activate, _ in pairs:
            _release(activate)
        return [device for _, device in pairs]

    return _session(api, work)


@dataclass
class _Live:
    """What the waiting thread may stop: the live media source, under one lock."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    source: Any = None
    stopped: str = ""

    def stop(self, why: str) -> None:
        with self.lock:
            if not self.stopped:
                self.stopped = why
            if self.source:
                _call(self.source, "IMFMediaSource", "Shutdown")  # pending reads fail with MF_E_SHUTDOWN


def _copy_frame(sample, width: int, height: int, default_stride: int) -> bytes:
    buffer = ctypes.c_void_p()
    hr = _call(sample, "IMFSample", "ConvertToContiguousBuffer", ctypes.byref(buffer),
               argtypes=(ctypes.POINTER(ctypes.c_void_p),))
    if hr < 0 or not buffer:
        raise _hr_error(hr, "ConvertToContiguousBuffer")
    row = 4 * width
    try:
        two_d = _query(buffer, "IMF2DBuffer")
        if two_d:
            try:
                scan0, pitch = ctypes.c_void_p(), ctypes.c_long(0)
                hr = _call(two_d, "IMF2DBuffer", "Lock2D", ctypes.byref(scan0), ctypes.byref(pitch),
                           argtypes=(ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_long)))
                if hr < 0 or not scan0:
                    raise _hr_error(hr, "Lock2D")
                try:
                    if abs(pitch.value) < row:
                        raise CameraError("camera_bad_frame", "the frame pitch is smaller than its width")
                    return b"".join(ctypes.string_at(scan0.value + y * pitch.value, row) for y in range(height))
                finally:
                    _call(two_d, "IMF2DBuffer", "Unlock2D")
            finally:
                _release(two_d)
        data, maximum, current = ctypes.c_void_p(), wintypes.DWORD(0), wintypes.DWORD(0)
        hr = _call(buffer, "IMFMediaBuffer", "Lock", ctypes.byref(data), ctypes.byref(maximum), ctypes.byref(current),
                   argtypes=(ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
                             ctypes.POINTER(wintypes.DWORD)))
        if hr < 0 or not data:
            raise _hr_error(hr, "IMFMediaBuffer::Lock")
        try:
            stride = default_stride or row
            if abs(stride) < row or current.value < abs(stride) * height:
                raise CameraError("camera_bad_frame", "the frame buffer is smaller than the frame it reports")
            top = data.value + (0 if stride > 0 else (height - 1) * -stride)
            return b"".join(ctypes.string_at(top + y * stride, row) for y in range(height))
        finally:
            _call(buffer, "IMFMediaBuffer", "Unlock")
    finally:
        _release(buffer)


def _set_guid(obj, key: str, value: str) -> int:
    return _call(obj, "IMFAttributes", "SetGUID", ctypes.byref(_GUID.of(key)), ctypes.byref(_GUID.of(value)),
                 argtypes=(ctypes.POINTER(_GUID), ctypes.POINTER(_GUID)))


def _read_one(api: MfApi, symbolic_link: str, live: _Live, deadline: float) -> Frame:
    """The worker: open the ONE approved camera, read until a frame is kept, close everything."""
    pairs = _enumerate(api)
    activate, source, attributes, reader, wanted, current = None, ctypes.c_void_p(), ctypes.c_void_p(), \
        ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        for candidate, device in pairs:
            if activate is None and device.symbolic_link.casefold() == symbolic_link.casefold():
                activate = candidate
            else:
                _release(candidate)
        if activate is None:
            raise CameraError("camera_gone", "the camera is no longer listed by Windows")
        hr = _call(activate, "IMFActivate", "ActivateObject", ctypes.byref(_GUID.of("IMFMediaSource")),
                   ctypes.byref(source), argtypes=(ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)))
        if hr < 0 or not source:
            raise _hr_error(hr, "ActivateObject")
        with live.lock:
            live.source = source
            stopped = live.stopped
        if stopped:
            raise CameraError("cancelled", stopped)
        hr = api.create_attributes(ctypes.byref(attributes), 1)
        if hr < 0:
            raise _hr_error(hr, "MFCreateAttributes")
        hr = _call(attributes, "IMFAttributes", "SetUINT32",
                   ctypes.byref(_GUID.of("MF_SOURCE_READER_ENABLE_ADVANCED_VIDEO_PROCESSING")), 1,
                   argtypes=(ctypes.POINTER(_GUID), ctypes.c_uint32))
        if hr < 0:
            raise _hr_error(hr, "SetUINT32(video processing)")
        hr = api.reader_from_source(source, attributes, ctypes.byref(reader))
        if hr < 0 or not reader:
            raise _hr_error(hr, "MFCreateSourceReaderFromMediaSource")
        first, every = READER["MF_SOURCE_READER_FIRST_VIDEO_STREAM"], READER["MF_SOURCE_READER_ALL_STREAMS"]
        for stream, selected in ((every, 0), (first, 1)):  # video only: never an audio stream
            hr = _call(reader, "IMFSourceReader", "SetStreamSelection", stream, selected,
                       argtypes=(wintypes.DWORD, wintypes.BOOL))
            if hr < 0:
                raise _hr_error(hr, "SetStreamSelection")
        hr = api.create_media_type(ctypes.byref(wanted))
        if hr < 0:
            raise _hr_error(hr, "MFCreateMediaType")
        for key, value in (("MF_MT_MAJOR_TYPE", "MFMediaType_Video"), ("MF_MT_SUBTYPE", "MFVideoFormat_RGB32")):
            hr = _set_guid(wanted, key, value)
            if hr < 0:
                raise _hr_error(hr, "SetGUID(media type)")
        hr = _call(reader, "IMFSourceReader", "SetCurrentMediaType", first, None, wanted,
                   argtypes=(wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p))
        if hr < 0:
            raise _hr_error(hr, "SetCurrentMediaType",
                            "camera_format_unsupported" if (hr & 0xFFFFFFFF) in _FORMAT_HR else "camera_failed")
        hr = _call(reader, "IMFSourceReader", "GetCurrentMediaType", first, ctypes.byref(current),
                   argtypes=(wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)))
        if hr < 0 or not current:
            raise _hr_error(hr, "GetCurrentMediaType")
        size = ctypes.c_uint64(0)
        hr = _call(current, "IMFAttributes", "GetUINT64", ctypes.byref(_GUID.of("MF_MT_FRAME_SIZE")), ctypes.byref(size),
                   argtypes=(ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_uint64)))
        width, height = (size.value >> 32) & 0xFFFFFFFF, size.value & 0xFFFFFFFF
        if hr < 0 or not (0 < width <= MAX_WIDTH and 0 < height <= MAX_HEIGHT):
            raise CameraError("camera_format_unsupported", f"the camera's frame size {width}x{height} is not supported")
        stride = ctypes.c_uint32(0)
        hr = _call(current, "IMFAttributes", "GetUINT32", ctypes.byref(_GUID.of("MF_MT_DEFAULT_STRIDE")),
                   ctypes.byref(stride), argtypes=(ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_uint32)))
        default_stride = ctypes.c_int32(stride.value).value if hr >= 0 else 0
        kept: bytes | None = None
        delivered = 0
        for _ in range(MAX_READS):
            if live.stopped or time.monotonic() > deadline:
                break
            index, flags, stamp, sample = wintypes.DWORD(0), wintypes.DWORD(0), ctypes.c_longlong(0), ctypes.c_void_p()
            hr = _call(reader, "IMFSourceReader", "ReadSample", first, 0, ctypes.byref(index), ctypes.byref(flags),
                       ctypes.byref(stamp), ctypes.byref(sample),
                       argtypes=(wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                 ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_longlong),
                                 ctypes.POINTER(ctypes.c_void_p)))
            try:
                if live.stopped:
                    break
                if hr < 0:
                    raise _hr_error(hr, "ReadSample")
                if flags.value & READER["MF_SOURCE_READERF_ERROR"]:
                    raise CameraError("camera_failed", "the camera stream reported an error")
                if flags.value & READER["MF_SOURCE_READERF_ENDOFSTREAM"]:
                    raise CameraError("camera_gone", "the camera stream ended")
                if not sample:
                    continue  # a stream tick or a gap: no frame
                delivered += 1
                kept = _copy_frame(sample, width, height, default_stride)  # earlier frames are simply replaced
                if delivered > WARMUP_FRAMES:
                    break
            finally:
                _release(sample)
        if live.stopped == "stopped":
            raise CameraError("cancelled", "Qronos was paused or stopped during the capture")
        if kept is None:
            raise CameraError("camera_timeout", "the camera delivered no frame in time")
        return Frame(width, height, kept, delivered)
    finally:
        _release(current)
        _release(wanted)
        _release(reader)
        _release(attributes)
        with live.lock:
            live.source = None
        if source:
            _call(source, "IMFMediaSource", "Shutdown")
        if activate is not None:
            _call(activate, "IMFActivate", "ShutdownObject")
            _release(activate)
        _release(source)


def _capture_with(api: MfApi, symbolic_link: str, should_stop: Callable[[], bool],
                  timeout: float = CAPTURE_TIMEOUT_S) -> Frame:
    """ONE frame from the ONE camera, in a worker; ``should_stop`` (Pause/Stop/shutdown) and the deadline stop it."""
    live = _Live()
    result: dict[str, Any] = {}
    deadline = time.monotonic() + timeout

    def worker() -> None:
        try:
            result["frame"] = _session(api, lambda: _read_one(api, symbolic_link, live, deadline))
        except CameraError as error:
            result["error"] = error
        except Exception as error:  # noqa: BLE001 - typed, never a crash of the hub
            result["error"] = CameraError("camera_failed", type(error).__name__)

    thread = threading.Thread(target=worker, name="qronos-camera-capture", daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(0.05)
        if not thread.is_alive():
            break
        if should_stop():
            live.stop("stopped")
            break
        if time.monotonic() > deadline:
            live.stop("timeout")
            break
    thread.join(JOIN_GRACE_S)
    if thread.is_alive():
        raise CameraError("camera_stuck", "the camera did not close in time; nothing was written")
    if live.stopped == "stopped" or should_stop():
        raise CameraError("cancelled", "Qronos was paused or stopped during the capture; nothing was written")
    if "error" in result:
        raise result["error"]
    return result["frame"]


class WinCamera(MfApi):
    """This PC's real Media Foundation cameras; constructed only for a request (blocked in tests)."""

    def list_devices(self) -> list[CameraDevice]:
        return _list_with(self)

    def capture(self, symbolic_link: str, should_stop: Callable[[], bool]) -> Frame:
        return _capture_with(self, symbolic_link, should_stop)


def default_camera() -> WinCamera:
    return WinCamera()
