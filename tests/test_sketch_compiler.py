from __future__ import annotations

import json

import pytest

from core.sketch_compiler import SketchCompileError, compile_sketch
from core.sketch_dsl import parse_sketch_json


def _compile(operations):
    document = parse_sketch_json(
        json.dumps({"version": 1, "name": "brain-sketch", "operations": operations})
    )
    return compile_sketch(document)


def test_compiles_arbitrary_composition_to_existing_shape_plan():
    plan = _compile(
        [
            {"type": "ellipse", "cx": 0.45, "cy": 0.5, "rx": 0.2, "ry": 0.12},
            {
                "type": "polygon",
                "points": [[0.65, 0.5], [0.85, 0.35], [0.85, 0.65]],
            },
            {"type": "line", "start": [0.25, 0.5], "end": [0.65, 0.5]},
        ]
    )

    assert plan.name == "brain-sketch"
    assert len(plan.strokes) == 3
    assert plan.strokes[1].points[0] == plan.strokes[1].points[-1]


def test_bezier_is_sampled_into_bounded_polyline():
    plan = _compile(
        [
            {
                "type": "bezier",
                "p0": [0.1, 0.8],
                "p1": [0.2, 0.1],
                "p2": [0.8, 0.1],
                "p3": [0.9, 0.8],
                "segments": 20,
            }
        ]
    )
    points = plan.strokes[0].points
    assert len(points) == 21
    assert (points[0].x, points[0].y) == pytest.approx((0.1, 0.8))
    assert (points[-1].x, points[-1].y) == pytest.approx((0.9, 0.8))


def test_arc_is_not_forced_closed():
    plan = _compile(
        [
            {
                "type": "arc",
                "cx": 0.5,
                "cy": 0.5,
                "rx": 0.3,
                "ry": 0.2,
                "start_degrees": 0,
                "end_degrees": 180,
                "segments": 16,
            }
        ]
    )
    assert plan.strokes[0].points[0] != plan.strokes[0].points[-1]


def test_compiler_inherits_shape_plan_total_point_bound():
    operations = [
        {
            "type": "ellipse",
            "cx": 0.5,
            "cy": 0.5,
            "rx": 0.2,
            "ry": 0.2,
            "segments": 256,
        }
        for _ in range(17)
    ]
    with pytest.raises(SketchCompileError):
        _compile(operations)
