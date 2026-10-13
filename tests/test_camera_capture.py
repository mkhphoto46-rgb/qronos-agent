"""One still frame from a camera through Media Foundation (core/camera_capture.py): the COM plumbing against FAKE
vtables (real ole32 task memory only; no Media Foundation, no camera, no device query), synthetic BGRX frames in every
memory layout, the error mapping, Pause/Stop and the deadline, and the one new file. The real WinCamera is blocked by
the live-device boundary; one read-only listing test is live-only and never captures."""
from __future__ import annotations

import ast
import ctypes
import io
import os
import re
import threading
from ctypes import wintypes
from pathlib import Path

import pytest
from PIL import Image

from core import camera_capture as cam
from core.camera_capture import CameraError, Frame
from core.wia_imaging import check_destination
from tests.live_device import requires_live_device

needs_windows = pytest.mark.skipif(os.name != "nt", reason="the COM calling convention is Windows only")
PROTOTYPE = ctypes.WINFUNCTYPE
HRESULT = ctypes.c_long
GUIDP, PVOIDP = ctypes.c_void_p, ctypes.c_void_p
LINK = r"\\?\usb#vid_046d&pid_0825&mi_00#7&1a2b3c&0&0000#{e5323777-f976-4f5b-9b55-b94699c46e44}\global"
OTHER = r"\\?\usb#vid_1234&pid_5678&mi_00#7&9&0&0000#{e5323777-f976-4f5b-9b55-b94699c46e44}\global"
W, H = 8, 6


def guid_text(pointer) -> str:
    import uuid

    return str(uuid.UUID(bytes_le=ctypes.string_at(pointer, 16)))


def bgrx(color) -> bytes:
    b, g, r = color
    return bytes([b, g, r, 0]) * (W * H)


def rows_of(color):
    """A frame whose rows differ (so an upside-down copy is caught): row y has red = color red + y."""
    b, g, r = color
    return [bytes([b, g, (r + y) % 256, 0]) * W for y in range(H)]


class FakeCom:
    def __init__(self, name: str, methods: dict, log: list, query=None) -> None:
        self.name, self.log, self.released, self._keep = name, log, 0, []
        self.vtable = (ctypes.c_void_p * (max([3, *methods]) + 1))()

        def query_interface(this, iid, out):
            target = query(guid_text(iid)) if query else None
            ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = target.pointer if target else None
            return 0 if target else -2147467262  # E_NOINTERFACE

        for slot, function in ((0, PROTOTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(query_interface)),
                               (1, PROTOTYPE(ctypes.c_ulong, ctypes.c_void_p)(lambda this: 1)),
                               (2, PROTOTYPE(ctypes.c_ulong, ctypes.c_void_p)(self._release))):
            self._keep.append(function)
            self.vtable[slot] = ctypes.cast(function, ctypes.c_void_p).value
        for slot, (argtypes, body) in methods.items():
            function = PROTOTYPE(HRESULT, ctypes.c_void_p, *argtypes)(body)
            self._keep.append(function)
            self.vtable[slot] = ctypes.cast(function, ctypes.c_void_p).value
        self.holder = ctypes.c_void_p(ctypes.addressof(self.vtable))
        self.pointer = ctypes.addressof(self.holder)

    def _release(self, this):
        self.released += 1
        self.log.append(("Release", self.name))
        return 0


def put(out, value, kind=ctypes.c_void_p):
    ctypes.cast(out, ctypes.POINTER(kind))[0] = value


class FakeMf(cam.MfApi):
    """The whole Media Foundation surface as fakes; only ole32 task memory is real (no MF DLL is loaded)."""

    def __init__(self, cameras=((LINK, "USB Camera"),), reads=None, layout="2d", stride=None, activate_hr=0,
                 set_type_hr=0, frame_size=(W, H), pitch=None) -> None:
        self.log: list = []
        self.objects: list[FakeCom] = []
        self.freed = 0
        self._ole = ctypes.WinDLL("ole32")
        self._ole.CoTaskMemAlloc.argtypes, self._ole.CoTaskMemAlloc.restype = [ctypes.c_size_t], ctypes.c_void_p
        self._ole.CoTaskMemFree.argtypes, self._ole.CoTaskMemFree.restype = [ctypes.c_void_p], None
        self.cameras, self.layout, self.stride, self.pitch = list(cameras), layout, stride, pitch
        self.activate_hr, self.set_type_hr, self.frame_size = activate_hr, set_type_hr, frame_size
        self.reads = list(reads if reads is not None else [("frame", (10, 20, 100 + i)) for i in range(8)])
        self.shutdown_event = threading.Event()
        self.memory: list = []

    # -- the SDK functions
    def co_initialize(self):
        self.log.append(("CoInitializeEx",))
        return 0

    def co_uninitialize(self):
        self.log.append(("CoUninitialize",))

    def task_free(self, pointer):
        self.freed += 1
        self._ole.CoTaskMemFree(pointer)

    def startup(self):
        self.log.append(("MFStartup",))
        return 0

    def shutdown(self):
        self.log.append(("MFShutdown",))
        return 0

    def make(self, name, methods, query=None) -> FakeCom:
        obj = FakeCom(name, methods, self.log, query)
        self.objects.append(obj)
        return obj

    def attributes(self, name):
        def set_guid(this, key, value):
            self.log.append(("SetGUID", name, guid_text(key), guid_text(value)))
            return 0

        def set_uint32(this, key, value):
            self.log.append(("SetUINT32", name, guid_text(key), value))
            return 0

        return self.make(name, {24: ((GUIDP, GUIDP), set_guid), 21: ((GUIDP, ctypes.c_uint32), set_uint32)})

    def create_attributes(self, out, count):
        put(out, self.attributes(f"attributes{len(self.objects)}").pointer)
        return 0

    def create_media_type(self, out):
        put(out, self.attributes("wanted").pointer)
        return 0

    def enum_device_sources(self, attributes, out_array, out_count):
        self.log.append(("MFEnumDeviceSources",))
        array = self._ole.CoTaskMemAlloc(ctypes.sizeof(ctypes.c_void_p) * max(1, len(self.cameras)))
        for index, (link, name) in enumerate(self.cameras):
            ctypes.cast(array, ctypes.POINTER(ctypes.c_void_p))[index] = self.activate(link, name).pointer
        put(out_array, array)
        put(out_count, len(self.cameras), ctypes.c_uint32)
        return 0

    def activate(self, link, name):
        def get_string(this, key, out, length):
            text = {cam.GUIDS["MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK"]: link,
                    cam.GUIDS["MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME"]: name}[guid_text(key)]
            memory = self._ole.CoTaskMemAlloc(2 * (len(text) + 1))
            ctypes.memmove(memory, ctypes.create_unicode_buffer(text), 2 * (len(text) + 1))
            put(out, memory)
            put(length, len(text), ctypes.c_uint32)
            return 0

        def activate_object(this, iid, out):
            self.log.append(("ActivateObject", link, guid_text(iid)))
            if self.activate_hr:
                return self.activate_hr
            put(out, self.source().pointer)
            return 0

        def shutdown_object(this):
            self.log.append(("ShutdownObject", link))
            return 0

        return self.make(f"activate:{name}", {13: ((GUIDP, PVOIDP, PVOIDP), get_string),
                                              33: ((GUIDP, PVOIDP), activate_object), 34: ((), shutdown_object)})

    def source(self):
        def shutdown(this):
            self.log.append(("Source.Shutdown", threading.current_thread().name))
            self.shutdown_event.set()
            return 0

        return self.make("source", {12: ((), shutdown)})

    def reader_from_source(self, source, attributes, out):
        def select(this, stream, selected):
            self.log.append(("SetStreamSelection", stream, selected))
            return 0

        def set_type(this, stream, reserved, media_type):
            self.log.append(("SetCurrentMediaType", stream))
            return self.set_type_hr

        def get_type(this, stream, out_type):
            put(out_type, self.current_type().pointer)
            return 0

        def read_sample(this, stream, flags_in, actual, flags, stamp, sample):
            self.log.append(("ReadSample",))
            put(sample, None)
            if self.shutdown_event.is_set():
                return cam.HRESULTS["MF_E_SHUTDOWN"]
            step = self.reads.pop(0) if self.reads else ("block",)
            if step[0] == "block":  # a camera that delivers nothing until the source is shut down
                self.shutdown_event.wait(10)
                return cam.HRESULTS["MF_E_SHUTDOWN"]
            if step[0] == "hr":
                return step[1]
            if step[0] == "flags":
                put(flags, step[1], wintypes.DWORD)
                return 0
            if step[0] == "tick":
                put(flags, cam.READER["MF_SOURCE_READERF_STREAMTICK"], wintypes.DWORD)
                return 0
            put(flags, 0, wintypes.DWORD)
            put(sample, self.sample(step[1]).pointer)
            return 0

        reader = self.make("reader", {4: ((wintypes.DWORD, wintypes.BOOL), select),
                                      6: ((wintypes.DWORD, PVOIDP), get_type),
                                      7: ((wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p), set_type),
                                      9: ((wintypes.DWORD, wintypes.DWORD, PVOIDP, PVOIDP, PVOIDP, PVOIDP),
                                          read_sample)})
        put(out, reader.pointer)
        return 0

    def current_type(self):
        def get_uint64(this, key, out):
            width, height = self.frame_size
            put(out, (width << 32) | height, ctypes.c_uint64)
            return 0

        def get_uint32(this, key, out):
            if self.stride is None:
                return -1072875802  # MF_E_ATTRIBUTENOTFOUND
            put(out, self.stride & 0xFFFFFFFF, ctypes.c_uint32)
            return 0

        return self.make("current", {7: ((GUIDP, PVOIDP), get_uint32), 8: ((GUIDP, PVOIDP), get_uint64)})

    def sample(self, color):
        rows = rows_of(color)
        top_down = b"".join(rows)
        if self.layout == "2d-bottom-up" or (self.layout == "plain" and (self.stride or 0) < 0):
            memory = ctypes.create_string_buffer(b"".join(reversed(rows)))
        else:
            memory = ctypes.create_string_buffer(top_down)
        self.memory.append(memory)
        base = ctypes.addressof(memory)
        row = 4 * W

        def lock2d(this, scan0, pitch):
            if self.layout == "2d-bottom-up":
                put(scan0, base + (H - 1) * row)
                put(pitch, -row, ctypes.c_long)
            else:
                put(scan0, base)
                put(pitch, self.pitch if self.pitch is not None else row, ctypes.c_long)
            return 0

        two_d = self.make("2dbuffer", {3: ((PVOIDP, PVOIDP), lock2d), 4: ((), lambda this: 0)}) \
            if self.layout.startswith("2d") else None

        def lock(this, data, maximum, current):
            put(data, base)
            put(maximum, len(top_down), wintypes.DWORD)
            put(current, len(top_down), wintypes.DWORD)
            return 0

        query = (lambda iid: two_d if iid == cam.IIDS["IMF2DBuffer"] else None) if self.layout.startswith("2d") else None
        buffer = self.make("buffer", {3: ((PVOIDP, PVOIDP, PVOIDP), lock), 4: ((), lambda this: 0)}, query)

        def contiguous(this, out):
            put(out, buffer.pointer)
            return 0

        return self.make("sample", {41: ((PVOIDP,), contiguous)})


def entries(fake, name):
    return [entry for entry in fake.log if entry[0] == name]


def all_released(fake):
    """Every object handed out was released exactly once (activates of unlisted cameras too)."""
    return all(obj.released == 1 for obj in fake.objects)


# ------------------------------------------------------------------ listing

@needs_windows
def test_the_listing_asks_windows_for_video_capture_devices_and_frees_everything():
    fake = FakeMf(cameras=[(LINK, "USB Camera"), (OTHER, "Integrated Camera")])
    devices = cam._list_with(fake)
    assert [(d.name, d.symbolic_link) for d in devices] == [("USB Camera", LINK), ("Integrated Camera", OTHER)]
    assert devices[0].device_id == cam.CameraDevice(LINK.upper(), "x").device_id  # identity = the symbolic link
    assert re.fullmatch(r"cam-[0-9a-f]{20}", devices[0].device_id) and devices[0].device_id != devices[1].device_id
    (_, _, key, value), = entries(fake, "SetGUID")
    assert key == cam.GUIDS["MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE"]
    assert value == cam.GUIDS["MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID"]
    assert fake.freed == 2 * 2 + 1  # two strings per camera + the array
    assert all_released(fake) and not entries(fake, "ActivateObject")  # listing never turns a camera on
    assert [e[0] for e in fake.log if e[0] in ("CoInitializeEx", "MFStartup", "MFShutdown", "CoUninitialize")] == \
        ["CoInitializeEx", "MFStartup", "MFShutdown", "CoUninitialize"]


# ------------------------------------------------------------------ one capture

@needs_windows
@pytest.mark.parametrize("layout, stride", [("2d", None), ("2d-bottom-up", None), ("plain", None),
                                            ("plain", -4 * W), ("plain", 4 * W)])
def test_one_capture_keeps_the_frame_after_the_warm_up_top_down_in_every_memory_layout(layout, stride):
    fake = FakeMf(cameras=[(OTHER, "Other"), (LINK, "USB Camera")], layout=layout, stride=stride,
                  reads=[("tick",), *[("frame", (10, 20, 100 + i)) for i in range(8)]])
    frame = cam._capture_with(fake, LINK, should_stop=lambda: False, timeout=5)
    assert (frame.width, frame.height, frame.frames_read) == (W, H, cam.WARMUP_FRAMES + 1)
    assert frame.bgrx == b"".join(rows_of((10, 20, 100 + cam.WARMUP_FRAMES)))  # the 5th frame, top row first
    assert [e[1] for e in entries(fake, "ActivateObject")] == [LINK]  # only the approved camera is turned on
    assert entries(fake, "ActivateObject")[0][2] == cam.IIDS["IMFMediaSource"]
    assert [(e[1], e[2]) for e in entries(fake, "SetStreamSelection")] == [
        (cam.READER["MF_SOURCE_READER_ALL_STREAMS"], 0), (cam.READER["MF_SOURCE_READER_FIRST_VIDEO_STREAM"], 1)]
    assert [(e[2], e[3]) for e in entries(fake, "SetUINT32")] == [
        (cam.GUIDS["MF_SOURCE_READER_ENABLE_ADVANCED_VIDEO_PROCESSING"], 1)]  # Windows' own converter to RGB32
    wanted = {(e[2], e[3]) for e in entries(fake, "SetGUID") if e[1] == "wanted"}
    assert wanted == {(cam.GUIDS["MF_MT_MAJOR_TYPE"], cam.GUIDS["MFMediaType_Video"]),
                      (cam.GUIDS["MF_MT_SUBTYPE"], cam.GUIDS["MFVideoFormat_RGB32"])}
    assert len(entries(fake, "Source.Shutdown")) == 1 and entries(fake, "ShutdownObject") == [("ShutdownObject", LINK)]
    assert all_released(fake) and ("MFShutdown",) in fake.log and fake.log[-1] == ("CoUninitialize",)


@needs_windows
@pytest.mark.parametrize("fake, code", [
    (lambda: FakeMf(activate_hr=cam.HRESULTS["E_ACCESSDENIED"] - (1 << 32)), "camera_privacy_denied"),
    (lambda: FakeMf(set_type_hr=cam.HRESULTS["MF_E_INVALIDMEDIATYPE"] - (1 << 32)), "camera_format_unsupported"),
    (lambda: FakeMf(set_type_hr=cam.HRESULTS["MF_E_TOPO_CODEC_NOT_FOUND"] - (1 << 32)), "camera_format_unsupported"),
    (lambda: FakeMf(reads=[("hr", cam.HRESULTS["MF_E_VIDEO_RECORDING_DEVICE_PREEMPTED"] - (1 << 32))]), "camera_busy"),
    (lambda: FakeMf(reads=[("hr", cam.HRESULTS["MF_E_HW_MFT_FAILED_START_STREAMING"] - (1 << 32))]), "camera_busy"),
    (lambda: FakeMf(reads=[("hr", cam.HRESULTS["MF_E_VIDEO_RECORDING_DEVICE_INVALIDATED"] - (1 << 32))]), "camera_gone"),
    (lambda: FakeMf(reads=[("flags", cam.READER["MF_SOURCE_READERF_ENDOFSTREAM"])]), "camera_gone"),
    (lambda: FakeMf(reads=[("flags", cam.READER["MF_SOURCE_READERF_ERROR"])]), "camera_failed"),
    (lambda: FakeMf(cameras=[(OTHER, "Other")]), "camera_gone"),
    (lambda: FakeMf(frame_size=(cam.MAX_WIDTH + 1, 10)), "camera_format_unsupported"),
    (lambda: FakeMf(frame_size=(0, 10)), "camera_format_unsupported"),
    (lambda: FakeMf(pitch=4 * W - 4), "camera_bad_frame"),
    (lambda: FakeMf(layout="plain", stride=4 * W - 4), "camera_bad_frame"),
])
def test_every_refusal_is_typed_and_everything_is_closed(fake, code):
    fake = fake()
    with pytest.raises(CameraError) as refused:
        cam._capture_with(fake, LINK, should_stop=lambda: False, timeout=5)
    assert refused.value.code == code
    assert all_released(fake) and fake.log[-1] == ("CoUninitialize",)
    if entries(fake, "ActivateObject") and not fake.activate_hr:
        assert len(entries(fake, "Source.Shutdown")) >= 1 and entries(fake, "ShutdownObject")


@needs_windows
def test_pause_or_stop_during_the_capture_shuts_the_camera_down_and_keeps_nothing():
    fake = FakeMf(reads=[("frame", (1, 2, 3)), ("block",)])
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return len(entries(fake, "ReadSample")) > 0 or calls["n"] > 3  # Pause lands while the camera streams

    with pytest.raises(CameraError) as refused:
        cam._capture_with(fake, LINK, should_stop=should_stop, timeout=5)
    assert refused.value.code == "cancelled"
    stops = entries(fake, "Source.Shutdown")
    assert stops and stops[0][1] != "qronos-camera-capture"  # the WAITING thread stopped the live source
    assert all_released(fake) and fake.log[-1] == ("CoUninitialize",)


@needs_windows
def test_the_deadline_stops_a_silent_camera_and_keeps_a_frame_only_if_one_arrived():
    silent = FakeMf(reads=[("block",)])
    with pytest.raises(CameraError) as refused:
        cam._capture_with(silent, LINK, should_stop=lambda: False, timeout=0.3)
    assert refused.value.code == "camera_timeout" and entries(silent, "Source.Shutdown") and all_released(silent)
    slow = FakeMf(reads=[("frame", (5, 6, 7)), ("block",)])
    frame = cam._capture_with(slow, LINK, should_stop=lambda: False, timeout=0.3)
    assert frame.frames_read == 1 and frame.bgrx == b"".join(rows_of((5, 6, 7))) and all_released(slow)


# ------------------------------------------------------------------ the one new file

def save_frame(frame, destination):
    return cam.save_frame(frame, destination, check_destination)


def test_the_kept_frame_becomes_exactly_one_new_png_or_jpeg_and_nothing_is_overwritten(tmp_path):
    frame = Frame(W, H, b"".join(rows_of((10, 20, 200))), 5)
    saved = save_frame(frame, tmp_path / "still.png")
    with Image.open(tmp_path / "still.png") as written:
        assert written.format == "PNG" and written.size == (W, H)
        assert written.getpixel((0, 0)) == (200, 20, 10) and written.getpixel((0, H - 1)) == (200 + H - 1, 20, 10)
        assert not written.getexif() and not written.info.get("exif")
    data = (tmp_path / "still.png").read_bytes()
    assert saved["bytes"] == len(data) and saved["sha256"] == __import__("hashlib").sha256(data).hexdigest()
    assert (saved["width"], saved["height"], saved["format"], saved["framesRead"], saved["dark"]) == (W, H, "PNG", 5, False)
    assert save_frame(frame, tmp_path / "still.jpg")["format"] == "JPEG"
    with pytest.raises(Exception) as refused:
        save_frame(frame, tmp_path / "still.png")
    assert getattr(refused.value, "code", "") == "destination_exists" and (tmp_path / "still.png").read_bytes() == data
    assert save_frame(Frame(W, H, bytes(4 * W * H), 5), tmp_path / "dark.png")["dark"] is True  # e.g. a covered lens
    with pytest.raises(CameraError) as bad:
        save_frame(Frame(W, H, b"\x00" * 7, 5), tmp_path / "short.png")
    assert bad.value.code == "camera_bad_frame" and not (tmp_path / "short.png").exists()
    for wrong in (tmp_path / "a.gif", Path("relative.png"), tmp_path / "missing" / "a.png"):
        with pytest.raises(Exception) as refused:
            save_frame(frame, wrong)
        assert getattr(refused.value, "code", "") == "bad_destination"


# ------------------------------------------------------------------ the SDK, the boundary and the source

SDK = Path("C:/Program Files (x86)/Windows Kits/10/Include/10.0.26100.0/um")


def test_every_slot_guid_and_constant_follows_the_sdk_headers():
    if not (SDK / "mfreadwrite.h").is_file():
        pytest.skip("the Windows SDK headers are not on this machine")
    text = {name: (SDK / name).read_text(encoding="utf-8", errors="replace")
            for name in ("mfobjects.h", "mfidl.h", "mfreadwrite.h", "mfapi.h", "mferror.h")}
    where = {"IMFAttributes": "mfobjects.h", "IMFActivate": "mfobjects.h", "IMFSample": "mfobjects.h",
             "IMFMediaBuffer": "mfobjects.h", "IMF2DBuffer": "mfobjects.h", "IMFMediaEventGenerator": "mfobjects.h",
             "IMFMediaSource": "mfidl.h", "IMFSourceReader": "mfreadwrite.h"}

    def methods(interface):
        match = re.search(r'MIDL_INTERFACE\("([0-9A-Fa-f-]+)"\)\s*\n\s*' + interface + r"\s*:\s*public\s+(\w+)",
                          text[where[interface]])
        body = text[where[interface]][match.end():]
        names = re.findall(r"STDMETHODCALLTYPE (\w+)\(", body[:body.index("};")])
        base = match.group(2)
        return match.group(1).lower(), ([] if base == "IUnknown" else methods(base)[1]) + names

    for (interface, method), slot in cam.SLOTS.items():
        assert methods(interface)[1].index(method) + 3 == slot, (interface, method)
    for name, iid in cam.IIDS.items():
        assert methods(name)[0] == iid
    everything = "\n".join(text.values())
    for name, value in cam.GUIDS.items():
        if name == "MFVideoFormat_RGB32":
            assert re.search(r"DEFINE_MEDIATYPE_GUID\(\s*MFVideoFormat_RGB32\s*,\s*D3DFMT_X8R8G8B8\s*\)", everything)
            assert value == "%08x-0000-0010-8000-00aa00389b71" % 22  # D3DFMT_X8R8G8B8
            continue
        parts = re.search(r"(?:EXTERN_GUID|DEFINE_GUID)\(\s*" + name + r"\s*,\s*([^)]*)\)", everything).group(1)
        numbers = [int(p.strip(), 16) for p in parts.split(",")]
        assert value == "%08x-%04x-%04x-%02x%02x-%s" % (*numbers[:5], "".join("%02x" % b for b in numbers[5:])), name
    for name, value in cam.READER.items():
        assert int(re.search(r"\b" + name + r"\s*=\s*(0x[0-9a-fA-F]+|\d+)", everything).group(1), 0) == value, name
    for name, value in cam.HRESULTS.items():
        if name == "E_ACCESSDENIED":
            continue  # winerror.h: _HRESULT_TYPEDEF_(0x80070005L)
        assert int(re.search(r"#define\s+" + name + r"\s+_HRESULT_TYPEDEF_\((0x[0-9A-Fa-f]+)L?\)",
                             text["mferror.h"]).group(1), 16) == value, name
    assert re.search(r"#define\s+MFSTARTUP_LITE\s+\(MFSTARTUP_NOSOCKET\)", everything) and cam.MFSTARTUP_LITE == 1
    assert cam.MF_VERSION == 0x00020070


def test_the_real_camera_layer_is_blocked_and_has_no_process_network_shell_or_model_route():
    assert getattr(cam.WinCamera, "live_device_stand_in", None)
    source = Path(cam.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "ctypes", "hashlib", "io", "os", "threading", "time", "uuid", "dataclasses",
                        "pathlib", "typing", "PIL"}, imported  # no other Qronos module, no WIA backend
    assert set(re.findall(r'WinDLL\("([a-z0-9]+)"', source)) == {"ole32", "mfplat", "mf", "mfreadwrite"}
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body
                  and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    code_words = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)] + \
        [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)] + \
        [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
         and id(n) not in docstrings]
    assert "windll" not in code_words  # the implicit ctypes.windll loader; only the explicit WinDLL set above
    for banned in ("shellexecute", "subprocess", "socket", "urllib", "http", "sinkwriter", "fromurl",
                   "audio", "audcap", "popen"):
        assert not [w for w in code_words if banned in w.lower()], banned
    assert "self._plat.MFStartup(MF_VERSION, MFSTARTUP_LITE)" in source  # no socket library
    module_calls = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                                                                        ast.ClassDef, ast.Assign, ast.AnnAssign))
                    and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    assert module_calls == []  # nothing runs at import


@requires_live_device
def test_windows_media_foundation_lists_cameras_read_only():
    """Live only (QRONOS_RUN_LIVE_DEVICE_TESTS=1): lists this PC's cameras; never turns one on."""
    devices = cam.WinCamera().list_devices()
    assert all(device.device_id.startswith("cam-") for device in devices)
