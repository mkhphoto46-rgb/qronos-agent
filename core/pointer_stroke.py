from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable, Iterable

from core.action_cancellation import CancellationToken, checkpoint
from core.input_safety import BoundInputAuthorization, BoundInputPoint
from core.mouse_button import MouseButton, MouseButtonController
from core.mouse_hover import MouseHoverController
from core.window_binding import WindowBinder, WindowBinding, WindowRect


SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class PointerStrokeResult:
    points: tuple[BoundInputPoint, ...]
    button: MouseButton


class StrokeProgress:
    """
    Caller-owned record of what one stroke actually delivered.

    Exceptions from ``stroke`` propagate unchanged (callers rely on their
    types); this object tells the caller what side effect exists afterwards.

    ``pressed``: a native mouse-down reached Windows, so the target may hold
    a (partial) mark. ``released``: the matching mouse-up was delivered.
    ``clean_release``: the up was delivered while the exact bound window was
    still the unchanged foreground target, so the mark was committed to the
    intended window and nowhere else. When False after a press, the release
    still happened but Qronos cannot prove where it landed.
    """

    __slots__ = ("pressed", "released", "clean_release", "points")

    def __init__(self) -> None:
        self.pressed = False
        self.released = False
        self.clean_release = False
        self.points: tuple[BoundInputPoint, ...] = ()


class PointerStrokeController:
    """
    Reusable press/move/release capability for drawing and drag-style actions.

    This module knows nothing about Paint or about shapes.  Higher-level
    adapters provide a bounded sequence of screen points and an allowed rect.

    Release guarantee: once a mouse-down has been delivered, every exit path -
    success, cancellation, revalidation failure, expired authorization or an
    unexpected exception - delivers a mouse-up before control leaves
    ``stroke``. Cancellation is cooperative and checked before every pointer
    step, so Stop ends a stroke within one step instead of after the shape.
    """

    def __init__(
        self,
        binder: WindowBinder,
        *,
        hover: MouseHoverController | None = None,
        buttons: MouseButtonController | None = None,
        sleep_fn: SleepFn | None = None,
    ) -> None:
        self._binder = binder
        self._hover = hover if hover is not None else MouseHoverController(binder)
        self._buttons = (
            buttons
            if buttons is not None
            else MouseButtonController(binder, hover=self._hover)
        )
        self._sleep_fn = sleep_fn

    def stroke(
        self,
        binding: WindowBinding,
        points: Iterable[tuple[int, int]],
        *,
        authorization: BoundInputAuthorization,
        button: MouseButton = MouseButton.LEFT,
        allowed_rect: WindowRect | None = None,
        step_delay_seconds: float = 0.0,
        max_step_pixels: float | None = None,
        press_settle_seconds: float = 0.0,
        release_settle_seconds: float = 0.0,
        cancel_token: CancellationToken | None = None,
        progress: StrokeProgress | None = None,
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

        # Nothing has been sent yet: a cancelled token stops with no side effect.
        checkpoint(cancel_token)

        state = progress if progress is not None else StrokeProgress()
        collected: list[BoundInputPoint] = []
        pressed = False
        last_x, last_y = path[0]
        presses_before = self._presses_delivered()

        try:
            down = self._buttons.down_at(
                binding,
                last_x,
                last_y,
                authorization=authorization,
                button=button,
                allowed_rect=allowed_rect,
            )
            collected.append(down.point)
            pressed = True
            state.pressed = True
            self._pause(press_settle_seconds, cancel_token)

            for x, y in path[1:]:
                checkpoint(cancel_token)
                moved = self._hover.hover(
                    binding,
                    x,
                    y,
                    authorization=authorization,
                    allowed_rect=allowed_rect,
                )
                collected.append(moved.point)
                last_x, last_y = x, y
                self._pause(step_delay_seconds, cancel_token)

            self._pause(release_settle_seconds, cancel_token)

            self._buttons.up_at(
                binding,
                last_x,
                last_y,
                authorization=authorization,
                button=button,
                allowed_rect=allowed_rect,
            )
            pressed = False
            state.released = True
            state.clean_release = True
        except BaseException:
            state.points = tuple(collected)
            if not pressed and self._held(button) is not True:
                presses_after = self._presses_delivered()
                if (
                    presses_before is not None
                    and presses_after is not None
                    and presses_after > presses_before
                ):
                    # down_at delivered a press, its post-check failed and it
                    # released atomically. A dot may exist; where is unproven.
                    state.pressed = True
                    state.released = True
                    state.clean_release = False
                raise
            state.pressed = True
            state.clean_release = self._release_after_interruption(
                binding,
                last_x,
                last_y,
                authorization=authorization,
                button=button,
                allowed_rect=allowed_rect,
            )
            state.released = True
            raise

        state.points = tuple(collected)
        return PointerStrokeResult(points=tuple(collected), button=button)

    def _pause(self, seconds: float, token: CancellationToken | None) -> None:
        if not seconds:
            return
        if token is not None:
            token.sleep(seconds)
        elif self._sleep_fn is not None:
            self._sleep_fn(seconds)
        else:
            time.sleep(seconds)

    def _presses_delivered(self) -> int | None:
        value = getattr(self._buttons, "presses_delivered", None)
        return int(value) if isinstance(value, int) else None

    def _held(self, button: MouseButton) -> bool | None:
        """Tracked button state, or None for doubles that do not track it."""
        is_held = getattr(self._buttons, "is_held", None)
        return bool(is_held(button)) if callable(is_held) else None

    def _release_after_interruption(
        self,
        binding: WindowBinding,
        x: int,
        y: int,
        *,
        authorization: BoundInputAuthorization,
        button: MouseButton,
        allowed_rect: WindowRect | None,
    ) -> bool:
        """
        Deliver the mouse-up for a stroke that will not complete.

        1. Preferred: a fully guarded ``up_at`` at the last delivered point.
        2. Authorization expired but the exact window is still the unchanged
           foreground target: release now; the up lands where the down did.
        3. Otherwise: unguarded emergency release. Never leave a button held.

        Returns True for cases 1 and 2 (the release provably ended the stroke
        in the intended window).
        """
        if self._held(button) is False:
            # A native up was already delivered (an up_at whose post-check
            # failed): the window changed around the release, so where the
            # stroke ended is unproven.
            return False

        try:
            self._buttons.up_at(
                binding,
                x,
                y,
                authorization=authorization,
                button=button,
                allowed_rect=allowed_rect,
            )
            if self._held(button) is not True:
                return True
        except BaseException:
            pass

        if self._held(button) is False:
            return False

        window_unchanged = False
        try:
            self._binder.revalidate(
                binding,
                require_foreground=True,
                require_same_layout=True,
            )
            window_unchanged = True
        except BaseException:
            window_unchanged = False

        self._buttons.release(button)
        return window_unchanged


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
