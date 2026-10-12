from __future__ import annotations

from dataclasses import dataclass

from core.keyboard_control import KeyboardController
from core.mouse_button import MouseButtonController
from core.mouse_click import MouseClickController
from core.mouse_hover import MouseHoverController
from core.mouse_wheel import MouseWheelController
from core.pointer_stroke import PointerStrokeController
from core.window_binding import WindowBinder


@dataclass(frozen=True)
class InputCapabilities:
    """
    Composition root for adapters that need one or more input abilities.

    All pieces remain independently injectable/editable.  Paint can use
    ``hover + buttons`` for drawing strokes; a form adapter may use ``click +
    keyboard``; an inspection feature can use ``hover`` alone.
    """

    hover: MouseHoverController
    buttons: MouseButtonController
    click: MouseClickController
    stroke: PointerStrokeController
    keyboard: KeyboardController
    wheel: MouseWheelController | None = None

    @classmethod
    def for_binder(cls, binder: WindowBinder) -> "InputCapabilities":
        hover = MouseHoverController(
            binder
        )

        buttons = MouseButtonController(
            binder,
            hover=hover,
            smooth_motion=True,
        )

        return cls(
            hover=hover,
            buttons=buttons,
            click=MouseClickController(
                binder,
                hover=hover,
                buttons=buttons,
            ),
            stroke=PointerStrokeController(
                binder,
                hover=hover,
                buttons=buttons,
            ),
            keyboard=KeyboardController(
                binder,
                default_text_interval_seconds=0.012,
            ),
            wheel=MouseWheelController(
                binder,
                hover=hover,
            ),
        )
