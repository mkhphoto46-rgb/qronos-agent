from __future__ import annotations

import pytest

from core.input_capabilities import InputCapabilities
from core.input_safety import InputOutOfBounds, InputPermissionRequired
from core.keyboard_control import KeyboardController
from core.mouse_button import MouseButton, MouseButtonController
from core.mouse_click import MouseClickController
from core.mouse_hover import MouseHoverController
from core.window_binding import (
    WindowBinder,
    WindowBinding,
    WindowBindingStale,
    WindowRect,
    WindowSnapshot,
)
from core.windows_input_native import absolute_mouse_coordinates, virtual_key_code


def _snapshot(*, foreground: bool = True) -> WindowSnapshot:
    return WindowSnapshot(
        hwnd=101,
        pid=202,
        thread_id=303,
        title="Paint",
        process_path=r"C:\Windows\mspaint.exe",
        process_start_time_100ns=404,
        window_rect=WindowRect(90, 90, 510, 410),
        client_rect=WindowRect(100, 100, 500, 400),
        dpi=96,
        visible=True,
        minimized=False,
        foreground=foreground,
    )


def _binding() -> WindowBinding:
    return WindowBinding(
        app_id="paint",
        display_name="Paint",
        snapshot=_snapshot(),
        identity_token="identity",
        layout_token="layout",
    )


class _Binder:
    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot or _snapshot()
        self.error = error
        self.calls = []

    def revalidate(self, binding, *, require_foreground=True, require_same_layout=True):
        self.calls.append((binding, require_foreground, require_same_layout))
        if self.error:
            raise self.error
        return self.snapshot


def test_hover_is_a_separate_capability_and_only_moves():
    binder = _Binder()
    moves = []
    hover = MouseHoverController(binder, move_fn=lambda x, y: moves.append((x, y)))

    result = hover.hover(_binding(), 250, 220, approved=True)

    assert moves == [(250, 220)]
    assert result.point.x == 250
    assert len(binder.calls) == 2


def test_mouse_button_state_is_separate_for_drawing_and_dragging():
    binder = _Binder()
    moves = []
    buttons = []
    hover = MouseHoverController(binder, move_fn=lambda x, y: moves.append((x, y)))
    control = MouseButtonController(
        binder,
        hover=hover,
        button_fn=lambda button, down: buttons.append((button, down)),
    )

    control.down_at(_binding(), 200, 200, approved=True)
    hover.hover(_binding(), 250, 250, approved=True)
    control.up_at(_binding(), 300, 300, approved=True)

    assert moves == [(200, 200), (250, 250), (300, 300)]
    assert buttons == [("left", True), ("left", False)]


def test_click_is_composition_not_a_second_native_mouse_implementation():
    binder = _Binder()
    moves = []
    buttons = []
    hover = MouseHoverController(binder, move_fn=lambda x, y: moves.append((x, y)))
    button_control = MouseButtonController(
        binder,
        hover=hover,
        button_fn=lambda button, down: buttons.append((button, down)),
    )
    click = MouseClickController(binder, hover=hover, buttons=button_control)

    result = click.click_at(_binding(), 300, 240, approved=True)

    # down_at and up_at each re-ground/revalidate the point by design.
    assert moves == [(300, 240), (300, 240)]
    assert buttons == [("left", True), ("left", False)]
    assert result.button is MouseButton.LEFT


def test_keyboard_is_independent_from_pointer_capabilities():
    binder = _Binder()
    text = []
    keys = []
    keyboard = KeyboardController(
        binder,
        text_fn=text.append,
        key_fn=lambda key, down: keys.append((key, down)),
    )

    keyboard.type_text(_binding(), "سلام", approved=True)
    keyboard.press(_binding(), "enter", approved=True)

    assert text == ["سلام"]
    assert keys == [("enter", True), ("enter", False)]


def test_every_capability_requires_explicit_approval():
    binder = _Binder()
    binding = _binding()
    hover = MouseHoverController(binder, move_fn=lambda _x, _y: None)
    keyboard = KeyboardController(binder, text_fn=lambda _text: None)

    with pytest.raises(InputPermissionRequired):
        hover.hover(binding, 200, 200, approved=False)
    with pytest.raises(InputPermissionRequired):
        keyboard.type_text(binding, "x", approved=False)


def test_pointer_is_rejected_outside_verified_client_rect():
    binder = _Binder()
    called = []
    hover = MouseHoverController(binder, move_fn=lambda x, y: called.append((x, y)))

    with pytest.raises(InputOutOfBounds):
        hover.hover(_binding(), 99, 200, approved=True)

    assert called == []


def test_adapter_can_narrow_pointer_to_its_own_allowed_region():
    binder = _Binder()
    called = []
    hover = MouseHoverController(binder, move_fn=lambda x, y: called.append((x, y)))
    canvas = WindowRect(150, 150, 450, 350)

    hover.hover(_binding(), 200, 200, approved=True, allowed_rect=canvas)
    with pytest.raises(InputOutOfBounds):
        hover.hover(_binding(), 120, 120, approved=True, allowed_rect=canvas)

    assert called == [(200, 200)]


def test_stale_window_prevents_all_input():
    binder = _Binder(error=WindowBindingStale("moved"))
    hover_calls = []
    hover = MouseHoverController(binder, move_fn=lambda x, y: hover_calls.append((x, y)))

    with pytest.raises(WindowBindingStale):
        hover.hover(_binding(), 200, 200, approved=True)

    assert hover_calls == []


def test_facade_keeps_four_replaceable_public_capabilities():
    capabilities = InputCapabilities.for_binder(WindowBinder())

    assert isinstance(capabilities.hover, MouseHoverController)
    assert isinstance(capabilities.buttons, MouseButtonController)
    assert isinstance(capabilities.click, MouseClickController)
    assert isinstance(capabilities.keyboard, KeyboardController)


def test_absolute_mouse_mapping_supports_secondary_and_negative_monitors():
    bounds = (-1920, 0, 5760, 2160)
    assert absolute_mouse_coordinates(-1920, 0, bounds=bounds) == (0, 0)
    assert absolute_mouse_coordinates(3839, 2159, bounds=bounds) == (65535, 65535)
    middle = absolute_mouse_coordinates(0, 1080, bounds=bounds)
    assert 0 < middle[0] < 65535
    assert 0 < middle[1] < 65535


def test_keyboard_key_vocabulary_is_bounded():
    assert virtual_key_code("enter") == 0x0D
    assert virtual_key_code("a") == ord("A")
    assert virtual_key_code("f12") == 0x7B
    with pytest.raises(ValueError):
        virtual_key_code("windows")


def test_emergency_release_does_not_require_a_live_binding():
    binder = _Binder(error=WindowBindingStale("lost focus"))
    buttons = []
    control = MouseButtonController(
        binder,
        button_fn=lambda button, down: buttons.append((button, down)),
    )

    control.release(MouseButton.LEFT)

    assert buttons == [("left", False)]
    assert binder.calls == []


def test_click_emergency_releases_if_window_goes_stale_after_down():
    class _StaleAfterDownBinder(_Binder):
        def revalidate(self, binding, *, require_foreground=True, require_same_layout=True):
            self.calls.append((binding, require_foreground, require_same_layout))
            # down_at performs four validations total through hover + guard. Make
            # the next validation fail inside up_at after synthetic mouse-down.
            if len(self.calls) >= 5:
                raise WindowBindingStale("lost focus after down")
            return self.snapshot

    binder = _StaleAfterDownBinder()
    buttons = []
    hover = MouseHoverController(binder, move_fn=lambda _x, _y: None)
    button_control = MouseButtonController(
        binder,
        hover=hover,
        button_fn=lambda button, down: buttons.append((button, down)),
    )
    click = MouseClickController(binder, hover=hover, buttons=button_control)

    with pytest.raises(WindowBindingStale):
        click.click_at(_binding(), 220, 220, approved=True)

    assert buttons == [("left", True), ("left", False)]


def test_pointer_stroke_is_generic_and_composes_separate_primitives():
    from core.pointer_stroke import PointerStrokeController

    binder = _Binder()
    moves = []
    buttons = []
    hover = MouseHoverController(binder, move_fn=lambda x, y: moves.append((x, y)))
    button_control = MouseButtonController(
        binder,
        hover=hover,
        button_fn=lambda button, down: buttons.append((button, down)),
    )
    stroke = PointerStrokeController(binder, hover=hover, buttons=button_control)

    result = stroke.stroke(
        _binding(),
        [(180, 180), (200, 200), (240, 220), (280, 240)],
        approved=True,
    )

    # down_at moves to the first point; intermediate movement is independent;
    # up_at moves/revalidates the final point once more before release.
    assert moves == [
        (180, 180),
        (200, 200),
        (240, 220),
        (280, 240),
        (280, 240),
    ]
    assert buttons == [("left", True), ("left", False)]
    assert [(p.x, p.y) for p in result.points] == [
        (180, 180),
        (200, 200),
        (240, 220),
        (280, 240),
    ]


def test_pointer_stroke_emergency_releases_if_midstroke_revalidation_fails():
    from core.pointer_stroke import PointerStrokeController

    class _StaleMidStrokeBinder(_Binder):
        def revalidate(self, binding, *, require_foreground=True, require_same_layout=True):
            self.calls.append((binding, require_foreground, require_same_layout))
            if len(self.calls) >= 5:
                raise WindowBindingStale("layout changed mid-stroke")
            return self.snapshot

    binder = _StaleMidStrokeBinder()
    buttons = []
    hover = MouseHoverController(binder, move_fn=lambda _x, _y: None)
    button_control = MouseButtonController(
        binder,
        hover=hover,
        button_fn=lambda button, down: buttons.append((button, down)),
    )
    stroke = PointerStrokeController(binder, hover=hover, buttons=button_control)

    with pytest.raises(WindowBindingStale):
        stroke.stroke(
            _binding(),
            [(180, 180), (220, 220), (260, 260)],
            approved=True,
        )

    assert buttons == [("left", True), ("left", False)]


def test_facade_exposes_reusable_stroke_capability():
    from core.pointer_stroke import PointerStrokeController

    capabilities = InputCapabilities.for_binder(WindowBinder())
    assert isinstance(capabilities.stroke, PointerStrokeController)


def test_pointer_stroke_can_resample_sparse_segments_and_settle(monkeypatch):
    from core.pointer_stroke import PointerStrokeController

    binder = _Binder()
    moves = []
    buttons = []
    sleeps = []
    hover = MouseHoverController(binder, move_fn=lambda x, y: moves.append((x, y)))
    button_control = MouseButtonController(
        binder,
        hover=hover,
        button_fn=lambda button, down: buttons.append((button, down)),
    )
    stroke = PointerStrokeController(binder, hover=hover, buttons=button_control)
    monkeypatch.setattr("core.pointer_stroke.time.sleep", lambda seconds: sleeps.append(seconds))

    result = stroke.stroke(
        _binding(),
        [(180, 180), (220, 180)],
        approved=True,
        max_step_pixels=10,
        press_settle_seconds=0.012,
        release_settle_seconds=0.015,
    )

    assert [(p.x, p.y) for p in result.points] == [
        (180, 180),
        (190, 180),
        (200, 180),
        (210, 180),
        (220, 180),
    ]
    assert moves == [
        (180, 180),
        (190, 180),
        (200, 180),
        (210, 180),
        (220, 180),
        (220, 180),
    ]
    assert buttons == [("left", True), ("left", False)]
    assert sleeps == [0.012, 0.015]


def test_pointer_stroke_rejects_invalid_fidelity_parameters():
    from core.pointer_stroke import PointerStrokeController

    stroke = PointerStrokeController(_Binder())
    with pytest.raises(ValueError, match="max_step_pixels"):
        stroke.stroke(_binding(), [(180, 180), (200, 200)], approved=True, max_step_pixels=0)
    with pytest.raises(ValueError, match="settle"):
        stroke.stroke(
            _binding(),
            [(180, 180), (200, 200)],
            approved=True,
            press_settle_seconds=-0.01,
        )
