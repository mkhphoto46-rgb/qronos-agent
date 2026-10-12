from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping, TypeAlias


SKETCH_SCHEMA_VERSION = 1
MAX_SKETCH_JSON_CHARS = 64 * 1024
MAX_SKETCH_OPERATIONS = 64
MAX_RAW_POINTS = 512
MAX_CURVE_SEGMENTS = 256


class SketchDslError(ValueError):
    """A model-proposed sketch document is malformed or outside safe bounds."""


Point: TypeAlias = tuple[float, float]


@dataclass(frozen=True)
class LineOp:
    start: Point
    end: Point
    kind: str = "line"


@dataclass(frozen=True)
class PolylineOp:
    points: tuple[Point, ...]
    closed: bool = False
    kind: str = "polyline"


@dataclass(frozen=True)
class PolygonOp:
    points: tuple[Point, ...]
    kind: str = "polygon"


@dataclass(frozen=True)
class RectangleOp:
    x: float
    y: float
    width: float
    height: float
    kind: str = "rectangle"


@dataclass(frozen=True)
class EllipseOp:
    cx: float
    cy: float
    rx: float
    ry: float
    segments: int = 64
    kind: str = "ellipse"


@dataclass(frozen=True)
class ArcOp:
    cx: float
    cy: float
    rx: float
    ry: float
    start_degrees: float
    end_degrees: float
    segments: int = 32
    kind: str = "arc"


@dataclass(frozen=True)
class BezierOp:
    p0: Point
    p1: Point
    p2: Point
    p3: Point
    segments: int = 32
    kind: str = "bezier"


SketchOperation: TypeAlias = (
    LineOp
    | PolylineOp
    | PolygonOp
    | RectangleOp
    | EllipseOp
    | ArcOp
    | BezierOp
)


@dataclass(frozen=True)
class SketchDocument:
    operations: tuple[SketchOperation, ...]
    name: str = "sketch"
    version: int = SKETCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        operations = tuple(self.operations)
        if not operations:
            raise SketchDslError("A sketch requires at least one operation.")
        if len(operations) > MAX_SKETCH_OPERATIONS:
            raise SketchDslError(
                f"A sketch may contain at most {MAX_SKETCH_OPERATIONS} operations."
            )
        name = str(self.name).strip()
        if not name:
            raise SketchDslError("Sketch name cannot be empty.")
        if len(name) > 80:
            raise SketchDslError("Sketch name may contain at most 80 characters.")
        if self.version != SKETCH_SCHEMA_VERSION:
            raise SketchDslError(
                f"Unsupported sketch schema version: {self.version!r}."
            )
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "name", name)


def parse_sketch_json(text: str) -> SketchDocument:
    """Parse exactly one strict JSON sketch document; surrounding prose is rejected."""

    if not isinstance(text, str):
        raise SketchDslError("Sketch output must be text containing one JSON object.")
    if not text.strip():
        raise SketchDslError("Sketch output is empty.")
    if len(text) > MAX_SKETCH_JSON_CHARS:
        raise SketchDslError(
            f"Sketch JSON exceeds the {MAX_SKETCH_JSON_CHARS}-character limit."
        )

    try:
        raw = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except SketchDslError:
        raise
    except json.JSONDecodeError as exc:
        raise SketchDslError(f"Sketch output is not strict JSON: {exc}.") from exc

    if not isinstance(raw, dict):
        raise SketchDslError("Sketch JSON root must be an object.")

    _require_exact_keys(raw, required={"version", "operations"}, optional={"name"}, where="root")

    version = _integer(raw["version"], "version", minimum=SKETCH_SCHEMA_VERSION, maximum=SKETCH_SCHEMA_VERSION)

    name_raw = raw.get("name", "sketch")
    if not isinstance(name_raw, str):
        raise SketchDslError("name must be a string.")
    name = name_raw.strip()
    if not name or len(name) > 80:
        raise SketchDslError("name must contain 1 through 80 characters.")

    operations_raw = raw["operations"]
    if not isinstance(operations_raw, list):
        raise SketchDslError("operations must be an array.")
    if not operations_raw:
        raise SketchDslError("operations cannot be empty.")
    if len(operations_raw) > MAX_SKETCH_OPERATIONS:
        raise SketchDslError(
            f"operations may contain at most {MAX_SKETCH_OPERATIONS} entries."
        )

    operations = tuple(
        _parse_operation(item, index)
        for index, item in enumerate(operations_raw)
    )
    return SketchDocument(operations=operations, name=name, version=version)


def _parse_operation(value: object, index: int) -> SketchOperation:
    where = f"operations[{index}]"
    if not isinstance(value, dict):
        raise SketchDslError(f"{where} must be an object.")

    kind = value.get("type")
    if not isinstance(kind, str):
        raise SketchDslError(f"{where}.type must be a string.")

    if kind == "line":
        _require_exact_keys(value, required={"type", "start", "end"}, where=where)
        start = _point(value["start"], f"{where}.start")
        end = _point(value["end"], f"{where}.end")
        if start == end:
            raise SketchDslError(f"{where} line endpoints must differ.")
        return LineOp(start=start, end=end)

    if kind == "polyline":
        _require_exact_keys(
            value,
            required={"type", "points"},
            optional={"closed"},
            where=where,
        )
        points = _points(value["points"], f"{where}.points", minimum=2)
        closed_raw = value.get("closed", False)
        if not isinstance(closed_raw, bool):
            raise SketchDslError(f"{where}.closed must be boolean.")
        if len(set(points)) < 2:
            raise SketchDslError(f"{where} must contain real motion.")
        return PolylineOp(points=points, closed=closed_raw)

    if kind == "polygon":
        _require_exact_keys(value, required={"type", "points"}, where=where)
        points = _points(value["points"], f"{where}.points", minimum=3)
        if len(set(points)) < 3:
            raise SketchDslError(f"{where} polygon needs at least three distinct points.")
        return PolygonOp(points=points)

    if kind == "rectangle":
        _require_exact_keys(
            value,
            required={"type", "x", "y", "width", "height"},
            where=where,
        )
        x = _normalized_number(value["x"], f"{where}.x")
        y = _normalized_number(value["y"], f"{where}.y")
        width = _positive_number(value["width"], f"{where}.width", maximum=1.0)
        height = _positive_number(value["height"], f"{where}.height", maximum=1.0)
        if x + width > 1.0 or y + height > 1.0:
            raise SketchDslError(f"{where} rectangle extends outside the normalized canvas.")
        return RectangleOp(x=x, y=y, width=width, height=height)

    if kind == "ellipse":
        _require_exact_keys(
            value,
            required={"type", "cx", "cy", "rx", "ry"},
            optional={"segments"},
            where=where,
        )
        cx, cy, rx, ry = _bounded_ellipse(value, where)
        segments = _segments(value.get("segments", 64), f"{where}.segments", minimum=12)
        return EllipseOp(cx=cx, cy=cy, rx=rx, ry=ry, segments=segments)

    if kind == "arc":
        _require_exact_keys(
            value,
            required={
                "type", "cx", "cy", "rx", "ry",
                "start_degrees", "end_degrees",
            },
            optional={"segments"},
            where=where,
        )
        cx, cy, rx, ry = _bounded_ellipse(value, where)
        start = _finite_number(value["start_degrees"], f"{where}.start_degrees")
        end = _finite_number(value["end_degrees"], f"{where}.end_degrees")
        span = end - start
        if span == 0.0 or abs(span) > 720.0:
            raise SketchDslError(
                f"{where} arc span must be non-zero and at most 720 degrees."
            )
        segments = _segments(value.get("segments", 32), f"{where}.segments", minimum=4)
        return ArcOp(
            cx=cx,
            cy=cy,
            rx=rx,
            ry=ry,
            start_degrees=start,
            end_degrees=end,
            segments=segments,
        )

    if kind == "bezier":
        _require_exact_keys(
            value,
            required={"type", "p0", "p1", "p2", "p3"},
            optional={"segments"},
            where=where,
        )
        return BezierOp(
            p0=_point(value["p0"], f"{where}.p0"),
            p1=_point(value["p1"], f"{where}.p1"),
            p2=_point(value["p2"], f"{where}.p2"),
            p3=_point(value["p3"], f"{where}.p3"),
            segments=_segments(value.get("segments", 32), f"{where}.segments", minimum=4),
        )

    raise SketchDslError(f"{where}.type has unknown primitive {kind!r}.")


def _bounded_ellipse(value: Mapping[str, object], where: str) -> tuple[float, float, float, float]:
    cx = _normalized_number(value["cx"], f"{where}.cx")
    cy = _normalized_number(value["cy"], f"{where}.cy")
    rx = _positive_number(value["rx"], f"{where}.rx", maximum=0.5)
    ry = _positive_number(value["ry"], f"{where}.ry", maximum=0.5)
    if cx - rx < 0.0 or cx + rx > 1.0 or cy - ry < 0.0 or cy + ry > 1.0:
        raise SketchDslError(f"{where} ellipse bounds extend outside the normalized canvas.")
    return cx, cy, rx, ry


def _points(value: object, where: str, *, minimum: int) -> tuple[Point, ...]:
    if not isinstance(value, list):
        raise SketchDslError(f"{where} must be an array of points.")
    if len(value) < minimum:
        raise SketchDslError(f"{where} requires at least {minimum} points.")
    if len(value) > MAX_RAW_POINTS:
        raise SketchDslError(f"{where} may contain at most {MAX_RAW_POINTS} points.")
    return tuple(_point(item, f"{where}[{index}]") for index, item in enumerate(value))


def _point(value: object, where: str) -> Point:
    if not isinstance(value, list) or len(value) != 2:
        raise SketchDslError(f"{where} must be [x, y].")
    return (
        _normalized_number(value[0], f"{where}[0]"),
        _normalized_number(value[1], f"{where}[1]"),
    )


def _normalized_number(value: object, where: str) -> float:
    number = _finite_number(value, where)
    if not 0.0 <= number <= 1.0:
        raise SketchDslError(f"{where} must be within 0.0..1.0.")
    return number


def _positive_number(value: object, where: str, *, maximum: float) -> float:
    number = _finite_number(value, where)
    if not 0.0 < number <= maximum:
        raise SketchDslError(f"{where} must be greater than 0 and at most {maximum}.")
    return number


def _finite_number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SketchDslError(f"{where} must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise SketchDslError(f"{where} must be a finite number.")
    return number


def _integer(value: object, where: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SketchDslError(f"{where} must be an integer.")
    if not minimum <= value <= maximum:
        raise SketchDslError(f"{where} must be from {minimum} through {maximum}.")
    return value


def _segments(value: object, where: str, *, minimum: int) -> int:
    return _integer(value, where, minimum=minimum, maximum=MAX_CURVE_SEGMENTS)


def _require_exact_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
    where: str,
) -> None:
    optional = optional or set()
    actual = set(value)
    missing = required - actual
    unknown = actual - required - optional
    if missing:
        raise SketchDslError(f"{where} is missing keys: {sorted(missing)!r}.")
    if unknown:
        raise SketchDslError(f"{where} contains unknown keys: {sorted(unknown)!r}.")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SketchDslError(f"Duplicate JSON key is not allowed: {key!r}.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise SketchDslError(f"Non-finite JSON constant is not allowed: {value}.")
