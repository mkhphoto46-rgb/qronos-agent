"""D3 freshness rails: grounded coordinates expire with time and with Qronos's own input.

Everything is real Qronos code except the Windows boundary: the window list
and snapshots, the pixel grab and the native mouse/keyboard senders are
injected fakes. No screen, window, mouse, keyboard or model is touched.
"""

from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pytest

from core.actions import ActionRequest
from core.grounding_freshness import (
    FreshnessExpired,
    advance_state_epoch,
    require_fresh,
    state_epoch,
)
from core.input_safety import BoundInputAuthorization
from core.keyboard_control import KeyboardController
from core.mouse_button import MouseButtonController
from core.mouse_click import MouseClickController
from core.mouse_hover import MouseHoverController
from core.paint_adapter import PaintAdapter, PaintAdapterError
from core.screen_capture import CaptureUnavailable, ScreenCapture
from core.shape_planner import ShapePlanner
from core.vision_grounding import (
    GroundingStale,
    confirm_grounded_target,
    ground_structured_response,
    revalidate_grounded_target,
)
from core.window_binding import WindowBinder, WindowRect, WindowSnapshot, WindowTarget
from security.gate import evaluate, issue_grant
from security.permissions import ActionCategory
from tests.fixtures.still_window import StillWindowCapture, canvas_from
from tests.test_screen_capture import busy_pixels, fake_geometry


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _snapshot() -> WindowSnapshot:
    return WindowSnapshot(
        hwnd=4242,
        pid=777,
        thread_id=888,
        title="Untitled - Paint",
        process_path=r"C:\Program Files\WindowsApps\Microsoft.Paint\mspaint.exe",
        process_start_time_100ns=123456,
        window_rect=WindowRect(0, 0, 400, 300),
        client_rect=WindowRect(8, 30, 392, 292),
        dpi=96,
        visible=True,
        minimized=False,
        foreground=True,
    )


def _bound():
    current = {"value": _snapshot()}
    binder = WindowBinder(
        enumerate_fn=lambda: (current["value"],),
        snapshot_fn=lambda hwnd: current["value"],
    )
    binding = binder.bind(
        WindowTarget(app_id="paint", display_name="Paint", executable="mspaint.exe",
                     process_names=("mspaint.exe",)),
        require_foreground=True,
    )
    return binder, binding, current


def _capture(grab_hook=None) -> ScreenCapture:
    def grab(window):
        if grab_hook is not None:
            grab_hook()
        return busy_pixels(400, 300), 400, 300

    return ScreenCapture(grab=grab, geometry_fn=fake_geometry(1920, 1080))


def _found(x: int, y: int, *, confidence: float = 0.95) -> str:
    return json.dumps({
        "status": "found",
        "image_width": 400,
        "image_height": 300,
        "candidates": [{
            "label": "Pencil",
            "confidence": confidence,
            "point": {"x": x, "y": y},
            "bbox": {"x": x - 5, "y": y - 5, "width": 10, "height": 10},
        }],
    })


def _authorization(binding) -> BoundInputAuthorization:
    request = ActionRequest(category=ActionCategory.GUI_AUTOMATION, target="app:paint",
                            summary="test GUI authorization")
    verdict = evaluate(request)
    grant = issue_grant(request, verdict,
                        approval=verdict.decision if verdict.needs_approval else None)
    return BoundInputAuthorization.bind(grant, binding)


# ------------------------------------------------------------ the rule itself


def test_fresh_evidence_passes_and_each_failure_is_refused():
    clock = Clock(100.0)
    token = "rule-window"
    epoch = state_epoch(token)
    require_fresh(captured_at=95.0, captured_epoch=epoch, identity_token=token,
                  max_age_seconds=10.0, clock=clock)
    for captured_at in (0.0, float("nan"), -5.0):
        with pytest.raises(FreshnessExpired, match="no capture time"):
            require_fresh(captured_at=captured_at, captured_epoch=epoch, identity_token=token,
                          max_age_seconds=10.0, clock=clock)
    with pytest.raises(FreshnessExpired, match="old"):
        require_fresh(captured_at=80.0, captured_epoch=epoch, identity_token=token,
                      max_age_seconds=10.0, clock=clock)
    with pytest.raises(FreshnessExpired, match="newer than now"):
        require_fresh(captured_at=150.0, captured_epoch=epoch, identity_token=token,
                      max_age_seconds=10.0, clock=clock)
    advance_state_epoch(token)
    with pytest.raises(FreshnessExpired, match="before Qronos changed the window"):
        require_fresh(captured_at=95.0, captured_epoch=epoch, identity_token=token,
                      max_age_seconds=10.0, clock=clock)


# ------------------------------------------------------------ capture provenance


def test_a_bound_capture_records_time_epoch_and_digest():
    binder, binding, _ = _bound()
    advance_state_epoch(binding.identity_token)
    frame = _capture().capture_bound(binding, binder, approved=True)
    assert frame.state_epoch == state_epoch(binding.identity_token)
    assert frame.captured_at > 0
    assert len(frame.pixel_sha256) == 64


def test_input_during_a_capture_discards_the_frame():
    binder, binding, _ = _bound()
    capture = _capture(lambda: advance_state_epoch(binding.identity_token))
    with pytest.raises(CaptureUnavailable, match="changed the window while it was being captured"):
        capture.capture_bound(binding, binder, approved=True)


# ------------------------------------------------------------ provenance expiry


def test_a_grounded_target_expires_after_the_age_limit():
    binder, binding, _ = _bound()
    frame = _capture().capture_bound(binding, binder, approved=True)
    target = ground_structured_response(frame, _found(100, 80))
    assert target.point.capture_sha256 == frame.pixel_sha256
    clock = Clock(frame.captured_at + 5.0)
    assert revalidate_grounded_target(target, binding, binder, clock=clock) == (100, 80)
    clock.now = frame.captured_at + 21.0
    with pytest.raises(GroundingStale, match="old"):
        revalidate_grounded_target(target, binding, binder, clock=clock)


def _mouse(binder, on_button=None):
    pressed = []

    def button(name, down):
        pressed.append(down)
        if on_button is not None:
            on_button(name, down)

    hover = MouseHoverController(binder, move_fn=lambda x, y: None, cursor_fn=lambda: (0, 0),
                                 sleep_fn=lambda s: None)
    buttons = MouseButtonController(binder, hover=hover, button_fn=button)
    return MouseClickController(binder, hover=hover, buttons=buttons), buttons, pressed


def test_a_click_expires_every_coordinate_grounded_before_it():
    binder, binding, _ = _bound()
    frame = _capture().capture_bound(binding, binder, approved=True)
    pencil = ground_structured_response(frame, _found(100, 80))
    other = ground_structured_response(frame, _found(200, 150))
    click, _, pressed = _mouse(binder)
    x, y = revalidate_grounded_target(pencil, binding, binder)
    click.click_at(binding, x, y, authorization=_authorization(binding))
    assert pressed == [True, False]
    with pytest.raises(GroundingStale, match="before Qronos changed the window"):
        revalidate_grounded_target(other, binding, binder)
    fresh = ground_structured_response(_capture().capture_bound(binding, binder, approved=True),
                                       _found(200, 150))
    assert revalidate_grounded_target(fresh, binding, binder) == (200, 150)


def test_input_through_another_binder_or_action_also_expires_coordinates():
    binder, binding, current = _bound()
    frame = _capture().capture_bound(binding, binder, approved=True)
    target = ground_structured_response(frame, _found(100, 80))
    # A second action with its own binder for the same exact window.
    other_binder = WindowBinder(enumerate_fn=lambda: (current["value"],),
                                snapshot_fn=lambda hwnd: current["value"])
    other_binding = other_binder.bind(
        WindowTarget(app_id="paint", display_name="Paint", executable="mspaint.exe",
                     process_names=("mspaint.exe",)),
        require_foreground=True,
    )
    click, _, _ = _mouse(other_binder)
    click.click_at(other_binding, 300, 200, authorization=_authorization(other_binding))
    with pytest.raises(GroundingStale, match="before Qronos changed the window"):
        revalidate_grounded_target(target, binding, binder)


def test_typing_and_keys_also_expire_earlier_coordinates():
    binder, binding, _ = _bound()
    keyboard = KeyboardController(binder, text_fn=lambda text: None, key_fn=lambda name, down: None)
    target = ground_structured_response(_capture().capture_bound(binding, binder, approved=True),
                                        _found(100, 80))
    keyboard.press(binding, "escape", authorization=_authorization(binding))
    with pytest.raises(GroundingStale):
        revalidate_grounded_target(target, binding, binder)


def test_a_failed_native_press_still_counts_as_a_possible_change():
    binder, binding, _ = _bound()
    before = state_epoch(binding.identity_token)

    def broken(name, down):
        raise OSError("SendInput failed")

    _, buttons, _ = _mouse(binder, on_button=broken)
    with pytest.raises(OSError):
        buttons.down_at(binding, 100, 80, authorization=_authorization(binding))
    # The approach motion and the failed press both count.
    assert state_epoch(binding.identity_token) >= before + 2


def test_low_confidence_and_ambiguous_answers_never_become_coordinates():
    from core.vision_grounding import GroundingAmbiguous, GroundingLowConfidence

    binder, binding, _ = _bound()
    frame = _capture().capture_bound(binding, binder, approved=True)
    with pytest.raises(GroundingLowConfidence):
        ground_structured_response(frame, _found(100, 80, confidence=0.3))
    two = json.loads(_found(100, 80))
    two["status"] = "ambiguous"
    two["candidates"].append(dict(two["candidates"][0], point={"x": 300, "y": 200},
                                  bbox={"x": 295, "y": 195, "width": 10, "height": 10}))
    with pytest.raises(GroundingAmbiguous):
        ground_structured_response(frame, json.dumps(two))


# ------------------------------------------------------------ external content changes


def _window_and_target(x=100, y=80):
    binder, binding, current = _bound()
    window = StillWindowCapture()
    frame = window.capture_bound(binding, binder)
    target = ground_structured_response(frame, _found(x, y))
    return binder, binding, current, window, target


def _confirm(target, binding, binder, window):
    return confirm_grounded_target(target, binding, binder, window, screen_approved=True,
                                   reason="test confirmation")


def test_an_unchanged_target_is_confirmed_from_a_fresh_frame():
    binder, binding, _, window, target = _window_and_target()
    assert _confirm(target, binding, binder, window) == (100, 80)
    assert len(window.calls) == 2, "grounding frame + fresh confirming frame"


def test_a_scroll_inside_the_same_window_refuses_the_coordinate():
    """Same hwnd, PID, rectangle, DPI and focus; no Qronos input; the content moved."""
    binder, binding, _, window, target = _window_and_target()
    window.pixels = np.roll(window.pixels, 37, axis=0)  # the document scrolled
    with pytest.raises(GroundingStale, match="changed on screen"):
        _confirm(target, binding, binder, window)
    # Grounding again from a new frame works.
    regrounded = ground_structured_response(window.capture_bound(binding, binder), _found(100, 80))
    assert _confirm(regrounded, binding, binder, window) == (100, 80)


def test_a_dialog_opened_over_the_target_refuses_the_coordinate():
    binder, binding, _, window, target = _window_and_target()
    window.pixels[60:120, 70:160] = 240  # an in-window dialog/popup over the target
    with pytest.raises(GroundingStale, match="changed on screen"):
        _confirm(target, binding, binder, window)


def test_a_tiny_repaint_far_from_the_target_does_not_block_it():
    """Below the stated context limit (about 0.05% of the window)."""
    binder, binding, _, window, target = _window_and_target()
    window.pixels[280:283, 380:383] = 0  # a 3x3 caret blink far away
    assert _confirm(target, binding, binder, window) == (100, 80)


def test_a_large_change_elsewhere_in_the_window_refuses_the_target():
    binder, binding, _, window, target = _window_and_target()
    window.pixels[250:290, 300:380] = 0  # a panel or document area changed
    with pytest.raises(GroundingStale, match="window around"):
        _confirm(target, binding, binder, window)


def _list_view(offset_rows: int = 0):
    """A list of rows: the same icon in every row, a different label per row."""
    picture = np.full((300, 400, 3), 255, dtype=np.uint8)
    for row in range(-2, 10):
        top = row * 40 + 10
        if top < 0 or top + 20 > 300:
            continue
        picture[top: top + 20, 10:30] = (30, 90, 200)  # identical icon
        label = row + offset_rows
        for i in range(12):  # a different "text" pattern for every row
            if (label * 7 + i * 3) % 5 < 2:
                picture[top + 4: top + 16, 50 + i * 20: 62 + i * 20] = 0
    return picture


def test_a_repeated_icon_left_in_place_by_a_one_row_scroll_is_refused():
    """Same icon pixels at the same spot, same hwnd/PID/geometry/focus, no Qronos input:
    the row it belongs to changed, so the coordinate must not be used."""
    binder, binding, _ = _bound()
    window = StillWindowCapture(_list_view(0))
    frame = window.capture_bound(binding, binder)
    icon = ground_structured_response(frame, _found(20, 60))  # row 1's icon
    window.pixels = _list_view(1)  # the list scrolled by exactly one row
    assert (window.pixels[46:74, 6:34] == frame_pixels(frame)[46:74, 6:34]).all(), (
        "the icon patch itself is pixel-identical after the scroll")
    with pytest.raises(GroundingStale, match="window around"):
        _confirm(icon, binding, binder, window)


def frame_pixels(frame):
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(frame.image.data)) as opened:
        return np.asarray(opened.convert("RGB"))


def test_a_target_without_pixel_evidence_is_never_confirmed():
    binder, binding, _, window, target = _window_and_target()
    with pytest.raises(GroundingStale, match="no pixel evidence"):
        _confirm(replace(target, fingerprint=None), binding, binder, window)


def test_focus_lost_to_a_dialog_window_refuses_before_any_capture():
    binder, binding, current, window, target = _window_and_target()
    current["value"] = replace(current["value"], foreground=False)
    import core.window_binding as window_binding

    original = window_binding.window_is_foreground
    window_binding.window_is_foreground = lambda hwnd: False
    try:
        with pytest.raises(GroundingStale, match="foreground"):
            _confirm(target, binding, binder, window)
    finally:
        window_binding.window_is_foreground = original
    assert len(window.calls) == 1, "no fresh frame is taken once focus is gone"


# ------------------------------------------------------------ the Paint canvas


CANVAS = WindowRect(60, 60, 360, 280)
TOOL = WindowRect(12, 32, 40, 52)  # a verified tool indicator, outside the canvas band


class _Stroke:
    """Draws each stroke into the window's pixels, as Paint would."""

    def __init__(self, window, *, on_stroke=None) -> None:
        self.window = window
        self.calls = 0
        self.on_stroke = on_stroke

    def stroke(self, binding, points, **kwargs):
        from types import SimpleNamespace

        self.calls += 1
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            steps = max(abs(x1 - x0), abs(y1 - y0), 1)
            for i in range(steps + 1):
                x = round(x0 + (x1 - x0) * i / steps)
                y = round(y0 + (y1 - y0) * i / steps)
                self.window.pixels[y - 1: y + 1, x - 1: x + 1] = 0
        if self.on_stroke is not None:
            self.on_stroke(self.calls)
        return SimpleNamespace(points=tuple(points))


def _paint(on_stroke=None):
    from types import SimpleNamespace

    binder, binding, _ = _bound()
    window = StillWindowCapture()
    canvas = canvas_from(binding, CANVAS, window.capture_bound(binding, binder))
    stroke = _Stroke(window, on_stroke=on_stroke)
    adapter = PaintAdapter(binder, inputs=SimpleNamespace(stroke=stroke), capture=window)
    return binder, binding, window, canvas, stroke, adapter


THREE_LINES = ShapePlanner.custom(
    (((0.1, 0.2), (0.9, 0.2)), ((0.1, 0.5), (0.9, 0.5)), ((0.1, 0.8), (0.9, 0.8))),
    name="three-lines",
)


def test_own_strokes_are_expected_and_every_stroke_gets_a_fresh_frame():
    binder, binding, window, canvas, stroke, adapter = _paint()
    adapter.execute(binding, THREE_LINES, canvas, authorization=_authorization(binding),
                    screen_approved=True, tool_regions=(TOOL,))
    assert stroke.calls == 3
    # canvas grounding frame + start check + one check before each later stroke
    assert len(window.calls) == 1 + 1 + 2
    assert all(call.get("approved") is True for call in window.calls[1:])


def test_a_change_inside_the_canvas_between_strokes_stops_the_drawing():
    state = {}

    def scribble(n):
        if n == 1:
            state["window"].pixels[200:230, 300:340] = 0  # someone else drew / a popup opened

    binder, binding, window, canvas, stroke, adapter = _paint(on_stroke=scribble)
    state["window"] = window
    with pytest.raises(PaintAdapterError, match="outside Qronos's own strokes"):
        adapter.execute(binding, THREE_LINES, canvas, authorization=_authorization(binding),
                        screen_approved=True, tool_regions=(TOOL,))
    assert stroke.calls == 1


def test_the_page_scrolling_between_strokes_stops_the_drawing():
    state = {}

    def scroll(n):
        if n == 1:
            window = state["window"]
            window.pixels = np.roll(window.pixels, 25, axis=1)

    binder, binding, window, canvas, stroke, adapter = _paint(on_stroke=scroll)
    state["window"] = window
    with pytest.raises(PaintAdapterError, match="changed during drawing"):
        adapter.execute(binding, THREE_LINES, canvas, authorization=_authorization(binding),
                        screen_approved=True, tool_regions=(TOOL,))
    assert stroke.calls == 1


def test_a_canvas_that_changed_after_grounding_is_refused_before_the_first_stroke():
    binder, binding, window, canvas, stroke, adapter = _paint()
    window.pixels[100:140, 100:180] = 255
    with pytest.raises(PaintAdapterError, match="Paint canvas changed on screen"):
        adapter.execute(binding, THREE_LINES, canvas, authorization=_authorization(binding),
                        screen_approved=True, tool_regions=(TOOL,))
    assert stroke.calls == 0


def test_a_canvas_grounded_before_a_toolbar_click_is_refused_before_any_stroke():
    binder, binding, window, canvas, stroke, adapter = _paint()
    advance_state_epoch(binding.identity_token)  # a toolbar click after the canvas frame
    with pytest.raises(PaintAdapterError, match="before Qronos changed the window"):
        adapter.execute(binding, THREE_LINES, canvas, authorization=_authorization(binding),
                        screen_approved=True, tool_regions=(TOOL,))
    assert stroke.calls == 0


def test_an_old_canvas_is_refused():
    from types import SimpleNamespace

    binder, binding, window, canvas, stroke, _ = _paint()
    adapter = PaintAdapter(binder, inputs=SimpleNamespace(stroke=stroke), capture=window,
                           clock=Clock(canvas.captured_at + 46.0))
    with pytest.raises(PaintAdapterError, match="old"):
        adapter.execute(binding, THREE_LINES, canvas, authorization=_authorization(binding),
                        screen_approved=True, tool_regions=(TOOL,))
    assert stroke.calls == 0


def test_drawing_without_a_verifying_capture_is_refused():
    from types import SimpleNamespace

    binder, binding, window, canvas, stroke, _ = _paint()
    adapter = PaintAdapter(binder, inputs=SimpleNamespace(stroke=stroke))
    with pytest.raises(PaintAdapterError, match="bound window capture"):
        adapter.execute(binding, THREE_LINES, canvas, authorization=_authorization(binding))
    assert stroke.calls == 0
