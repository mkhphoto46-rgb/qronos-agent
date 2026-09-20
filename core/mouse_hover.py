from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.input_safety import BoundInputAuthorization, BoundInputGuard, BoundInputPoint
from core.window_binding import WindowBinder, WindowBinding, WindowRect
from core.windows_input_native import send_mouse_move_absolute


MoveFn = Callable[[int, int], None]


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
    ) -> None:
        self._guard = BoundInputGuard(binder)
        self._move = move_fn

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
