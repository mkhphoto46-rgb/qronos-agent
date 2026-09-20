from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence

from core.window_binding import WindowRect


MAX_STROKES = 64
MAX_POINTS_PER_STROKE = 512
MAX_TOTAL_POINTS = 4096


class ShapePlanError(ValueError):
    """A proposed vector shape is malformed or exceeds bounded limits."""


@dataclass(frozen=True)
class NormalizedPoint:
    """One canvas-independent point in normalized ``0.0 .. 1.0`` space."""

    x: float
    y: float

    def __post_init__(self) -> None:
        x = float(self.x)
        y = float(self.y)
        if not math.isfinite(x) or not math.isfinite(y):
            raise ShapePlanError("Shape coordinates must be finite numbers.")
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ShapePlanError(
                f"Normalized point ({x}, {y}) is outside the 0.0..1.0 canvas."
            )
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)


@dataclass(frozen=True)
class StrokePlan:
    """One continuous pointer stroke, independent of any application."""

    points: tuple[NormalizedPoint, ...]

    def __post_init__(self) -> None:
        points = tuple(self.points)
        if len(points) < 2:
            raise ShapePlanError("A stroke requires at least two points.")
        if len(points) > MAX_POINTS_PER_STROKE:
            raise ShapePlanError(
                f"A stroke may contain at most {MAX_POINTS_PER_STROKE} points."
            )
        if len({(point.x, point.y) for point in points}) < 2:
            raise ShapePlanError("A stroke must contain at least two distinct points.")
        object.__setattr__(self, "points", points)

    @classmethod
    def from_xy(cls, points: Iterable[tuple[float, float]]) -> "StrokePlan":
        return cls(tuple(NormalizedPoint(x, y) for x, y in points))


@dataclass(frozen=True)
class ShapePlan:
    """
    Bounded vector plan that can be rendered by any pointer-capable adapter.

    The plan deliberately contains no Paint, HWND, monitor, or screen coordinates.
    Adapters project it into their own verified ``WindowRect`` immediately before
    execution.  This lets the same plan drive Paint today and another canvas-like
    application later.
    """

    strokes: tuple[StrokePlan, ...]
    name: str = "custom"

    def __post_init__(self) -> None:
        strokes = tuple(self.strokes)
        if not strokes:
            raise ShapePlanError("A shape requires at least one stroke.")
        if len(strokes) > MAX_STROKES:
            raise ShapePlanError(f"A shape may contain at most {MAX_STROKES} strokes.")
        total = sum(len(stroke.points) for stroke in strokes)
        if total > MAX_TOTAL_POINTS:
            raise ShapePlanError(
                f"A shape may contain at most {MAX_TOTAL_POINTS} total points."
            )
        name = str(self.name or "custom").strip() or "custom"
        object.__setattr__(self, "strokes", strokes)
        object.__setattr__(self, "name", name)

    @classmethod
    def from_strokes(
        cls,
        strokes: Iterable[Iterable[tuple[float, float]]],
        *,
        name: str = "custom",
    ) -> "ShapePlan":
        """Build an arbitrary simple shape from normalized vector strokes."""

        return cls(
            strokes=tuple(StrokePlan.from_xy(stroke) for stroke in strokes),
            name=name,
        )

    def to_screen_strokes(
        self,
        rect: WindowRect,
        *,
        inset_pixels: int = 0,
        preserve_aspect: bool = False,
    ) -> tuple[tuple[tuple[int, int], ...], ...]:
        """Project normalized geometry into a caller-owned verified screen rect.

        ``preserve_aspect`` fits the unit-square plan into the largest centered
        square inside the caller-owned rectangle. This avoids distorting
        normalized geometry when a preview or canvas is rectangular.
        """

        if not isinstance(inset_pixels, int) or isinstance(inset_pixels, bool):
            raise ShapePlanError("inset_pixels must be an integer.")
        if not isinstance(preserve_aspect, bool):
            raise ShapePlanError("preserve_aspect must be a bool.")
        if inset_pixels < 0:
            raise ShapePlanError("inset_pixels cannot be negative.")
        if not rect.usable:
            raise ShapePlanError("Cannot project a shape into an unusable rectangle.")

        inner = WindowRect(
            left=rect.left + inset_pixels,
            top=rect.top + inset_pixels,
            right=rect.right - inset_pixels,
            bottom=rect.bottom - inset_pixels,
        )
        if not inner.usable:
            raise ShapePlanError("inset_pixels consumes the entire target rectangle.")

        if preserve_aspect:
            side = min(inner.width, inner.height)
            left = inner.left + (inner.width - side) // 2
            top = inner.top + (inner.height - side) // 2
            inner = WindowRect(
                left=left,
                top=top,
                right=left + side,
                bottom=top + side,
            )

        # Win32 RECT right/bottom are exclusive.  Using width-1/height-1 ensures
        # normalized 1.0 still maps to a point accepted by BoundInputGuard.
        span_x = inner.width - 1
        span_y = inner.height - 1

        return tuple(
            tuple(
                (
                    inner.left + round(point.x * span_x),
                    inner.top + round(point.y * span_y),
                )
                for point in stroke.points
            )
            for stroke in self.strokes
        )


class ShapePlanner:
    """
    Generic normalized vector planner.

    Named helpers are conveniences, not an allow-list.  ``custom`` accepts any
    bounded simple shape produced by a deterministic planner or a future LLM
    structured-output layer.
    """

    @staticmethod
    def custom(
        strokes: Iterable[Iterable[tuple[float, float]]],
        *,
        name: str = "custom",
    ) -> ShapePlan:
        return ShapePlan.from_strokes(strokes, name=name)

    @staticmethod
    def polyline(
        points: Sequence[tuple[float, float]],
        *,
        close: bool = False,
        name: str = "polyline",
    ) -> ShapePlan:
        path = list(points)
        if close:
            if not path:
                raise ShapePlanError("Cannot close an empty polyline.")
            if path[-1] != path[0]:
                path.append(path[0])
        return ShapePlan.from_strokes((path,), name=name)

    @staticmethod
    def line(
        start: tuple[float, float] = (0.15, 0.5),
        end: tuple[float, float] = (0.85, 0.5),
    ) -> ShapePlan:
        return ShapePlanner.polyline((start, end), name="line")

    @staticmethod
    def rectangle(*, margin: float = 0.15) -> ShapePlan:
        margin = _bounded_margin(margin)
        low = margin
        high = 1.0 - margin
        return ShapePlanner.polyline(
            ((low, low), (high, low), (high, high), (low, high)),
            close=True,
            name="rectangle",
        )

    @staticmethod
    def triangle(*, margin: float = 0.15) -> ShapePlan:
        margin = _bounded_margin(margin)
        return ShapePlanner.polyline(
            (
                (0.5, margin),
                (1.0 - margin, 1.0 - margin),
                (margin, 1.0 - margin),
            ),
            close=True,
            name="triangle",
        )

    @staticmethod
    def regular_polygon(
        sides: int,
        *,
        radius: float = 0.36,
        rotation_radians: float = -math.pi / 2.0,
    ) -> ShapePlan:
        if isinstance(sides, bool) or not isinstance(sides, int) or sides < 3 or sides > 64:
            raise ShapePlanError("Polygon sides must be an integer from 3 through 64.")
        radius = _bounded_radius(radius)
        points = _radial_points(sides, radius, rotation_radians)
        return ShapePlanner.polyline(points, close=True, name=f"polygon-{sides}")

    @staticmethod
    def circle(*, radius: float = 0.36, segments: int = 64) -> ShapePlan:
        if (
            isinstance(segments, bool)
            or not isinstance(segments, int)
            or not (12 <= segments <= 256)
        ):
            raise ShapePlanError("Circle segments must be an integer from 12 through 256.")
        radius = _bounded_radius(radius)
        points = _radial_points(segments, radius, 0.0)
        return ShapePlanner.polyline(points, close=True, name="circle")

    @staticmethod
    def star(
        *,
        points: int = 5,
        outer_radius: float = 0.38,
        inner_radius: float = 0.16,
        rotation_radians: float = -math.pi / 2.0,
    ) -> ShapePlan:
        if isinstance(points, bool) or not isinstance(points, int) or not (3 <= points <= 32):
            raise ShapePlanError("Star points must be an integer from 3 through 32.")
        outer_radius = _bounded_radius(outer_radius)
        inner_radius = _bounded_radius(inner_radius)
        if inner_radius >= outer_radius:
            raise ShapePlanError("Star inner_radius must be smaller than outer_radius.")

        vertices: list[tuple[float, float]] = []
        for index in range(points * 2):
            radius = outer_radius if index % 2 == 0 else inner_radius
            angle = rotation_radians + (math.pi * index / points)
            vertices.append(
                (0.5 + radius * math.cos(angle), 0.5 + radius * math.sin(angle))
            )
        return ShapePlanner.polyline(vertices, close=True, name="star")

    @staticmethod
    def arrow() -> ShapePlan:
        # Separate head strokes keep the representation useful outside Paint as
        # well; no adapter-specific mouse semantics are embedded here.
        return ShapePlan.from_strokes(
            (
                ((0.15, 0.5), (0.82, 0.5)),
                ((0.62, 0.30), (0.82, 0.5)),
                ((0.62, 0.70), (0.82, 0.5)),
            ),
            name="arrow",
        )


def _bounded_margin(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not (0.0 < value < 0.5):
        raise ShapePlanError("margin must be finite and strictly between 0.0 and 0.5.")
    return value


def _bounded_radius(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not (0.0 < value <= 0.5):
        raise ShapePlanError("radius must be finite and in the interval (0.0, 0.5].")
    return value


def _radial_points(
    count: int,
    radius: float,
    rotation_radians: float,
) -> tuple[tuple[float, float], ...]:
    return tuple(
        (
            0.5 + radius * math.cos(rotation_radians + (2.0 * math.pi * i / count)),
            0.5 + radius * math.sin(rotation_radians + (2.0 * math.pi * i / count)),
        )
        for i in range(count)
    )
