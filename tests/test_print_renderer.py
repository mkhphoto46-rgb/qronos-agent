"""Print renderer: options, pagination, page selection and the real GDI/GDI+/PDF
drawing paths, exercised without any printer (memory DC, synthetic files)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import struct
import zlib

import pytest

from core import print_renderer as pr
from core.print_renderer import (
    PrintOptions,
    RenderError,
    paginate_text,
    parse_page_ranges,
    render_document,
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="GDI, GDI+ and Windows.Data.Pdf are Windows facilities.")


# ----------------------------------------------------------------- options


@pytest.mark.parametrize("spec, expected", [
    ("", ()), ("all", ()), ("همه", ()),
    ("1-3,5", ((1, 3), (5, 5))),
    ("۲-۴، ۷", ((2, 4), (7, 7))),
    ("5, 1-2", ((1, 2), (5, 5))),
])
def test_page_ranges_parse_strictly(spec, expected):
    assert parse_page_ranges(spec) == expected


@pytest.mark.parametrize("spec", ["0", "3-1", "1-3,2-4", "abc", "1;2", "1-99999", ",".join(["1"] * 21), "-3"])
def test_bad_page_ranges_are_refused(spec):
    with pytest.raises(ValueError):
        parse_page_ranges(spec)


@pytest.mark.parametrize("kwargs", [{"copies": 0}, {"copies": 100}, {"copies": True}, {"color": "sepia"},
                                    {"pages": ((3, 1),)}])
def test_invalid_options_are_refused(kwargs):
    with pytest.raises(ValueError):
        PrintOptions(**kwargs)


def test_options_describe_themselves_for_the_plan():
    options = PrintOptions(copies=2, color="monochrome", pages=((1, 3), (5, 5)))
    assert options.to_plan() == {"copies": 2, "color": "monochrome", "pages": "1-3,5"}
    assert not options.is_default and PrintOptions().is_default
    assert [options.wants(n) for n in range(1, 7)] == [True, True, True, False, True, False]


# -------------------------------------------------------------- pagination


def test_text_wraps_at_word_boundaries_and_splits_into_pages():
    pages = paginate_text("one two three four five six", measure=len, max_width=9, lines_per_page=2)
    assert pages == [["one two", "three"], ["four five", "six"]]


def test_a_word_longer_than_a_line_is_broken_and_blank_lines_kept():
    pages = paginate_text("abcdefghij\n\nxy", measure=len, max_width=4, lines_per_page=10)
    assert pages == [["abcd", "efgh", "ij", "", "xy"]]


class RecordingCanvas:
    """Test boundary standing in for a device context."""

    def __init__(self, width=1000, height=400, dpi=100, line=50):
        self.width, self.height, self.dpi, self._line = width, height, dpi, line
        self.events = []

    def start_page(self):
        self.events.append("start")

    def end_page(self):
        self.events.append("end")

    def measure(self, text):
        return len(text) * 10

    def line_height(self):
        return self._line

    def draw_text(self, x, y, text):
        self.events.append(("text", text))

    def draw_image(self, path):
        self.events.append(("image", Path(path).name))


def test_only_the_approved_text_pages_are_drawn(tmp_path):
    document = tmp_path / "notes.txt"
    document.write_text("\n".join(f"line {n}" for n in range(1, 13)), encoding="utf-8")
    canvas = RecordingCanvas()  # 400 px tall, half-inch (50 px) margins, 50 px lines -> 6 lines per page
    report = render_document("text", document, canvas, PrintOptions(pages=((2, 2),)))
    assert report.page_numbers == [2]
    assert [e[1] for e in canvas.events if isinstance(e, tuple)] == [f"line {n}" for n in range(7, 13)]
    assert canvas.events.count("start") == 1


def test_a_range_past_the_end_of_the_document_prints_nothing(tmp_path):
    document = tmp_path / "short.txt"
    document.write_text("only one page", encoding="utf-8")
    with pytest.raises(RenderError, match="None of the approved pages"):
        render_document("text", document, RecordingCanvas(), PrintOptions(pages=((3, 4),)))


def test_images_are_one_page_and_pdf_pages_follow_the_selection(tmp_path):
    canvas = RecordingCanvas()
    render_document("image", tmp_path / "a.png", canvas, PrintOptions())
    with pytest.raises(RenderError, match="one page"):
        render_document("image", tmp_path / "a.png", RecordingCanvas(), PrintOptions(pages=((2, 2),)))
    pdf_canvas = RecordingCanvas()
    report = render_document("pdf", tmp_path / "d.pdf", pdf_canvas, PrintOptions(),
                             pdf_pages=[(2, tmp_path / "page-00002.png"), (4, tmp_path / "page-00004.png")])
    assert report.page_numbers == [2, 4]


def test_cancellation_stops_between_pages(tmp_path):
    document = tmp_path / "long.txt"
    document.write_text("\n".join(str(n) for n in range(40)), encoding="utf-8")
    canvas = RecordingCanvas()
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(RenderError, match="Stopped"):
        render_document("text", document, canvas, PrintOptions(), cancelled=cancelled)
    assert canvas.events.count("start") == 1


def test_selected_pdf_pages_respect_options_and_bounds():
    assert pr.selected_pdf_pages(PrintOptions(pages=((2, 3), (9, 9))), 5) == [2, 3]
    with pytest.raises(RenderError):
        pr.selected_pdf_pages(PrintOptions(pages=((7, 8),)), 5)


# ----------------------------------------------------------------- DEVMODE


class DEVMODEW(ctypes.Structure):
    """Independent definition from the Windows SDK wingdi.h layout."""

    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD), ("dmOrientation", ctypes.c_short), ("dmPaperSize", ctypes.c_short),
        ("dmPaperLength", ctypes.c_short), ("dmPaperWidth", ctypes.c_short), ("dmScale", ctypes.c_short),
        ("dmCopies", ctypes.c_short), ("dmDefaultSource", ctypes.c_short), ("dmPrintQuality", ctypes.c_short),
        ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD), ("dmNup", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD), ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
    ]


@pytest.mark.parametrize("color, expected_color, color_bit", [
    ("default", 0, False), ("color", 2, True), ("monochrome", 1, True),
])
def test_job_options_land_in_the_right_devmode_fields(color, expected_color, color_bit):
    buffer = ctypes.create_string_buffer(ctypes.sizeof(DEVMODEW) + 64)  # driver-private tail
    pr.apply_options_to_devmode(buffer, PrintOptions(copies=3, color=color))
    view = DEVMODEW.from_buffer(buffer)
    assert view.dmCopies == 3 and view.dmCollate == 1
    assert view.dmColor == expected_color
    assert view.dmFields & 0x100 and view.dmFields & 0x8000
    assert bool(view.dmFields & 0x800) is color_bit
    assert view.dmOrientation == view.dmPaperSize == view.dmDuplex == 0, "nothing else is touched"


# ------------------------------------------------- GDI on a memory DC (no printer)


def _png(path: Path, width=4, height=3) -> Path:
    raw = b"".join(b"\x00" + bytes([200, 30, 30]) * width for _ in range(height))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


@pytest.fixture
def memory_canvas():
    gdi = ctypes.WinDLL("gdi32")
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    hdc = gdi.CreateCompatibleDC(None)
    bitmap = gdi.CreateCompatibleBitmap(hdc, 400, 300)
    previous = pr._gdi32.SelectObject(hdc, bitmap)
    pages = []
    canvas = pr.GdiCanvas(hdc, page_hooks=(lambda: pages.append("start"), lambda: pages.append("end")))
    yield canvas, pages
    canvas.close()
    pr._gdi32.SelectObject(hdc, previous)
    pr._gdi32.DeleteObject(bitmap)
    pr._gdi32.DeleteDC(hdc)


@windows_only
def test_gdi_canvas_measures_and_draws_persian_and_latin_text(memory_canvas):
    canvas, _ = memory_canvas
    assert canvas.measure("گزارش ماهانه") > 0 and canvas.measure("Monthly report") > 0
    assert canvas.measure("") == 0 and canvas.line_height() > 0
    canvas.draw_text(10, 10, "گزارش ماهانه — Monthly report")


@windows_only
def test_gdi_plus_draws_an_image_and_refuses_a_broken_one(memory_canvas, tmp_path):
    canvas, _ = memory_canvas
    canvas.draw_image(_png(tmp_path / "pixel.png"))
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    with pytest.raises(RenderError):
        canvas.draw_image(broken)


@windows_only
def test_rendering_a_text_document_through_gdi_produces_the_selected_pages(memory_canvas, tmp_path):
    canvas, pages = memory_canvas
    document = tmp_path / "long.txt"
    document.write_text("\n".join(f"سطر {n} line" for n in range(400)), encoding="utf-8")
    report = render_document("text", document, canvas, PrintOptions(pages=((1, 1), (3, 3))))
    assert report.page_numbers == [1, 3] and pages == ["start", "end", "start", "end"]


# -------------------------------------------------- PDF pages (Windows.Data.Pdf)


def _pdf(path: Path, pages: int) -> Path:
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               "<< /Type /Pages /Kids [" + " ".join(f"{3 + 2 * i} 0 R" for i in range(pages)) + f"] /Count {pages} >>"]
    for i in range(pages):
        content = f"0 0 1 rg {50 + 10 * i} 50 100 100 re f"
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents {4 + 2 * i} 0 R >>")
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("ascii")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode("ascii")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    path.write_bytes(bytes(out))
    return path


@windows_only
def test_windows_pdf_renderer_counts_and_rasterises_only_the_approved_pages(tmp_path):
    document = _pdf(tmp_path / "two pages.pdf", 2)
    pdf = pr.PdfPages()
    assert pdf.page_count(document) == 2
    folder = tmp_path / "render"
    folder.mkdir()
    rendered = pdf.render(document, [2], folder)
    assert [number for number, _ in rendered] == [2]
    assert rendered[0][1].read_bytes().startswith(b"\x89PNG")
    assert sorted(item.name for item in folder.iterdir()) == ["page-00002.png"]


def test_pdf_renderer_output_outside_its_folder_is_refused(tmp_path):
    import json
    import subprocess

    folder = tmp_path / "render"
    folder.mkdir()
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(b"x")

    def run(*args, **kwargs):
        payload = {"ok": True, "pageCount": 1, "pages": [[1, str(outside)]]}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    with pytest.raises(RenderError, match="outside"):
        pr.PdfPages(run=run).render(tmp_path / "x.pdf", [1], folder)


def test_cleanup_removes_only_rendered_pages(tmp_path):
    folder = tmp_path / "render"
    folder.mkdir()
    (folder / "page-00001.png").write_bytes(b"x")
    pr.remove_rendered_pages(folder)
    assert not folder.exists()
    folder.mkdir()
    (folder / "page-00001.png").write_bytes(b"x")
    (folder / "unexpected.txt").write_text("keep", encoding="utf-8")
    pr.remove_rendered_pages(folder)
    assert sorted(item.name for item in folder.iterdir()) == ["unexpected.txt"]


@windows_only
def test_a_failure_before_the_job_starts_is_reported_as_not_submitted(monkeypatch, tmp_path):
    monkeypatch.setattr(pr, "job_devmode", lambda printer, options: (_ for _ in ()).throw(RenderError("refused")))
    submitter = pr.RendererSubmitter()
    document = tmp_path / "a.txt"
    document.write_text("x", encoding="utf-8")
    job = submitter.start(document, "Office Laser", PrintOptions(copies=2))
    job._done.wait(10)
    assert job.finished() and job.job_id is None
    assert job.not_submitted_reason == "refused" and job.exit_code() == 1
