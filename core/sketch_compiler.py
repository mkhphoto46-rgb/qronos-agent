from __future__ import annotations

import math

from core.shape_planner import ShapePlan, ShapePlanError
from core.sketch_dsl import (
    ArcOp,
    BezierOp,
    EllipseOp,
    LineOp,
    PolygonOp,
    PolylineOp,
    RectangleOp,
    SketchDocument,
    SketchDslError,
)


class SketchCompileError(SketchDslError):
    """A validated sketch could not be compiled into a bounded ShapePlan."""


def compile_sketch(document: SketchDocument) -> ShapePlan:
    """Compile validated DSL primitives into the existing generic stroke plan."""

    strokes: list[tuple[tuple[float, float], ...]] = []

    for operation in document.operations:
        if isinstance(operation, LineOp):
            strokes.append((operation.start, operation.end))
        elif isinstance(operation, PolylineOp):
            points = list(operation.points)
            if operation.closed and points[-1] != points[0]:
                points.append(points[0])
            strokes.append(tuple(points))
        elif isinstance(operation, PolygonOp):
            points = list(operation.points)
            if points[-1] != points[0]:
                points.append(points[0])
            strokes.append(tuple(points))
        elif isinstance(operation, RectangleOp):
            x0, y0 = operation.x, operation.y
            x1, y1 = x0 + operation.width, y0 + operation.height
            strokes.append(
                ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))
            )
        elif isinstance(operation, EllipseOp):
            strokes.append(
                _ellipse_points(
                    operation.cx,
                    operation.cy,
                    operation.rx,
                    operation.ry,
                    0.0,
                    360.0,
                    operation.segments,
                    close=True,
                )
            )
        elif isinstance(operation, ArcOp):
            strokes.append(
                _ellipse_points(
                    operation.cx,
                    operation.cy,
                    operation.rx,
                    operation.ry,
                    operation.start_degrees,
                    operation.end_degrees,
                    operation.segments,
                    close=False,
                )
            )
        elif isinstance(operation, BezierOp):
            strokes.append(_bezier_points(operation))
        else:  # pragma: no cover - defensive against future unvalidated subclasses.
            raise SketchCompileError(
                f"Unsupported validated sketch operation: {type(operation).__name__}."
            )

    try:
        return ShapePlan.from_strokes(strokes, name=document.name)
    except ShapePlanError as exc:
        raise SketchCompileError(f"Compiled sketch violates ShapePlan bounds: {exc}") from exc


def _ellipse_points(
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    start_degrees: float,
    end_degrees: float,
    segments: int,
    *,
    close: bool,
) -> tuple[tuple[float, float], ...]:
    start = math.radians(start_degrees)
    span = math.radians(end_degrees - start_degrees)
    if close:
        points = tuple(
            (
                cx + rx * math.cos(start + span * (index / segments)),
                cy + ry * math.sin(start + span * (index / segments)),
            )
            for index in range(segments)
        )
        return (*points, points[0])

    return tuple(
        (
            cx + rx * math.cos(start + span * (index / segments)),
            cy + ry * math.sin(start + span * (index / segments)),
        )
        for index in range(segments + 1)
    )


def _bezier_points(operation: BezierOp) -> tuple[tuple[float, float], ...]:
    points: list[tuple[float, float]] = []
    for index in range(operation.segments + 1):
        t = index / operation.segments
        u = 1.0 - t
        x = (
            (u ** 3) * operation.p0[0]
            + 3.0 * (u ** 2) * t * operation.p1[0]
            + 3.0 * u * (t ** 2) * operation.p2[0]
            + (t ** 3) * operation.p3[0]
        )
        y = (
            (u ** 3) * operation.p0[1]
            + 3.0 * (u ** 2) * t * operation.p1[1]
            + 3.0 * u * (t ** 2) * operation.p2[1]
            + (t ** 3) * operation.p3[1]
        )
        points.append((x, y))
    return tuple(points)
