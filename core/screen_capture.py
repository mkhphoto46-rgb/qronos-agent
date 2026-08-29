"""
Taking a picture of the screen, once, after somebody has said yes.

This is the first thing in Qronos that acts on the machine rather than
describing it, which makes it the first entry in ``EXECUTOR_MODULES`` and the
change that stops ``tests/test_gate_discipline.py`` being vacuous. Every
capture goes through :mod:`security.gate` and there is no path here that does
not.

Windows GDI through ``ctypes``, following ``core/whisper_cpp_vad_runtime.py``'s
precedent rather than adding a screenshot dependency. Three things about it are
worth knowing.

**The process stays DPI-unaware, on purpose.** This display is 3840x2160 at
125% scaling. A DPI-aware process sees all 3840 pixels; an unaware one is
handed 3072x1728, scaled down by Windows before it arrives. That is fewer
pixels to copy, for free, and the text in them is no less legible because the
scaling factor is exactly what made them larger in the first place. On a 4K
laptop at 200% it is 1080p for nothing. Qronos never calls
``SetProcessDpiAwareness``, so this is what it gets — but the code reads both
numbers and records them, because "we got fewer pixels than the panel has" is
otherwise indistinguishable from a bug.

**The foreground window is read at the moment the hotkey fires**, not when the
model is ready for it. Push-to-talk is a global shortcut: focus has not moved
yet, so at that instant the window the user is looking at is still the
foreground one. A second later Qronos's own window may be it, and "read this
window" would read Qronos.

**One window is captured from the screen, not from the window.** The obvious
way — ``GetDC(hwnd)`` and copy — returns a rectangle of solid black for any
application that draws through the GPU, which today is the browser, the
terminal, the editor and most of what anybody would ask Qronos to read.
Measured on this machine: one distinct colour from the window's own device
context, more than a hundred thousand from the screen's at the same
coordinates. So the window's rectangle is looked up and that region of the
composited screen is copied instead. It works for every renderer, and it is
correct because the window being asked about is the foreground one, which is
on top by definition.

**Nothing is written to disk.** The bitmap goes from GDI into memory, is
encoded to PNG in memory, and is handed on as bytes. There is no temporary file
to clean up, no janitor to trust, and nothing left behind if the process is
killed halfway. That is the retention policy: there is no retention.

On anything that is not Windows the module imports, reports itself
unavailable, and refuses — so the tests, and the rest of Qronos, run on Ubuntu
unchanged.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Callable

from dataclasses import replace

from core.actions import ActionRequest
from core.vision_image import PreparedImage, prepare_bytes
from security.gate import AuditSink, evaluate
from security.permissions import ActionCategory


#: ``GetDeviceCaps`` indices. 118 is the panel's real width in pixels;
#: 8 is what this process is being shown. They differ exactly by the
#: display's scaling factor.
DESKTOPHORZRES = 118
DESKTOPVERTRES = 117
HORZRES = 8
VERTRES = 10

#: ``BitBlt``: copy the source rectangle as it is.
SRCCOPY = 0x00CC0020

#: ``GetDIBits``: 32 bits per pixel, no compression, top-down rows.
BI_RGB = 0
DIB_RGB_COLORS = 0


class CaptureUnavailable(RuntimeError):
    """The screen cannot be captured on this machine, and why."""


class CaptureRefused(RuntimeError):
    """The screen could have been captured and was not permitted to be."""


@dataclass(frozen=True)
class DisplayGeometry:
    """
    What the screen is, and what this process is being shown of it.

    Both numbers, because they differ on any scaled display and a capture
    smaller than the panel is otherwise indistinguishable from a bug.
    """

    #: What the panel actually has.
    physical_width: int
    physical_height: int

    #: What this process is handed, after Windows has scaled for DPI.
    logical_width: int
    logical_height: int

    @property
    def scaling(self) -> float:
        """125% comes back as 1.25. One when nothing is scaled."""
        if not self.logical_width:
            return 1.0

        return self.physical_width / self.logical_width

    def describe(self) -> str:
        if self.scaling <= 1.0:
            return f"{self.physical_width}x{self.physical_height}"

        return (
            f"{self.logical_width}x{self.logical_height} "
            f"(a {self.physical_width}x{self.physical_height} display at "
            f"{self.scaling:.0%})"
        )


@dataclass(frozen=True)
class Capture:
    """One picture of the screen, and what it was a picture of."""

    image: PreparedImage
    geometry: DisplayGeometry

    #: The window handle this was a picture of, or None for the whole screen.
    #: Kept as a number because it is only ever compared and logged.
    window: int | None = None

    #: True when almost every pixel is the same colour.
    #:
    #: Worth reporting rather than hiding. It is what a locked screen, a
    #: display that has gone to sleep, or protected video looks like, and it is
    #: also what a broken capture looks like — and a model shown a black
    #: rectangle will describe a black rectangle at some length rather than
    #: saying anything useful.
    blank: bool = False

    def describe(self) -> str:
        what = "the screen" if self.window is None else "one window"
        blank = ", and it is blank" if self.blank else ""

        return (
            f"A picture of {what}: {self.geometry.describe()}, "
            f"sent as {self.image.describe()}{blank}"
        )


def available() -> bool:
    """
    True when this machine can be captured from at all.

    Not a capture, and it asks nobody for permission: it is called to decide
    whether to *offer* the capability, including on paths where the answer is
    no and nothing should have happened.
    """
    return sys.platform == "win32" and hasattr(ctypes, "windll")


def foreground_window() -> int | None:
    """
    The window the user is looking at, right now.

    Call this at the instant the hotkey fires. A moment later Qronos may have
    focus itself, and "read this window" would read Qronos.
    """
    if not available():
        return None

    handle = ctypes.windll.user32.GetForegroundWindow()

    return int(handle) or None


def geometry() -> DisplayGeometry:
    """What the display is, without capturing anything from it."""
    if not available():
        raise CaptureUnavailable(
            "Qronos can only look at the screen on Windows."
        )

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    screen_dc = user32.GetDC(None)

    if not screen_dc:
        raise CaptureUnavailable("Windows would not hand over the screen.")

    try:
        return DisplayGeometry(
            physical_width=gdi32.GetDeviceCaps(screen_dc, DESKTOPHORZRES),
            physical_height=gdi32.GetDeviceCaps(screen_dc, DESKTOPVERTRES),
            logical_width=gdi32.GetDeviceCaps(screen_dc, HORZRES),
            logical_height=gdi32.GetDeviceCaps(screen_dc, VERTRES),
        )
    finally:
        user32.ReleaseDC(None, screen_dc)


class ScreenCapture:
    """
    Takes a picture of the screen, when allowed to.

    ``approved`` is the person's answer to the confirmation the gate asks for,
    and it is a required piece of information rather than an optional one: the
    default is no, so a caller that forgets it gets a refusal, which is the
    failure that costs nothing.

    ``grab`` exists so that everything except the twenty lines of GDI can be
    tested on a machine with no screen — including on Ubuntu, where the twenty
    lines cannot run at all.
    """

    def __init__(
        self,
        grab: Callable[[int | None], tuple[bytes, int, int]] | None = None,
        geometry_fn: Callable[[], DisplayGeometry] | None = None,
        audit: AuditSink | None = None,
        read_text: Callable[[bytes], str] | None = None,
    ) -> None:
        self._grab = grab if grab is not None else _grab_with_gdi
        self._geometry = geometry_fn if geometry_fn is not None else geometry
        self._audit = audit

        # Optional, and it runs here rather than later because here is the only
        # place the full-resolution pixels exist. By the time the picture
        # reaches a model it has been shrunk to a 1280-pixel long edge, and
        # reading it at full size is the entire reason the hint is worth
        # having. See core/windows_ocr.py for the measurement.
        self._read_text = read_text

    def health_check(self) -> bool:
        """True when a capture could be attempted. Starts nothing."""
        return self._grab is not _grab_with_gdi or available()

    def capture(
        self,
        approved: bool = False,
        window: int | None = None,
        reason: str = "Look at what is on the screen.",
    ) -> Capture:
        """
        One picture, now, of the screen or of one window.

        Raises rather than returning a failure value, because there is no
        useful half-answer: a caller that could not capture has nothing to
        show a model.
        """
        request = ActionRequest(
            category=ActionCategory.READ_SCREEN,
            target="the whole screen" if window is None else "one window",
            summary=reason,
            parameters={"window": int(window) if window else 0},
        )

        verdict = evaluate(request, audit=self._audit)

        if verdict.refused:
            raise CaptureRefused(verdict.reason)

        if verdict.needs_approval and not approved:
            raise CaptureRefused(
                f"{verdict.reason} Nothing was captured."
            )

        shape = self._geometry()
        raw, width, height = self._grab(window)

        if not raw or width <= 0 or height <= 0:
            # A minimised or closed window hands back an empty bitmap rather
            # than failing, so this is the normal case and not a crash.
            raise CaptureUnavailable(
                "There was nothing to capture — the window may be minimised "
                "or may have closed."
            )

        encoded = _encode_png(raw, width, height)
        image = prepare_bytes(encoded)
        blank = _looks_blank(image.data)

        if self._read_text is not None and not blank:
            try:
                hint = self._read_text(encoded)
            except Exception:
                # A hint that throws is worse than a hint that is missing. The
                # picture is going to the model either way.
                hint = ""

            if hint:
                image = replace(image, hint=hint)

        return Capture(
            image=image,
            geometry=shape,
            window=window,
            blank=blank,
        )


#: How many distinct colours a picture of something has. A real screen has
#: thousands; a solid rectangle has one, and a solid rectangle with a cursor
#: on it has a handful.
BLANK_COLOUR_LIMIT = 8


def _looks_blank(png: bytes) -> bool:
    """
    Whether the picture is a flat rectangle rather than a picture of anything.

    Counted on the already-shrunk image, which is a fixed small size, so the
    cost does not depend on the size of the display.
    """
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(png)) as image:
        colours = image.convert("RGB").getcolors(maxcolors=BLANK_COLOUR_LIMIT)

    # getcolors returns None once there are more colours than the limit, which
    # is the ordinary case and the one that means "this is a picture".
    return colours is not None


def _encode_png(raw: bytes, width: int, height: int) -> bytes:
    """
    The raw bitmap as a PNG, in memory.

    GDI hands back BGRA with the alpha channel unset — every byte zero — so it
    is dropped rather than trusted. Keeping it would make the whole picture
    fully transparent, which encodes and sends perfectly well and arrives at
    the model as nothing at all.
    """
    from io import BytesIO

    from PIL import Image

    image = Image.frombuffer(
        "RGBA", (width, height), raw, "raw", "BGRA", 0, 1
    ).convert("RGB")

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=False)

    return buffer.getvalue()


def _grab_with_gdi(window: int | None) -> tuple[bytes, int, int]:
    """
    Copy the pixels, through GDI, releasing everything on every path.

    Each of these handles is a process-wide resource that Windows does not
    reclaim until the process exits. A capture that leaks one leaks it on every
    press of the hotkey, and the symptom appears an hour later somewhere else.
    """
    if not available():
        raise CaptureUnavailable(
            "Qronos can only look at the screen on Windows."
        )

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    # Always the screen's device context, even for one window. See the module
    # docstring: a window's own context is black for anything drawn on the GPU.
    source_dc = user32.GetDC(None)

    if not source_dc:
        raise CaptureUnavailable("Windows would not hand over the screen.")

    memory_dc = None
    bitmap = None

    try:
        screen_width = gdi32.GetDeviceCaps(source_dc, HORZRES)
        screen_height = gdi32.GetDeviceCaps(source_dc, VERTRES)

        if window:
            left, top, width, height = _window_region(
                window, screen_width, screen_height
            )
        else:
            left, top = 0, 0
            width, height = screen_width, screen_height

        if width <= 0 or height <= 0:
            return b"", 0, 0

        memory_dc = gdi32.CreateCompatibleDC(source_dc)

        if not memory_dc:
            raise CaptureUnavailable("Windows would not make room for a copy.")

        bitmap = gdi32.CreateCompatibleBitmap(source_dc, width, height)

        if not bitmap:
            raise CaptureUnavailable("Windows would not make room for a copy.")

        gdi32.SelectObject(memory_dc, bitmap)

        if not gdi32.BitBlt(
            memory_dc, 0, 0, width, height, source_dc, left, top, SRCCOPY
        ):
            raise CaptureUnavailable("The screen could not be copied.")

        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        # Negative height asks for top-down rows. Bitmaps are stored bottom-up
        # by default, and a picture of the screen upside down is a picture of
        # nothing useful.
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB

        info = _BITMAPINFO()
        info.bmiHeader = header

        buffer = ctypes.create_string_buffer(width * height * 4)

        copied = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            buffer,
            ctypes.byref(info),
            DIB_RGB_COLORS,
        )

        if copied != height:
            raise CaptureUnavailable(
                "Only part of the screen could be read back."
            )

        return buffer.raw, width, height

    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)

        if memory_dc:
            gdi32.DeleteDC(memory_dc)

        user32.ReleaseDC(None, source_dc)


def _window_region(
    window: int,
    screen_width: int,
    screen_height: int,
) -> tuple[int, int, int, int]:
    """
    Where on the screen that window is, clipped to the screen.

    Clipped because a window can be dragged half off the edge, and copying
    from outside the screen's own bitmap gives whatever happens to be there —
    usually black, sometimes the last thing that was.
    """
    user32 = ctypes.windll.user32
    rect = _RECT()

    if not user32.GetWindowRect(window, ctypes.byref(rect)):
        raise CaptureUnavailable(
            "Windows would not say where that window is."
        )

    if user32.IsIconic(window):
        # A minimised window has a rectangle, off-screen, and no pixels.
        return 0, 0, 0, 0

    left = max(0, rect.left)
    top = max(0, rect.top)
    right = min(screen_width, rect.right)
    bottom = min(screen_height, rect.bottom)

    return left, top, max(0, right - left), max(0, bottom - top)


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", ctypes.c_uint32 * 3),
    ]
