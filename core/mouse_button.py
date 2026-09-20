from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from core.input_safety import BoundInputAuthorization, BoundInputGuard, BoundInputPoint
from core.mouse_hover import MouseHoverController
from core.window_binding import WindowBinder, WindowBinding, WindowRect
from core.windows_input_native import send_mouse_button


ButtonFn = Callable[[str, bool], None]


class MouseButton(Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


@dataclass(frozen=True)
class MouseButtonResult:
    point: BoundInputPoint
    button: MouseButton
    down: bool


class MouseButtonController:
    """
    Reusable mouse-down/mouse-up capability.

    Keeping button state separate from pointer movement is what lets a Paint
    adapter draw a stroke by composing ``down_at -> hover... -> up_at`` while
    another adapter can reuse the exact same primitives for drag-and-drop.
    """

    def __init__(
        self,
        binder: WindowBinder,
        *,
        hover: MouseHoverController | None = None,
        button_fn: ButtonFn = send_mouse_button,
    ) -> None:
        self._guard = BoundInputGuard(binder)
        self._hover = hover if hover is not None else MouseHoverController(binder)
        self._button = button_fn

    def down_at(
        self,
        binding: WindowBinding,
        x: int,
        y: int,
        *,
        authorization: BoundInputAuthorization,
        button: MouseButton = MouseButton.LEFT,
        allowed_rect: WindowRect | None = None,
    ) -> MouseButtonResult:
        return self._set_at(
            binding,
            x,
            y,
            authorization=authorization,
            button=button,
            down=True,
            allowed_rect=allowed_rect,
        )

    def up_at(
        self,
        binding: WindowBinding,
        x: int,
        y: int,
        *,
        authorization: BoundInputAuthorization,
        button: MouseButton = MouseButton.LEFT,
        allowed_rect: WindowRect | None = None,
    ) -> MouseButtonResult:
        return self._set_at(
            binding,
            x,
            y,
            authorization=authorization,
            button=button,
            down=False,
            allowed_rect=allowed_rect,
        )

    def release(self, button: MouseButton = MouseButton.LEFT) -> None:
        """Best-effort emergency release for Qronos-injected button state.

        This deliberately does not require a valid WindowBinding. Releasing a
        synthetic button is the fail-safe cleanup path when focus/layout
        becomes stale after mouse-down.
        """
        if not isinstance(button, MouseButton):
            raise ValueError("button must be a MouseButton.")
        self._button(button.value, False)

    def _set_at(
        self,
        binding: WindowBinding,
        x: int,
        y: int,
        *,
        authorization: BoundInputAuthorization,
        button: MouseButton,
        down: bool,
        allowed_rect: WindowRect | None,
    ) -> MouseButtonResult:
        if not isinstance(button, MouseButton):
            raise ValueError("button must be a MouseButton.")
        hover_result = self._hover.hover(
            binding,
            x,
            y,
            authorization=authorization,
            allowed_rect=allowed_rect,
        )
        self._guard.revalidate(binding, authorization=authorization)
        self._button(button.value, down)
        self._guard.revalidate(binding, authorization=authorization)
        return MouseButtonResult(
            point=hover_result.point,
            button=button,
            down=down,
        )
