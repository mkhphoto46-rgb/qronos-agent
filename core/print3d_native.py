"""The Windows 3D manufacturing print path, local driver and spooler only (Device Hub, H1/H2).

Primary sources: Microsoft Learn "3D printing" (Windows 8.1+ 3D manufacturing API) and "Print Schema keywords for 3D
manufacturing"; COM interfaces, GUIDs and method order are those of the Windows SDK headers DocumentTarget.h,
xpsobjectmodel.h, xpsobjectmodel_2.h, msopc.h and prntvpt.h (10.0.26100).

* Proof of a 3D printer (read-only): the queue's PrintCapabilities (Print Ticket API, prntvpt.dll) carry the psk3d
  keywords of namespace .../3dmanufacturing/2013/01/pskeywords3d: Job3DOutputArea (build volume, microns),
  Job3D3MFVersion and Job3D3MFExtensions. Nothing else (no name or driver text) proves it.
* Submission: CreateDocumentPackageTargetForPrintJob(queue, job) -> the target must offer
  ID_DOCUMENTPACKAGETARGET_OPENXPS_WITH_3D -> IXpsDocumentPackageTarget3D::GetXpsOMPackageWriter3D(document sequence,
  -, /3D/3dmodel.model, model stream) -> AddModelTexture for each texture -> Close. The model and textures are opaque
  streams from one exact .3mf package; the spooler and the device's own driver do the rest. There is no helper
  process, shell, G-code, serial port or network path here.
* What is claimed afterwards: only that Windows accepted the package for the queue (and the job id when the spooler
  lists the unique job name). Never that anything was printed.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import os
import posixpath
import re
import uuid
import zipfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree

PSK3D_NAMESPACES = ("http://schemas.microsoft.com/3dmanufacturing/2013/01/pskeywords3d",
                    "https://schemas.microsoft.com/3dmanufacturing/2013/01/pskeywords3d")
PSF_NAMESPACES = ("http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework",
                  "https://schemas.microsoft.com/windows/2003/08/printing/printschemaframework")
REL_3DMODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
REL_3DTEXTURE = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dtexture"
LEGACY_3MF_VERSION = "http://schemas.microsoft.com/3dmanufacturing/2013/01"  # the documented default when omitted
MODEL_PART = "/3D/3dmodel.model"
DOCUMENT_SEQUENCE_PART = "/FixedDocumentSequence.fdseq"

MAX_CAPABILITIES_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_MODEL_BYTES = 256 * 1024 * 1024
MAX_TEXTURES = 64
MAX_TEXTURE_BYTES = 64 * 1024 * 1024
MAX_RATIO = 200  # compressed -> expanded, against archive bombs
MAX_PARTS_BYTES = 512 * 1024 * 1024  # the model and its textures together
MAX_MEMBERS = 4096
MAX_RELATIONSHIPS = 256
MAX_RELATIONSHIPS_BYTES = 1024 * 1024


class Print3DError(RuntimeError):
    """The package or the native path refused; ``code`` is a stable word for the desktop."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


# ------------------------------------------------------------ capabilities (pure)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_3d_capabilities(xml: bytes) -> dict[str, Any] | None:
    """The proven 3D capabilities in a PrintCapabilities document, or None when it is not a 3D printer's."""
    if not xml or len(xml) > MAX_CAPABILITIES_BYTES:
        return None
    text = xml.decode("utf-8", "replace") if isinstance(xml, (bytes, bytearray)) else str(xml)
    prefixes = {m.group(1) for m in re.finditer(r'xmlns:([A-Za-z_][\w.-]*)\s*=\s*"([^"]+)"', text)
                if m.group(2).rstrip("/") in PSK3D_NAMESPACES}
    if not prefixes:
        return None
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    values: dict[str, str] = {}
    for element in root.iter():
        name = element.get("name") or ""
        if ":" not in name or name.split(":", 1)[0] not in prefixes or _local(element.tag) != "Property":
            continue
        for child in element:
            if _local(child.tag) == "Value" and child.text is not None:
                values[name.split(":", 1)[1]] = child.text.strip()
                break
    try:
        area = {key: int(values[f"Job3DOutputArea{key}"]) for key in ("Width", "Depth", "Height")}
    except (KeyError, ValueError):
        return None  # Job3DOutputArea with its three dimensions is what a 3D printer driver MUST declare
    if min(area.values()) <= 0:
        return None
    version = values.get("Job3D3MFVersion") or LEGACY_3MF_VERSION
    extensions = [item for item in values.get("Job3D3MFExtensions", "").split() if item][:16]
    return {"buildVolumeMicrons": {"width": area["Width"], "depth": area["Depth"], "height": area["Height"]},
            "threeMfVersion": version[:200], "threeMfExtensions": [item[:200] for item in extensions]}


# ------------------------------------------------------------ the .3mf package (pure)


@dataclass(frozen=True)
class Package3MF:
    path: Path
    size: int
    sha256: str
    model: bytes
    model_namespace: str
    required_extensions: tuple[str, ...]
    textures: tuple[tuple[str, bytes], ...]


def _same_namespace(a: str, b: str) -> bool:
    strip = lambda value: re.sub(r"^https?://", "", value.strip().rstrip("/"))  # noqa: E731
    return strip(a) == strip(b)


def _model_root(model: bytes) -> tuple[str, tuple[str, ...]]:
    """The model root's namespace and the namespaces of its required extensions, read without parsing the mesh."""
    prefixes: dict[str, str] = {}
    try:
        for event, item in ElementTree.iterparse(io.BytesIO(model), events=("start-ns", "start")):
            if event == "start-ns":
                prefixes[item[0]] = item[1]
                continue
            if _local(item.tag) != "model" or not item.tag.startswith("{"):
                break
            required = (item.get("requiredextensions") or "").split()
            unknown = [prefix for prefix in required if prefix not in prefixes]
            if unknown or len(required) > 16:
                raise Print3DError("bad_package", "the model names an undeclared required extension")
            return item.tag[1:].split("}", 1)[0], tuple(prefixes[prefix] for prefix in required)
    except ElementTree.ParseError:
        raise Print3DError("bad_package", "the 3D model part is not XML") from None
    raise Print3DError("bad_package", "the 3D model part has no 3MF model root")


class _Archive:
    """The package's members by OPC part name (case-insensitive), refusing a package that could name a path outside
    itself: absolute, drive, UNC, backslash or traversal member names, duplicates, or too many members."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise Print3DError("bad_package", "the package has too many parts")
        self.archive = archive
        self.parts: dict[str, zipfile.ZipInfo] = {}
        for info in members:
            name = info.filename
            if not name or "\\" in name or ":" in name or name.startswith("/") or "\0" in name \
                    or any(segment in ("..", ".") for segment in name.split("/")):
                raise Print3DError("bad_package", "a package member names a path outside the package")
            key = "/" + name.casefold()
            if key in self.parts:
                raise Print3DError("bad_package", "the package has duplicate parts")
            self.parts[key] = info

    def read(self, part: str, limit: int) -> bytes:
        info = self.parts.get(part.casefold())
        if info is None or info.is_dir():
            raise Print3DError("bad_package", f"missing part {part[:120]}")
        if info.file_size > limit or (info.compress_size and info.file_size > MAX_RATIO * max(info.compress_size, 1)):
            raise Print3DError("too_large", f"part {part[:120]} is too large")
        with self.archive.open(info) as stream:
            data = stream.read(limit + 1)  # never more than the bound, whatever the header claims
        if len(data) != info.file_size:
            raise Print3DError("bad_package", f"part {part[:120]} size mismatch")
        return data

    def relationships(self, part: str) -> list[tuple[str, str]]:
        """(type, target) of a relationships part; an External target refuses the package."""
        if part.casefold() not in self.parts:
            return []
        try:
            root = ElementTree.fromstring(self.read(part, MAX_RELATIONSHIPS_BYTES))
        except ElementTree.ParseError:
            raise Print3DError("bad_package", "a relationships part is not XML") from None
        found = []
        for item in root:
            if _local(item.tag) != "Relationship":
                continue
            if (item.get("TargetMode") or "Internal").casefold() != "internal":
                raise Print3DError("bad_package", "the package has an external relationship")
            found.append((item.get("Type") or "", item.get("Target") or ""))
        return found[:MAX_RELATIONSHIPS]


def _part_name(source_dir: str, target: str) -> str:
    """The part a relationship target names, only inside the package: no scheme, drive, UNC, backslash, query,
    fragment, percent-encoding or dot segment."""
    if not target or any(mark in target for mark in ("\\", ":", "%", "?", "#", "\0")) or target.startswith("//") \
            or any(segment in ("..", ".") for segment in target.split("/")):
        raise Print3DError("bad_package", "a relationship points outside the package")
    joined = target if target.startswith("/") else posixpath.join("/" + source_dir.strip("/"), target)
    normalized = posixpath.normpath(joined)
    if not normalized.startswith("/") or normalized.startswith("//") or normalized == "/":
        raise Print3DError("bad_package", "a relationship points outside the package")
    return normalized


def read_3mf(path: Path, capabilities: dict[str, Any] | None = None) -> Package3MF:
    """One exact local .3mf package: its 3D model part and textures, bounded, read once (the bytes hashed are the
    bytes handed over). With the device's proven capabilities, its 3MF version and extensions must match."""
    path = Path(path)
    if path.suffix.lower() != ".3mf":
        raise Print3DError("bad_package", "a .3mf file is required")
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise Print3DError("too_large", "the package is too large")
    data = path.read_bytes()
    if len(data) > MAX_PACKAGE_BYTES:
        raise Print3DError("too_large", "the package is too large")
    digest = hashlib.sha256(data).hexdigest()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError):
        raise Print3DError("bad_package", "not a 3MF (OPC/ZIP) package") from None
    with archive:
        parts = _Archive(archive)
        models = [target for kind, target in parts.relationships("/_rels/.rels") if kind == REL_3DMODEL]
        if len(models) != 1:
            raise Print3DError("bad_package", "the package must have exactly one 3D model part")
        model_part = _part_name("", models[0])
        if "/_rels/" in model_part.casefold() + "/":
            raise Print3DError("bad_package", "the 3D model part is a relationships part")
        model = parts.read(model_part, MAX_MODEL_BYTES)
        namespace, required = _model_root(model)
        if capabilities is not None:
            version = str(capabilities.get("threeMfVersion") or LEGACY_3MF_VERSION)
            if not _same_namespace(namespace, version):
                raise Print3DError("incompatible_3mf_version", f"model {namespace[:120]} but the printer expects "
                                                               f"{version[:120]}")
            offered = [str(item) for item in capabilities.get("threeMfExtensions") or ()]
            missing = [item for item in required if not any(_same_namespace(item, have) for have in offered)]
            if missing:
                raise Print3DError("unsupported_3mf_extension", f"the printer does not declare {missing[0][:120]}")
        directory, file_name = posixpath.split(model_part)
        rels = parts.relationships(posixpath.join(directory, "_rels", file_name + ".rels"))
        textures: list[str] = []
        for kind, target in rels:
            if kind != REL_3DTEXTURE:
                continue
            name = _part_name(directory, target)
            if name.casefold() == model_part.casefold() or "/_rels/" in name.casefold():
                raise Print3DError("bad_package", "a texture names the model or a relationships part")
            if name.casefold() not in (item.casefold() for item in textures):
                textures.append(name)
        if len(textures) > MAX_TEXTURES:
            raise Print3DError("too_large", "too many textures")
        total = len(model)
        texture_data = []
        for name in textures:
            texture = parts.read(name, MAX_TEXTURE_BYTES)
            total += len(texture)
            if total > MAX_PARTS_BYTES:
                raise Print3DError("too_large", "the model and textures are too large together")
            texture_data.append((name, texture))
    return Package3MF(path, len(data), digest, model, namespace, required, tuple(texture_data))


# ------------------------------------------------------------ Windows (COM through vtables from the SDK headers)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


CLSID_PrintDocumentPackageTargetFactory = "348ef17d-6c81-4982-92b4-ee188a43867a"
IID_IPrintDocumentPackageTargetFactory = "d2959bf7-b31b-4a3d-9600-712eb1335ba4"
ID_DOCUMENTPACKAGETARGET_OPENXPS_WITH_3D = "63dbd720-8b14-4577-b074-7bb11b596d28"
IID_IXpsDocumentPackageTarget3D = "60ba71b8-3101-4984-9199-f4ea775ff01d"
CLSID_OpcFactory = "6b2d6ba0-9f3e-4f27-920b-313cc426a39e"
IID_IOpcFactory = "6d0b4446-cd73-4ab3-94f4-8ccdf6116154"
# vtable slots (IUnknown 0-2), from the headers named above
_FACTORY_CREATE_TARGET = 3            # IPrintDocumentPackageTargetFactory::CreateDocumentPackageTargetForPrintJob
_TARGET_TYPES, _TARGET_GET, _TARGET_CANCEL = 3, 4, 5  # IPrintDocumentPackageTarget
_TARGET3D_WRITER = 3                  # IXpsDocumentPackageTarget3D::GetXpsOMPackageWriter3D
_WRITER_CLOSE, _WRITER3D_TEXTURE = 6, 8  # IXpsOMPackageWriter::Close; IXpsOMPackageWriter3D::AddModelTexture
_OPC_CREATE_PART_URI = 4              # IOpcFactory::CreatePartUri
_STREAM_SEEK = 5                      # IStream::Seek (after ISequentialStream Read/Write)


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise Print3DError("native_refused", f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


def _call(obj: ctypes.c_void_p, slot: int, *args, argtypes: tuple = ()) -> int:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])
    return int(function(obj, *args))


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)


class ComPackageApi:
    """COM, OPC part names and memory streams on Windows; touches no printer, queue or driver."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise Print3DError("unavailable", "3D printing through Windows is only available on Windows")
        self._ole = ctypes.WinDLL("ole32")
        self._shlwapi = ctypes.WinDLL("shlwapi")
        self._ole.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        self._ole.CoCreateInstance.restype = ctypes.c_long
        self._ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self._ole.CoInitializeEx.restype = ctypes.c_long
        self._ole.CoUninitialize.argtypes, self._ole.CoUninitialize.restype = [], None
        self._ole.CreateStreamOnHGlobal.argtypes = [ctypes.c_void_p, wintypes.BOOL, ctypes.POINTER(ctypes.c_void_p)]
        self._ole.CreateStreamOnHGlobal.restype = ctypes.c_long
        self._ole.GetHGlobalFromStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self._ole.GetHGlobalFromStream.restype = ctypes.c_long
        self._ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self._ole.CoTaskMemFree.restype = None
        self._shlwapi.SHCreateMemStream.argtypes = [ctypes.c_void_p, wintypes.UINT]
        self._shlwapi.SHCreateMemStream.restype = ctypes.c_void_p

    def _apartment(self, work: Callable[..., Any], *args: Any) -> Any:
        """Run ``work`` with COM initialised on this thread (and uninitialised again when this call did it)."""
        hr = self._ole.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        if hr < 0 and (hr & 0xFFFFFFFF) != 0x80010106:  # RPC_E_CHANGED_MODE: COM is already up on this thread
            _check(hr, "CoInitializeEx")
        try:
            return work(*args)
        finally:
            if hr >= 0:  # S_OK and S_FALSE are each balanced by one CoUninitialize
                self._ole.CoUninitialize()

    def self_check(self) -> dict[str, bool]:
        """Exercise the OPC part-URI and memory-stream calls only (no printer, queue or driver is touched)."""
        return self._apartment(self._self_check)

    def _create(self, clsid: str, iid: str) -> ctypes.c_void_p:
        out = ctypes.c_void_p()
        _check(self._ole.CoCreateInstance(ctypes.byref(_GUID.parse(clsid)), None, 0x1,  # CLSCTX_INPROC_SERVER
                                          ctypes.byref(_GUID.parse(iid)), ctypes.byref(out)), "CoCreateInstance")
        return out

    def _stream(self, data: bytes) -> ctypes.c_void_p:
        buffer = ctypes.create_string_buffer(data, len(data))
        stream = self._shlwapi.SHCreateMemStream(buffer, len(data))
        if not stream:
            raise Print3DError("native_refused", "SHCreateMemStream failed")
        return ctypes.c_void_p(stream)

    def _part_uri(self, factory: ctypes.c_void_p, name: str) -> ctypes.c_void_p:
        uri = ctypes.c_void_p()
        _check(_call(factory, _OPC_CREATE_PART_URI, name, ctypes.byref(uri),
                     argtypes=(wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))), "IOpcFactory::CreatePartUri")
        return uri

    def _self_check(self) -> dict[str, bool]:
        """Exercise the OPC part-URI and memory-stream calls only (no printer, queue or driver is touched)."""
        factory = self._create(CLSID_OpcFactory, IID_IOpcFactory)
        try:
            uri = self._part_uri(factory, MODEL_PART)
            stream = self._stream(b"<model/>")
            ok = bool(uri) and bool(stream)
            _release(uri)
            _release(stream)
        finally:
            _release(factory)
        return {"opcPartUri": ok, "memoryStream": ok}


class Win3DPrinting(ComPackageApi):
    """The native 3D path to a real queue and its driver; constructed only when a request needs it."""

    def capabilities(self, printer: str) -> dict[str, Any] | None:
        """The queue's proven 3D capabilities (PrintCapabilities via the Print Ticket API), or None."""
        return self._apartment(self._capabilities, printer)

    def submit(self, printer: str, job_name: str, package: Package3MF) -> None:
        """Hand one 3MF model (and its textures) to the queue's 3D print path; raises Print3DError on refusal."""
        self._apartment(_submit_package, self, printer, job_name, package)

    def _capabilities(self, printer: str) -> dict[str, Any] | None:
        """The queue's proven 3D capabilities (PrintCapabilities via the Print Ticket API), or None."""
        prntvpt = ctypes.WinDLL("prntvpt")
        prntvpt.PTOpenProvider.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        prntvpt.PTOpenProvider.restype = ctypes.c_long
        prntvpt.PTGetPrintCapabilities.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                                   ctypes.POINTER(ctypes.c_void_p)]
        prntvpt.PTGetPrintCapabilities.restype = ctypes.c_long
        prntvpt.PTCloseProvider.argtypes = [wintypes.HANDLE]
        prntvpt.PTCloseProvider.restype = ctypes.c_long
        kernel = ctypes.WinDLL("kernel32")
        kernel.GlobalLock.argtypes, kernel.GlobalLock.restype = [ctypes.c_void_p], ctypes.c_void_p
        kernel.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel.GlobalSize.argtypes, kernel.GlobalSize.restype = [ctypes.c_void_p], ctypes.c_size_t
        provider = wintypes.HANDLE()
        if prntvpt.PTOpenProvider(printer, 1, ctypes.byref(provider)) < 0:
            return None
        stream = ctypes.c_void_p()
        try:
            _check(self._ole.CreateStreamOnHGlobal(None, True, ctypes.byref(stream)), "CreateStreamOnHGlobal")
            if prntvpt.PTGetPrintCapabilities(provider, None, stream, None) < 0:
                return None
            written = ctypes.c_ulonglong()  # IStream::Seek(0, STREAM_SEEK_CUR): the bytes the provider wrote
            _check(_call(stream, _STREAM_SEEK, 0, 1, ctypes.byref(written),
                         argtypes=(ctypes.c_longlong, wintypes.DWORD, ctypes.POINTER(ctypes.c_ulonglong))),
                   "IStream::Seek")
            handle = ctypes.c_void_p()
            _check(self._ole.GetHGlobalFromStream(stream, ctypes.byref(handle)), "GetHGlobalFromStream")
            size = min(int(written.value), int(kernel.GlobalSize(handle)), MAX_CAPABILITIES_BYTES + 1)
            pointer = kernel.GlobalLock(handle)
            try:
                xml = ctypes.string_at(pointer, size) if pointer else b""
            finally:
                kernel.GlobalUnlock(handle)
            return parse_3d_capabilities(xml.rstrip(b"\0"))
        finally:
            _release(stream)
            prntvpt.PTCloseProvider(provider)


def _submit_package(api: ComPackageApi, printer: str, job_name: str, package: Package3MF) -> None:
    """Hand one 3MF model (and its textures) to the queue's 3D print path; raises Print3DError on refusal.

    Any failure after the print job target exists cancels that target, so no partly written job is left."""
    owned: list[ctypes.c_void_p] = []
    target, finished = ctypes.c_void_p(), False
    try:
        factory = api._create(CLSID_PrintDocumentPackageTargetFactory, IID_IPrintDocumentPackageTargetFactory)
        owned.append(factory)
        opc = api._create(CLSID_OpcFactory, IID_IOpcFactory)
        owned.append(opc)
        _check(_call(factory, _FACTORY_CREATE_TARGET, printer, job_name, None, None, ctypes.byref(target),
                     argtypes=(wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_void_p))), "CreateDocumentPackageTargetForPrintJob")
        owned.append(target)
        count, types = ctypes.c_uint32(), ctypes.POINTER(_GUID)()
        _check(_call(target, _TARGET_TYPES, ctypes.byref(count), ctypes.byref(types),
                     argtypes=(ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.POINTER(_GUID)))),
               "GetPackageTargetTypes")
        wanted = bytes(_GUID.parse(ID_DOCUMENTPACKAGETARGET_OPENXPS_WITH_3D))
        offered = bool(types) and any(bytes(types[i]) == wanted for i in range(min(count.value, 16)))
        if types:
            api._ole.CoTaskMemFree(types)
        if not offered:
            raise Print3DError("not_3d_target", "this queue does not accept 3D manufacturing packages")
        target3d = ctypes.c_void_p()
        _check(_call(target, _TARGET_GET, ctypes.byref(_GUID.parse(ID_DOCUMENTPACKAGETARGET_OPENXPS_WITH_3D)),
                     ctypes.byref(_GUID.parse(IID_IXpsDocumentPackageTarget3D)), ctypes.byref(target3d),
                     argtypes=(ctypes.POINTER(_GUID), ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))),
               "GetPackageTarget(3D)")
        owned.append(target3d)
        sequence = api._part_uri(opc, DOCUMENT_SEQUENCE_PART)
        owned.append(sequence)
        model_part = api._part_uri(opc, MODEL_PART)
        owned.append(model_part)
        model_stream = api._stream(package.model)
        owned.append(model_stream)
        writer = ctypes.c_void_p()
        _check(_call(target3d, _TARGET3D_WRITER, sequence, None, model_part, model_stream, ctypes.byref(writer),
                     argtypes=(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_void_p))), "GetXpsOMPackageWriter3D")
        owned.append(writer)
        for name, data in package.textures:
            uri = api._part_uri(opc, name)
            owned.append(uri)
            stream = api._stream(data)
            owned.append(stream)
            _check(_call(writer, _WRITER3D_TEXTURE, uri, stream, argtypes=(ctypes.c_void_p, ctypes.c_void_p)),
                   "AddModelTexture")
        _check(_call(writer, _WRITER_CLOSE), "IXpsOMPackageWriter::Close")
        finished = True
    finally:
        if target and not finished:
            _call(target, _TARGET_CANCEL)  # best effort: no partly written job is left in the queue
        for obj in reversed(owned):
            _release(obj)
