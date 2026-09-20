from __future__ import annotations

from typing import Protocol

from core.paint_adapter import PaintCanvasRegion
from core.screen_capture import Capture, ScreenCapture
from core.vision_grounding import (
    GroundedTarget,
    GroundingStale,
    GroundingUnavailable,
    revalidate_grounded_target,
)
from core.window_binding import WindowBinder, WindowBinding, WindowRect


class PaintCanvasGroundingError(RuntimeError):
    """Paint's drawing canvas could not be grounded safely."""


class _Grounder(Protocol):
    def locate(self, target: str, capture: Capture) -> GroundedTarget: ...


CANVAS_TARGET = (
    "the drawable white canvas/page area in Microsoft Paint where pencil strokes "
    "appear; return the bounding box of the actual drawing surface only, excluding "
    "the ribbon, toolbars, colour palette, rulers, scrollbars, status bar, window "
    "chrome, and grey workspace outside the white page"
)

MIN_CANVAS_WIDTH = 300
MIN_CANVAS_HEIGHT = 200


def ground_paint_canvas(
    binding: WindowBinding,
    binder: WindowBinder,
    capture: ScreenCapture,
    grounder: _Grounder,
    *,
    screen_approved: bool,
) -> PaintCanvasRegion:
    """Return a canvas capability only from fresh, revalidated Vision evidence."""
    if binding.app_id != "paint" or binding.snapshot.process_name != "mspaint.exe":
        raise PaintCanvasGroundingError("Paint canvas grounding requires a Paint binding.")

    frame = capture.capture_bound(
        binding,
        binder,
        approved=screen_approved,
        require_foreground=True,
        reason=(
            "Locate only the drawable Microsoft Paint canvas before any canvas input."
        ),
    )

    try:
        grounded = grounder.locate(CANVAS_TARGET, frame)
        revalidate_grounded_target(
            grounded,
            binding,
            binder,
            require_foreground=True,
        )
    except (GroundingUnavailable, GroundingStale) as exc:
        raise PaintCanvasGroundingError(
            f"Paint canvas grounding was not safely actionable: {exc}"
        ) from exc

    fresh = binder.revalidate(
        binding,
        require_foreground=True,
        require_same_layout=True,
    )
    rect = grounded.screen_box

    if not _rect_inside(fresh.client_rect, rect):
        raise PaintCanvasGroundingError(
            "Vision canvas bounds are outside the verified Paint client area."
        )
    if rect.width < MIN_CANVAS_WIDTH or rect.height < MIN_CANVAS_HEIGHT:
        raise PaintCanvasGroundingError(
            "Vision canvas bounds are too small to be accepted as the Paint drawing surface."
        )

    return PaintCanvasRegion.verified(binding, rect)


def _rect_inside(outer: WindowRect, inner: WindowRect) -> bool:
    return (
        inner.usable
        and outer.left <= inner.left
        and outer.top <= inner.top
        and inner.right <= outer.right
        and inner.bottom <= outer.bottom
    )
