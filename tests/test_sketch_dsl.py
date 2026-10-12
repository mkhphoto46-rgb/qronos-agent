from __future__ import annotations

import json
import math

import pytest

from core.sketch_dsl import (
    MAX_SKETCH_OPERATIONS,
    ArcOp,
    BezierOp,
    EllipseOp,
    SketchDslError,
    parse_sketch_json,
)


def _doc(operations):
    return json.dumps({"version": 1, "name": "test", "operations": operations})


def test_accepts_general_primitives_without_named_shape_library():
    parsed = parse_sketch_json(
        _doc(
            [
                {"type": "line", "start": [0.1, 0.2], "end": [0.8, 0.2]},
                {
                    "type": "bezier",
                    "p0": [0.1, 0.7],
                    "p1": [0.3, 0.2],
                    "p2": [0.7, 0.2],
                    "p3": [0.9, 0.7],
                },
            ]
        )
    )
    assert len(parsed.operations) == 2
    assert isinstance(parsed.operations[1], BezierOp)


def test_rejects_prose_or_markdown_around_json():
    payload = _doc([{"type": "line", "start": [0.1, 0.1], "end": [0.9, 0.9]}])
    with pytest.raises(SketchDslError):
        parse_sketch_json("Here is the drawing:\n" + payload)
    with pytest.raises(SketchDslError):
        parse_sketch_json("```json\n" + payload + "\n```")


def test_rejects_duplicate_json_keys():
    with pytest.raises(SketchDslError, match="Duplicate JSON key"):
        parse_sketch_json(
            '{"version":1,"version":1,"operations":'
            '[{"type":"line","start":[0,0],"end":[1,1]}]}'
        )


@pytest.mark.parametrize(
    "value",
    [
        '{"version":1,"operations":[{"type":"powershell","command":"x"}]}',
        '{"version":1,"operations":[{"type":"line","start":[-0.1,0],"end":[1,1]}]}',
        '{"version":1,"operations":[{"type":"line","start":[true,0],"end":[1,1]}]}',
        '{"version":1,"operations":[{"type":"line","start":[NaN,0],"end":[1,1]}]}',
        '{"version":1,"operations":[{"type":"line","start":[0,0],"end":[1,1],"extra":1}]}',
    ],
)
def test_rejects_unknown_unsafe_or_malformed_values(value):
    with pytest.raises(SketchDslError):
        parse_sketch_json(value)


def test_rectangle_must_remain_inside_normalized_canvas():
    with pytest.raises(SketchDslError):
        parse_sketch_json(
            _doc(
                [
                    {
                        "type": "rectangle",
                        "x": 0.8,
                        "y": 0.2,
                        "width": 0.3,
                        "height": 0.2,
                    }
                ]
            )
        )


def test_ellipse_and_arc_bounds_are_checked():
    parsed = parse_sketch_json(
        _doc(
            [
                {
                    "type": "ellipse",
                    "cx": 0.5,
                    "cy": 0.5,
                    "rx": 0.3,
                    "ry": 0.2,
                },
                {
                    "type": "arc",
                    "cx": 0.5,
                    "cy": 0.5,
                    "rx": 0.3,
                    "ry": 0.2,
                    "start_degrees": 20,
                    "end_degrees": 220,
                },
            ]
        )
    )
    assert isinstance(parsed.operations[0], EllipseOp)
    assert isinstance(parsed.operations[1], ArcOp)

    with pytest.raises(SketchDslError):
        parse_sketch_json(
            _doc(
                [
                    {
                        "type": "ellipse",
                        "cx": 0.1,
                        "cy": 0.5,
                        "rx": 0.3,
                        "ry": 0.2,
                    }
                ]
            )
        )


def test_operation_count_is_bounded():
    operation = {"type": "line", "start": [0.1, 0.1], "end": [0.9, 0.9]}
    with pytest.raises(SketchDslError):
        parse_sketch_json(_doc([operation] * (MAX_SKETCH_OPERATIONS + 1)))
