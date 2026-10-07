from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any

from core.brain_runtime import BrainMessage, BrainMessageRole, BrainRuntime
from core.model_registry import get_model
from core.screen_capture import Capture
from core.vision_grounding import (
    GroundedPoint,
    ground_point,
    validate_grounding_capture,
)


class VisualTargetingError(RuntimeError):
    """Base error for generic visual target detection."""


class VisualTargetNotFound(VisualTargetingError):
    """The requested target was not visible."""


class VisualTargetAmbiguous(VisualTargetingError):
    """More than one target plausibly matched."""


class VisualTargetLowConfidence(VisualTargetingError):
    """The model found something, but not confidently enough to act."""


class VisualTargetInvalid(VisualTargetingError):
    """The model returned an unsafe or malformed targeting response."""


@dataclass(frozen=True)
class VisualTarget:
    """One visually located target tied to one exact captured window layout."""

    label: str
    confidence: float
    point: GroundedPoint

    @property
    def screen_x(self) -> int:
        return self.point.screen_x

    @property
    def screen_y(self) -> int:
        return self.point.screen_y

    @property
    def hwnd(self) -> int:
        return self.point.hwnd

    @property
    def binding_token(self) -> str:
        return self.point.binding_token

    @property
    def layout_token(self) -> str:
        return self.point.layout_token


def visual_target_schema(width: int, height: int) -> dict[str, Any]:
    """
    Structured-output grammar for generic UI targeting.

    Deliberately point-only.

    Large model-generated bounding boxes proved unreliable in live testing.
    For a click target we need one safe interior point, while exact window
    provenance and input authorization remain the actual safety boundary.
    """

    width = int(width)
    height = int(height)

    if width <= 0 or height <= 0:
        raise ValueError("Visual target image dimensions must be positive.")

    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["found", "not_found", "ambiguous"],
            },
            "image_width": {
                "type": "integer",
                "enum": [width],
            },
            "image_height": {
                "type": "integer",
                "enum": [height],
            },
            "candidates": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "point": {
                            "type": "object",
                            "properties": {
                                "x": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": width - 1,
                                },
                                "y": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": height - 1,
                                },
                            },
                            "required": ["x", "y"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["label", "confidence", "point"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "status",
            "image_width",
            "image_height",
            "candidates",
        ],
        "additionalProperties": False,
    }


def visual_target_prompt(
    target: str,
    *,
    width: int,
    height: int,
) -> str:
    wanted = str(target or "").strip()

    if not wanted:
        raise ValueError("Visual target description must not be empty.")

    return (
        "Locate the requested visible UI target in this exact screenshot. "
        "This is generic computer-control perception and is not specific to "
        "any application. The screenshot dimensions are exactly "
        f"{width}x{height}. Coordinates use (0,0) at the top-left. "
        "For one clearly visible matching target return status='found' and "
        "exactly one candidate. For no visible match return status='not_found'. "
        "If multiple targets could reasonably satisfy the request, return "
        "status='ambiguous' and list them instead of guessing. "
        "For each candidate, point must be a safe visible interior point near "
        "the visual center of the requested control or object. Never place the "
        "point on a border, outside the image, or on a nearby unrelated item. "
        "Do not infer hidden, covered, off-screen, or disabled UI. "
        "Use the exact image dimensions supplied by the response schema. "
        "Return only the requested structured JSON.\n\n"
        f"Requested target: {wanted}"
    )


class VisualTargetLocator:
    """
    Generic visual target locator.

    It performs perception only. It never clicks, types, drags, scrolls,
    launches applications, or changes machine state.
    """

    def __init__(
        self,
        runtime: BrainRuntime,
        *,
        min_confidence: float = 0.85,
    ) -> None:
        threshold = float(min_confidence)

        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1.")

        self.runtime = runtime
        self.min_confidence = threshold

    def locate(
        self,
        target: str,
        capture: Capture,
    ) -> VisualTarget:
        validate_grounding_capture(capture)

        width = int(capture.image.width)
        height = int(capture.image.height)

        prompt = visual_target_prompt(
            target,
            width=width,
            height=height,
        )

        profile = get_model("vision")

        try:
            raw = self.runtime.chat(
                model_name=profile.name,
                messages=[
                    BrainMessage(
                        role=BrainMessageRole.USER,
                        content=prompt,
                        images=(capture.image,),
                    )
                ],
                think=False,
                num_predict=256,
                num_ctx=profile.context_tokens,
                keep_alive="0",
                response_format=visual_target_schema(width, height),
            )
        finally:
            self.runtime.stop_model(profile.name)

        parsed = _parse_response(
            raw,
            width=width,
            height=height,
        )

        status = parsed["status"]
        candidates = parsed["candidates"]

        if status == "not_found":
            raise VisualTargetNotFound(
                "Vision did not find the requested target in this window."
            )

        if status == "ambiguous":
            raise VisualTargetAmbiguous(
                "Vision found multiple plausible targets and refused to guess."
            )

        candidate = candidates[0]

        confidence = candidate["confidence"]

        if confidence < self.min_confidence:
            raise VisualTargetLowConfidence(
                f"Visual target confidence {confidence:.3f} is below "
                f"{self.min_confidence:.3f}."
            )

        image_x = candidate["point"]["x"]
        image_y = candidate["point"]["y"]

        point = ground_point(
            capture,
            image_x=image_x,
            image_y=image_y,
            label=candidate["label"],
        )

        return VisualTarget(
            label=candidate["label"],
            confidence=confidence,
            point=point,
        )


def _parse_response(
    raw: str,
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise VisualTargetInvalid("Vision returned no targeting response.")

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
        )
    except json.JSONDecodeError as exc:
        raise VisualTargetInvalid(
            "Visual targeting response was not valid JSON."
        ) from exc

    if not isinstance(payload, dict):
        raise VisualTargetInvalid(
            "Visual targeting response must be a JSON object."
        )

    required = {
        "status",
        "image_width",
        "image_height",
        "candidates",
    }

    if set(payload) != required:
        raise VisualTargetInvalid(
            "Visual targeting response has missing or unexpected fields."
        )

    status = payload["status"]

    if status not in {"found", "not_found", "ambiguous"}:
        raise VisualTargetInvalid("Unknown visual targeting status.")

    returned_width = _strict_int(
        payload["image_width"],
        "image_width",
    )
    returned_height = _strict_int(
        payload["image_height"],
        "image_height",
    )

    if returned_width != width or returned_height != height:
        raise VisualTargetInvalid(
            "Vision dimensions do not match the exact screenshot."
        )

    candidates = payload["candidates"]

    if not isinstance(candidates, list):
        raise VisualTargetInvalid("candidates must be a list.")

    if len(candidates) > 5:
        raise VisualTargetInvalid("Too many visual target candidates.")

    if status == "found" and len(candidates) != 1:
        raise VisualTargetInvalid(
            "status='found' requires exactly one candidate."
        )

    if status == "not_found" and candidates:
        raise VisualTargetInvalid(
            "status='not_found' must not contain candidates."
        )

    if status == "ambiguous" and len(candidates) < 2:
        raise VisualTargetInvalid(
            "status='ambiguous' requires at least two candidates."
        )

    parsed_candidates = [
        _parse_candidate(
            candidate,
            width=width,
            height=height,
        )
        for candidate in candidates
    ]

    return {
        "status": status,
        "image_width": width,
        "image_height": height,
        "candidates": parsed_candidates,
    }


def _parse_candidate(
    candidate: object,
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise VisualTargetInvalid("Candidate must be an object.")

    if set(candidate) != {"label", "confidence", "point"}:
        raise VisualTargetInvalid(
            "Candidate has missing or unexpected fields."
        )

    label = candidate["label"]

    if not isinstance(label, str) or not label.strip():
        raise VisualTargetInvalid(
            "Candidate label must be non-empty text."
        )

    confidence_value = candidate["confidence"]

    if isinstance(confidence_value, bool) or not isinstance(
        confidence_value,
        (int, float),
    ):
        raise VisualTargetInvalid(
            "Candidate confidence must be numeric."
        )

    confidence = float(confidence_value)

    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise VisualTargetInvalid(
            "Candidate confidence must be between 0 and 1."
        )

    point = candidate["point"]

    if not isinstance(point, dict) or set(point) != {"x", "y"}:
        raise VisualTargetInvalid(
            "Candidate point must contain only x and y."
        )

    x = _strict_int(point["x"], "point.x")
    y = _strict_int(point["y"], "point.y")

    if not 0 <= x < width or not 0 <= y < height:
        raise VisualTargetInvalid(
            "Candidate point is outside the screenshot."
        )

    return {
        "label": label.strip(),
        "confidence": confidence,
        "point": {
            "x": x,
            "y": y,
        },
    }


def _strict_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VisualTargetInvalid(
            f"{name} must be an integer."
        )

    return value


def _object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise VisualTargetInvalid(
                f"Duplicate JSON field: {key!r}."
            )

        result[key] = value

    return result
