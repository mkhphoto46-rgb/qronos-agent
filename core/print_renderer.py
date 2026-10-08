"""
Print plain text, images and PDF pages with the approved per-job options.

The shell "printto" verb can only use a printer's defaults. This renderer
applies exactly the options a person approved, to one job only:

    copies       1..99, collated
    colour       printer default, colour, or monochrome
    pages        all, or explicit ranges such as "1-3,5"

It uses only Windows facilities:

    - Windows.Data.Pdf (the PDF renderer built into Windows, reached from
      Windows PowerShell through WinRT) rasterises only the approved PDF
      pages into a private temporary folder that is always removed;
    - GDI draws text and GDI+ draws images onto a printer DC created for the
      exact printer with a per-job DEVMODE (DocumentPropertiesW merges the
      copies, collation and colour into the driver's own settings; the
      user's saved printer defaults are never changed);
    - StartDocW names the job after the file and returns its exact job id,
      which the workflow then verifies in the spooler.

The drawing code takes a canvas, so tests run the same GDI and GDI+ calls
against a memory DC: nothing is printed and no screen is read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from typing import Callable, Protocol, Sequence

from core.persian_text import unify_digits


MAX_COPIES = 99
MAX_RANGES = 20
MAX_PAGE_NUMBER = 9999
MAX_RENDERED_PAGES = 200
MAX_TEXT_BYTES = 4 * 1024 * 1024
PDF_RENDER_DPI = 200
PDF_TIMEOUT_SECONDS = 300.0

TEXT_EXTENSIONS = frozenset({".txt"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"})
PDF_EXTENSIONS = frozenset({".pdf"})
RENDERABLE_EXTENSIONS = TEXT_EXTENSIONS | IMAGE_EXTENSIONS | PDF_EXTENSIONS


class RenderError(RuntimeError):
    """The document could not be prepared; nothing was sent to the printer."""


# ------------------------------------------------------------------ options


@dataclass(frozen=True)
class PrintOptions:
    copies: int = 1
    color: str = "default"  # "default", "color" or "monochrome"
    pages: tuple[tuple[int, int], ...] = ()  # empty: every page

    def __post_init__(self) -> None:
        if not isinstance(self.copies, int) or isinstance(self.copies, bool) or not 1 <= self.copies <= MAX_COPIES:
            raise ValueError(f"Copies must be a whole number from 1 to {MAX_COPIES}.")
        if self.color not in {"default", "color", "monochrome"}:
            raise ValueError("Colour must be default, color or monochrome.")
        for start, end in self.pages:
            if not 1 <= start <= end <= MAX_PAGE_NUMBER:
                raise ValueError("Page ranges must be increasing page numbers.")

    @property
    def is_default(self) -> bool:
        return self.copies == 1 and self.color == "default" and not self.pages

    def wants(self, page: int) -> bool:
        return not self.pages or any(start <= page <= end for start, end in self.pages)

    @property
    def last_page(self) -> int | None:
        return max(end for _, end in self.pages) if self.pages else None

    def describe_pages(self) -> str:
        if not self.pages:
            return "all"
        return ",".join(str(s) if s == e else f"{s}-{e}" for s, e in self.pages)

    def to_plan(self) -> dict:
        return {"copies": self.copies, "color": self.color, "pages": self.describe_pages()}


_RANGE = re.compile(r"^\s*(\d{1,4})\s*(?:[-–]\s*(\d{1,4}))?\s*$")


def parse_page_ranges(spec: str) -> tuple[tuple[int, int], ...]:
    """'all' or '' -> every page; '1-3,5' -> ((1,3),(5,5)). Strict, bounded."""
    text = unify_digits(str(spec or "")).strip().casefold()
    if text in {"", "all", "همه", "همه صفحه‌ها", "همه صفحه ها"}:
        return ()
    parts = [part for part in re.split(r"[,،]", text)]
    if len(parts) > MAX_RANGES:
        raise ValueError(f"At most {MAX_RANGES} page ranges are allowed.")
    ranges: list[tuple[int, int]] = []
    for part in parts:
        match = _RANGE.match(part)
        if not match:
            raise ValueError("Page ranges look like 1-3,5.")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not 1 <= start <= end <= MAX_PAGE_NUMBER:
            raise ValueError("Page ranges must be increasing page numbers.")
        ranges.append((start, end))
    ranges.sort()
    for (_, previous_end), (start, _) in zip(ranges, ranges[1:]):
        if start <= previous_end:
            raise ValueError("Page ranges must not overlap.")
    return tuple(ranges)


def document_kind(path: Path) -> str | None:
    suffix = path.suffix.casefold()
    if suffix in TEXT_EXTENSIONS:
        return "text"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in PDF_EXTENSIONS:
        return "pdf"
    return None


# --------------------------------------------------------------- text layout


def paginate_text(
    text: str,
    *,
    measure: Callable[[str], int],
    max_width: int,
    lines_per_page: int,
) -> list[list[str]]:
    """Wrap text to the printable width and split it into pages of lines."""
    if max_width <= 0 or lines_per_page <= 0:
        raise RenderError("The printable area is too small.")
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        paragraph = paragraph.replace("\t", "    ")
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in re.split(r"(\s+)", paragraph):
            candidate = current + word
            if measure(candidate) <= max_width:
                current = candidate
                continue
            if current.strip():
                lines.append(current.rstrip())
                current = ""
                word = word.lstrip()
            # A word longer than a line is broken by characters.
            while word and measure(word) > max_width:
                cut = len(word)
                while cut > 1 and measure(word[:cut]) > max_width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        lines.append(current.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        lines = [""]
    return [lines[index:index + lines_per_page] for index in range(0, len(lines), lines_per_page)]


# ------------------------------------------------------------------- canvas


class Canvas(Protocol):
    width: int
    height: int
    dpi: int

    def start_page(self) -> None: ...
    def end_page(self) -> None: ...
    def measure(self, text: str) -> int: ...
    def line_height(self) -> int: ...
    def draw_text(self, x: int, y: int, text: str) -> None: ...
    def draw_image(self, path: Path) -> None: ...


@dataclass
class RenderReport:
    pages_drawn: int = 0
    page_numbers: list[int] = field(default_factory=list)


def render_document(
    kind: str,
    path: Path,
    canvas: Canvas,
    options: PrintOptions,
    *,
    pdf_pages: Sequence[tuple[int, Path]] = (),
    cancelled: Callable[[], bool] = lambda: False,
) -> RenderReport:
    """Draw the selected pages of one document onto the canvas."""
    report = RenderReport()

    def page(number: int, draw: Callable[[], None]) -> None:
        if cancelled():
            raise RenderError("Stopped while drawing pages.")
        canvas.start_page()
        draw()
        canvas.end_page()
        report.pages_drawn += 1
        report.page_numbers.append(number)

    if kind == "text":
        raw = path.read_bytes()
        if len(raw) > MAX_TEXT_BYTES:
            raise RenderError("The text file is too large to print.")
        text = _decode_text(raw)
        margin = canvas.dpi // 2
        height = canvas.line_height()
        pages = paginate_text(
            text,
            measure=canvas.measure,
            max_width=canvas.width - 2 * margin,
            lines_per_page=max(1, (canvas.height - 2 * margin) // height),
        )
        for number, lines in enumerate(pages, start=1):
            if options.wants(number):
                page(number, lambda lines=lines: [
                    canvas.draw_text(margin, margin + row * height, line) for row, line in enumerate(lines)
                ])
    elif kind == "image":
        if options.pages and not options.wants(1):
            raise RenderError("An image has one page.")
        page(1, lambda: canvas.draw_image(path))
    elif kind == "pdf":
        for number, image in pdf_pages:
            page(number, lambda image=image: canvas.draw_image(image))
    else:
        raise RenderError("This file type cannot be rendered.")
    if report.pages_drawn == 0:
        raise RenderError("None of the approved pages exist in this document.")
    if report.pages_drawn > MAX_RENDERED_PAGES:
        raise RenderError("Too many pages to print at once.")
    return report


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            if encoding == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1252", errors="replace")


# --------------------------------------------------------------------- GDI

if os.name == "nt":
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
    _gdiplus = ctypes.WinDLL("gdiplus", use_last_error=True)

HORZRES = 8
VERTRES = 10
LOGPIXELSY = 90
DM_COPIES = 0x00000100
DM_COLOR = 0x00000800
DM_COLLATE = 0x00008000
DM_OUT_BUFFER = 2
DM_IN_BUFFER = 8
DMCOLOR_MONOCHROME = 1
DMCOLOR_COLOR = 2
DMCOLLATE_TRUE = 1
# Offsets in DEVMODEW (wingdi.h): 32 WCHAR device name, then WORD x4,
# DWORD dmFields, the printer union (dmCopies is its 6th short), dmColor,
# dmDuplex, dmYResolution, dmTTOption, dmCollate.
_DM_FIELDS = 72
_DM_COPIES = 86
_DM_COLOR = 92
_DM_COLLATE = 100
TRANSPARENT = 1


class _DOCINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_int), ("lpszDocName", wintypes.LPCWSTR), ("lpszOutput", wintypes.LPCWSTR),
        ("lpszDatatype", wintypes.LPCWSTR), ("fwType", wintypes.DWORD),
    ]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL), ("SuppressExternalCodecs", wintypes.BOOL),
    ]


def _bind_gdi() -> None:
    g = _gdi32
    g.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
    g.CreateDCW.restype = wintypes.HDC
    g.DeleteDC.argtypes = [wintypes.HDC]
    g.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
    g.GetDeviceCaps.restype = ctypes.c_int
    g.StartDocW.argtypes = [wintypes.HDC, ctypes.POINTER(_DOCINFOW)]
    g.StartDocW.restype = ctypes.c_int
    for name in ("StartPage", "EndPage", "EndDoc", "AbortDoc"):
        getattr(g, name).argtypes = [wintypes.HDC]
        getattr(g, name).restype = ctypes.c_int
    g.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
    g.CreateFontW.restype = wintypes.HFONT
    g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g.SelectObject.restype = wintypes.HGDIOBJ
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    g.TextOutW.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int]
    g.TextOutW.restype = wintypes.BOOL
    g.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.SIZE)]
    g.GetTextExtentPoint32W.restype = wintypes.BOOL
    p = _gdiplus
    p.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(_GdiplusStartupInput), ctypes.c_void_p]
    p.GdiplusShutdown.argtypes = [ctypes.c_size_t]
    p.GdipLoadImageFromFile.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    p.GdipGetImageWidth.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    p.GdipGetImageHeight.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    p.GdipCreateFromHDC.argtypes = [wintypes.HDC, ctypes.POINTER(ctypes.c_void_p)]
    p.GdipDrawImageRectI.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4
    p.GdipDeleteGraphics.argtypes = [ctypes.c_void_p]
    p.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    w = _winspool
    w.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    w.OpenPrinterW.restype = wintypes.BOOL
    w.ClosePrinter.argtypes = [wintypes.HANDLE]
    w.DocumentPropertiesW.argtypes = [
        wintypes.HWND, wintypes.HANDLE, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    ]
    w.DocumentPropertiesW.restype = ctypes.c_long
    w.DeviceCapabilitiesW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.WORD, ctypes.c_void_p, ctypes.c_void_p]
    w.DeviceCapabilitiesW.restype = ctypes.c_int


if os.name == "nt":
    _bind_gdi()


class GdiCanvas:
    """GDI text and GDI+ images on one device context (printer or memory)."""

    def __init__(self, hdc: int, *, page_hooks: tuple[Callable[[], None], Callable[[], None]] | None = None) -> None:
        self.hdc = hdc
        self.width = _gdi32.GetDeviceCaps(hdc, HORZRES)
        self.height = _gdi32.GetDeviceCaps(hdc, VERTRES)
        self.dpi = max(72, _gdi32.GetDeviceCaps(hdc, LOGPIXELSY))
        self._hooks = page_hooks
        # 10 pt, a font with broad script coverage (Persian and Latin).
        size = -round(10 * self.dpi / 72)
        self._font = _gdi32.CreateFontW(size, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
        self._previous_font = _gdi32.SelectObject(hdc, self._font)
        _gdi32.SetBkMode(hdc, TRANSPARENT)
        self._gdiplus_token = ctypes.c_size_t()
        startup = _GdiplusStartupInput(1, None, False, False)
        if _gdiplus.GdiplusStartup(ctypes.byref(self._gdiplus_token), ctypes.byref(startup), None) != 0:
            raise RenderError("GDI+ could not start.")

    def start_page(self) -> None:
        if self._hooks:
            self._hooks[0]()

    def end_page(self) -> None:
        if self._hooks:
            self._hooks[1]()

    def measure(self, text: str) -> int:
        if not text:
            return 0
        size = wintypes.SIZE()
        if not _gdi32.GetTextExtentPoint32W(self.hdc, text, len(text), ctypes.byref(size)):
            raise RenderError("Text could not be measured.")
        return int(size.cx)

    def line_height(self) -> int:
        size = wintypes.SIZE()
        _gdi32.GetTextExtentPoint32W(self.hdc, "Ag", 2, ctypes.byref(size))
        return max(1, int(size.cy * 1.2))

    def draw_text(self, x: int, y: int, text: str) -> None:
        if text and not _gdi32.TextOutW(self.hdc, int(x), int(y), text, len(text)):
            raise RenderError("Text could not be drawn.")

    def draw_image(self, path: Path) -> None:
        image = ctypes.c_void_p()
        if _gdiplus.GdipLoadImageFromFile(str(path), ctypes.byref(image)) != 0:
            raise RenderError("The image could not be read.")
        graphics = ctypes.c_void_p()
        try:
            width, height = ctypes.c_uint(), ctypes.c_uint()
            _gdiplus.GdipGetImageWidth(image, ctypes.byref(width))
            _gdiplus.GdipGetImageHeight(image, ctypes.byref(height))
            if not width.value or not height.value:
                raise RenderError("The image is empty.")
            box_w, box_h = self.width, self.height
            scale = min(box_w / width.value, box_h / height.value)
            draw_w, draw_h = max(1, int(width.value * scale)), max(1, int(height.value * scale))
            left, top = (box_w - draw_w) // 2, (box_h - draw_h) // 2
            if _gdiplus.GdipCreateFromHDC(self.hdc, ctypes.byref(graphics)) != 0:
                raise RenderError("Drawing could not start.")
            if _gdiplus.GdipDrawImageRectI(graphics, image, left, top, draw_w, draw_h) != 0:
                raise RenderError("The image could not be drawn.")
        finally:
            if graphics.value:
                _gdiplus.GdipDeleteGraphics(graphics)
            _gdiplus.GdipDisposeImage(image)

    def close(self) -> None:
        if self._previous_font:
            _gdi32.SelectObject(self.hdc, self._previous_font)
            self._previous_font = None
        if self._font:
            _gdi32.DeleteObject(self._font)
            self._font = None
        if self._gdiplus_token.value:
            _gdiplus.GdiplusShutdown(self._gdiplus_token)
            self._gdiplus_token = ctypes.c_size_t()


def printer_capabilities(printer: str, port: str | None = None) -> tuple[int, bool]:
    """(maximum copies, colour device) from DeviceCapabilitiesW; read-only."""
    copies = _winspool.DeviceCapabilitiesW(printer, port, 18, None, None)  # DC_COPIES
    color = _winspool.DeviceCapabilitiesW(printer, port, 32, None, None)   # DC_COLORDEVICE
    if copies < 0 or color < 0:
        raise RenderError("The printer's capabilities could not be read.")
    return max(1, copies), color == 1


def job_devmode(printer: str, options: PrintOptions) -> ctypes.Array:
    """The driver's DEVMODE for this printer with only this job's options merged in."""
    handle = wintypes.HANDLE()
    if not _winspool.OpenPrinterW(printer, ctypes.byref(handle), None):
        raise RenderError("The printer could not be opened.")
    try:
        size = _winspool.DocumentPropertiesW(None, handle, printer, None, None, 0)
        if size <= 0:
            raise RenderError("The printer settings could not be read.")
        buffer = ctypes.create_string_buffer(size)
        if _winspool.DocumentPropertiesW(None, handle, printer, buffer, None, DM_OUT_BUFFER) < 0:
            raise RenderError("The printer settings could not be read.")
        apply_options_to_devmode(buffer, options)
        merged = ctypes.create_string_buffer(size)
        if _winspool.DocumentPropertiesW(None, handle, printer, merged, buffer, DM_IN_BUFFER | DM_OUT_BUFFER) < 0:
            raise RenderError("The printer refused these options.")
        return merged
    finally:
        _winspool.ClosePrinter(handle)


def _create_printer_dc(printer: str, devmode: ctypes.Array):
    """A device context on the real printer, with this job's DEVMODE."""
    return _gdi32.CreateDCW("WINSPOOL", printer, None, devmode)


def apply_options_to_devmode(buffer: ctypes.Array, options: PrintOptions) -> None:
    """Write copies, collation and colour into a DEVMODEW buffer (offsets from wingdi.h)."""
    fields = ctypes.c_uint32.from_buffer(buffer, _DM_FIELDS)
    ctypes.c_int16.from_buffer(buffer, _DM_COPIES).value = options.copies
    ctypes.c_int16.from_buffer(buffer, _DM_COLLATE).value = DMCOLLATE_TRUE
    fields.value |= DM_COPIES | DM_COLLATE
    if options.color != "default":
        ctypes.c_int16.from_buffer(buffer, _DM_COLOR).value = (
            DMCOLOR_COLOR if options.color == "color" else DMCOLOR_MONOCHROME
        )
        fields.value |= DM_COLOR


# ---------------------------------------------------------------- PDF pages


_PDF_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$methods = [System.WindowsRuntimeSystemExtensions].GetMethods()
$asTaskOf = $methods | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
$asTask = $methods | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } | Select-Object -First 1
function Await-Result($operation, [Type]$type) { $asTaskOf.MakeGenericMethod($type).Invoke($null, @($operation)).GetAwaiter().GetResult() }
function Await-Action($action) { $asTask.Invoke($null, @($action)).GetAwaiter().GetResult() }
$request = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__REQUEST__')) | ConvertFrom-Json
$file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($request.path)) ([Windows.Storage.StorageFile])
$pdf = Await-Result ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
$count = [int]$pdf.PageCount
$written = @()
if (-not $request.countOnly) {
    foreach ($number in $request.pages) {
        if ($number -lt 1 -or $number -gt $count) { continue }
        $page = $pdf.GetPage([uint32]($number - 1))
        $options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
        $options.DestinationWidth = [uint32][Math]::Min(6000, [Math]::Round($page.Size.Width * $request.dpi / 96))
        $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
        Await-Action ($page.RenderToStreamAsync($stream, $options))
        $target = Join-Path $request.folder ("page-{0:D5}.png" -f $number)
        $input = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream.GetInputStreamAt(0))
        $output = [System.IO.File]::Create($target)
        try { $input.CopyTo($output) } finally { $output.Dispose(); $input.Dispose(); $stream.Dispose(); $page.Dispose() }
        $written += ,@($number, $target)
    }
}
[ordered]@{ ok = $true; pageCount = $count; pages = $written } | ConvertTo-Json -Compress -Depth 4
"""


class PdfPages:
    """Rasterise approved PDF pages with Windows.Data.Pdf into a private folder."""

    def __init__(self, *, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self._run = run

    def _call(self, path: Path, *, pages: Sequence[int], folder: Path | None, count_only: bool) -> dict:
        import base64
        import json

        request = {
            "path": str(path), "pages": list(pages), "folder": str(folder or ""),
            "dpi": PDF_RENDER_DPI, "countOnly": count_only,
        }
        encoded = base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
        script = _PDF_SCRIPT.replace("__REQUEST__", encoded)
        try:
            finished = self._run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=PDF_TIMEOUT_SECONDS, encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RenderError(f"The PDF could not be rendered ({type(error).__name__}).") from None
        if finished.returncode != 0:
            raise RenderError("Windows could not read this PDF.")
        try:
            payload = json.loads((finished.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RenderError("The PDF renderer returned no result.") from None
        if payload.get("ok") is not True:
            raise RenderError("The PDF renderer reported a failure.")
        return payload

    def page_count(self, path: Path) -> int:
        count = int(self._call(path, pages=(), folder=None, count_only=True).get("pageCount", 0))
        if count <= 0:
            raise RenderError("The PDF has no pages.")
        return count

    def render(self, path: Path, pages: Sequence[int], folder: Path) -> list[tuple[int, Path]]:
        payload = self._call(path, pages=pages, folder=folder, count_only=False)
        rendered: list[tuple[int, Path]] = []
        for item in payload.get("pages") or []:
            number, target = int(item[0]), Path(str(item[1]))
            if target.parent.resolve() != folder.resolve() or not target.is_file():
                raise RenderError("The PDF renderer wrote outside its folder.")
            rendered.append((number, target))
        if len(rendered) != len(list(pages)):
            raise RenderError("Some approved PDF pages could not be rendered.")
        return rendered


def selected_pdf_pages(options: PrintOptions, page_count: int) -> list[int]:
    pages = [number for number in range(1, page_count + 1) if options.wants(number)]
    if not pages:
        raise RenderError("None of the approved pages exist in this PDF.")
    if len(pages) > MAX_RENDERED_PAGES:
        raise RenderError("Too many pages to print at once.")
    return pages


def remove_rendered_pages(folder: Path) -> None:
    """Delete only the page images this renderer writes, then the empty folder."""
    for item in folder.iterdir():
        if re.fullmatch(r"page-\d{5}\.png", item.name) and item.is_file() and not item.is_symlink():
            item.unlink()
    try:
        folder.rmdir()
    except OSError:
        pass  # something unexpected is inside: left alone, never deleted wholesale


# ------------------------------------------------------- submitter / handle


class RenderedJob:
    """A renderer job on a worker thread, polled like a handler process.

    ``job_id`` is set once StartDoc succeeded. ``not_submitted_reason`` is
    set when preparation failed before anything reached the spooler.
    """

    def __init__(self) -> None:
        self.job_id: int | None = None
        self.not_submitted_reason: str | None = None
        self.error: str | None = None
        self._done = threading.Event()

    def finished(self) -> bool:
        return self._done.is_set()

    def exit_code(self) -> int | None:
        if not self._done.is_set():
            return None
        return 0 if self.error is None and self.not_submitted_reason is None else 1

    def close(self) -> None:
        """Nothing to release: the drawing thread finishes on its own."""


class RendererSubmitter:
    """Print text, images and PDF pages with per-job options through GDI."""

    IDENTITY = "Qronos renderer (Windows GDI, Windows.Data.Pdf)"

    def __init__(self, *, pdf: PdfPages | None = None, temp_root: Path | None = None) -> None:
        if os.name != "nt":
            raise RenderError("The print renderer needs Windows.")
        self._pdf = pdf or PdfPages()
        self._temp_root = temp_root

    def handler_for(self, path: Path) -> str | None:
        return self.IDENTITY if document_kind(path) else None

    def page_count(self, path: Path) -> int | None:
        return self._pdf.page_count(path) if document_kind(path) == "pdf" else None

    def capabilities(self, printer: str, port: str | None = None) -> tuple[int, bool]:
        return printer_capabilities(printer, port)

    def start(self, path: Path, printer: str, options: PrintOptions | None = None) -> RenderedJob:
        job = RenderedJob()
        options = options or PrintOptions()
        thread = threading.Thread(
            target=self._render, args=(job, path, printer, options), name="qronos-print-render", daemon=True,
        )
        thread.start()
        return job

    def _render(self, job: RenderedJob, path: Path, printer: str, options: PrintOptions) -> None:
        folder = None
        hdc = None
        canvas = None
        started = False
        try:
            kind = document_kind(path)
            pdf_pages: list[tuple[int, Path]] = []
            if kind == "pdf":
                folder = Path(tempfile.mkdtemp(prefix="qronos-print-", dir=self._temp_root))
                wanted = selected_pdf_pages(options, self._pdf.page_count(path))
                pdf_pages = self._pdf.render(path, wanted, folder)
            devmode = job_devmode(printer, options)
            hdc = _create_printer_dc(printer, devmode)
            if not hdc:
                raise RenderError("The printer device could not be created.")
            info = _DOCINFOW(ctypes.sizeof(_DOCINFOW), path.name, None, None, 0)
            job_id = _gdi32.StartDocW(hdc, ctypes.byref(info))
            if job_id <= 0:
                raise RenderError("Windows did not accept the print job.")
            started = True
            job.job_id = int(job_id)

            def start_page() -> None:
                if _gdi32.StartPage(hdc) <= 0:
                    raise RenderError("A page could not be started.")

            def end_page() -> None:
                if _gdi32.EndPage(hdc) <= 0:
                    raise RenderError("A page could not be finished.")

            canvas = GdiCanvas(hdc, page_hooks=(start_page, end_page))
            render_document(kind, path, canvas, options, pdf_pages=pdf_pages)
            if _gdi32.EndDoc(hdc) <= 0:
                raise RenderError("The print job could not be finished.")
        except Exception as error:  # noqa: BLE001 - reported through the handle
            if started:
                if hdc:
                    _gdi32.AbortDoc(hdc)
                job.error = str(error)[:200] if isinstance(error, RenderError) else type(error).__name__
            else:
                job.not_submitted_reason = str(error)[:200] if isinstance(error, RenderError) else type(error).__name__
        finally:
            if canvas is not None:
                canvas.close()
            if hdc:
                _gdi32.DeleteDC(hdc)
            if folder is not None:
                # Rendered pages contain the document's content: always removed.
                remove_rendered_pages(folder)
            job._done.set()
