"""The 3D native path (core/print3d_native.py): driver capability proof, the bounded .3mf package and the COM chain.

No printer, queue or driver is opened. The PrintCapabilities documents are fixtures; the submission chain runs
against fake COM objects (Python vtables) and records every call; the one real COM use is the OPC part-name and
memory-stream self-check, which touches no device.
"""
from __future__ import annotations

import ast
import ctypes
import os
import re
import uuid
import zipfile
from ctypes import wintypes
from pathlib import Path

import pytest

from core import device_hub, print3d_native as native
from core.print3d_native import Print3DError, parse_3d_capabilities, read_3mf
from tests.threemf_fixture import (CORE_2013, CORE_2015, MATERIAL_EXT, REL_MODEL, REL_TEXTURE, model_xml,
                                   relationships, write_3mf)

PSK3D = "https://schemas.microsoft.com/3dmanufacturing/2013/01/pskeywords3d"


def capabilities_xml(*, namespace: str = PSK3D, width: str = "250000", height: str | None = "220000",
                     version: str | None = CORE_2015, extensions: str | None = MATERIAL_EXT) -> bytes:
    """A PrintCapabilities document shaped like Microsoft's 3D Print Schema keyword examples."""
    def prop(name: str, value: str, kind: str = "integer") -> str:
        return (f'<psf:Property name="psk3d:{name}"><psf:Value xsi:type="xsd:{kind}">{value}</psf:Value>'
                '</psf:Property>')

    area = prop("Job3DOutputAreaWidth", width) + prop("Job3DOutputAreaDepth", "210000")
    if height is not None:
        area += prop("Job3DOutputAreaHeight", height)
    body = f'<psf:Property name="psk3d:Job3DOutputArea">{area}</psf:Property>'
    if version is not None:
        body += prop("Job3D3MFVersion", version, "string")
    if extensions is not None:
        body += prop("Job3D3MFExtensions", extensions, "string")
    return (
        '<?xml version="1.0" encoding="UTF-8"?><psf:PrintCapabilities version="1" '
        'xmlns:psf="http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework" '
        'xmlns:psk="http://schemas.microsoft.com/windows/2003/08/printing/printschemakeywords" '
        f'xmlns:psk3d="{namespace}" xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<psf:Feature name="psk:PageMediaSize"><psf:Option name="psk:ISOA4"/></psf:Feature>'
        f'{body}</psf:PrintCapabilities>'
    ).encode("utf-8")


# ------------------------------------------------------------ the driver's proof

def test_the_3d_keywords_in_print_capabilities_prove_a_3d_printer():
    proven = parse_3d_capabilities(capabilities_xml())
    assert proven == {"buildVolumeMicrons": {"width": 250000, "depth": 210000, "height": 220000},
                      "threeMfVersion": CORE_2015, "threeMfExtensions": [MATERIAL_EXT]}
    legacy = parse_3d_capabilities(capabilities_xml(namespace=PSK3D.replace("https", "http"), version=None,
                                                    extensions=None))
    assert legacy["threeMfVersion"] == CORE_2013 and legacy["threeMfExtensions"] == []  # the documented default


@pytest.mark.parametrize("document", [
    capabilities_xml(namespace="http://example.com/not-3d"),  # the same words under another namespace
    capabilities_xml(height=None),                              # the build volume is incomplete
    capabilities_xml(width="0"),
    capabilities_xml(width="wide"),
    b'<psf:PrintCapabilities xmlns:psf="http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework"/>',
    b"<not xml",
    b"",
])
def test_anything_less_is_not_proof(document):
    assert parse_3d_capabilities(document) is None


# ------------------------------------------------------------ the package

def test_a_3mf_is_read_once_with_its_model_and_textures(tmp_path):
    package = write_3mf(tmp_path / "a.3mf", textures={"/3D/Textures/t.png": b"png"})
    read = read_3mf(package, {"threeMfVersion": CORE_2015, "threeMfExtensions": []})
    assert read.model == model_xml() and read.textures == (("/3D/Textures/t.png", b"png"),)
    assert read.size == package.stat().st_size and read.model_namespace == CORE_2015
    assert read.required_extensions == ()


@pytest.mark.parametrize("build, code", [
    (lambda p: write_3mf(p, root_rels=relationships()), "bad_package"),                         # no model
    (lambda p: write_3mf(p, root_rels=relationships((REL_MODEL, "/3D/missing.model"))), "bad_package"),
    (lambda p: write_3mf(p, model=b"<model"), "bad_package"),                                   # not XML
    (lambda p: write_3mf(p, model=b'<scene xmlns="urn:x"/>'), "bad_package"),                   # not a model
    (lambda p: write_3mf(p, model=model_xml(required="m")), "bad_package"),                     # undeclared prefix
    (lambda p: write_3mf(p, root_rels="<Relationships"), "bad_package"),
    (lambda p: write_3mf(p, textures={"/3D/zeros.png": bytes(4 * 1024 * 1024)}), "too_large"),  # ratio bomb
])
def test_a_malformed_or_hostile_package_is_refused(tmp_path, build, code):
    with pytest.raises(Print3DError) as refused:
        read_3mf(build(tmp_path / "x.3mf"))
    assert refused.value.code == code


def external(target: str, kind: str = REL_MODEL) -> str:
    return ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="r0" Type="{kind}" Target="{target}" TargetMode="External"/></Relationships>')


def texture_rels(target: str) -> dict[str, bytes]:
    return {"3D/_rels/3dmodel.model.rels": relationships((REL_TEXTURE, target)).encode("utf-8")}


def raw_zip(path: Path, members: list[tuple[str, bytes]]) -> Path:
    """A ZIP written member by member, so duplicate or hostile member names can be stored as is."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about a duplicate name; that is the point here
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members:
                archive.writestr(zipfile.ZipInfo(name), data)
    return path


def backslash_member(path: Path, placeholder: bytes) -> Path:
    """Store a backslash in a member name byte for byte (zipfile would turn it into "/" while writing)."""
    data = path.read_bytes()
    assert data.count(placeholder) == 2  # the local header and the central directory
    path.write_bytes(data.replace(placeholder, placeholder.replace(b"|", b"\\")))
    return path


BASIC = [("_rels/.rels", relationships((REL_MODEL, "/3D/3dmodel.model")).encode()), ("3D/3dmodel.model", model_xml())]


@pytest.mark.parametrize("build", [
    # the model or a texture reached outside the archive: external, URI, UNC, drive, backslash, encoded, traversal
    lambda p: write_3mf(p, root_rels=external("/3D/3dmodel.model")),
    lambda p: write_3mf(p, root_rels=external("file:///C:/Windows/win.ini")),
    lambda p: write_3mf(p, root_rels=relationships((REL_MODEL, "http://host.example/3dmodel.model"))),
    lambda p: write_3mf(p, root_rels=relationships((REL_MODEL, "//server/share/3dmodel.model"))),
    lambda p: write_3mf(p, root_rels=relationships((REL_MODEL, "\\\\server\\share\\3dmodel.model"))),
    lambda p: write_3mf(p, root_rels=relationships((REL_MODEL, "C:/models/3dmodel.model"))),
    lambda p: write_3mf(p, root_rels=relationships((REL_MODEL, "/3D/../3D/3dmodel.model"))),
    lambda p: write_3mf(p, root_rels=relationships((REL_MODEL, "/3D/%2e%2e/3dmodel.model"))),
    lambda p: write_3mf(p, root_rels=relationships((REL_MODEL, "/_rels/.rels"))),
    lambda p: write_3mf(p, extra=texture_rels("../../outside.png")),
    lambda p: write_3mf(p, extra=texture_rels("\\\\server\\share\\t.png")),
    lambda p: write_3mf(p, extra=texture_rels("/3D/3dmodel.model")),
    lambda p: write_3mf(p, extra={"3D/_rels/3dmodel.model.rels": external("t.png", REL_TEXTURE).encode()}),
    # hostile members: duplicates (also by case), traversal, absolute, drive and backslash names
    lambda p: raw_zip(p, BASIC + [("3D/3dmodel.model", model_xml(CORE_2013))]),  # a valid second model
    lambda p: raw_zip(p, BASIC + [("3d/3DMODEL.model", model_xml(CORE_2013))]),
    lambda p: raw_zip(p, BASIC + [("../outside.txt", b"x")]),
    lambda p: raw_zip(p, BASIC + [("/abs.txt", b"x")]),
    lambda p: raw_zip(p, BASIC + [("C:/drive.txt", b"x")]),
    # a stored backslash name (zipfile reads it as "/"): it collides with the model part and is refused as such
    lambda p: backslash_member(raw_zip(p, BASIC + [("3D|3dmodel.model", model_xml(CORE_2013))]),
                               b"3D|3dmodel.model"),
])
def test_a_package_cannot_name_anything_outside_itself(tmp_path, build):
    with pytest.raises(Print3DError) as refused:
        read_3mf(build(tmp_path / "x.3mf"))
    assert refused.value.code == "bad_package"


def test_bombs_and_oversized_parts_are_refused_before_they_are_expanded(tmp_path, monkeypatch):
    bomb = write_3mf(tmp_path / "bomb.3mf", model=model_xml() + b" " * (8 * 1024 * 1024))  # ~1000:1
    with pytest.raises(Print3DError) as refused:
        read_3mf(bomb)
    assert refused.value.code == "too_large"
    monkeypatch.setattr(native, "MAX_TEXTURE_BYTES", 8)
    with pytest.raises(Print3DError) as refused:
        read_3mf(write_3mf(tmp_path / "texture.3mf", textures={"/3D/t.png": b"0123456789"}))
    assert refused.value.code == "too_large"
    monkeypatch.setattr(native, "MAX_TEXTURE_BYTES", 1024)
    monkeypatch.setattr(native, "MAX_PARTS_BYTES", len(model_xml()) + 10)
    with pytest.raises(Print3DError) as refused:
        read_3mf(write_3mf(tmp_path / "total.3mf", textures={"/3D/a.png": b"012345", "/3D/b.png": b"678901"}))
    assert refused.value.code == "too_large"
    monkeypatch.setattr(native, "MAX_MEMBERS", 3)
    with pytest.raises(Print3DError) as refused:
        read_3mf(write_3mf(tmp_path / "members.3mf", extra={"a.txt": b"a", "b.txt": b"b"}))
    assert refused.value.code == "bad_package"


def test_only_the_model_and_its_textures_are_handed_over(tmp_path):
    read = read_3mf(write_3mf(tmp_path / "x.3mf", textures={"/3D/t.png": b"t"}, extra={
        "Metadata/thumbnail.png": b"thumbnail", "secret.txt": b"not referenced"}))
    assert [name for name, _ in read.textures] == ["/3D/t.png"] and read.model == model_xml()


def test_package_bounds_are_enforced(tmp_path, monkeypatch):
    many = write_3mf(tmp_path / "many.3mf", textures={"/3D/a.png": b"a", "/3D/b.png": b"b"})
    monkeypatch.setattr(native, "MAX_TEXTURES", 1)
    with pytest.raises(Print3DError) as refused:
        read_3mf(many)
    assert refused.value.code == "too_large"
    monkeypatch.setattr(native, "MAX_MODEL_BYTES", 64)
    with pytest.raises(Print3DError) as refused:
        read_3mf(write_3mf(tmp_path / "big.3mf"))
    assert refused.value.code == "too_large"
    with pytest.raises(Print3DError):
        read_3mf(tmp_path / "model.stl")
    (tmp_path / "zip.3mf").write_bytes(b"PK not really")
    with pytest.raises(Print3DError) as refused:
        read_3mf(tmp_path / "zip.3mf")
    assert refused.value.code == "bad_package"


# ------------------------------------------------------------ the COM chain, against recording fakes

PROTOTYPE = ctypes.WINFUNCTYPE
HRESULT = ctypes.c_long
E_FAIL = -2147467259


class FakeCom:
    """One fake COM object: a vtable of Python callbacks; slots 0-2 are IUnknown and Release is counted."""

    registry: dict[int, "FakeCom"] = {}

    def __init__(self, name: str, log: list, methods: dict[int, tuple[tuple, object]], data=None) -> None:
        self.name, self.log, self.data, self.released = name, log, data, 0
        size = max([3, *methods]) + 1
        self._keep = []
        self.vtable = (ctypes.c_void_p * size)()
        unknown = PROTOTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(lambda *_: E_FAIL)
        add_ref = PROTOTYPE(ctypes.c_ulong, ctypes.c_void_p)(lambda this: 1)
        release = PROTOTYPE(ctypes.c_ulong, ctypes.c_void_p)(self._release)
        for slot, function in ((0, unknown), (1, add_ref), (2, release)):
            self._keep.append(function)
            self.vtable[slot] = ctypes.cast(function, ctypes.c_void_p).value
        for slot, (argtypes, body) in methods.items():
            function = PROTOTYPE(HRESULT, ctypes.c_void_p, *argtypes)(body)
            self._keep.append(function)
            self.vtable[slot] = ctypes.cast(function, ctypes.c_void_p).value
        self.holder = ctypes.c_void_p(ctypes.addressof(self.vtable))
        self.pointer = ctypes.addressof(self.holder)
        FakeCom.registry[self.pointer] = self

    def _release(self, this) -> int:
        self.released += 1
        return 0

    @property
    def ref(self) -> ctypes.c_void_p:
        return ctypes.c_void_p(self.pointer)


def of(pointer) -> FakeCom | None:
    return FakeCom.registry.get(pointer) if pointer else None


def guid_at(pointer) -> str:
    return str(uuid.UUID(bytes_le=ctypes.string_at(pointer, 16)))


class FakeDriverPath:
    """The print package target factory, its 3D target and writer, as the SDK headers order their methods."""

    def __init__(self, *, offer_3d: bool = True, texture_hr: int = 0) -> None:
        self.log: list[tuple] = []
        self.objects: list[FakeCom] = []
        ole = ctypes.WinDLL("ole32")
        ole.CoTaskMemAlloc.argtypes, ole.CoTaskMemAlloc.restype = [ctypes.c_size_t], ctypes.c_void_p
        offered = [native.ID_DOCUMENTPACKAGETARGET_OPENXPS_WITH_3D] if offer_3d else []
        offered.append("9cae40a8-ded1-41c9-a9fd-d735ef33aeda")  # MSXPS, offered by any XPS queue

        def write_pointer(out, obj: FakeCom) -> None:
            ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = obj.pointer

        def add_texture(this, uri, stream):
            self.log.append(("AddModelTexture", of(uri).data, of(stream).data))
            return texture_hr

        self.writer = self.make("writer", {
            6: ((), lambda this: self.log.append(("Close",)) or 0),
            8: ((ctypes.c_void_p, ctypes.c_void_p), add_texture),
        })

        def get_writer(this, sequence, discard, model_part, model_stream, out):
            self.log.append(("GetXpsOMPackageWriter3D", of(sequence).data, discard, of(model_part).data,
                             of(model_stream).data))
            write_pointer(out, self.writer)
            return 0

        self.target3d = self.make("target3d", {
            3: ((ctypes.c_void_p,) * 5, get_writer),
        })

        def types(this, count, array):
            memory = ole.CoTaskMemAlloc(16 * len(offered))
            for index, text in enumerate(offered):
                ctypes.memmove(memory + 16 * index, uuid.UUID(text).bytes_le, 16)
            ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = len(offered)
            ctypes.cast(array, ctypes.POINTER(ctypes.c_void_p))[0] = memory
            self.log.append(("GetPackageTargetTypes",))
            return 0

        def get_target(this, kind, iid, out):
            self.log.append(("GetPackageTarget", guid_at(kind), guid_at(iid)))
            write_pointer(out, self.target3d)
            return 0

        self.target = self.make("target", {
            3: ((ctypes.c_void_p, ctypes.c_void_p), types),
            4: ((ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p), get_target),
            5: ((), lambda this: self.log.append(("Cancel",)) or 0),
        })

        def create_target(this, printer, job, output, ticket, out):
            self.log.append(("CreateDocumentPackageTargetForPrintJob", printer, job, output, ticket))
            write_pointer(out, self.target)
            return 0

        self.factory = self.make("factory", {
            3: ((wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p),
                create_target),
        })
        self.opc = self.make("opc", {})

    def make(self, name: str, methods: dict, data=None) -> FakeCom:
        obj = FakeCom(name, self.log, methods, data)
        self.objects.append(obj)
        return obj

    def api(self) -> native.ComPackageApi:
        api = native.ComPackageApi()
        by_clsid = {native.CLSID_PrintDocumentPackageTargetFactory: self.factory, native.CLSID_OpcFactory: self.opc}
        api._create = lambda clsid, iid: by_clsid[clsid].ref
        api._part_uri = lambda factory, name: self.make("uri", {}, name).ref
        api._stream = lambda data: self.make("stream", {}, bytes(data)).ref
        return api


needs_windows = pytest.mark.skipif(os.name != "nt", reason="the COM calling convention is Windows only")


def package(tmp_path: Path, textures=None):
    return read_3mf(write_3mf(tmp_path / "p.3mf", textures=textures))


@needs_windows
def test_the_3d_chain_hands_the_model_and_textures_to_the_3d_package_target_then_closes(tmp_path):
    fake = FakeDriverPath()
    sent = package(tmp_path, {"/3D/Textures/t.png": b"png"})
    native._submit_package(fake.api(), "Workshop", "Qronos 3D 0a1b2c", sent)
    assert fake.log == [
        ("CreateDocumentPackageTargetForPrintJob", "Workshop", "Qronos 3D 0a1b2c", None, None),
        ("GetPackageTargetTypes",),
        ("GetPackageTarget", native.ID_DOCUMENTPACKAGETARGET_OPENXPS_WITH_3D, native.IID_IXpsDocumentPackageTarget3D),
        ("GetXpsOMPackageWriter3D", native.DOCUMENT_SEQUENCE_PART, None, native.MODEL_PART, sent.model),
        ("AddModelTexture", "/3D/Textures/t.png", b"png"),
        ("Close",),
    ]
    assert all(obj.released == 1 for obj in fake.objects)  # every reference is released once


@needs_windows
@pytest.mark.parametrize("offer_3d, texture_hr, code", [(False, 0, "not_3d_target"), (True, E_FAIL, "native_refused")])
def test_a_queue_without_the_3d_target_or_a_failed_step_cancels_the_job(tmp_path, offer_3d, texture_hr, code):
    fake = FakeDriverPath(offer_3d=offer_3d, texture_hr=texture_hr)
    with pytest.raises(Print3DError) as refused:
        native._submit_package(fake.api(), "Plain XPS", "Qronos 3D 0a1b2c", package(tmp_path, {"/3D/t.png": b"t"}))
    assert refused.value.code == code
    assert fake.log.count(("Cancel",)) == 1 and ("Close",) not in fake.log
    assert fake.factory.released == fake.opc.released == fake.target.released == 1
    assert all(obj.released <= 1 for obj in fake.objects)


@needs_windows
def test_the_com_self_check_uses_only_opc_part_names_and_memory_streams():
    assert native.ComPackageApi().self_check() == {"opcPartUri": True, "memoryStream": True}


# ------------------------------------------------------------ boundaries

def test_the_real_spooler_and_3d_driver_path_are_blocked_in_tests():
    assert getattr(native.Win3DPrinting, "live_device_stand_in", None)
    assert getattr(device_hub.WinDeviceSpooler, "live_device_stand_in", None)
    assert getattr(device_hub.Win3DPrinting, "live_device_stand_in", None)


def test_the_3d_path_has_no_network_process_shell_or_machine_command_route():
    allowed = {"__future__", "ctypes", "hashlib", "io", "os", "posixpath", "re", "uuid", "zipfile", "dataclasses",
               "pathlib", "typing", "xml"}
    source = Path(native.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= allowed, imported - allowed
    assert "windll" not in source  # every native library is named below
    dlls = set(re.findall(r'WinDLL\("([a-z0-9]+)"\)', source))
    assert dlls == {"ole32", "shlwapi", "prntvpt", "kernel32"}
