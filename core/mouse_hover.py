from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable

from core.input_safety import BoundInputAuthorization, BoundInputGuard, BoundInputPoint
from core.window_binding import WindowBinder, WindowBinding, WindowRect
from core.windows_input_native import (
    get_cursor_position,
    send_mouse_move_absolute,
)


MoveFn = Callable[[int, int], None]
CursorFn = Callable[[], tuple[int, int]]
SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class MouseMotionProfile:
    min_duration_seconds: float = 0.055
    max_duration_seconds: float = 0.300
    pixels_per_second: float = 1800.0
    updates_per_second: float = 90.0
    max_steps: int = 36

    def __post_init__(self) -> None:
        numbers = (
            self.min_duration_seconds,
            self.max_duration_seconds,
            self.pixels_per_second,
            self.updates_per_second,
        )

        if not all(
            math.isfinite(
                float(
                    value
                )
            )
            for value in numbers
        ):
            raise ValueError(
                "Mouse motion profile must contain finite values."
            )

        if (
            self.min_duration_seconds < 0
            or self.max_duration_seconds
            < self.min_duration_seconds
            or self.pixels_per_second <= 0
            or self.updates_per_second <= 0
            or self.max_steps < 1
        ):
            raise ValueError(
                "Mouse motion profile is invalid."
            )


@dataclass(frozen=True)
class MouseHoverResult:
    point: BoundInputPoint


class MouseHoverController:
    """
    Reusable pointer-move/hover capability.

    No click is performed here.  Paint, browsers, Premiere, Telegram, and other
    adapters may all reuse this controller without importing one another.
    """

    def __init__(
        self,
        binder: WindowBinder,
        *,
        move_fn: MoveFn = send_mouse_move_absolute,
        cursor_fn: CursorFn = get_cursor_position,
        sleep_fn: SleepFn = time.sleep,
        motion_profile: MouseMotionProfile | None = None,
    ) -> None:
        self._guard = BoundInputGuard(
            binder
        )
        self._move = move_fn
        self._cursor = cursor_fn
        self._sleep = sleep_fn
        self._motion_profile = (
            motion_profile
            if motion_profile is not None
            else MouseMotionProfile()
        )

    def smooth_hover(
        self,
        binding: WindowBinding,
        x: int,
        y: int,
        *,
        authorization: BoundInputAuthorization,
        allowed_rect: WindowRect | None = None,
    ) -> MouseHoverResult:
        """
        Move visibly and smoothly inside the authorized region.

        If the real cursor starts outside the authorized window/region, Qronos
        performs one safe entry jump to the nearest authorized edge and only
        then animates inside the verified capability. It never synthesizes a
        smooth path through unrelated desktop content.
        """
        auth = (
            self._guard.require_authorization(
                authorization,
                binding,
            )
        )

        _snapshot, target = (
            self._guard.point(
                binding,
                x,
                y,
                authorization=auth,
                allowed_rect=allowed_rect,
            )
        )

        effective = (
            allowed_rect
            if allowed_rect is not None
            else auth.allowed_rect
        )

        try:
            current_x, current_y = (
                self._cursor()
            )
        except Exception:
            self._move(
                target.x,
                target.y,
            )

            self._guard.revalidate(
                binding,
                authorization=auth,
            )

            return MouseHoverResult(
                point=target
            )

        current_x = int(
            current_x
        )

        current_y = int(
            current_y
        )

        inside = (
            effective.left
            <= current_x
            < effective.right
            and effective.top
            <= current_y
            < effective.bottom
        )

        if not inside:
            entry_x = min(
                max(
                    current_x,
                    effective.left,
                ),
                effective.right - 1,
            )

            entry_y = min(
                max(
                    current_y,
                    effective.top,
                ),
                effective.bottom - 1,
            )

            _entry_snapshot, entry = (
                self._guard.point(
                    binding,
                    entry_x,
                    entry_y,
                    authorization=auth,
                    allowed_rect=allowed_rect,
                )
            )

            self._move(
                entry.x,
                entry.y,
            )

            self._guard.revalidate(
                binding,
                authorization=auth,
            )

            current_x = entry.x
            current_y = entry.y

        distance = math.hypot(
            target.x - current_x,
            target.y - current_y,
        )

        if distance < 1.0:
            return MouseHoverResult(
                point=target
            )

        profile = (
            self._motion_profile
        )

        duration = max(
            profile.min_duration_seconds,
            min(
                profile.max_duration_seconds,
                distance
                / profile.pixels_per_second,
            ),
        )

        steps = max(
            2,
            min(
                int(
                    profile.max_steps
                ),
                int(
                    math.ceil(
                        duration
                        * profile.updates_per_second
                    )
                ),
            ),
        )

        delay = (
            duration
            / steps
        )

        previous = (
            current_x,
            current_y,
        )

        for index in range(
            1,
            steps + 1,
        ):
            t = (
                index
                / steps
            )

            # smootherstep:
            # zero velocity/acceleration at both ends.
            eased = (
                6 * t**5
                - 15 * t**4
                + 10 * t**3
            )

            next_x = round(
                current_x
                + (
                    target.x
                    - current_x
                )
                * eased
            )

            next_y = round(
                current_y
                + (
                    target.y
                    - current_y
                )
                * eased
            )

            candidate = (
                next_x,
                next_y,
            )

            if candidate == previous:
                continue

            _fresh, point = (
                self._guard.point(
                    binding,
                    next_x,
                    next_y,
                    authorization=auth,
                    allowed_rect=allowed_rect,
                )
            )

            self._move(
                point.x,
                point.y,
            )

            self._guard.revalidate(
                binding,
                authorization=auth,
            )

            previous = candidate

            if (
                index
                < steps
                and delay > 0
            ):
                self._sleep(
                    delay
                )

        return MouseHoverResult(
            point=target
        )

    def hover(
        self,
        binding: WindowBinding,
        x: int,
        y: int,
        *,
        authorization: BoundInputAuthorization,
        allowed_rect: WindowRect | None = None,
    ) -> MouseHoverResult:
        _snapshot, point = self._guard.point(
            binding,
            x,
            y,
            authorization=authorization,
            allowed_rect=allowed_rect,
        )
        self._move(point.x, point.y)
        # Input is only considered successful if the target stayed identical,
        # foreground, and layout-stable throughout the primitive.
        self._guard.revalidate(binding, authorization=authorization)
        return MouseHoverResult(point=point)
