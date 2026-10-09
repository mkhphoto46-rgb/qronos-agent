"""Fresh-frame verification of the Paint canvas before every stroke (D3/D4).

A canvas rectangle grounded once is not enough to draw a whole picture: the
user could scroll the page, a dialog could open over it, Paint could show a
panel that moves the page. None of that changes the window's identity,
geometry or focus. So before each stroke the exact Paint window is captured
again, permission-gated, and two things must hold:

* the band of pixels just outside the canvas looks exactly as it did when
  drawing started (the page has not moved, nothing covers its edge);
* inside the canvas, nothing changed except along the strokes Qronos itself
  already drew (a corridor of 3 native pixels on each side of them);
* the verified tool, colour and size indicators look exactly as they did when
  drawing started (a tool switched to the eraser or fill, or another colour,
  stops the drawing before the next stroke).

Before the first stroke the canvas must also still look as it did in the
frame it was grounded from. Any difference stops the drawing between strokes,
with the button up, and the result is reported as a failure, never success.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from core.grounding_freshness import (
    CANVAS_MAX_CHANGED_NATIVE_PIXELS,
    MIN_CHANGED_PIXELS,
    FreshnessExpired,
    _rgb_region,
    changed_mask,
    require_region_unchanged,
)
from core.window_binding import WindowBinder, WindowBinding, WindowRect

#: Width of the band outside the canvas that must not change.
RING_PIXELS = 10
#: Half-width, in native pixels, of the corridor around Qronos's own strokes
#: where change is expected: the minimum Pencil width plus one pixel either
#: side. An overlay lying wholly inside that corridor is a stated limit.
CORRIDOR_RADIUS = 3
#: Margin around each watched tool indicator box.
TOOL_MARGIN_PIXELS = 2


class CanvasChanged(RuntimeError):
    """The canvas or its surroundings changed in a way Qronos did not cause."""


class PaintCanvasWatch:
    def __init__(
        self,
        capture_source: Any,
        binder: WindowBinder,
        binding: WindowBinding,
        canvas: Any,
        *,
        screen_approved: bool,
        tool_regions: tuple[WindowRect, ...] = (),
    ) -> None:
        self._capture = capture_source
        self._binder = binder
        self._binding = binding
        self._canvas = canvas
        self._approved = bool(screen_approved)
        client = binding.snapshot.client_rect
        rect = canvas.rect
        self._ring_outer = WindowRect(
            max(client.left, rect.left - RING_PIXELS),
            max(client.top, rect.top - RING_PIXELS),
            min(client.right, rect.right + RING_PIXELS),
            min(client.bottom, rect.bottom + RING_PIXELS),
        )
        self._tool_rects = tuple(
            WindowRect(
                max(client.left, r.left - TOOL_MARGIN_PIXELS),
                max(client.top, r.top - TOOL_MARGIN_PIXELS),
                min(client.right, r.right + TOOL_MARGIN_PIXELS),
                min(client.bottom, r.bottom + TOOL_MARGIN_PIXELS),
            )
            for r in tool_regions
        )
        self._baseline_ring = None
        self._ring_mask = None
        self._baseline_canvas = None
        self._baseline_tools: list[np.ndarray] = []
        self._drawn: list[tuple[tuple[int, int], ...]] = []
        self.frames_checked = 0

    def _frame(self, why: str):
        frame = self._capture.capture_bound(
            self._binding,
            self._binder,
            approved=self._approved,
            require_foreground=True,
            reason=why,
        )
        if frame.binding_token != self._binding.identity_token or frame.layout_token != self._binding.layout_token:
            raise CanvasChanged("The verification frame belongs to a different Paint window or layout.")
        self.frames_checked += 1
        return frame

    def start(self) -> None:
        """Baseline the canvas, its band and the tool indicators; the canvas must match its grounding frame."""
        frame = self._frame("Confirm the grounded Paint canvas immediately before drawing.")
        fingerprint = getattr(self._canvas, "fingerprint", None)
        if fingerprint is None:
            raise CanvasChanged("The canvas carries no pixel evidence from its grounding frame.")
        try:
            require_region_unchanged(
                fingerprint, frame, what="The Paint canvas", max_native_pixels=CANVAS_MAX_CHANGED_NATIVE_PIXELS
            )
            self._baseline_ring = _rgb_region(frame, self._ring_outer)
            self._baseline_canvas = _rgb_region(frame, self._canvas.rect)
            self._baseline_tools = [_rgb_region(frame, rect) for rect in self._tool_rects]
        except FreshnessExpired as exc:
            raise CanvasChanged(str(exc)) from exc
        self._frame_scale = (frame.image.width / frame.frame_rect.width,
                             frame.image.height / frame.frame_rect.height)
        self._frame_origin = (frame.frame_rect.left, frame.frame_rect.top)
        self._ring_mask = self._inner_mask()

    def stroke_drawn(self, points: tuple[tuple[int, int], ...]) -> None:
        self._drawn.append(tuple((int(x), int(y)) for x, y in points))

    def before_stroke(self) -> None:
        """A fresh frame must show only Qronos's own strokes as changes, and the same tools."""
        if self._baseline_canvas is None:
            raise CanvasChanged("Canvas verification was not started.")
        frame = self._frame("Verify the Paint canvas and tools have not changed before the next stroke.")
        try:
            ring = _rgb_region(frame, self._ring_outer)
            canvas = _rgb_region(frame, self._canvas.rect)
            tools = [_rgb_region(frame, rect) for rect in self._tool_rects]
        except FreshnessExpired as exc:
            raise CanvasChanged(str(exc)) from exc
        if ring.shape != self._baseline_ring.shape or canvas.shape != self._baseline_canvas.shape \
                or any(now.shape != then.shape for now, then in zip(tools, self._baseline_tools)):
            raise CanvasChanged("The Paint frame changed size during drawing.")

        limit = max(float(MIN_CHANGED_PIXELS),
                    CANVAS_MAX_CHANGED_NATIVE_PIXELS * self._frame_scale[0] * self._frame_scale[1])
        ring_changed = changed_mask(self._baseline_ring, ring) & ~self._ring_mask
        _refuse_if_changed(int(ring_changed.sum()), limit, "The area around the Paint canvas")

        expected = self._corridor(canvas.shape[:2])
        unexpected = changed_mask(self._baseline_canvas, canvas) & ~expected
        _refuse_if_changed(int(unexpected.sum()), limit, "The Paint canvas outside Qronos's own strokes")

        for now, then in zip(tools, self._baseline_tools):
            changed = int(changed_mask(then, now).sum())
            if changed > MIN_CHANGED_PIXELS:
                raise CanvasChanged(
                    f"The selected Paint tool, colour or size changed during drawing ({changed} pixels); "
                    "drawing stopped before the next stroke."
                )

    # -------------------------------------------------------------- helpers

    def _to_crop(self, x: int, y: int, rect: WindowRect) -> tuple[float, float]:
        sx, sy = self._frame_scale
        ox, oy = self._frame_origin
        return (
            (x - ox) * sx - math.floor((rect.left - ox) * sx),
            (y - oy) * sy - math.floor((rect.top - oy) * sy),
        )

    def _inner_mask(self) -> np.ndarray:
        """True where the canvas itself lies inside the ring crop."""
        height, width = self._baseline_ring.shape[:2]
        mask = Image.new("1", (width, height), 0)
        left, top = self._to_crop(self._canvas.rect.left, self._canvas.rect.top, self._ring_outer)
        right, bottom = self._to_crop(self._canvas.rect.right, self._canvas.rect.bottom, self._ring_outer)
        ImageDraw.Draw(mask).rectangle((left, top, right - 1, bottom - 1), fill=1)
        return np.asarray(mask, dtype=bool)

    def _corridor(self, shape) -> np.ndarray:
        height, width = shape
        mask = Image.new("1", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        # At least 2 model pixels: resampling of a scaled-down frame spreads a
        # 1-pixel line over about two model pixels.
        radius = max(2, round(CORRIDOR_RADIUS * max(self._frame_scale)))
        for stroke in self._drawn:
            path = [self._to_crop(x, y, self._canvas.rect) for x, y in stroke]
            if len(path) == 1:
                path = path * 2
            draw.line(path, fill=1, width=radius * 2 + 1, joint="curve")
            for px, py in path:
                draw.ellipse((px - radius, py - radius, px + radius, py + radius), fill=1)
        return np.asarray(mask, dtype=bool)


def _refuse_if_changed(changed: int, limit: float, what: str) -> None:
    if changed > limit:
        raise CanvasChanged(
            f"{what} changed during drawing ({changed} pixels) — something other than "
            "Qronos changed Paint, so drawing stopped."
        )
