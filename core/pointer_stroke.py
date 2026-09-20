from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Iterable

from core.input_safety import BoundInputPoint
from core.mouse_button import MouseButton, MouseButtonController
from core.mouse_hover import MouseHoverController
from core.window_binding import WindowBinder, WindowBinding, WindowRect


@dataclass(frozen=True)
class PointerStrokeResult:
    points: tuple[BoundInputPoint, ...]
    button: MouseButton


class PointerStrokeController:
    """
    Reusable press/move/release capability for drawing and drag-style actions.

    This module knows nothing about Paint or about shapes.  Higher-level
    adapters provide a bounded sequence of screen points and an allowed rect.
    A final emergency mouse-up is sent if any revalidation/move step fails
    after mouse-down so Qronos cannot leave a synthetic button held.
    """

    def __init__(
        self,
        binder: WindowBinder,
        *,
        hover: MouseHoverController | None = None,
        buttons: MouseButtonController | None = None,
    ) -> None:
        self._hover = hover if hover is not None else MouseHoverController(binder)
        self._buttons = (
            buttons
            if buttons is not None
            else MouseButtonController(binder, hover=self._hover)
        )

    def stroke(
        self,
        binding: WindowBinding,
        points: Iterable[tuple[int, int]],
        *,
        approved: bool,
        button: MouseButton = MouseButton.LEFT,
        allowed_rect: WindowRect | None = None,
        step_delay_seconds: float = 0.0,
        max_step_pixels: float | None = None,
        press_settle_seconds: float = 0.0,
        release_settle_seconds: float = 0.0,
    ) -> PointerStrokeResult:
        path = tuple(points)
        if len(path) < 2:
            raise ValueError("A pointer stroke requires at least two points.")
        if step_delay_seconds < 0:
            raise ValueError("step_delay_seconds cannot be negative.")
        if press_settle_seconds < 0 or release_settle_seconds < 0:
            raise ValueError("stroke settle delays cannot be negative.")
        if max_step_pixels is not None:
            max_step_pixels = float(max_step_pixels)
            if not math.isfinite(max_step_pixels) or max_step_pixels <= 0:
                raise ValueError("max_step_pixels must be a finite positive number.")
            path = _densify_path(path, max_step_pixels=max_step_pixels)

        collected: list[BoundInputPoint] = []
        pressed = False
        last_x, last_y = path[0]

        try:
            down = self._buttons.down_at(
                binding,
                last_x,
                last_y,
                approved=approved,
                button=button,
                allowed_rect=allowed_rect,
            )
            collected.append(down.point)
            pressed = True
            if press_settle_seconds:
                time.sleep(press_settle_seconds)

            for x, y in path[1:]:
                moved = self._hover.hover(
                    binding,
                    x,
                    y,
                    approved=approved,
                    allowed_rect=allowed_rect,
                )
                collected.append(moved.point)
                last_x, last_y = x, y
                if step_delay_seconds:
                    time.sleep(step_delay_seconds)

            if release_settle_seconds:
                time.sleep(release_settle_seconds)

            self._buttons.up_at(
                binding,
                last_x,
                last_y,
                approved=approved,
                button=button,
                allowed_rect=allowed_rect,
            )
            pressed = False
        finally:
            if pressed:
                self._buttons.release(button)

        return PointerStrokeResult(points=tuple(collected), button=button)


def _densify_path(
    points: tuple[tuple[int, int], ...],
    *,
    max_step_pixels: float,
) -> tuple[tuple[int, int], ...]:
    """Insert intermediate pointer positions without changing stroke geometry."""

    if len(points) < 2:
        return points

    dense: list[tuple[int, int]] = [points[0]]
    for start, end in zip(points, points[1:]):
        x1, y1 = start
        x2, y2 = end
        distance = math.hypot(x2 - x1, y2 - y1)
        steps = max(1, int(math.ceil(distance / max_step_pixels)))
        for index in range(1, steps + 1):
            t = index / steps
            point = (
                round(x1 + (x2 - x1) * t),
                round(y1 + (y2 - y1) * t),
            )
            if point != dense[-1]:
                dense.append(point)
    return tuple(dense)
