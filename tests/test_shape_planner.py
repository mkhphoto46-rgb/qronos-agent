from __future__ import annotations

import math

import pytest

from core.shape_planner import (
    MAX_POINTS_PER_STROKE,
    NormalizedPoint,
    ShapePlan,
    ShapePlanError,
    ShapePlanner,
)
from core.window_binding import WindowRect


def test_custom_plan_is_not_limited_to_named_shapes():
    plan = ShapePlanner.custom(
        (
            ((0.10, 0.20), (0.30, 0.40), (0.50, 0.20)),
            ((0.60, 0.70), (0.80, 0.90)),
        ),
        name="future-brain-output",
    )

    assert plan.name == "future-brain-output"
    assert len(plan.strokes) == 2
    assert len(plan.strokes[0].points) == 3


def test_normalized_point_rejects_non_finite_and_out_of_frame_values():
    for x, y in ((-0.1, 0.5), (1.1, 0.5), (math.nan, 0.5), (0.5, math.inf)):
        with pytest.raises(ShapePlanError):
            NormalizedPoint(x, y)


def test_stroke_requires_real_motion():
    with pytest.raises(ShapePlanError):
        ShapePlan.from_strokes((((0.2, 0.2), (0.2, 0.2)),))


def test_shape_complexity_is_bounded():
    too_many = tuple((i / MAX_POINTS_PER_STROKE, 0.5) for i in range(MAX_POINTS_PER_STROKE + 1))
    with pytest.raises(ShapePlanError):
        ShapePlan.from_strokes((too_many,))


def test_projection_uses_caller_rect_and_supports_secondary_monitor_coordinates():
    plan = ShapePlanner.line((0.0, 0.0), (1.0, 1.0))
    rect = WindowRect(left=2562, top=324, right=3582, bottom=749)

    assert plan.to_screen_strokes(rect) == (((2562, 324), (3581, 748)),)


def test_projection_keeps_right_and_bottom_exclusive():
    plan = ShapePlanner.rectangle(margin=0.1)
    rect = WindowRect(100, 200, 500, 600)

    for stroke in plan.to_screen_strokes(rect):
        for x, y in stroke:
            assert rect.left <= x < rect.right
            assert rect.top <= y < rect.bottom


def test_projection_can_apply_adapter_owned_inset():
    plan = ShapePlanner.line((0.0, 0.5), (1.0, 0.5))
    rect = WindowRect(100, 100, 500, 400)

    stroke = plan.to_screen_strokes(rect, inset_pixels=20)[0]

    assert stroke[0][0] == 120
    assert stroke[-1][0] == 479
    assert all(120 <= y <= 379 for _, y in stroke)


def test_rectangle_is_one_closed_generic_stroke():
    plan = ShapePlanner.rectangle()
    points = plan.strokes[0].points

    assert len(plan.strokes) == 1
    assert points[0] == points[-1]
    assert len(points) == 5


def test_triangle_is_closed():
    plan = ShapePlanner.triangle()
    assert plan.strokes[0].points[0] == plan.strokes[0].points[-1]
    assert len(plan.strokes[0].points) == 4


def test_circle_is_bounded_closed_polyline_not_a_paint_primitive():
    plan = ShapePlanner.circle(segments=48)
    points = plan.strokes[0].points

    assert plan.name == "circle"
    assert len(points) == 49
    assert points[0] == points[-1]
    assert all(0.0 <= p.x <= 1.0 and 0.0 <= p.y <= 1.0 for p in points)


def test_regular_polygon_supports_arbitrary_simple_polygons():
    plan = ShapePlanner.regular_polygon(7)
    assert len(plan.strokes[0].points) == 8
    assert plan.strokes[0].points[0] == plan.strokes[0].points[-1]


def test_star_is_geometry_not_a_hardcoded_mouse_script():
    plan = ShapePlanner.star(points=5)
    assert len(plan.strokes) == 1
    assert len(plan.strokes[0].points) == 11
    assert plan.strokes[0].points[0] == plan.strokes[0].points[-1]


def test_arrow_demonstrates_multi_stroke_composition():
    plan = ShapePlanner.arrow()
    assert len(plan.strokes) == 3
    assert all(len(stroke.points) == 2 for stroke in plan.strokes)


def test_invalid_factory_parameters_fail_closed():
    with pytest.raises(ShapePlanError):
        ShapePlanner.rectangle(margin=0.5)
    with pytest.raises(ShapePlanError):
        ShapePlanner.circle(segments=5)
    with pytest.raises(ShapePlanError):
        ShapePlanner.regular_polygon(2)
    with pytest.raises(ShapePlanError):
        ShapePlanner.star(inner_radius=0.4, outer_radius=0.3)


def test_projection_can_preserve_aspect_inside_rectangular_canvas():
    plan = ShapePlanner.line((0.0, 0.0), (1.0, 1.0))
    rect = WindowRect(100, 200, 700, 500)

    stroke = plan.to_screen_strokes(rect, preserve_aspect=True)[0]

    # Largest centered square is 300x300 at x=250..550.
    assert stroke == ((250, 200), (549, 499))


def test_projection_rejects_non_boolean_preserve_aspect():
    with pytest.raises(ShapePlanError, match="preserve_aspect"):
        ShapePlanner.line().to_screen_strokes(
            WindowRect(0, 0, 400, 300),
            preserve_aspect=1,  # type: ignore[arg-type]
        )
