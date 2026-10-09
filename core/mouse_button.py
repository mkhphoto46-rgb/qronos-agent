from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import threading
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
        smooth_motion: bool = False,
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
        self._button = button_fn
        self._smooth_motion = bool(
            smooth_motion
        )
        # Buttons for which this controller delivered a native down and has
        # not yet delivered the matching up. Cleanup paths consult this so a
        # failed stroke never sends a stray up that ends a user's own drag.
        self._held: set[MouseButton] = set()
        self._held_lock = threading.Lock()
        self._presses_delivered = 0

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
        try:
            self._button(button.value, False)
        finally:
            with self._held_lock:
                self._held.discard(button)

    @property
    def presses_delivered(self) -> int:
        """Monotonic count of native mouse-downs this controller delivered."""
        with self._held_lock:
            return self._presses_delivered

    def is_held(self, button: MouseButton = MouseButton.LEFT) -> bool:
        """True while a Qronos-delivered down has no delivered up yet."""
        with self._held_lock:
            return button in self._held

    def release_if_held(self, button: MouseButton = MouseButton.LEFT) -> bool:
        """Emergency-release only a button this controller actually pressed."""
        if not self.is_held(button):
            return False
        self.release(button)
        return True

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
        move = (
            self._hover.smooth_hover
            if (
                down
                and self._smooth_motion
            )
            else self._hover.hover
        )

        hover_result = move(
            binding,
            x,
            y,
            authorization=authorization,
            allowed_rect=allowed_rect,
        )
        self._guard.revalidate(binding, authorization=authorization)
        try:
            self._button(button.value, down)
        finally:
            # Delivered or not, this press/release may have changed the app:
            # every coordinate grounded before it expires.
            self._guard.note_state_change(binding)
        with self._held_lock:
            if down:
                self._held.add(button)
                self._presses_delivered += 1
            else:
                self._held.discard(button)
        try:
            self._guard.revalidate(binding, authorization=authorization)
        except BaseException:
            # A press is atomic from the caller's view: if the window changed
            # right after mouse-down, release before reporting the failure so
            # no caller can lose track of a held synthetic button.
            if down:
                self.release(button)
            raise
        return MouseButtonResult(
            point=hover_result.point,
            button=button,
            down=down,
        )
