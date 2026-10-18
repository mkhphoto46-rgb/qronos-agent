"""Regressions for the independent D3/D4 review.

Each test asserts the CORRECTED behaviour on realistic synthetic frames.
Everything Windows-facing is injected: window snapshots (real WindowBinder
over fake snapshots), the pixel source (PNGs of in-memory arrays, prepared by
the production prepare_bytes for 4K), the cursor/button/key senders and the
Vision grounder. No screen, window, mouse, keyboard, Paint or model is used.
"""

from __future__ import annotations

from io import BytesIO
import json
import time
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from core.action_cancellation import ActionCancelled, CancellationToken
from core.actions import ActionRequest
from core.confirmed_press import press_grounded_target
from core.grounding_freshness import (
    ContentChanged,
    EvidenceTooOld,
    InputSinceCapture,
    state_epoch,
)
from core.input_safety import BoundInputAuthorization
from core.mouse_button import MouseButtonController
from core.mouse_click import MouseClickController
from core.mouse_hover import MouseHoverController
from core.paint_adapter import (
    PaintAdapter,
    PaintAdapterError,
    PaintCanvasChanged,
    PaintCanvasExpired,
    PaintVerificationUnavailable,
)
from core.paint_draw_integration import paint_failure_kind
from core.paint_draw_parser import PaintDrawParseError, parse_paint_draw_action
from core.paint_state import PaintStateError
from core.paint_templates import match_paint_template
from core.screen_capture import Capture, DisplayGeometry
from core.shape_planner import ShapePlanner
from core.vision_grounding import GroundingStale, confirm_grounded_target, ground_structured_response
from core.vision_image import prepare_bytes
from core.window_binding import WindowBinder, WindowRect, WindowSnapshot, WindowTarget
from security.gate import evaluate, issue_grant
from security.permissions import ActionCategory
from tests.fixtures.still_window import StillWindowCapture, canvas_from


# ------------------------------------------------------------------ fixtures


def _binding(width: int, height: int, exe: str = "explorer.exe", app_id: str = "files"):
    snapshot = WindowSnapshot(
        hwnd=6161, pid=616, thread_id=626, title="Files",
        process_path="C:/Apps/" + exe, process_start_time_100ns=77,
        window_rect=WindowRect(0, 0, width, height),
        client_rect=WindowRect(0, 0, width, height),
        dpi=96, visible=True, minimized=False, foreground=True,
    )
    binder = WindowBinder(enumerate_fn=lambda: (snapshot,), snapshot_fn=lambda hwnd: snapshot)
    binding = binder.bind(
        WindowTarget(app_id=app_id, display_name="Files", executable=exe, process_names=(exe,)),
        require_foreground=True,
    )
    return binder, binding


class Window4K(StillWindowCapture):
    """Prepares the frame exactly as production does (prepare_bytes: 1280-pixel long edge)."""

    def capture_bound(self, binding, binder, **kwargs):
        self.calls.append(kwargs)
        rect = binding.snapshot.window_rect
        buffer = BytesIO()
        Image.fromarray(self.pixels, "RGB").save(buffer, format="PNG")
        return Capture(
            image=prepare_bytes(buffer.getvalue()), geometry=DisplayGeometry(3840, 2160, 3840, 2160),
            window=binding.hwnd, binding_token=binding.identity_token, layout_token=binding.layout_token,
            source_width=rect.width, source_height=rect.height, frame_rect=rect,
            captured_at=time.monotonic(), state_epoch=state_epoch(binding.identity_token),
        )


def _found(frame, x, y, w, h, label="Delete"):
    return json.dumps({
        "status": "found", "image_width": frame.image.width, "image_height": frame.image.height,
        "candidates": [{"label": label, "confidence": 0.95, "point": {"x": x, "y": y},
                        "bbox": {"x": x - w // 2, "y": y - h // 2, "width": w, "height": h}}],
    })


def _confirm(target, binding, binder, window):
    return confirm_grounded_target(target, binding, binder, window, screen_approved=True, reason="test")


def _file_manager(width=1280, height=800, *, selected_row=2, label_seed=0, highlight=(204, 232, 255)):
    """A flat UI: white list, a light selection highlight, a status label, a dark red Delete button."""
    picture = np.full((height, width, 3), 255, dtype=np.uint8)
    for row in range(12):
        top = 120 + row * 40
        if row == selected_row:
            picture[top: top + 36, 20: width - 20] = highlight
        picture[top + 10: top + 26, 30:50] = (30, 90, 200)
        for i in range(10):
            if (row * 5 + i * 3) % 4 < 2:
                picture[top + 13: top + 23, 70 + i * 14: 80 + i * 14] = 40
    for i in range(8):
        if (label_seed + i * 2) % 3 == 0:
            picture[height - 60: height - 50, 40 + i * 9: 46 + i * 9] = 60
    picture[height - 100: height - 70, width - 160: width - 60] = (180, 30, 30)
    return picture


def _grounded_delete(picture, *, window_cls=StillWindowCapture, width=1280, height=800):
    binder, binding = _binding(width, height)
    window = window_cls(picture.copy())
    frame = window.capture_bound(binding, binder)
    sx, sy = frame.image.width / width, frame.image.height / height
    button = ground_structured_response(
        frame, _found(frame, round((width - 110) * sx), round((height - 85) * sy),
                      max(4, round(100 * sx)), max(4, round(30 * sy))))
    return binder, binding, window, button


def _authorization(binding, app="files"):
    request = ActionRequest(category=ActionCategory.GUI_AUTOMATION, target=f"app:{app}", summary="test")
    verdict = evaluate(request)
    grant = issue_grant(request, verdict, approval=verdict.decision if verdict.needs_approval else None)
    return BoundInputAuthorization.bind(grant, binding)


# ------------------------------------------------------- colour-aware


def test_F1_a_low_contrast_selection_moved_to_another_row_is_refused():
    binder, binding, window, button = _grounded_delete(_file_manager(selected_row=2))
    window.pixels = _file_manager(selected_row=7)
    with pytest.raises(GroundingStale, match="changed on screen"):
        _confirm(button, binding, binder, window)


def test_F1_an_equal_grey_colour_swap_on_the_target_is_refused():
    binder, binding, window, button = _grounded_delete(_file_manager())
    window.pixels[700:730, 1120:1220] = (0, 128, 0)  # same luminance as (180, 30, 30)
    with pytest.raises(GroundingStale, match="changed on screen"):
        _confirm(button, binding, binder, window)


def test_F1_a_subtle_disabled_state_tint_on_the_target_is_refused():
    binder, binding, window, button = _grounded_delete(_file_manager())
    window.pixels[700:730, 1120:1220] = (196, 46, 46)  # 16 levels lighter: "disabled" tint
    with pytest.raises(GroundingStale):
        _confirm(button, binding, binder, window)


def test_F1_an_unchanged_realistic_frame_is_confirmed():
    binder, binding, window, button = _grounded_delete(_file_manager())
    assert _confirm(button, binding, binder, window) == (button.point.screen_x, button.point.screen_y)


# ------------------------------------------------------- context bounds


def test_F2_a_changed_status_label_on_1280_is_refused():
    binder, binding, window, button = _grounded_delete(_file_manager(label_seed=0))
    window.pixels = _file_manager(label_seed=1)
    with pytest.raises(GroundingStale, match="window around"):
        _confirm(button, binding, binder, window)


def test_F2_a_thin_native_label_rewritten_on_a_4k_window_is_refused():
    picture = np.full((2160, 3840, 3), 255, dtype=np.uint8)
    picture[1900:1990, 3400:3700] = (180, 30, 30)
    binder, binding, window, button = _grounded_delete(picture, window_cls=Window4K, width=3840, height=2160)
    for i in range(0, 150, 6):
        window.pixels[300:324, 200 + i: 202 + i] = 0
    with pytest.raises(GroundingStale):
        _confirm(button, binding, binder, window)


def test_F2_a_small_change_in_the_targets_own_row_is_refused_strictly():
    """10x2 px next to the button: far below the window allowance, but in its row band."""
    binder, binding, window, button = _grounded_delete(_file_manager())
    window.pixels[712:714, 1000:1010] = 0
    with pytest.raises(GroundingStale, match="row or column"):
        _confirm(button, binding, binder, window)


def test_F2_a_tiny_caret_blink_far_away_is_tolerated_at_1280_and_4k():
    binder, binding, window, button = _grounded_delete(_file_manager())
    window.pixels[60:63, 400:403] = 0  # 3x3 native, far from the target's row/column
    assert _confirm(button, binding, binder, window)
    picture = np.full((2160, 3840, 3), 255, dtype=np.uint8)
    picture[1900:1990, 3400:3700] = (180, 30, 30)
    binder, binding, window, button = _grounded_delete(picture, window_cls=Window4K, width=3840, height=2160)
    window.pixels[200:203, 600:603] = 0
    assert _confirm(button, binding, binder, window)


# ------------------------------------------------------- move -> confirm -> press


class _Desk:
    """Native senders for the real hover/button/click controllers; pixels react to the cursor."""

    def __init__(self, window, on_arrive=None):
        self.window = window
        self.cursor = (5, 5)
        self.moves: list[tuple[int, int]] = []
        self.buttons: list[bool] = []
        self.on_arrive = on_arrive
        self.events: list[str] = []

    def move(self, x, y):
        self.cursor = (x, y)
        self.moves.append((x, y))

    def button(self, name, down):
        self.buttons.append(down)
        self.events.append("down" if down else "up")


def _inputs(binder, desk):
    hover = MouseHoverController(binder, move_fn=desk.move, cursor_fn=lambda: desk.cursor, sleep_fn=lambda s: None)
    buttons = MouseButtonController(binder, hover=hover, button_fn=desk.button, smooth_motion=True)
    click = MouseClickController(binder, hover=hover, buttons=buttons)

    class Hover:
        def smooth_hover(self, binding, x, y, **kwargs):
            result = hover.smooth_hover(binding, x, y, **kwargs)
            desk.events.append("arrived")
            if desk.on_arrive is not None:
                desk.on_arrive()
            return result

    return SimpleNamespace(hover=Hover(), click=click)


class _CountingWindow(StillWindowCapture):
    def __init__(self, pixels, desk_ref):
        super().__init__(pixels)
        self.desk_ref = desk_ref

    def capture_bound(self, binding, binder, **kwargs):
        if self.desk_ref:
            self.desk_ref[0].events.append("frame")
        return super().capture_bound(binding, binder, **kwargs)


def _press_rig(on_arrive=None):
    binder, binding = _binding(1280, 800)
    desk_ref: list[_Desk] = []
    window = _CountingWindow(_file_manager(), desk_ref)
    frame = window.capture_bound(binding, binder)
    button = ground_structured_response(frame, _found(frame, 1170, 715, 100, 30))
    desk = _Desk(window, on_arrive=on_arrive)
    desk_ref.append(desk)
    return binder, binding, window, button, desk


def test_F3_the_press_follows_the_motion_and_a_fresh_frame_with_no_further_motion():
    binder, binding, window, button, desk = _press_rig()
    press_grounded_target(button, binding, binder, window, _inputs(binder, desk),
                          authorization=_authorization(binding), allowed_rect=binding.snapshot.client_rect,
                          screen_approved=True, reason="test")
    assert desk.events == ["frame", "arrived", "frame", "down", "up"]
    target = (button.point.screen_x, button.point.screen_y)
    arrived_at = len(desk.moves)
    assert desk.moves[-1] == target
    # Every move after arrival (the press approach and release) stays exactly on the target.
    assert all(move == target for move in desk.moves[arrived_at - 1:])


def test_F3_a_tooltip_that_opens_because_of_the_motion_refuses_the_press():
    holder = {}

    def tooltip():
        holder["window"].pixels[740:770, 1100:1240] = (255, 255, 225)  # appears below the button

    binder, binding, window, button, desk = _press_rig(on_arrive=tooltip)
    holder["window"] = window
    with pytest.raises(GroundingStale, match="after the cursor moved"):
        press_grounded_target(button, binding, binder, window, _inputs(binder, desk),
                              authorization=_authorization(binding), allowed_rect=binding.snapshot.client_rect,
                              screen_approved=True, reason="test")
    assert desk.buttons == []


def test_F3_the_controls_own_hover_highlight_does_not_block_the_press():
    holder = {}

    def highlight():
        holder["window"].pixels[700:730, 1120:1220] = (200, 50, 50)  # hover tint inside the box

    binder, binding, window, button, desk = _press_rig(on_arrive=highlight)
    holder["window"] = window
    press_grounded_target(button, binding, binder, window, _inputs(binder, desk),
                          authorization=_authorization(binding), allowed_rect=binding.snapshot.client_rect,
                          screen_approved=True, reason="test")
    assert desk.buttons == [True, False]


def test_F3_stop_after_the_motion_prevents_the_press():
    token = CancellationToken()
    binder, binding, window, button, desk = _press_rig(on_arrive=token.cancel)
    with pytest.raises(ActionCancelled):
        press_grounded_target(button, binding, binder, window, _inputs(binder, desk),
                              authorization=_authorization(binding), allowed_rect=binding.snapshot.client_rect,
                              screen_approved=True, reason="test", cancel_token=token)
    assert desk.buttons == []


def test_F3_a_native_release_failure_still_leaves_no_button_held():
    binder, binding, window, button, desk = _press_rig()
    inputs = _inputs(binder, desk)
    original = desk.button

    def failing(name, down):
        original(name, down)
        if not down and desk.buttons.count(False) == 1:
            raise OSError("SendInput failed on release")

    desk.button = failing
    inputs = _inputs(binder, desk)
    with pytest.raises(OSError):
        press_grounded_target(button, binding, binder, window, inputs,
                              authorization=_authorization(binding), allowed_rect=binding.snapshot.client_rect,
                              screen_approved=True, reason="test")
    assert desk.buttons[0] is True and False in desk.buttons


# ------------------------------------------------------- hover epoch


def test_F10_hover_motion_advances_the_input_epoch():
    binder, binding = _binding(400, 300)
    hover = MouseHoverController(binder, move_fn=lambda x, y: None, cursor_fn=lambda: (100, 100),
                                 sleep_fn=lambda s: None)
    before = state_epoch(binding.identity_token)
    hover.hover(binding, 120, 110, authorization=_authorization(binding))
    assert state_epoch(binding.identity_token) == before + 1
    hover.smooth_hover(binding, 150, 150, authorization=_authorization(binding))
    assert state_epoch(binding.identity_token) == before + 2


# ------------------------------------------------------- Paint watch


CANVAS = WindowRect(60, 80, 340, 260)
TOOL = WindowRect(40, 38, 70, 56)
THREE_LINES = ShapePlanner.custom(
    (((0.1, 0.2), (0.9, 0.2)), ((0.1, 0.5), (0.9, 0.5)), ((0.1, 0.8), (0.9, 0.8))), name="three-lines")


def _paint(on_stroke):
    binder, binding = _binding(400, 300, exe="mspaint.exe", app_id="paint")
    window = StillWindowCapture(np.full((300, 400, 3), 230, dtype=np.uint8))
    window.pixels[CANVAS.top:CANVAS.bottom, CANVAS.left:CANVAS.right] = 255
    window.pixels[35:60, 20:380] = 200
    window.pixels[TOOL.top:TOOL.bottom, TOOL.left:TOOL.right] = (60, 60, 60)  # selected Pencil indicator
    canvas = canvas_from(binding, CANVAS, window.capture_bound(binding, binder))
    calls = {"n": 0}

    def stroke(binding_, points, **kwargs):
        calls["n"] += 1
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            steps = max(abs(x1 - x0), abs(y1 - y0), 1)
            for i in range(steps + 1):
                x = round(x0 + (x1 - x0) * i / steps)
                y = round(y0 + (y1 - y0) * i / steps)
                window.pixels[y - 1: y + 1, x - 1: x + 1] = 0
        on_stroke(calls["n"], window)
        return SimpleNamespace(points=tuple(points))

    adapter = PaintAdapter(binder, inputs=SimpleNamespace(stroke=SimpleNamespace(stroke=stroke)), capture=window)
    return binder, binding, window, canvas, adapter, calls


def _draw(adapter, binding, canvas, **kwargs):
    return adapter.execute(binding, THREE_LINES, canvas, authorization=_authorization(binding, "paint"),
                           screen_approved=True, **kwargs)


def test_F5_a_tool_indicator_changed_between_strokes_stops_the_drawing():
    def eraser(n, window):
        if n == 1:
            window.pixels[TOOL.top:TOOL.bottom, TOOL.left:TOOL.right] = (60, 60, 200)  # another tool/colour
    binder, binding, window, canvas, adapter, calls = _paint(eraser)
    with pytest.raises(PaintCanvasChanged, match="tool, colour or size"):
        _draw(adapter, binding, canvas, tool_regions=(TOOL,))
    assert calls["n"] == 1


def test_F5_drawing_without_watched_tool_indicators_is_refused_before_any_stroke():
    binder, binding, window, canvas, adapter, calls = _paint(lambda n, w: None)
    with pytest.raises(PaintVerificationUnavailable, match="tool, colour and size"):
        _draw(adapter, binding, canvas)
    assert calls["n"] == 0


def test_F6_a_small_overlay_lying_on_an_own_stroke_is_now_refused():
    def overlay(n, window):
        if n == 1:
            y = round(CANVAS.top + 0.2 * CANVAS.height) - 4
            window.pixels[y - 5: y + 5, 180:204] = 90
    binder, binding, window, canvas, adapter, calls = _paint(overlay)
    with pytest.raises(PaintCanvasChanged):
        _draw(adapter, binding, canvas, tool_regions=(TOOL,))
    assert calls["n"] == 1


def test_F6_own_strokes_alone_never_stop_the_drawing():
    binder, binding, window, canvas, adapter, calls = _paint(lambda n, w: None)
    _draw(adapter, binding, canvas, tool_regions=(TOOL,))
    assert calls["n"] == 3


# ------------------------------------------------------- language


@pytest.mark.parametrize("text, name", [
    ("لطفاً یک مثلث ساده در Paint بکش", "triangle"),
    ("یک مثلث در Paint بکش لطفاً", "triangle"),
    ("یک خانهٔ ساده در Paint بکش", "house"),
    ("یک خانه\u200cی ساده در Paint بکش", "house"),
    ("یک خانه\u0650 ساده در Paint بکش", "house"),  # kasra ezafe
    ("can you draw a house in paint", "house"),
    ("could you draw a circle in Paint", "circle"),
    ("would you please draw a star in paint for me", "star"),
    ("draw a house in paint", "house"),
])
def test_F7_common_phrasings_reach_the_reviewed_templates(text, name):
    template = match_paint_template(parse_paint_draw_action(text).prompt)
    assert template is not None and template.name == name


@pytest.mark.parametrize("text", ["draw a red house in paint", "draw a cat in paint", "draw two circles in paint"])
def test_F7_other_subjects_still_are_not_templates(text):
    assert match_paint_template(parse_paint_draw_action(text).prompt) is None


@pytest.mark.parametrize("text", [
    "don't draw a house in paint", "do not draw a house in paint", "never draw a circle in paint",
    "don\u2019t draw a house in paint", "please don't draw a star in Paint",
    "در Paint یک خانه نکش", "در Paint خانه رسم نکن", "نباید در Paint خانه بکشی",
])
def test_F8_negated_requests_are_refused_explicitly(text):
    with pytest.raises(PaintDrawParseError, match="not to draw"):
        parse_paint_draw_action(text)


# ------------------------------------------------------- failure kinds


def _wrapped(outer_cls, inner):
    try:
        try:
            raise inner
        except BaseException as cause:
            raise outer_cls("wrapper") from cause
    except BaseException as wrapped:
        return wrapped


def test_F9_failure_kinds_name_the_actual_reason():
    assert paint_failure_kind(PaintCanvasExpired("50 s old")) == "expired"
    assert paint_failure_kind(PaintVerificationUnavailable("no capture")) == "verification_unavailable"
    assert paint_failure_kind(PaintCanvasChanged("popup")) == "window_changed"
    assert paint_failure_kind(_wrapped(PaintStateError, _wrapped(GroundingStale, EvidenceTooOld("old")))) == "expired"
    assert paint_failure_kind(_wrapped(PaintStateError, _wrapped(GroundingStale, InputSinceCapture("x")))) == "expired"
    assert paint_failure_kind(_wrapped(PaintStateError, _wrapped(GroundingStale, ContentChanged("x")))) \
        == "window_changed"
    assert paint_failure_kind(PaintStateError("Vision found nothing")) == "not_grounded"
    assert paint_failure_kind(PaintAdapterError("non-Paint binding")) == "window_changed"


# ------------------------------------------------------- exact stated limits


def test_F2_context_limit_is_64_native_pixels_away_from_the_target_band_at_1280():
    binder, binding, window, button = _grounded_delete(_file_manager())
    window.pixels[40:46, 400:406] = 0  # 36 native px, far from the row/column bands: tolerated
    assert _confirm(button, binding, binder, window)
    window.pixels[40:52, 400:412] = 0  # 144 native px: refused
    with pytest.raises(GroundingStale, match="window around"):
        _confirm(button, binding, binder, window)


def test_F2_context_limit_is_counted_in_native_pixels_on_a_4k_window():
    picture = np.full((2160, 3840, 3), 255, dtype=np.uint8)
    picture[1900:1990, 3400:3700] = (180, 30, 30)
    binder, binding, window, button = _grounded_delete(picture, window_cls=Window4K, width=3840, height=2160)
    window.pixels[150:180, 600:630] = 0  # 900 native px (about 100 model px)
    with pytest.raises(GroundingStale, match="window around"):
        _confirm(button, binding, binder, window)


def test_F6_corridor_is_three_native_pixels_wide_on_each_side():
    """A 24x8 overlay from 5 px above to 2 px below stroke 1: inside a 6 px corridor, not a 3 px one."""
    def overlay(n, window):
        if n == 1:
            y = round(CANVAS.top + 0.2 * CANVAS.height)
            window.pixels[y - 5: y + 3, 180:204] = 90
    binder, binding, window, canvas, adapter, calls = _paint(overlay)
    with pytest.raises(PaintCanvasChanged):
        _draw(adapter, binding, canvas, tool_regions=(TOOL,))
    assert calls["n"] == 1  # stroke 1 lies at y=116 for this canvas
