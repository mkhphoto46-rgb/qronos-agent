from __future__ import annotations

from dataclasses import dataclass
import math

from core.semantic_sketch import (
    INTERNAL_GROUP_PRIMITIVE,
    SemanticPart,
    SemanticSketchDocument,
    SemanticSketchError,
)
from core.shape_planner import ShapePlan, ShapePlanError


class SemanticLayoutError(SemanticSketchError):
    """A valid semantic scene graph could not be laid out safely."""


@dataclass(frozen=True)
class _Box:
    cx: float
    cy: float
    width: float
    height: float

    @property
    def left(self) -> float:
        return self.cx - self.width / 2.0

    @property
    def right(self) -> float:
        return self.cx + self.width / 2.0

    @property
    def top(self) -> float:
        return self.cy - self.height / 2.0

    @property
    def bottom(self) -> float:
        return self.cy + self.height / 2.0


_SCENE_SIZE = {"tiny": 0.16, "small": 0.26, "medium": 0.40, "large": 0.58}
_RELATED_SIZE = {"tiny": 0.18, "small": 0.30, "medium": 0.46, "large": 0.64}
_SIBLING_SIZE = {"tiny": 0.14, "small": 0.22, "medium": 0.32, "large": 0.43}
_MARGIN = 0.055
_GAP = 0.035
_SCENE_ROOT_SIZE = 0.78


def compile_semantic_sketch(document: SemanticSketchDocument) -> ShapePlan:
    """Deterministically turn semantic relationships into normalized strokes."""

    by_id = {part.part_id: part for part in document.parts}
    roots = {part.part_id for part in document.parts if part.relation == "scene_center"}
    hidden_non_root = [
        part.part_id
        for part in document.parts
        if part.primitive == INTERNAL_GROUP_PRIMITIVE and part.part_id not in roots
    ]
    if hidden_non_root:
        names = ", ".join(repr(name) for name in hidden_non_root[:6])
        raise SemanticLayoutError(
            "Flat-visible semantic layout forbids non-root group parts: "
            f"{names}."
        )

    resolved: dict[str, _Box] = {}

    def resolve(part_id: str) -> _Box:
        if part_id in resolved:
            return resolved[part_id]

        part = by_id[part_id]
        if part.relation == "scene_center":
            if part.primitive == INTERNAL_GROUP_PRIMITIVE:
                box = _Box(0.5, 0.5, _SCENE_ROOT_SIZE, _SCENE_ROOT_SIZE)
            else:
                # Legacy/internal documents may still use a drawable root.
                base = _SCENE_SIZE[part.size]
                box = _box_for_size(0.5, 0.5, base, part.aspect)
        else:
            assert part.target is not None
            target = resolve(part.target)
            target_part = by_id[part.target]
            if part.relation in {"inside", "attached_top", "attached_bottom",
                                 "attached_left", "attached_right"}:
                base = min(target.width, target.height) * _RELATED_SIZE[part.size]
            else:
                base = _SIBLING_SIZE[part.size]
            child = _box_for_size(0.5, 0.5, base, part.aspect)
            box = _place_relative(part, child, target, target_part=target_part)

        box = _clamp_box(box)
        resolved[part_id] = box
        return box

    strokes: list[tuple[tuple[float, float], ...]] = []
    for part in document.parts:
        box = resolve(part.part_id)
        if part.primitive == INTERNAL_GROUP_PRIMITIVE:
            continue
        target_part = by_id.get(part.target) if part.target is not None else None
        target_box = resolved.get(part.target) if part.target is not None else None
        strokes.append(_render_part(part, box, target_part=target_part, target_box=target_box))

    try:
        return ShapePlan.from_strokes(strokes, name=document.name)
    except ShapePlanError as exc:
        raise SemanticLayoutError(
            f"Semantic sketch compiled outside ShapePlan bounds: {exc}"
        ) from exc


def _box_for_size(cx: float, cy: float, base: float, aspect: float) -> _Box:
    root = math.sqrt(aspect)
    width = min(0.82, base * root)
    height = min(0.82, base / root)
    return _Box(cx=cx, cy=cy, width=width, height=height)


def _place_relative(
    part: SemanticPart,
    child: _Box,
    target: _Box,
    *,
    target_part: SemanticPart,
) -> _Box:
    relation = part.relation

    if relation.startswith("attached_") and target_part.primitive == "line":
        endpoint = _visible_line_attachment_endpoint(
            target_part,
            target,
            relation=relation,
            anchor=part.anchor,
        )
        cx, cy = endpoint
        # Non-line end-caps (head/hand/leaf/etc.) should touch the visible
        # endpoint rather than be centered on it, which would make the parent
        # stroke cut through the child. Line children use this box only for
        # length, so no visual offset is needed for them.
        if part.primitive != "line":
            if relation == "attached_top":
                cy -= child.height * 0.45
            elif relation == "attached_bottom":
                cy += child.height * 0.45
            elif relation == "attached_left":
                cx -= child.width * 0.45
            elif relation == "attached_right":
                cx += child.width * 0.45
        return _Box(cx, cy, child.width, child.height)

    if relation == "inside":
        cx, cy = _inside_anchor(target, part.anchor)
        max_w = max(0.025, target.width * 0.82)
        max_h = max(0.025, target.height * 0.82)
        child = _Box(cx, cy, min(child.width, max_w), min(child.height, max_h))
        return _fit_inside(child, target)

    if relation in {"attached_top", "above"}:
        cx = _horizontal_anchor(target, part.anchor)
        edge_gap = -0.04 * child.height if relation == "attached_top" else _GAP
        cy = target.top - edge_gap - child.height / 2.0
        return _Box(cx, cy, child.width, child.height)

    if relation in {"attached_bottom", "below"}:
        cx = _horizontal_anchor(target, part.anchor)
        edge_gap = -0.04 * child.height if relation == "attached_bottom" else _GAP
        cy = target.bottom + edge_gap + child.height / 2.0
        return _Box(cx, cy, child.width, child.height)

    if relation in {"attached_left", "left_of"}:
        cy = _vertical_anchor(target, part.anchor)
        edge_gap = -0.04 * child.width if relation == "attached_left" else _GAP
        cx = target.left - edge_gap - child.width / 2.0
        return _Box(cx, cy, child.width, child.height)

    if relation in {"attached_right", "right_of"}:
        cy = _vertical_anchor(target, part.anchor)
        edge_gap = -0.04 * child.width if relation == "attached_right" else _GAP
        cx = target.right + edge_gap + child.width / 2.0
        return _Box(cx, cy, child.width, child.height)

    raise SemanticLayoutError(f"Unsupported semantic relation: {relation!r}.")


def _inside_anchor(target: _Box, anchor: str) -> tuple[float, float]:
    x_factor = 0.5
    y_factor = 0.5
    if anchor in {"left", "top_left", "bottom_left"}:
        x_factor = 0.27
    elif anchor in {"right", "top_right", "bottom_right"}:
        x_factor = 0.73

    if anchor in {"top", "top_left", "top_right"}:
        y_factor = 0.27
    elif anchor in {"bottom", "bottom_left", "bottom_right"}:
        y_factor = 0.73

    return (
        target.left + target.width * x_factor,
        target.top + target.height * y_factor,
    )


def _horizontal_anchor(target: _Box, anchor: str) -> float:
    if anchor in {"left", "top_left", "bottom_left"}:
        return target.left + target.width * 0.25
    if anchor in {"right", "top_right", "bottom_right"}:
        return target.left + target.width * 0.75
    return target.cx


def _vertical_anchor(target: _Box, anchor: str) -> float:
    if anchor in {"top", "top_left", "top_right"}:
        return target.top + target.height * 0.25
    if anchor in {"bottom", "bottom_left", "bottom_right"}:
        return target.top + target.height * 0.75
    return target.cy


def _fit_inside(child: _Box, target: _Box) -> _Box:
    half_w = child.width / 2.0
    half_h = child.height / 2.0
    cx = min(max(child.cx, target.left + half_w), target.right - half_w)
    cy = min(max(child.cy, target.top + half_h), target.bottom - half_h)
    return _Box(cx, cy, child.width, child.height)


def _clamp_box(box: _Box) -> _Box:
    width = min(box.width, 1.0 - 2 * _MARGIN)
    height = min(box.height, 1.0 - 2 * _MARGIN)
    half_w = width / 2.0
    half_h = height / 2.0
    cx = min(max(box.cx, _MARGIN + half_w), 1.0 - _MARGIN - half_w)
    cy = min(max(box.cy, _MARGIN + half_h), 1.0 - _MARGIN - half_h)
    return _Box(cx, cy, width, height)


def _render_part(
    part: SemanticPart,
    box: _Box,
    *,
    target_part: SemanticPart | None,
    target_box: _Box | None,
) -> tuple[tuple[float, float], ...]:
    if part.primitive == "ellipse":
        return _ellipse(box, 48)
    if part.primitive == "rectangle":
        return (
            (box.left, box.top),
            (box.right, box.top),
            (box.right, box.bottom),
            (box.left, box.bottom),
            (box.left, box.top),
        )
    if part.primitive == "triangle":
        return _triangle(box, part.orientation or "up")
    if part.primitive == "line":
        if part.relation.startswith("attached_") and target_part is not None and target_box is not None:
            return _attached_line(part, box, target_part, target_box)
        return _line(box, part.orientation or "horizontal")
    if part.primitive == "arc":
        return _arc(box, part.orientation or "up", 32)
    raise SemanticLayoutError(f"Unsupported primitive: {part.primitive!r}.")


def _ellipse(box: _Box, segments: int) -> tuple[tuple[float, float], ...]:
    points = tuple(
        (
            box.cx + (box.width / 2.0) * math.cos(2.0 * math.pi * i / segments),
            box.cy + (box.height / 2.0) * math.sin(2.0 * math.pi * i / segments),
        )
        for i in range(segments)
    )
    return (*points, points[0])


def _triangle(box: _Box, orientation: str) -> tuple[tuple[float, float], ...]:
    if orientation == "up":
        pts = ((box.cx, box.top), (box.right, box.bottom), (box.left, box.bottom))
    elif orientation == "down":
        pts = ((box.left, box.top), (box.right, box.top), (box.cx, box.bottom))
    elif orientation == "left":
        pts = ((box.left, box.cy), (box.right, box.top), (box.right, box.bottom))
    elif orientation == "right":
        pts = ((box.left, box.top), (box.right, box.cy), (box.left, box.bottom))
    else:
        raise SemanticLayoutError(f"Unsupported triangle orientation: {orientation!r}.")
    return (*pts, pts[0])


def _line(box: _Box, orientation: str) -> tuple[tuple[float, float], ...]:
    if orientation == "horizontal":
        return ((box.left, box.cy), (box.right, box.cy))
    if orientation == "vertical":
        return ((box.cx, box.top), (box.cx, box.bottom))
    if orientation == "up_right":
        return ((box.left, box.bottom), (box.right, box.top))
    if orientation == "up_left":
        return ((box.right, box.bottom), (box.left, box.top))
    if orientation == "down_right":
        return ((box.left, box.top), (box.right, box.bottom))
    if orientation == "down_left":
        return ((box.right, box.top), (box.left, box.bottom))
    raise SemanticLayoutError(f"Unsupported line orientation: {orientation!r}.")


def _arc(box: _Box, orientation: str, segments: int) -> tuple[tuple[float, float], ...]:
    if orientation == "up":
        start, end = math.pi, 2.0 * math.pi
    elif orientation == "down":
        start, end = 0.0, math.pi
    elif orientation == "left":
        start, end = math.pi / 2.0, 3.0 * math.pi / 2.0
    elif orientation == "right":
        start, end = -math.pi / 2.0, math.pi / 2.0
    else:
        raise SemanticLayoutError(f"Unsupported arc orientation: {orientation!r}.")

    return tuple(
        (
            box.cx + (box.width / 2.0) * math.cos(start + (end - start) * i / segments),
            box.cy + (box.height / 2.0) * math.sin(start + (end - start) * i / segments),
        )
        for i in range(segments + 1)
    )


def _attached_line(
    part: SemanticPart,
    box: _Box,
    target_part: SemanticPart,
    target_box: _Box,
) -> tuple[tuple[float, float], ...]:
    if target_part.primitive == "line" and part.relation in {"attached_left", "attached_right"}:
        start = _visible_line_branch_point(target_part, target_box, part.anchor)
    else:
        start = _attachment_point(target_part, target_box, part.relation, part.anchor)
    orientation = part.orientation or "horizontal"
    hint = part.direction_hint

    vectors = {
        "up": (0.0, -1.0),
        "down": (0.0, 1.0),
        "left": (-1.0, 0.0),
        "right": (1.0, 0.0),
        "up_right": (1.0, -1.0),
        "up_left": (-1.0, -1.0),
        "down_right": (1.0, 1.0),
        "down_left": (-1.0, 1.0),
    }
    if hint in vectors:
        dx, dy = vectors[hint]
        if dx and dy:
            norm = math.sqrt(2.0)
            dx /= norm
            dy /= norm
    elif orientation in vectors:
        dx, dy = vectors[orientation]
        if dx and dy:
            norm = math.sqrt(2.0)
            dx /= norm
            dy /= norm
    elif orientation == "horizontal":
        dx = -1.0 if part.relation == "attached_left" else 1.0
        dy = 0.0
    elif orientation == "vertical":
        dx = 0.0
        dy = -1.0 if part.relation == "attached_top" else 1.0
    else:
        raise SemanticLayoutError(f"Unsupported attached line orientation: {orientation!r}.")

    length = max(box.width, box.height)
    end = (
        min(max(start[0] + dx * length, _MARGIN), 1.0 - _MARGIN),
        min(max(start[1] + dy * length, _MARGIN), 1.0 - _MARGIN),
    )
    if end == start:
        raise SemanticLayoutError("Attached line collapsed to a zero-length stroke.")
    return (start, end)


def _visible_line_branch_point(
    target_part: SemanticPart,
    target: _Box,
    anchor: str,
) -> tuple[float, float]:
    """Pick a visible point along a parent line for branch-like attachments."""

    first, last = _line(target, target_part.orientation or "horizontal")
    if anchor in {"top", "top_left", "top_right"}:
        endpoint = min((first, last), key=lambda point: (point[1], point[0]))
        other = last if endpoint == first else first
        weight = 0.18
        return (
            endpoint[0] * (1.0 - weight) + other[0] * weight,
            endpoint[1] * (1.0 - weight) + other[1] * weight,
        )
    if anchor in {"bottom", "bottom_left", "bottom_right"}:
        endpoint = max((first, last), key=lambda point: (point[1], point[0]))
        other = last if endpoint == first else first
        weight = 0.18
        return (
            endpoint[0] * (1.0 - weight) + other[0] * weight,
            endpoint[1] * (1.0 - weight) + other[1] * weight,
        )
    return ((first[0] + last[0]) / 2.0, (first[1] + last[1]) / 2.0)


def _visible_line_attachment_endpoint(
    target_part: SemanticPart,
    target: _Box,
    *,
    relation: str,
    anchor: str,
) -> tuple[float, float]:
    points = _line(target, target_part.orientation or "horizontal")
    first, last = points[0], points[-1]

    if anchor == "center" and relation in {"attached_left", "attached_right"}:
        return ((first[0] + last[0]) / 2.0, (first[1] + last[1]) / 2.0)

    # Placement is the model's endpoint hint. Honor it before the coarse
    # attached_* side so arms can attach to a shoulder endpoint of a vertical
    # torso instead of arbitrarily selecting the lower endpoint on an x tie.
    if anchor in {"top", "top_left", "top_right"}:
        return min((first, last), key=lambda point: (point[1], point[0]))
    if anchor in {"bottom", "bottom_left", "bottom_right"}:
        return max((first, last), key=lambda point: (point[1], point[0]))
    if anchor == "left":
        return min((first, last), key=lambda point: (point[0], point[1]))
    if anchor == "right":
        return max((first, last), key=lambda point: (point[0], point[1]))

    if relation == "attached_top":
        return min((first, last), key=lambda point: (point[1], point[0]))
    if relation == "attached_bottom":
        return max((first, last), key=lambda point: (point[1], point[0]))
    if relation == "attached_left":
        return min((first, last), key=lambda point: (point[0], point[1]))
    if relation == "attached_right":
        return max((first, last), key=lambda point: (point[0], point[1]))

    return first


def _attachment_point(
    target_part: SemanticPart,
    target: _Box,
    relation: str,
    anchor: str,
) -> tuple[float, float]:
    # A line's conceptual layout box gives it room for attachments, but the
    # visible stroke can be diagonal or directional. Always attach to an actual
    # visible endpoint instead of the abstract bounding box.
    if target_part.primitive == "line":
        return _visible_line_attachment_endpoint(
            target_part,
            target,
            relation=relation,
            anchor=anchor,
        )

    if relation == "attached_top":
        return _horizontal_anchor(target, anchor), target.top
    if relation == "attached_bottom":
        return _horizontal_anchor(target, anchor), target.bottom
    if relation == "attached_left":
        return target.left, _vertical_anchor(target, anchor)
    if relation == "attached_right":
        return target.right, _vertical_anchor(target, anchor)
    raise SemanticLayoutError(f"Not an attached relation: {relation!r}.")
