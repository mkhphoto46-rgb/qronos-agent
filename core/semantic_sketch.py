from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping


MAX_SEMANTIC_PARTS = 32
MAX_SEMANTIC_NAME_CHARS = 80
MAX_PART_ID_CHARS = 40
MAX_RELATION_DEPTH = 8

PRIMITIVES = frozenset({"ellipse", "rectangle", "triangle", "line", "arc"})
INTERNAL_GROUP_PRIMITIVE = "group"
MODEL_PRIMITIVES = frozenset(set(PRIMITIVES) | {INTERNAL_GROUP_PRIMITIVE})
RELATIONS = frozenset({
    "scene_center",
    "inside",
    "attached_top",
    "attached_bottom",
    "attached_left",
    "attached_right",
    "above",
    "below",
    "left_of",
    "right_of",
})
ANCHORS = frozenset({
    "center",
    "top",
    "bottom",
    "left",
    "right",
    "top_left",
    "top_right",
    "bottom_left",
    "bottom_right",
})
SIZES = frozenset({"tiny", "small", "medium", "large"})
LINE_ORIENTATIONS = frozenset({
    "horizontal",
    "vertical",
    "up_left",
    "up_right",
    "down_left",
    "down_right",
})
CARDINAL_ORIENTATIONS = frozenset({"up", "down", "left", "right"})
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")


class SemanticSketchError(ValueError):
    """A semantic sketch description is malformed or unsafe."""


@dataclass(frozen=True)
class SemanticPart:
    part_id: str
    primitive: str
    relation: str
    target: str | None
    anchor: str
    size: str
    aspect: float
    orientation: str | None
    direction_hint: str = "auto"
    object_id: str = ""


@dataclass(frozen=True)
class SemanticRequirement:
    subject: str
    primitive: str
    relation: str
    target: str | None
    direction: str


@dataclass(frozen=True)
class SemanticSketchDocument:
    version: int
    name: str
    parts: tuple[SemanticPart, ...]
    requirements: tuple[SemanticRequirement, ...] = ()


MODEL_ROOT_TARGET = "__scene__"
MODEL_PLACEMENTS = ANCHORS
MODEL_SCALES = SIZES
MODEL_PROPORTIONS = frozenset({"default", "tall", "wide", "thin"})
MODEL_REQUIREMENT_PRIMITIVES = frozenset({"any"} | set(PRIMITIVES))
MODEL_REQUIREMENT_RELATIONS = frozenset({
    "any",
    "attached",
    "inside",
    "above",
    "below",
    "left_of",
    "right_of",
})
MODEL_REQUIREMENT_DIRECTIONS = frozenset({
    "any",
    "upward",
    "downward",
    "leftward",
    "rightward",
    "horizontal",
    "vertical",
})
MODEL_REQUIREMENT_ANY_TARGET = "__any__"
MODEL_DIRECTIONS = frozenset({
    "auto",
    "horizontal",
    "vertical",
    "up",
    "down",
    "left",
    "right",
    "up_left",
    "up_right",
    "down_left",
    "down_right",
})

# Compatibility aliases for older internal callers/tests. They are no longer
# part of the model wire format; model output uses categorical ``direction``.
MODEL_ORIENTATION_NONE = "none"
MODEL_ORIENTATIONS = frozenset(
    {MODEL_ORIENTATION_NONE} | LINE_ORIENTATIONS | CARDINAL_ORIENTATIONS
)

# The model-facing wire contract is deliberately categorical. Live Gemma tests
# showed that asking the model for numeric geometry (for example aspect ratios)
# creates a stream of avoidable schema failures. The model therefore describes
# only WHAT parts exist and HOW they relate. Qronos deterministically converts
# those categories into the numeric geometry used by the internal layout engine.
SEMANTIC_SKETCH_RESPONSE_FORMAT: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "name", "requirements", "parts"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "name": {"type": "string", "minLength": 1, "maxLength": MAX_SEMANTIC_NAME_CHARS},
        "requirements": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["subject", "primitive", "relation", "target", "direction"],
                "properties": {
                    "subject": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PART_ID_CHARS,
                        "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,39}$",
                    },
                    "primitive": {"enum": sorted(MODEL_REQUIREMENT_PRIMITIVES)},
                    "relation": {"enum": sorted(MODEL_REQUIREMENT_RELATIONS)},
                    "target": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PART_ID_CHARS,
                        "pattern": "^(__any__|[A-Za-z][A-Za-z0-9_-]{0,39})$",
                    },
                    "direction": {"enum": sorted(MODEL_REQUIREMENT_DIRECTIONS)},
                },
            },
        },
        "parts": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_SEMANTIC_PARTS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "object_id",
                    "primitive",
                    "relation",
                    "target",
                    "placement",
                    "scale",
                    "proportion",
                    "direction",
                ],
                "properties": {
                    "id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PART_ID_CHARS,
                        "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,39}$",
                    },
                    "object_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PART_ID_CHARS,
                        "pattern": "^(__scene__|[A-Za-z][A-Za-z0-9_-]{0,39})$",
                    },
                    "primitive": {"enum": sorted(MODEL_PRIMITIVES)},
                    "relation": {"enum": sorted(RELATIONS)},
                    "target": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PART_ID_CHARS,
                        "pattern": "^(__scene__|[A-Za-z][A-Za-z0-9_-]{0,39})$",
                    },
                    "placement": {"enum": sorted(MODEL_PLACEMENTS)},
                    "scale": {"enum": sorted(MODEL_SCALES)},
                    "proportion": {"enum": sorted(MODEL_PROPORTIONS)},
                    "direction": {"enum": sorted(MODEL_DIRECTIONS)},
                },
            },
        },
    },
}


def parse_semantic_model_json(text: str) -> SemanticSketchDocument:
    """Parse categorical model output into Qronos's strict internal graph.

    No model-provided numeric geometry reaches the internal scene graph. Scale,
    proportion, placement, and direction are categorical hints converted by
    deterministic runtime rules. Graph identity/target errors remain fail-closed.
    """

    cleaned = str(text).strip()
    if not cleaned:
        raise SemanticSketchError("Semantic sketch output is empty.")

    try:
        raw = json.loads(
            cleaned,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except SemanticSketchError:
        raise
    except json.JSONDecodeError as exc:
        raise SemanticSketchError(
            f"Semantic sketch output is not strict JSON: {exc}."
        ) from exc

    if not isinstance(raw, dict):
        raise SemanticSketchError("Semantic sketch root must be a JSON object.")

    _require_exact_keys(
        raw,
        required={"version", "name", "requirements", "parts"},
        where="root",
    )

    if raw["version"] != 1 or isinstance(raw["version"], bool):
        raise SemanticSketchError("Semantic sketch version must be integer 1.")

    name = raw["name"]
    if not isinstance(name, str):
        raise SemanticSketchError("Semantic sketch name must be a string.")
    name = name.strip()
    if not name or len(name) > MAX_SEMANTIC_NAME_CHARS:
        raise SemanticSketchError(
            f"Semantic sketch name must be 1..{MAX_SEMANTIC_NAME_CHARS} characters."
        )

    requirements_raw = raw["requirements"]
    if not isinstance(requirements_raw, list):
        raise SemanticSketchError("root.requirements must be an array.")
    if not 1 <= len(requirements_raw) <= 16:
        raise SemanticSketchError("root.requirements must contain 1..16 requirements.")

    parts_raw = raw["parts"]
    if not isinstance(parts_raw, list):
        raise SemanticSketchError("root.parts must be an array.")
    if not 1 <= len(parts_raw) <= MAX_SEMANTIC_PARTS:
        raise SemanticSketchError(
            f"root.parts must contain 1..{MAX_SEMANTIC_PARTS} parts."
        )

    requirements = tuple(
        _parse_model_requirement(item, index)
        for index, item in enumerate(requirements_raw)
    )
    parts = tuple(_parse_model_part(item, index) for index, item in enumerate(parts_raw))
    _validate_graph(parts)
    _validate_model_structure(parts)
    document = SemanticSketchDocument(
        version=1,
        name=name,
        parts=parts,
        requirements=requirements,
    )
    validate_semantic_requirements(document)
    return document


def _parse_model_part(value: object, index: int) -> SemanticPart:
    where = f"root.parts[{index}]"
    if not isinstance(value, dict):
        raise SemanticSketchError(f"{where} must be an object.")

    _require_exact_keys(
        value,
        required={
            "id",
            "primitive",
            "relation",
            "target",
            "placement",
            "scale",
            "proportion",
            "direction",
        },
        optional={"object_id"},
        where=where,
    )

    part_id = value["id"]
    if not isinstance(part_id, str) or not _ID_RE.fullmatch(part_id):
        raise SemanticSketchError(f"{where}.id must match {_ID_RE.pattern!r}.")

    object_id_raw = value.get("object_id", "")
    if not isinstance(object_id_raw, str):
        raise SemanticSketchError(f"{where}.object_id must be a string.")

    primitive = value["primitive"]
    if primitive not in MODEL_PRIMITIVES:
        raise SemanticSketchError(f"{where}.primitive is not supported.")

    relation = value["relation"]
    if relation not in RELATIONS:
        raise SemanticSketchError(f"{where}.relation is not supported.")

    target_raw = value["target"]
    if not isinstance(target_raw, str):
        raise SemanticSketchError(f"{where}.target must be a string in model output.")
    if relation == "scene_center":
        if target_raw != MODEL_ROOT_TARGET:
            raise SemanticSketchError(
                f"{where}.target must be {MODEL_ROOT_TARGET!r} for scene_center."
            )
        target: str | None = None
        object_id = MODEL_ROOT_TARGET
    else:
        if target_raw == MODEL_ROOT_TARGET or not _ID_RE.fullmatch(target_raw):
            raise SemanticSketchError(
                f"{where}.target must name a valid part id for relation {relation!r}."
            )
        target = target_raw
        if object_id_raw:
            if object_id_raw == MODEL_ROOT_TARGET or not _ID_RE.fullmatch(object_id_raw):
                raise SemanticSketchError(
                    f"{where}.object_id must be a valid non-scene object id."
                )
            object_id = object_id_raw
        else:
            # Backward-compatible parser path for older local fixtures. Live model
            # output is still schema-constrained to include object_id.
            object_id = ""

    placement = value["placement"]
    if placement not in MODEL_PLACEMENTS:
        raise SemanticSketchError(f"{where}.placement is not supported.")

    scale = value["scale"]
    if scale not in MODEL_SCALES:
        raise SemanticSketchError(f"{where}.scale is not supported.")

    proportion = value["proportion"]
    if proportion not in MODEL_PROPORTIONS:
        raise SemanticSketchError(f"{where}.proportion is not supported.")

    direction = value["direction"]
    if direction not in MODEL_DIRECTIONS:
        raise SemanticSketchError(f"{where}.direction is not supported.")

    # scene_center is an implicit, invisible container. Never trust the model's
    # primitive choice for the root: normalize it to an internal group so the
    # scene anchor can never become a giant visible ellipse/triangle.
    if relation == "scene_center":
        primitive = INTERNAL_GROUP_PRIMITIVE
        placement = "center"
        scale = "large"
        proportion = "default"
        direction = "auto"

    orientation = _orientation_from_categories(
        primitive=primitive,
        relation=relation,
        direction=direction,
    )
    aspect = _aspect_from_categories(
        primitive=primitive,
        proportion=proportion,
        orientation=orientation,
    )

    return SemanticPart(
        part_id=part_id,
        primitive=primitive,
        relation=relation,
        target=target,
        anchor=placement,
        size=scale,
        aspect=aspect,
        orientation=orientation,
        direction_hint=direction,
        object_id=object_id,
    )


def _parse_model_requirement(value: object, index: int) -> SemanticRequirement:
    where = f"root.requirements[{index}]"
    if not isinstance(value, dict):
        raise SemanticSketchError(f"{where} must be an object.")

    _require_exact_keys(
        value,
        required={"subject", "primitive", "relation", "target", "direction"},
        where=where,
    )

    subject = value["subject"]
    if not isinstance(subject, str) or not _ID_RE.fullmatch(subject):
        raise SemanticSketchError(f"{where}.subject must be a valid part id.")

    primitive = value["primitive"]
    if primitive not in MODEL_REQUIREMENT_PRIMITIVES:
        raise SemanticSketchError(f"{where}.primitive is not supported.")

    relation = value["relation"]
    if relation not in MODEL_REQUIREMENT_RELATIONS:
        raise SemanticSketchError(f"{where}.relation is not supported.")

    target_raw = value["target"]
    if not isinstance(target_raw, str):
        raise SemanticSketchError(f"{where}.target must be a string.")
    if target_raw == MODEL_REQUIREMENT_ANY_TARGET:
        target: str | None = None
    elif _ID_RE.fullmatch(target_raw):
        target = target_raw
    else:
        raise SemanticSketchError(
            f"{where}.target must be {MODEL_REQUIREMENT_ANY_TARGET!r} or a valid part id."
        )

    direction = value["direction"]
    if direction not in MODEL_REQUIREMENT_DIRECTIONS:
        raise SemanticSketchError(f"{where}.direction is not supported.")

    return SemanticRequirement(
        subject=subject,
        primitive=primitive,
        relation=relation,
        target=target,
        direction=direction,
    )


def _validate_model_structure(parts: tuple[SemanticPart, ...]) -> None:
    """Enforce the Stage 4B.3K flat-visible scene contract.

    ``scene_center`` is the only invisible group. Every other part must be a
    drawable primitive so the semantic graph cannot hide bodies, legs, wheels,
    foliage, or other meaningful geometry inside structural nodes that render
    nothing.
    """

    roots = [part for part in parts if part.relation == "scene_center"]
    if len(roots) != 1:
        return  # _validate_graph owns the exact-root error text.

    root = roots[0]
    if root.primitive != INTERNAL_GROUP_PRIMITIVE:
        raise SemanticSketchError("The scene_center root must be an invisible group.")

    non_root = [part for part in parts if part.part_id != root.part_id]
    if not non_root:
        raise SemanticSketchError(
            "The semantic sketch must contain at least one drawable non-root part."
        )

    hidden = [
        part.part_id
        for part in non_root
        if part.primitive == INTERNAL_GROUP_PRIMITIVE
    ]
    if hidden:
        names = ", ".join(repr(name) for name in hidden[:6])
        raise SemanticSketchError(
            "Only the scene_center root may use primitive 'group'; every non-root "
            f"part must be directly drawable. Hidden group part(s): {names}."
        )

    by_id = {part.part_id: part for part in parts}

    # Direct drawable children of the scene root are placed *inside* the scene.
    # Attaching to the root edge creates giant/disconnected compositions.
    for part in non_root:
        if part.target == root.part_id and part.relation != "inside":
            raise SemanticSketchError(
                f"Part {part.part_id!r} targets the invisible scene root using "
                f"{part.relation!r}; direct drawable children of scene_root must "
                "use relation 'inside'."
            )

        # ``object_id`` is metadata only. Containment/attachment stays within the
        # same semantic object; cross-object layout is expressed with above/below/
        # left_of/right_of.
        if (
            part.target is not None
            and part.target != root.part_id
            and part.relation in {
                "inside",
                "attached_top",
                "attached_bottom",
                "attached_left",
                "attached_right",
            }
        ):
            target = by_id[part.target]
            if part.object_id and target.object_id and part.object_id != target.object_id:
                raise SemanticSketchError(
                    f"Part {part.part_id!r} uses relation {part.relation!r} across "
                    f"different object_id values ({part.object_id!r} -> "
                    f"{target.object_id!r}). Use relative placement between objects "
                    "and attachment/inside within an object."
                )


def validate_semantic_requirements(document: SemanticSketchDocument) -> None:
    """Fail closed if the generated graph contradicts its explicit requirements."""

    by_id = {part.part_id: part for part in document.parts}
    roots = {part.part_id for part in document.parts if part.relation == "scene_center"}

    for index, requirement in enumerate(document.requirements):
        where = f"requirement[{index}]"
        part = by_id.get(requirement.subject)
        if part is None:
            raise SemanticSketchError(
                f"{where} subject {requirement.subject!r} does not exist in parts."
            )
        if part.part_id in roots:
            raise SemanticSketchError(
                f"{where} cannot use the invisible scene root as its subject."
            )

        if requirement.primitive != "any" and part.primitive != requirement.primitive:
            raise SemanticSketchError(
                f"{where} requires primitive {requirement.primitive!r} for "
                f"{part.part_id!r}, got {part.primitive!r}."
            )

        relation = requirement.relation
        if relation == "attached":
            relation_ok = part.relation.startswith("attached_")
        elif relation == "any":
            relation_ok = True
        else:
            relation_ok = part.relation == relation
        if not relation_ok:
            raise SemanticSketchError(
                f"{where} requires relation {relation!r} for {part.part_id!r}, "
                f"got {part.relation!r}."
            )

        if requirement.target is not None and part.target != requirement.target:
            raise SemanticSketchError(
                f"{where} requires target {requirement.target!r} for {part.part_id!r}, "
                f"got {part.target!r}."
            )

        if not _requirement_direction_matches(requirement.direction, part.direction_hint):
            raise SemanticSketchError(
                f"{where} requires direction {requirement.direction!r} for "
                f"{part.part_id!r}, got {part.direction_hint!r}."
            )


def _requirement_direction_matches(requirement: str, actual: str) -> bool:
    if requirement == "any":
        return True
    if requirement == "upward":
        return actual in {"up", "up_left", "up_right"}
    if requirement == "downward":
        return actual in {"down", "down_left", "down_right"}
    if requirement == "leftward":
        return actual in {"left", "up_left", "down_left"}
    if requirement == "rightward":
        return actual in {"right", "up_right", "down_right"}
    if requirement == "horizontal":
        return actual in {"horizontal", "left", "right"}
    if requirement == "vertical":
        return actual in {"vertical", "up", "down"}
    return False


def _orientation_from_categories(
    *,
    primitive: str,
    relation: str,
    direction: str,
) -> str | None:
    """Normalize a categorical direction without trusting cross-field accuracy."""

    if primitive in {"ellipse", "rectangle", INTERNAL_GROUP_PRIMITIVE}:
        return None

    if primitive == "line":
        if direction in LINE_ORIENTATIONS:
            return direction
        if direction in {"up", "down"}:
            return "vertical"
        if direction in {"left", "right"}:
            return "horizontal"
        if relation in {"attached_top", "attached_bottom", "above", "below"}:
            return "vertical"
        if relation in {"attached_left", "attached_right", "left_of", "right_of"}:
            return "horizontal"
        return "horizontal"

    # Triangle/arc only need a cardinal orientation. Diagonals collapse to their
    # vertical component; "auto" is inferred from the semantic relation.
    if direction in CARDINAL_ORIENTATIONS:
        return direction
    if direction in {"up_left", "up_right"}:
        return "up"
    if direction in {"down_left", "down_right"}:
        return "down"
    inferred = {
        "attached_top": "up",
        "above": "up",
        "attached_bottom": "down",
        "below": "down",
        "attached_left": "left",
        "left_of": "left",
        "attached_right": "right",
        "right_of": "right",
    }.get(relation)
    return inferred or "up"


def _aspect_from_categories(
    *,
    primitive: str,
    proportion: str,
    orientation: str | None,
) -> float:
    """Convert categorical proportions into bounded deterministic geometry."""

    if primitive == INTERNAL_GROUP_PRIMITIVE:
        return 1.0

    if primitive == "line":
        if orientation == "horizontal":
            return 3.0
        if orientation == "vertical":
            return 0.35
        return 1.0

    if proportion == "tall":
        return 0.55
    if proportion == "wide":
        return 1.8
    if proportion == "thin":
        return 0.45

    # Primitive-specific defaults keep common semantic parts recognizable while
    # still avoiding model-generated numeric geometry.
    if primitive == "rectangle":
        return 0.75
    return 1.0



def parse_semantic_sketch_json(text: str) -> SemanticSketchDocument:
    cleaned = str(text).strip()
    if not cleaned:
        raise SemanticSketchError("Semantic sketch output is empty.")

    try:
        raw = json.loads(
            cleaned,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except SemanticSketchError:
        raise
    except json.JSONDecodeError as exc:
        raise SemanticSketchError(
            f"Semantic sketch output is not strict JSON: {exc}."
        ) from exc

    if not isinstance(raw, dict):
        raise SemanticSketchError("Semantic sketch root must be a JSON object.")

    _require_exact_keys(
        raw,
        required={"version", "parts"},
        optional={"name"},
        where="root",
    )

    if raw["version"] != 1 or isinstance(raw["version"], bool):
        raise SemanticSketchError("Semantic sketch version must be integer 1.")

    name = str(raw.get("name", "sketch")).strip()
    if not name or len(name) > MAX_SEMANTIC_NAME_CHARS:
        raise SemanticSketchError(
            f"Semantic sketch name must be 1..{MAX_SEMANTIC_NAME_CHARS} characters."
        )

    parts_raw = raw["parts"]
    if not isinstance(parts_raw, list):
        raise SemanticSketchError("root.parts must be an array.")
    if not 1 <= len(parts_raw) <= MAX_SEMANTIC_PARTS:
        raise SemanticSketchError(
            f"root.parts must contain 1..{MAX_SEMANTIC_PARTS} parts."
        )

    parts = tuple(_parse_part(item, index) for index, item in enumerate(parts_raw))
    _validate_graph(parts)
    return SemanticSketchDocument(version=1, name=name, parts=parts)


def _parse_part(value: object, index: int) -> SemanticPart:
    where = f"root.parts[{index}]"
    if not isinstance(value, dict):
        raise SemanticSketchError(f"{where} must be an object.")

    _require_exact_keys(
        value,
        required={"id", "primitive", "relation", "size"},
        optional={"target", "anchor", "aspect", "orientation"},
        where=where,
    )

    part_id = value["id"]
    if not isinstance(part_id, str) or not _ID_RE.fullmatch(part_id):
        raise SemanticSketchError(
            f"{where}.id must match {_ID_RE.pattern!r}."
        )

    primitive = value["primitive"]
    if primitive not in PRIMITIVES:
        raise SemanticSketchError(f"{where}.primitive is not supported.")

    relation = value["relation"]
    if relation not in RELATIONS:
        raise SemanticSketchError(f"{where}.relation is not supported.")

    size = value["size"]
    if size not in SIZES:
        raise SemanticSketchError(f"{where}.size is not supported.")

    anchor = value.get("anchor", "center")
    if anchor not in ANCHORS:
        raise SemanticSketchError(f"{where}.anchor is not supported.")

    aspect = _finite_number(value.get("aspect", 1.0), f"{where}.aspect")
    if not 0.35 <= aspect <= 3.0:
        raise SemanticSketchError(f"{where}.aspect must be within 0.35..3.0.")

    target = value.get("target")
    if target is not None and (
        not isinstance(target, str) or not _ID_RE.fullmatch(target)
    ):
        raise SemanticSketchError(f"{where}.target must be a valid part id.")

    orientation = value.get("orientation")
    if orientation is not None and not isinstance(orientation, str):
        raise SemanticSketchError(f"{where}.orientation must be a string.")

    if primitive == "line":
        if orientation not in LINE_ORIENTATIONS:
            raise SemanticSketchError(
                f"{where}.orientation for a line must be one of "
                f"{sorted(LINE_ORIENTATIONS)!r}."
            )
    elif primitive in {"triangle", "arc"}:
        if orientation not in CARDINAL_ORIENTATIONS:
            raise SemanticSketchError(
                f"{where}.orientation for {primitive} must be one of "
                f"{sorted(CARDINAL_ORIENTATIONS)!r}."
            )
    else:
        if orientation is not None:
            raise SemanticSketchError(
                f"{where}.orientation is only valid for line, triangle, or arc."
            )

    if relation == "scene_center":
        if target is not None:
            raise SemanticSketchError(
                f"{where}.target must be omitted for scene_center."
            )
    else:
        if target is None:
            raise SemanticSketchError(
                f"{where}.target is required for relation {relation!r}."
            )

    return SemanticPart(
        part_id=part_id,
        primitive=primitive,
        relation=relation,
        target=target,
        anchor=anchor,
        size=size,
        aspect=aspect,
        orientation=orientation,
    )


def _validate_graph(parts: tuple[SemanticPart, ...]) -> None:
    ids = [part.part_id for part in parts]
    if len(set(ids)) != len(ids):
        raise SemanticSketchError("Semantic sketch part ids must be unique.")

    roots = [part for part in parts if part.relation == "scene_center"]
    if len(roots) != 1:
        raise SemanticSketchError(
            "Semantic sketch must contain exactly one scene_center root."
        )

    by_id = {part.part_id: part for part in parts}
    for part in parts:
        if part.target is not None:
            if part.target == part.part_id:
                raise SemanticSketchError(
                    f"Part {part.part_id!r} cannot target itself."
                )
            if part.target not in by_id:
                raise SemanticSketchError(
                    f"Part {part.part_id!r} targets unknown part {part.target!r}."
                )

    root_id = roots[0].part_id
    for part in parts:
        seen: set[str] = set()
        current = part
        depth = 0
        while current.relation != "scene_center":
            if current.part_id in seen:
                raise SemanticSketchError("Semantic sketch relation graph contains a cycle.")
            seen.add(current.part_id)
            depth += 1
            if depth > MAX_RELATION_DEPTH:
                raise SemanticSketchError(
                    f"Semantic sketch relation depth exceeds {MAX_RELATION_DEPTH}."
                )
            assert current.target is not None
            current = by_id[current.target]
        if current.part_id != root_id:
            raise SemanticSketchError(
                f"Part {part.part_id!r} is not connected to the single scene root."
            )


def _finite_number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SemanticSketchError(f"{where} must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise SemanticSketchError(f"{where} must be a finite number.")
    return number


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
        raise SemanticSketchError(f"{where} is missing keys: {sorted(missing)!r}.")
    if unknown:
        raise SemanticSketchError(f"{where} contains unknown keys: {sorted(unknown)!r}.")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticSketchError(f"Duplicate JSON key is not allowed: {key!r}.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise SemanticSketchError(f"Non-finite JSON constant is not allowed: {value}.")
