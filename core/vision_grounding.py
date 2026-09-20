"""Fail-closed coordinate provenance for vision-grounded UI targets.

This module does not click, type, or call a model. It turns structured model
output into a target tied to the exact verified window frame the model saw.
Every coordinate therefore carries enough provenance to expire when the
window identity, layout, focus, or capture geometry changes.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any

from core.screen_capture import Capture
from core.window_binding import WindowBinder, WindowBinding, WindowBindingStale, WindowRect


class GroundingUnavailable(RuntimeError):
    """A capture or model response cannot be grounded safely."""


class GroundingStale(RuntimeError):
    """A grounded target no longer belongs to the current verified layout."""


class GroundingResponseInvalid(GroundingUnavailable):
    """The vision model returned data outside the structured grounding contract."""


class GroundingNotFound(GroundingUnavailable):
    """The vision model explicitly did not find the requested target."""


class GroundingAmbiguous(GroundingUnavailable):
    """The vision model found more than one plausible target."""


class GroundingLowConfidence(GroundingUnavailable):
    """The only candidate is below the minimum confidence accepted for use."""


DEFAULT_MIN_CONFIDENCE = 0.70
MAX_GROUNDING_CANDIDATES = 5
MAX_TARGET_LABEL_CHARS = 200


# This is supplied to the runtime as Ollama's structured response format. The
# Python validator below is still authoritative: model-side schema support is a
# convenience, never the safety boundary.
GROUNDING_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["found", "not_found", "ambiguous"],
        },
        "image_width": {"type": "integer", "minimum": 1},
        "image_height": {"type": "integer", "minimum": 1},
        "candidates": {
            "type": "array",
            "maxItems": MAX_GROUNDING_CANDIDATES,
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "point": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "minimum": 0},
                            "y": {"type": "integer", "minimum": 0},
                        },
                        "required": ["x", "y"],
                        "additionalProperties": False,
                    },
                    "bbox": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "minimum": 0},
                            "y": {"type": "integer", "minimum": 0},
                            "width": {"type": "integer", "minimum": 1},
                            "height": {"type": "integer", "minimum": 1},
                        },
                        "required": ["x", "y", "width", "height"],
                        "additionalProperties": False,
                    },
                },
                "required": ["label", "confidence", "point", "bbox"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["status", "image_width", "image_height", "candidates"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class GroundedPoint:
    """One model-image point tied to one immutable captured window layout."""

    label: str
    image_x: int
    image_y: int
    screen_x: int
    screen_y: int
    hwnd: int
    binding_token: str
    layout_token: str
    frame_rect: WindowRect
    model_width: int
    model_height: int
    source_width: int
    source_height: int


@dataclass(frozen=True)
class ImageBox:
    """A half-open rectangle in the exact image sent to the vision model."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom


@dataclass(frozen=True)
class VisionCandidate:
    """One fully validated candidate expressed in model-image coordinates."""

    label: str
    confidence: float
    image_x: int
    image_y: int
    image_box: ImageBox


@dataclass(frozen=True)
class VisionGroundingResponse:
    """A parsed model response before any candidate is trusted for action."""

    status: str
    image_width: int
    image_height: int
    candidates: tuple[VisionCandidate, ...]


@dataclass(frozen=True)
class GroundedTarget:
    """One structured vision target carrying point, bounds, and provenance."""

    label: str
    confidence: float
    point: GroundedPoint
    image_box: ImageBox
    screen_box: WindowRect

    @property
    def hwnd(self) -> int:
        return self.point.hwnd

    @property
    def binding_token(self) -> str:
        return self.point.binding_token

    @property
    def layout_token(self) -> str:
        return self.point.layout_token


def grounding_prompt(target: str, *, image_width: int, image_height: int) -> str:
    """A deterministic request for coordinates, not prose or reasoning."""
    wanted = str(target or "").strip()
    if not wanted:
        raise ValueError("A grounding request must name the UI target to locate.")

    return (
        "Locate the requested UI target in the attached image. Return only the "
        "structured JSON requested by the response schema. Coordinates are in "
        "the exact image you receive, with (0,0) at its top-left. Echo the exact "
        f"image dimensions as {image_width}x{image_height}. For a visible unique "
        "target use status='found' and exactly one candidate. If it is not "
        "visible use status='not_found' with no candidates. If two or more "
        "targets are plausibly the requested target use status='ambiguous' and "
        "include those candidates rather than guessing. bbox uses x,y,width,height "
        "and the point must lie inside that bbox. Never infer off-screen UI or "
        "invent coordinates.\n\n"
        f"Requested target: {wanted}"
    )


def validate_grounding_capture(capture: Capture) -> None:
    """Refuse unbound or geometrically inconsistent frames before model work."""
    _validate_bound_capture(capture)


def ground_point(
    capture: Capture,
    *,
    image_x: int,
    image_y: int,
    label: str = "",
) -> GroundedPoint:
    """Map a point from the model image back to the verified screen frame."""
    _validate_bound_capture(capture)

    model_width = int(capture.image.width)
    model_height = int(capture.image.height)
    x = int(image_x)
    y = int(image_y)

    if model_width <= 0 or model_height <= 0:
        raise GroundingUnavailable("The model image has invalid dimensions.")
    if x < 0 or x >= model_width or y < 0 or y >= model_height:
        raise ValueError(
            f"Vision point ({x}, {y}) is outside the {model_width}x{model_height} model image."
        )

    source_x = min(
        capture.source_width - 1,
        (x * capture.source_width) // model_width,
    )
    source_y = min(
        capture.source_height - 1,
        (y * capture.source_height) // model_height,
    )

    return GroundedPoint(
        label=str(label or ""),
        image_x=x,
        image_y=y,
        screen_x=capture.frame_rect.left + source_x,
        screen_y=capture.frame_rect.top + source_y,
        hwnd=int(capture.window),
        binding_token=capture.binding_token,
        layout_token=capture.layout_token,
        frame_rect=capture.frame_rect,
        model_width=model_width,
        model_height=model_height,
        source_width=capture.source_width,
        source_height=capture.source_height,
    )


def parse_grounding_response(
    raw: str,
    *,
    expected_width: int,
    expected_height: int,
) -> VisionGroundingResponse:
    """Parse model JSON and reject every shape not explicitly in the contract."""
    if not isinstance(raw, str) or not raw.strip():
        raise GroundingResponseInvalid("Vision grounding returned no structured response.")
    if expected_width <= 0 or expected_height <= 0:
        raise GroundingResponseInvalid("Expected model-image dimensions are invalid.")

    try:
        payload = json.loads(raw, object_pairs_hook=_json_object_without_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise GroundingResponseInvalid("Vision grounding did not return valid JSON.") from exc

    root = _exact_object(
        payload,
        required={"status", "image_width", "image_height", "candidates"},
        where="grounding response",
    )

    status = root["status"]
    if not isinstance(status, str) or status not in {"found", "not_found", "ambiguous"}:
        raise GroundingResponseInvalid("Grounding status is not recognised.")

    width = _strict_int(root["image_width"], "image_width")
    height = _strict_int(root["image_height"], "image_height")
    if width != expected_width or height != expected_height:
        raise GroundingResponseInvalid(
            "Vision grounding dimensions do not match the exact image sent to the model."
        )

    raw_candidates = root["candidates"]
    if not isinstance(raw_candidates, list):
        raise GroundingResponseInvalid("Grounding candidates must be a JSON array.")
    if len(raw_candidates) > MAX_GROUNDING_CANDIDATES:
        raise GroundingResponseInvalid("Vision grounding returned too many candidates.")

    candidates = tuple(
        _parse_candidate(item, width=width, height=height, index=index)
        for index, item in enumerate(raw_candidates)
    )
    _reject_duplicate_candidates(candidates)

    if status == "found" and len(candidates) != 1:
        raise GroundingResponseInvalid("status='found' requires exactly one candidate.")
    if status == "not_found" and candidates:
        raise GroundingResponseInvalid("status='not_found' must not contain candidates.")
    if status == "ambiguous" and len(candidates) < 2:
        raise GroundingResponseInvalid("status='ambiguous' requires at least two candidates.")

    return VisionGroundingResponse(
        status=status,
        image_width=width,
        image_height=height,
        candidates=candidates,
    )


def ground_structured_response(
    capture: Capture,
    raw: str,
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> GroundedTarget:
    """Turn one validated unique model target into immutable screen coordinates."""
    _validate_bound_capture(capture)
    threshold = float(min_confidence)
    if not math.isfinite(threshold) or threshold < 0.0 or threshold > 1.0:
        raise ValueError("min_confidence must be a finite value between 0 and 1.")

    parsed = parse_grounding_response(
        raw,
        expected_width=int(capture.image.width),
        expected_height=int(capture.image.height),
    )

    if parsed.status == "not_found":
        raise GroundingNotFound("Vision did not find the requested UI target in this frame.")
    if parsed.status == "ambiguous":
        raise GroundingAmbiguous(
            f"Vision found {len(parsed.candidates)} plausible UI targets and Qronos will not guess."
        )

    candidate = parsed.candidates[0]
    if candidate.confidence < threshold:
        raise GroundingLowConfidence(
            f"Vision confidence {candidate.confidence:.3f} is below the required {threshold:.3f}."
        )

    point = ground_point(
        capture,
        image_x=candidate.image_x,
        image_y=candidate.image_y,
        label=candidate.label,
    )
    screen_box = _screen_box(capture, candidate.image_box)

    if not (
        screen_box.left <= point.screen_x < screen_box.right
        and screen_box.top <= point.screen_y < screen_box.bottom
    ):
        raise GroundingResponseInvalid(
            "Mapped target point fell outside its mapped bounding box."
        )

    return GroundedTarget(
        label=candidate.label,
        confidence=candidate.confidence,
        point=point,
        image_box=candidate.image_box,
        screen_box=screen_box,
    )


def revalidate_grounded_point(
    point: GroundedPoint,
    binding: WindowBinding,
    binder: WindowBinder,
    *,
    require_foreground: bool = True,
) -> tuple[int, int]:
    """Return the screen point only while its original binding/layout is current."""
    if point.hwnd != binding.hwnd:
        raise GroundingStale("The grounded point belongs to a different window handle.")
    if point.binding_token != binding.identity_token:
        raise GroundingStale("The grounded point belongs to a different process instance.")
    if point.layout_token != binding.layout_token:
        raise GroundingStale("The grounded point belongs to a different window layout.")

    try:
        fresh = binder.revalidate(
            binding,
            require_foreground=require_foreground,
            require_same_layout=True,
        )
    except WindowBindingStale as exc:
        raise GroundingStale(f"Grounded coordinates expired: {exc}") from exc

    if not (
        fresh.window_rect.left <= point.screen_x < fresh.window_rect.right
        and fresh.window_rect.top <= point.screen_y < fresh.window_rect.bottom
    ):
        raise GroundingStale("The grounded point is outside the verified window bounds.")

    return point.screen_x, point.screen_y


def revalidate_grounded_target(
    target: GroundedTarget,
    binding: WindowBinding,
    binder: WindowBinder,
    *,
    require_foreground: bool = True,
) -> tuple[int, int]:
    """Revalidate the target provenance and return only its safe action point."""
    point = revalidate_grounded_point(
        target.point,
        binding,
        binder,
        require_foreground=require_foreground,
    )

    if not (
        binding.snapshot.window_rect.left <= target.screen_box.left
        and binding.snapshot.window_rect.top <= target.screen_box.top
        and target.screen_box.right <= binding.snapshot.window_rect.right
        and target.screen_box.bottom <= binding.snapshot.window_rect.bottom
    ):
        raise GroundingStale("The grounded target bounds are outside the verified window.")

    return point


def _validate_bound_capture(capture: Capture) -> None:
    if capture.window is None:
        raise GroundingUnavailable(
            "Vision grounding requires a verified window capture, not a whole-screen frame."
        )
    if not capture.binding_token or not capture.layout_token:
        raise GroundingUnavailable("The capture is not tied to a verified WindowBinding.")
    if capture.frame_rect is None or not capture.frame_rect.usable:
        raise GroundingUnavailable(
            "The capture does not record the exact screen rectangle it represents."
        )
    if capture.source_width <= 0 or capture.source_height <= 0:
        raise GroundingUnavailable("The capture does not record its source pixel size.")
    if (
        capture.frame_rect.width != capture.source_width
        or capture.frame_rect.height != capture.source_height
    ):
        raise GroundingUnavailable(
            "Captured source dimensions do not match the verified screen rectangle."
        )


def _json_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GroundingResponseInvalid(
                f"Vision grounding JSON contains duplicate field {key!r}."
            )
        result[key] = value
    return result


def _exact_object(value: Any, *, required: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GroundingResponseInvalid(f"{where} must be a JSON object.")
    keys = set(value)
    if keys != required:
        missing = sorted(required - keys)
        extra = sorted(keys - required)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise GroundingResponseInvalid(f"{where} has invalid fields: {'; '.join(details)}.")
    return value


def _strict_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GroundingResponseInvalid(f"{name} must be an integer.")
    return value


def _finite_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GroundingResponseInvalid("Candidate confidence must be numeric.")
    confidence = float(value)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise GroundingResponseInvalid("Candidate confidence must be finite and between 0 and 1.")
    return confidence


def _parse_candidate(value: Any, *, width: int, height: int, index: int) -> VisionCandidate:
    candidate = _exact_object(
        value,
        required={"label", "confidence", "point", "bbox"},
        where=f"candidate[{index}]",
    )

    label = candidate["label"]
    if not isinstance(label, str) or not label.strip():
        raise GroundingResponseInvalid(f"candidate[{index}].label must be non-empty text.")
    label = label.strip()
    if len(label) > MAX_TARGET_LABEL_CHARS:
        raise GroundingResponseInvalid(f"candidate[{index}].label is too long.")

    confidence = _finite_confidence(candidate["confidence"])

    point = _exact_object(
        candidate["point"],
        required={"x", "y"},
        where=f"candidate[{index}].point",
    )
    x = _strict_int(point["x"], f"candidate[{index}].point.x")
    y = _strict_int(point["y"], f"candidate[{index}].point.y")
    if x < 0 or x >= width or y < 0 or y >= height:
        raise GroundingResponseInvalid(f"candidate[{index}] point is outside the model image.")

    box_value = _exact_object(
        candidate["bbox"],
        required={"x", "y", "width", "height"},
        where=f"candidate[{index}].bbox",
    )
    box = ImageBox(
        x=_strict_int(box_value["x"], f"candidate[{index}].bbox.x"),
        y=_strict_int(box_value["y"], f"candidate[{index}].bbox.y"),
        width=_strict_int(box_value["width"], f"candidate[{index}].bbox.width"),
        height=_strict_int(box_value["height"], f"candidate[{index}].bbox.height"),
    )
    if box.x < 0 or box.y < 0 or box.width <= 0 or box.height <= 0:
        raise GroundingResponseInvalid(f"candidate[{index}] bbox has invalid geometry.")
    if box.right > width or box.bottom > height:
        raise GroundingResponseInvalid(f"candidate[{index}] bbox is outside the model image.")
    if not box.contains(x, y):
        raise GroundingResponseInvalid(f"candidate[{index}] point is outside its bbox.")

    return VisionCandidate(
        label=label,
        confidence=confidence,
        image_x=x,
        image_y=y,
        image_box=box,
    )


def _reject_duplicate_candidates(candidates: tuple[VisionCandidate, ...]) -> None:
    # Geometry is the identity here. Two labels attached to the same point and
    # box are not two independent alternatives and must not manufacture
    # ambiguity.
    seen: set[tuple[int, int, ImageBox]] = set()
    for candidate in candidates:
        key = (
            candidate.image_x,
            candidate.image_y,
            candidate.image_box,
        )
        if key in seen:
            raise GroundingResponseInvalid("Vision grounding returned a duplicate candidate.")
        seen.add(key)


def _screen_box(capture: Capture, box: ImageBox) -> WindowRect:
    # Preserve coverage during down/up-scaling: floor the leading edge and ceil
    # the trailing edge. Coordinates remain half-open throughout.
    model_width = int(capture.image.width)
    model_height = int(capture.image.height)
    source_left = (box.x * capture.source_width) // model_width
    source_top = (box.y * capture.source_height) // model_height
    source_right = min(
        capture.source_width,
        ((box.right * capture.source_width) + model_width - 1) // model_width,
    )
    source_bottom = min(
        capture.source_height,
        ((box.bottom * capture.source_height) + model_height - 1) // model_height,
    )

    return WindowRect(
        capture.frame_rect.left + source_left,
        capture.frame_rect.top + source_top,
        capture.frame_rect.left + source_right,
        capture.frame_rect.top + source_bottom,
    )
