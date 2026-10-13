"""Scanners through WIA 2.0 (core/wia_imaging.py): the device-type rule, the exact new image file, the staging
folder, and the COM plumbing against FAKE vtables (real oleaut32 BSTR/PROPVARIANT memory only; no WIA service, no
device). The real WinWia is blocked by the live-device boundary; one read-only listing test is live-only."""
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

from core import wia_imaging as wia
from core.wia_imaging import ImagingError, check_destination, finalize_scan, kind_of, remove_staging
from tests.live_device import requires_live_device

needs_windows = pytest.mark.skipif(os.name != "nt", reason="the COM calling convention and junctions are Windows only")


def test_the_device_type_is_the_high_word_and_only_scanners_are_scanners():
    assert kind_of((1 << 16) | 7) == "scanner" and kind_of(2 << 16) == "camera" and kind_of(3 << 16) == "video"
    assert kind_of(0) == "other" and kind_of(9 << 16) == "other"
    one = wia.ImagingDevice("{6BDD1FC6-810F-11D0-BEC7-08002BE2092F}\\0001", "S", "V", "D", "scanner")
    assert one.device_id == wia.ImagingDevice(one.wia_id.lower(), "x", "y", "z", "scanner").device_id
    assert re.fullmatch(r"img-[0-9a-f]{20}", one.device_id)


def _image(path: Path, fmt: str, size=(40, 30), mode="RGB", **options) -> Path:
    Image.new(mode, size, (200, 30, 30) if mode == "RGB" else 128).save(path, format=fmt, **options)
    return path


def test_a_destination_must_be_an_exact_new_png_or_jpeg_in_a_real_folder(tmp_path):
    assert check_destination(tmp_path / "a.png") == "PNG" and check_destination(tmp_path / "b.JPG") == "JPEG"
    for bad, code in ((Path("rel.png"), "bad_destination"), (tmp_path / "a.gif", "bad_destination"),
                      (tmp_path / "missing" / "a.png", "bad_destination")):
        with pytest.raises(ImagingError) as refused:
            check_destination(bad)
        assert refused.value.code == code
    (tmp_path / "exists.png").write_bytes(b"x")
    with pytest.raises(ImagingError) as refused:
        check_destination(tmp_path / "exists.png")
    assert refused.value.code == "destination_exists"


@needs_windows
def test_a_destination_inside_a_junction_is_refused(tmp_path):
    import _winapi

    real = tmp_path / "real"
    real.mkdir()
    _winapi.CreateJunction(str(real), str(tmp_path / "link"))
    with pytest.raises(ImagingError) as refused:
        check_destination(tmp_path / "link" / "scan.png")
    assert refused.value.code == "bad_destination"


def _staged(tmp_path, maker) -> tuple[Path, list[str]]:
    staging = tmp_path / "qronos-scan-test"
    staging.mkdir()
    return staging, [str(maker(staging))]


def test_one_staged_image_becomes_the_exact_new_file_reencoded_without_metadata(tmp_path):
    exif = Image.Exif()
    exif[0x010E] = "secret description"  # ImageDescription
    staging, staged = _staged(tmp_path, lambda d: _image(d / "qronos-scan.jpg", "JPEG", exif=exif.tobytes()))
    destination = tmp_path / "out" / "page.png"
    destination.parent.mkdir()
    saved = finalize_scan(staged, staging, destination)
    assert saved["format"] == "PNG" and saved["scannedFormat"] == "JPEG" and (saved["width"], saved["height"]) == (40, 30)
    data = destination.read_bytes()
    assert saved["bytes"] == len(data) and saved["sha256"] == __import__("hashlib").sha256(data).hexdigest()
    with Image.open(io.BytesIO(data)) as written:
        assert written.format == "PNG" and not written.getexif()  # metadata not copied
    second = tmp_path / "second"
    second.mkdir()
    staging2, staged2 = _staged(second, lambda d: _image(d / "qronos-scan.bmp", "BMP", mode="L"))
    jpg = finalize_scan(staged2, staging2, tmp_path / "out" / "page.jpg")
    assert jpg["format"] == "JPEG" and jpg["scannedFormat"] == "BMP"


@pytest.mark.parametrize("case, code", [
    ("two_files", "not_one_image"), ("reported_outside", "not_one_image"), ("missing", "not_one_image"),
    ("not_image", "not_an_image"), ("truncated", "not_an_image"), ("empty", "empty_image"), ("bomb", "too_large"),
])
def test_anything_but_one_valid_staged_image_writes_nothing(tmp_path, monkeypatch, case, code):
    staging = tmp_path / "qronos-scan-case"
    staging.mkdir()
    good = _image(staging / "qronos-scan.png", "PNG")
    staged = [str(good)]
    if case == "two_files":
        _image(staging / "second.png", "PNG")
    elif case == "reported_outside":
        staged = [str(_image(tmp_path / "elsewhere.png", "PNG"))]
    elif case == "missing":
        staged = [str(staging / "nope.png")]
    elif case == "not_image":
        good.write_bytes(b"MZ\x90\x00 not an image")
    elif case == "truncated":
        good.write_bytes(good.read_bytes()[:60])
    elif case == "empty":
        good.write_bytes(b"")
    elif case == "bomb":
        monkeypatch.setattr(wia, "MAX_PIXELS", 100)  # 40 x 30 = 1,200 pixels is "too many" here
    destination = tmp_path / "page.png"
    with pytest.raises(ImagingError) as refused:
        finalize_scan(staged, staging, destination)
    assert refused.value.code == code and not destination.exists()


def test_staging_is_removed_only_when_it_is_ours_and_holds_no_link(tmp_path):
    ours = wia.new_staging()
    (ours / "qronos-scan.png").write_bytes(b"x")
    remove_staging(ours)
    assert not ours.exists()
    other = tmp_path / "not-ours"
    other.mkdir()
    remove_staging(other)
    assert other.exists()


# ------------------------------------------------------------------ COM plumbing against fake vtables

PROTOTYPE = ctypes.WINFUNCTYPE
HRESULT = ctypes.c_long


class FakeCom:
    def __init__(self, log: list, methods: dict) -> None:
        self.released, self._keep = 0, []
        self.vtable = (ctypes.c_void_p * (max([3, *methods]) + 1))()
        for slot, function in ((0, PROTOTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(lambda *_: -1)),
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
        return 0


class FakeWia(wia.WiaComApi):
    """The real oleaut32/ole32 memory functions; the WIA manager is a fake COM object (no service, no device)."""

    def __init__(self, devices, scan) -> None:
        super().__init__()
        self.log, self.objects, self.devices, self.scan = [], [], devices, scan
        oleaut, ole = self._oleaut, ctypes.WinDLL("ole32")
        ole.CoTaskMemAlloc.argtypes, ole.CoTaskMemAlloc.restype = [ctypes.c_size_t], ctypes.c_void_p
        cursor = {"next": 0}

        def write(out, obj):
            ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = obj.pointer

        def storage_for(values):
            def read_multiple(this, count, specs, out):
                spec = ctypes.cast(specs, ctypes.POINTER(wia._PROPSPEC))
                var = ctypes.cast(out, ctypes.POINTER(wia._PROPVARIANT))
                for index in range(count):
                    value = values.get(int(spec[index].propid))
                    if isinstance(value, str):
                        var[index].vt, var[index].value = wia.VT_BSTR, oleaut.SysAllocString(value)
                    elif isinstance(value, int):
                        var[index].vt, var[index].value = wia.VT_I4, value
                self.log.append(("ReadMultiple", [int(spec[i].propid) for i in range(count)]))
                return 0
            return self.make({3: ((wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p), read_multiple)})

        def enum_next(this, celt, out, fetched):
            if cursor["next"] >= len(self.devices):
                ctypes.cast(fetched, ctypes.POINTER(wintypes.ULONG))[0] = 0
                return 1  # S_FALSE: no more
            write(out, storage_for(self.devices[cursor["next"]]))
            cursor["next"] += 1
            ctypes.cast(fetched, ctypes.POINTER(wintypes.ULONG))[0] = 1
            return 0

        enum = self.make({3: ((wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p), enum_next)})

        def enum_devices(this, flags, out):
            self.log.append(("EnumDeviceInfo", flags))
            write(out, enum)
            return 0

        def get_image_dlg(this, flags, device, hwnd, folder, name, count, paths, item):
            folder_text, device_text = ctypes.wstring_at(folder), ctypes.wstring_at(device)
            self.log.append(("GetImageDlg", flags, device_text, hwnd, folder_text, ctypes.wstring_at(name)))
            result, files = self.scan(Path(folder_text))
            array = ole.CoTaskMemAlloc(ctypes.sizeof(ctypes.c_void_p) * max(1, len(files)))
            for index, path in enumerate(files):
                ctypes.cast(array, ctypes.POINTER(ctypes.c_void_p))[index] = oleaut.SysAllocString(str(path))
            ctypes.cast(count, ctypes.POINTER(ctypes.c_long))[0] = len(files)
            ctypes.cast(paths, ctypes.POINTER(ctypes.c_void_p))[0] = array
            return result

        self.manager = self.make({3: ((ctypes.c_long, ctypes.c_void_p), enum_devices),
                                  10: ((ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p),
                                       get_image_dlg)})

    def make(self, methods) -> FakeCom:
        obj = FakeCom(self.log, methods)
        self.objects.append(obj)
        return obj

    def _manager(self):
        return ctypes.c_void_p(self.manager.pointer)


DEVICES = [
    {2: "{6BDD1FC6-810F-11D0-BEC7-08002BE2092F}\\0000", 5: (1 << 16) | 3, 7: "Office Scanner", 3: "Acme", 4: "A4"},
    {2: "{6BDD1FC6-810F-11D0-BEC7-08002BE2092F}\\0001", 5: 2 << 16, 7: "Old Camera", 3: "Cam Co", 4: "WIA 1"},
]


@needs_windows
def test_the_listing_reads_each_devices_wia_properties_and_frees_them():
    fake = FakeWia(DEVICES, scan=lambda folder: (0, []))
    devices = wia._list_with(fake)
    assert [(d.name, d.kind, d.vendor) for d in devices] == [("Office Scanner", "scanner", "Acme"),
                                                             ("Old Camera", "camera", "Cam Co")]
    assert ("EnumDeviceInfo", wia.WIA_DEVINFO_ENUM_LOCAL) in fake.log
    assert ("ReadMultiple", [2, 5, 7, 3, 4]) in fake.log
    assert all(obj.released <= 1 for obj in fake.objects) and fake.manager.released == 1


@needs_windows
def test_one_scan_is_windows_dialog_into_the_staging_folder_and_a_cancel_keeps_nothing(tmp_path):
    def scanned(folder):
        return 0, [_image(folder / "qronos-scan.bmp", "BMP")]

    fake = FakeWia(DEVICES, scan=scanned)
    files = wia._acquire_with(fake, DEVICES[0][2], tmp_path)
    (_, flags, device, hwnd, folder, name), = [entry for entry in fake.log if entry[0] == "GetImageDlg"]
    assert flags == wia.WIA_DEVICE_DIALOG_USE_COMMON_UI and device == DEVICES[0][2] and not hwnd
    assert folder == str(tmp_path) and name == "qronos-scan" and files == [str(tmp_path / "qronos-scan.bmp")]
    # Documented: on S_FALSE (cancel) pages scanned before the cancel may still be returned; the array is freed and
    # nothing is kept (the staging folder they sit in is removed by the caller).
    partial = tmp_path / "partial"
    partial.mkdir()
    cancelled = FakeWia(DEVICES, scan=lambda folder: (wia.S_FALSE, [_image(partial / "page1.bmp", "BMP")]))
    assert wia._acquire_with(cancelled, DEVICES[0][2], partial) == []
    failing = FakeWia(DEVICES, scan=lambda folder: (-2147024891, []))  # E_ACCESSDENIED
    with pytest.raises(ImagingError) as refused:
        wia._acquire_with(failing, DEVICES[0][2], tmp_path)
    assert refused.value.code == "scan_failed"


def test_the_vtable_slots_follow_the_sdk_header_order():
    header = Path("C:/Program Files (x86)/Windows Kits/10/Include/10.0.26100.0/um/wia_lh.h")
    if not header.is_file():
        pytest.skip("the Windows SDK header is not on this machine")
    text = header.read_text(encoding="utf-8", errors="replace")
    body = text[text.index("IWiaDevMgr2 : public IUnknown"):]
    body = body[:body.index("};")]
    methods = re.findall(r"STDMETHODCALLTYPE (\w+)\(", body)
    assert methods.index("EnumDeviceInfo") + 3 == wia._MGR_ENUM_DEVICE_INFO
    assert methods.index("GetImageDlg") + 3 == wia._MGR_GET_IMAGE_DLG
    assert "B6C292BC-7C88-41ee-8B54-8EC92617E599".lower() == wia.CLSID_WIA_DEV_MGR2
    assert "79C07CF1-CBDD-41ee-8EC3-F00080CADA7A".lower() == wia.IID_IWIA_DEV_MGR2


def test_the_real_wia_layer_is_blocked_and_has_no_process_network_or_shell_route():
    assert getattr(wia.WinWia, "live_device_stand_in", None)
    source = Path(wia.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "ctypes", "hashlib", "io", "os", "shutil", "stat", "tempfile", "uuid",
                        "dataclasses", "pathlib", "typing", "PIL"}, imported
    assert set(re.findall(r'WinDLL\("([a-z0-9]+)"', source)) == {"ole32", "oleaut32"}
    assert "windll" not in source and "ShellExecute" not in source


@requires_live_device
def test_windows_wia_lists_imaging_devices_read_only():
    """Live only (QRONOS_RUN_LIVE_DEVICE_TESTS=1): lists this PC's WIA devices; never scans."""
    devices = wia.WinWia().list_devices()
    assert all(device.kind in {"scanner", "camera", "video", "other"} for device in devices)
