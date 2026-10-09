"""D3 N1 (whole-control hover highlight) and D4 N2 (plan not approved), through the real paths.

The generic visual click runs unmodified: VisualActionExecutor.click_target ->
confirmed_press -> the production MouseHover/MouseButton/MouseClick controllers
composed as InputCapabilities.for_binder does (smooth motion). Only the native
layer is faked: window snapshots (a real WindowBinder over a fake snapshot), the
cursor/move/button functions, and the window picture (an in-memory array, PNG
encoded and prepared by the production core.vision_image.prepare_bytes). The
locators are fakes of the two real kinds: one that grounds a box with the point
(like VisionGrounder, through the production ground_structured_response) and a
point-only one (like VisualTargetLocator, through ground_point). Production
thresholds are not patched. No screen, window, input, model or Paint is touched.
"""

from __future__ import annotations

import json
import time
from io import BytesIO
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from core.actions import ActionRequest, ActionResult, ActionOutcome
from core.grounding_freshness import state_epoch
from core.input_capabilities import InputCapabilities
from core.mouse_button import MouseButtonController
from core.mouse_click import MouseClickController
from core.mouse_hover import MouseHoverController
from core.paint_adapter import PaintAdapter, PaintPlanNotApproved
from core.paint_draw_integration import (
    PaintDrawExecution,
    PaintDrawSideEffects,
    paint_draw_failure_response_fa,
    paint_failure_kind,
)
from core.pointer_stroke import PointerStrokeController
from core.screen_capture import Capture, DisplayGeometry
from core.shape_planner import ShapePlanner
from core.sketch_approval import VisualApprovedSketch
from core.sketch_preview import digest_shape_plan
from core.vision_grounding import GroundingStale, ground_point, ground_structured_response
from core.vision_image import prepare_bytes
from core.visual_action_executor import VisualActionExecutor
from core.visual_targeting import VisualTarget
from core.window_binding import WindowBinder, WindowRect, WindowSnapshot, WindowTarget
from security.gate import evaluate, issue_grant
from security.permissions import ActionCategory


@pytest.fixture(autouse=True)
def private_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("QRONOS_PERMISSION_POLICY_PATH", str(tmp_path / "permissions.json"))
    monkeypatch.setenv("QRONOS_PERMISSION_AUDIT_PATH", str(tmp_path / "permission-audit.jsonl"))


class Desk:
    """The fake native layer: the window picture, a cursor and a left button; records every call."""

    def __init__(self, pixels: np.ndarray):
        self.pixels = pixels
        self.cursor = (0, 0)
        self.events: list[tuple] = []
        self.presses = 0
        self.on_arrive = None

    def move(self, x, y):
        self.cursor = (int(x), int(y))
        self.events.append(("move", *self.cursor))
        if self.on_arrive is not None:
            self.on_arrive(self, *self.cursor)

    def cursor_fn(self):
        return self.cursor

    def button(self, name, down):
        self.events.append(("down" if down else "up", *self.cursor))
        if down:
            self.presses += 1


class Window:
    def __init__(self, desk: Desk):
        self.desk = desk

    def capture_bound(self, binding, binder, **kwargs):
        self.desk.events.append(("capture",))
        rect = binding.snapshot.window_rect
        buffer = BytesIO()
        Image.fromarray(self.desk.pixels, "RGB").save(buffer, format="PNG")
        return Capture(
            image=prepare_bytes(buffer.getvalue()),
            geometry=DisplayGeometry(rect.width, rect.height, rect.width, rect.height),
            window=binding.hwnd, binding_token=binding.identity_token, layout_token=binding.layout_token,
            source_width=rect.width, source_height=rect.height, frame_rect=rect,
            captured_at=time.monotonic(), state_epoch=state_epoch(binding.identity_token),
        )


def bind(width, height, dpi=96):
    snapshot = WindowSnapshot(
        hwnd=8181, pid=5151, thread_id=5252, title="Files", process_path="C:/Apps/explorer.exe",
        process_start_time_100ns=77, window_rect=WindowRect(0, 0, width, height),
        client_rect=WindowRect(8, 30, width - 8, height - 8), dpi=dpi, visible=True, minimized=False,
        foreground=True,
    )
    binder = WindowBinder(enumerate_fn=lambda: (snapshot,), snapshot_fn=lambda hwnd: snapshot)
    binding = binder.bind(WindowTarget(app_id="files", display_name="files", executable="explorer.exe",
                                       process_names=("explorer.exe",)), require_foreground=True)
    return binder, binding


def inputs_for(binder, desk):
    hover = MouseHoverController(binder, move_fn=desk.move, cursor_fn=desk.cursor_fn, sleep_fn=lambda s: None)
    buttons = MouseButtonController(binder, hover=hover, button_fn=desk.button, smooth_motion=True)
    return InputCapabilities(
        hover=hover, buttons=buttons, click=MouseClickController(binder, hover=hover, buttons=buttons),
        stroke=PointerStrokeController(binder, hover=hover, buttons=buttons, sleep_fn=lambda s: None),
        keyboard=None,
    )


def grant():
    request = ActionRequest(category=ActionCategory.GUI_AUTOMATION, target="app:files", summary="click")
    verdict = evaluate(request)
    return issue_grant(request, verdict, approval=verdict.decision if verdict.needs_approval else None)


def files_window(width=1280, height=800):
    """A file list with a selected row, item icons and labels, a status label and a red Delete button."""
    picture = np.full((height, width, 3), 255, dtype=np.uint8)
    for row in range(12):
        top = 120 + row * 40
        if row == 2:
            picture[top: top + 36, 20: width - 20] = (204, 232, 255)
        picture[top + 10: top + 26, 30:50] = (30, 90, 200)
        for i in range(10):
            if (row * 5 + i * 3) % 4 < 2:
                picture[top + 13: top + 23, 70 + i * 14: 80 + i * 14] = 40
    for i in range(8):
        if i % 3 == 0:
            picture[height - 60: height - 50, 40 + i * 9: 46 + i * 9] = 60
    picture[height - 100: height - 70, width - 160: width - 60] = (180, 30, 30)  # Delete: 1120..1220 x 700..730
    return picture


DELETE_POINT = (1170, 715)
DELETE_BOX = (100, 30)


class BoxLocator:
    """Like VisionGrounder: the model's point AND box, validated by ground_structured_response."""

    def __init__(self, point=DELETE_POINT, box=DELETE_BOX, centre=None, during=None):
        self.point, self.box, self.centre, self.during = point, box, centre or point, during

    def locate(self, description, frame):
        scale = frame.image.width / frame.source_width
        x, y = round(self.point[0] * scale), round(self.point[1] * scale)
        cx, cy = round(self.centre[0] * scale), round(self.centre[1] * scale)
        w, h = round(self.box[0] * scale), round(self.box[1] * scale)
        target = ground_structured_response(frame, json.dumps({
            "status": "found", "image_width": frame.image.width, "image_height": frame.image.height,
            "candidates": [{"label": description, "confidence": 0.95, "point": {"x": x, "y": y},
                            "bbox": {"x": cx - w // 2, "y": cy - h // 2, "width": w, "height": h}}],
        }))
        if self.during is not None:
            self.during()  # the world changes while the model is thinking (before the pre-motion check)
        return target


class PointLocator:
    """Like the production VisualTargetLocator: a point only, no box."""

    def locate(self, description, frame):
        scale = frame.image.width / frame.source_width
        point = ground_point(frame, image_x=round(DELETE_POINT[0] * scale), image_y=round(DELETE_POINT[1] * scale),
                             label=description)
        return VisualTarget(label=description, confidence=0.95, point=point)


def click(desk, locator, *, width=1280, height=800, dpi=96):
    binder, binding = bind(width, height, dpi)
    executor = VisualActionExecutor(binder, SimpleNamespace(acquire_foreground=lambda b, **kw: None), Window(desk),
                                    locator, inputs=inputs_for(binder, desk))
    return executor.click_target(binding, "Delete", grant=grant(), screen_approved=True)


def refused(desk, locator, **kwargs) -> str:
    with pytest.raises(GroundingStale) as stale:
        click(desk, locator, **kwargs)
    assert desk.presses == 0, desk.events[-6:]
    return str(stale.value)


def on_arrival(change):
    def hook(desk, x, y):
        if (x, y) == DELETE_POINT:
            change(desk.pixels)
    return hook


def hover_inside(left, top, right, bottom, colour=(210, 60, 60)):
    """Like a real button: it lights up as a whole while the cursor is over it."""
    def hook(desk, x, y):
        if left <= x < right and top <= y < bottom:
            desk.pixels[top:bottom, left:right] = colour
    return hook


def whole_button_highlight(pixels):
    pixels[700:730, 1120:1220] = (210, 60, 60)  # the grounded button box lights up as a whole


# ------------------------------------------------------------------ N1: what must still be pressed


def test_the_whole_buttons_own_hover_highlight_is_pressed_once_at_the_point():
    desk = Desk(files_window())
    desk.on_arrive = on_arrival(whole_button_highlight)
    click(desk, BoxLocator())
    assert desk.presses == 1 and tuple(desk.pixels[705, 1125]) == (210, 60, 60)  # highlighted, then pressed
    assert [e for e in desk.events if e[0] == "down"] == [("down", *DELETE_POINT)]


def test_a_highlight_with_a_one_pixel_glow_around_the_button_is_pressed():
    desk = Desk(files_window())
    desk.on_arrive = on_arrival(lambda p: p.__setitem__((slice(697, 733), slice(1117, 1223)), (210, 60, 60)))
    click(desk, BoxLocator())
    assert desk.presses == 1 and tuple(desk.pixels[698, 1118]) == (210, 60, 60)


def test_a_wide_toolbar_button_at_twice_the_dpi_is_pressed():
    """500x150 native pixels is a button at 192 DPI (250x75 logical)."""
    picture = np.full((1600, 2560, 3), 255, dtype=np.uint8)
    picture[1300:1450, 1900:2400] = (180, 30, 30)
    desk = Desk(picture)
    desk.on_arrive = hover_inside(1900, 1300, 2400, 1450)
    click(desk, BoxLocator(point=(2150, 1375), box=(500, 150)), width=2560, height=1600, dpi=192)
    assert desk.presses == 1 and tuple(desk.pixels[1310, 1910]) == (210, 60, 60)


def test_a_tiny_grounded_box_never_shrinks_the_point_scope_allowance():
    desk = Desk(files_window())
    desk.on_arrive = on_arrival(lambda p: p.__setitem__((slice(709, 722), slice(1164, 1177)), (210, 60, 60)))
    click(desk, BoxLocator(box=(4, 4)))
    assert desk.presses == 1 and tuple(desk.pixels[710, 1165]) == (210, 60, 60)


# ------------------------------------------------------------------ N1: what must still be refused


def test_a_tooltip_outside_the_grounded_box_still_refuses_with_no_press():
    desk = Desk(files_window())

    def tooltip(pixels):
        whole_button_highlight(pixels)
        pixels[740:770, 1040:1240] = (255, 255, 225)
        pixels[750:760, 1050:1200] = 30

    desk.on_arrive = on_arrival(tooltip)
    assert "cursor moved" in refused(desk, BoxLocator())


def test_a_changed_surrounding_action_context_after_the_motion_refuses():
    """The hover allowance is the button; the selected row and status label next to it are not."""
    desk = Desk(files_window())

    def context(pixels):
        whole_button_highlight(pixels)
        pixels[120 + 2 * 40: 120 + 2 * 40 + 36, 20:1260] = 255        # selection cleared
        pixels[120 + 7 * 40: 120 + 7 * 40 + 36, 20:1260] = (204, 232, 255)  # another row selected

    desk.on_arrive = on_arrival(context)
    refused(desk, BoxLocator())


def test_before_the_motion_the_button_box_is_not_widened():
    """Changed while the model was thinking: the right half of the button (outside the point scope) changes.
    The pre-motion confirmation stays strict: refused before any motion or press."""
    desk = Desk(files_window())

    def change():
        desk.pixels[700:730, 1190:1220] = (196, 66, 66)

    refused(desk, BoxLocator(during=change))
    assert not any(e[0] == "move" for e in desk.events)


def test_an_enormous_grounded_box_is_not_used_and_the_point_scope_stays_strict():
    """A 'box' covering half the window is not a button: the whole-button highlight is refused as before."""
    desk = Desk(files_window())
    desk.on_arrive = on_arrival(whole_button_highlight)
    refused(desk, BoxLocator(box=(1100, 500), centre=(700, 520)))


def test_the_same_box_is_too_big_at_ordinary_dpi():
    picture = np.full((1600, 2560, 3), 255, dtype=np.uint8)
    picture[1300:1450, 1900:2400] = (180, 30, 30)
    desk = Desk(picture)
    desk.on_arrive = hover_inside(1900, 1300, 2400, 1450)
    refused(desk, BoxLocator(point=(2150, 1375), box=(500, 150)), width=2560, height=1600, dpi=96)
    assert tuple(desk.pixels[1310, 1910]) == (210, 60, 60)  # the highlight did appear


def test_a_box_reaching_out_of_the_client_area_is_clipped_to_it():
    """The grounded box runs up into the title bar (rows 0-30). Clipped to the client area it is the
    button: the button's own highlight is pressed, a change in the title bar after the motion is not."""
    locator = dict(point=(1170, 45), box=(100, 60), centre=(1170, 30))  # rows 0..60; client starts at 30

    def scene():
        picture = files_window()
        picture[30:60, 1120:1220] = (180, 30, 30)
        return picture

    def highlight(pixels):
        pixels[30:60, 1120:1220] = (210, 60, 60)

    desk = Desk(scene())
    desk.on_arrive = lambda d, x, y: (x, y) == (1170, 45) and highlight(d.pixels)
    click(desk, BoxLocator(**locator))
    assert desk.presses == 1 and tuple(desk.pixels[32, 1125]) == (210, 60, 60)

    desk = Desk(scene())

    def title_bar(pixels):
        highlight(pixels)
        pixels[4:24, 1120:1220] = (0, 0, 0)  # above the client area, inside the model's box

    desk.on_arrive = lambda d, x, y: (x, y) == (1170, 45) and title_bar(d.pixels)
    refused(desk, BoxLocator(**locator))


def test_a_point_only_locator_grounds_no_box_so_only_the_point_scope_may_change():
    desk = Desk(files_window())
    desk.on_arrive = on_arrival(whole_button_highlight)
    refused(desk, PointLocator())

    small = Desk(files_window())
    small.on_arrive = on_arrival(lambda p: p.__setitem__((slice(709, 722), slice(1164, 1177)), (210, 60, 60)))
    click(small, PointLocator())
    assert small.presses == 1 and tuple(small.pixels[710, 1165]) == (210, 60, 60)


# ------------------------------------------------------------------ N2: plan not approved


def test_a_sketch_whose_plan_is_not_the_approved_one_is_refused_and_named_honestly(tmp_path):
    approved = ShapePlanner.custom((((0.1, 0.1), (0.9, 0.9)),), name="approved")
    swapped = ShapePlanner.custom((((0.1, 0.9), (0.9, 0.1)),), name="approved")
    buffer = BytesIO()
    Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
    sketch = VisualApprovedSketch(
        request="draw a line", plan=swapped, plan_digest=digest_shape_plan(approved), preview_digest="0" * 64,
        preview_image=prepare_bytes(buffer.getvalue()), critic_model="fixture", critic_confidence=0.9,
        candidate_attempts=1, critic_summary="fixture",
    )
    picture = np.full((300, 400, 3), 255, dtype=np.uint8)
    desk = Desk(picture)
    binder, binding = bind(400, 300)
    adapter = PaintAdapter(binder, inputs=inputs_for(binder, desk), capture=Window(desk))
    with pytest.raises(PaintPlanNotApproved) as refusal:
        adapter.execute_approved(binding, sketch, None, authorization=None, tool_regions=())
    assert desk.events == []  # nothing captured, moved or pressed

    kind = paint_failure_kind(refusal.value)
    assert kind == "plan_not_approved"
    request = ActionRequest(category=ActionCategory.GUI_AUTOMATION, target="app:paint", summary="draw")
    execution = PaintDrawExecution(
        request, ActionResult(action_id=request.action_id, outcome=ActionOutcome.FAILED, detail=str(refusal.value)),
        side_effects=PaintDrawSideEffects(), failure_kind=kind)
    answer = paint_draw_failure_response_fa(execution)
    assert "طرح تأییدشده" in answer and "پنجرهٔ Paint" not in answer
    assert "هیچ تغییری ایجاد نشد" in answer and "digest" not in answer
