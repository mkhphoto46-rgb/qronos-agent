from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.input_safety import (
    BoundInputAuthorization,
    BoundInputGuard,
    BoundInputPoint,
)
from core.mouse_hover import (
    MouseHoverController,
)
from core.window_binding import (
    WindowBinder,
    WindowBinding,
    WindowRect,
)
from core.windows_input_native import (
    send_mouse_wheel,
)


WheelFn = Callable[
    [int],
    None,
]


@dataclass(frozen=True)
class MouseWheelResult:
    point: BoundInputPoint
    steps: int


class MouseWheelController:
    """
    Bounded vertical wheel capability.

    Scrolling is delivered only after a verified pointer target inside the
    authorized app region and is revalidated immediately afterwards.
    """

    def __init__(
        self,
        binder: WindowBinder,
        *,
        hover: MouseHoverController | None = None,
        wheel_fn: WheelFn = send_mouse_wheel,
    ) -> None:
        self._guard = BoundInputGuard(
            binder
        )

        self._hover = (
            hover
            if hover is not None
            else MouseHoverController(
                binder
            )
        )

        self._wheel = wheel_fn

    def scroll_at(
        self,
        binding: WindowBinding,
        x: int,
        y: int,
        steps: int,
        *,
        authorization: BoundInputAuthorization,
        allowed_rect: WindowRect | None = None,
    ) -> MouseWheelResult:

        if (
            isinstance(
                steps,
                bool,
            )
            or not isinstance(
                steps,
                int,
            )
        ):
            raise ValueError(
                "scroll steps must be an integer."
            )

        if (
            steps == 0
            or abs(
                steps
            )
            > 10
        ):
            raise ValueError(
                "One bounded scroll must be between -10 and 10 non-zero steps."
            )

        moved = (
            self._hover.smooth_hover(
                binding,
                x,
                y,
                authorization=authorization,
                allowed_rect=allowed_rect,
            )
        )

        self._guard.revalidate(
            binding,
            authorization=authorization,
        )

        self._wheel(
            steps
        )

        self._guard.revalidate(
            binding,
            authorization=authorization,
        )

        return MouseWheelResult(
            point=moved.point,
            steps=steps,
        )
