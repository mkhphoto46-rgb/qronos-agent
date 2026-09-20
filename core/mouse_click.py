from __future__ import annotations

from dataclasses import dataclass

from core.input_safety import BoundInputAuthorization, BoundInputPoint
from core.mouse_button import MouseButton, MouseButtonController
from core.mouse_hover import MouseHoverController
from core.window_binding import WindowBinder, WindowBinding, WindowRect


@dataclass(frozen=True)
class MouseClickResult:
    point: BoundInputPoint
    button: MouseButton


class MouseClickController:
    """Reusable click capability composed from separate hover and button APIs."""

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

    def click_at(
        self,
        binding: WindowBinding,
        x: int,
        y: int,
        *,
        authorization: BoundInputAuthorization,
        button: MouseButton = MouseButton.LEFT,
        allowed_rect: WindowRect | None = None,
    ) -> MouseClickResult:
        pressed = False
        try:
            down = self._buttons.down_at(
                binding,
                x,
                y,
                authorization=authorization,
                button=button,
                allowed_rect=allowed_rect,
            )
            pressed = True
            # up_at intentionally re-runs hover + revalidation.  A click therefore
            # fails closed if focus/layout changed after mouse-down.
            self._buttons.up_at(
                binding,
                x,
                y,
                authorization=authorization,
                button=button,
                allowed_rect=allowed_rect,
            )
            pressed = False
            return MouseClickResult(point=down.point, button=button)
        finally:
            if pressed:
                self._buttons.release(button)
