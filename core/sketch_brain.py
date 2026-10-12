from __future__ import annotations

from dataclasses import dataclass

from core.brain_runtime import BrainMessage, BrainMessageRole, BrainRuntime
from core.model_registry import ModelProfile, get_model
from core.shape_planner import ShapePlan
from core.sketch_compiler import compile_sketch
from core.sketch_dsl import SketchDocument, SketchDslError, parse_sketch_json


MAX_SKETCH_REQUEST_CHARS = 1200
MAX_SKETCH_REPAIR_ATTEMPTS = 2
MIN_SKETCH_NUM_PREDICT = 768


SKETCH_RESPONSE_FORMAT: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "operations"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "name": {"type": "string", "minLength": 1, "maxLength": 80},
        "operations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "start", "end"],
                        "properties": {
                            "type": {"const": "line"},
                            "start": {"$ref": "#/$defs/point"},
                            "end": {"$ref": "#/$defs/point"},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "points"],
                        "properties": {
                            "type": {"const": "polyline"},
                            "points": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 512,
                                "items": {"$ref": "#/$defs/point"},
                            },
                            "closed": {"type": "boolean"},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "points"],
                        "properties": {
                            "type": {"const": "polygon"},
                            "points": {
                                "type": "array",
                                "minItems": 3,
                                "maxItems": 512,
                                "items": {"$ref": "#/$defs/point"},
                            },
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "x", "y", "width", "height"],
                        "properties": {
                            "type": {"const": "rectangle"},
                            "x": {"$ref": "#/$defs/unit"},
                            "y": {"$ref": "#/$defs/unit"},
                            "width": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                            "height": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "cx", "cy", "rx", "ry"],
                        "properties": {
                            "type": {"const": "ellipse"},
                            "cx": {"$ref": "#/$defs/unit"},
                            "cy": {"$ref": "#/$defs/unit"},
                            "rx": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5},
                            "ry": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5},
                            "segments": {"type": "integer", "minimum": 12, "maximum": 256},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "type", "cx", "cy", "rx", "ry",
                            "start_degrees", "end_degrees",
                        ],
                        "properties": {
                            "type": {"const": "arc"},
                            "cx": {"$ref": "#/$defs/unit"},
                            "cy": {"$ref": "#/$defs/unit"},
                            "rx": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5},
                            "ry": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5},
                            "start_degrees": {"type": "number"},
                            "end_degrees": {"type": "number"},
                            "segments": {"type": "integer", "minimum": 4, "maximum": 256},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "p0", "p1", "p2", "p3"],
                        "properties": {
                            "type": {"const": "bezier"},
                            "p0": {"$ref": "#/$defs/point"},
                            "p1": {"$ref": "#/$defs/point"},
                            "p2": {"$ref": "#/$defs/point"},
                            "p3": {"$ref": "#/$defs/point"},
                            "segments": {"type": "integer", "minimum": 4, "maximum": 256},
                        },
                    },
                ]
            },
        },
    },
    "$defs": {
        "unit": {"type": "number", "minimum": 0, "maximum": 1},
        "point": {
            "type": "array",
            "prefixItems": [
                {"$ref": "#/$defs/unit"},
                {"$ref": "#/$defs/unit"},
            ],
            "minItems": 2,
            "maxItems": 2,
        },
    },
}


_SYSTEM_PROMPT = """You are Qronos's local vector-sketch planner.
Convert the user's requested simple drawing into ONE JSON document matching the supplied schema.

Rules:
- Plan geometry only. Never emit Python, shell commands, application actions, pixels, HWNDs, or screen coordinates.
- All geometry uses normalized 0.0..1.0 coordinates.
- Prefer a modest margin (roughly 0.08..0.92) and simple recognizable geometry.
- Compose unfamiliar objects from line/polyline/polygon/rectangle/ellipse/arc/bezier primitives.
- Do not depend on a named-shape library. Decompose the requested object yourself.
- Prefer few operations and few points; complexity does not imply quality.
- Prefer ellipse/arc/bezier primitives over spelling smooth curves as long point lists.
- Keep the JSON concise; use no more than about 16 operations unless the request truly needs more.
- Output only the structured JSON required by the response schema.
"""


@dataclass(frozen=True)
class BrainSketch:
    request: str
    document: SketchDocument
    plan: ShapePlan
    model_name: str
    attempts: int = 1


class SketchBrainPlanner:
    """Local-brain text -> strict Sketch DSL -> bounded ShapePlan pipeline."""

    def __init__(
        self,
        runtime: BrainRuntime,
        *,
        role: str = "fast",
        effort: str = "default",
        timeout_seconds: float = 45.0,
        max_repair_attempts: int = MAX_SKETCH_REPAIR_ATTEMPTS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        if (
            isinstance(max_repair_attempts, bool)
            or not isinstance(max_repair_attempts, int)
            or not (0 <= max_repair_attempts <= MAX_SKETCH_REPAIR_ATTEMPTS)
        ):
            raise ValueError(
                "max_repair_attempts must be an integer from 0 through "
                f"{MAX_SKETCH_REPAIR_ATTEMPTS}."
            )
        self._runtime = runtime
        self._profile: ModelProfile = get_model(role)
        self._effort = effort
        self._timeout = float(timeout_seconds)
        self._max_repair_attempts = max_repair_attempts

    @property
    def model_name(self) -> str:
        return self._profile.name

    def plan(self, request: str) -> BrainSketch:
        cleaned = str(request).strip()
        if not cleaned:
            raise SketchDslError("Sketch request cannot be empty.")
        if len(cleaned) > MAX_SKETCH_REQUEST_CHARS:
            raise SketchDslError(
                f"Sketch request exceeds {MAX_SKETCH_REQUEST_CHARS} characters."
            )

        generation = self._profile.generation_for(self._effort)
        num_predict = max(generation.num_predict or 0, MIN_SKETCH_NUM_PREDICT)
        last_error: SketchDslError | None = None

        for attempt_index in range(self._max_repair_attempts + 1):
            messages = self._messages_for_attempt(
                cleaned,
                attempt_index=attempt_index,
                previous_error=last_error,
            )
            raw = self._runtime.chat(
                self._profile.name,
                messages=messages,
                think=generation.think,
                num_predict=num_predict,
                num_ctx=self._profile.context_tokens,
                keep_alive=generation.keep_alive,
                response_format=SKETCH_RESPONSE_FORMAT,
                timeout_seconds=self._timeout,
            )
            try:
                document = parse_sketch_json(raw)
                plan = compile_sketch(document)
            except SketchDslError as exc:
                last_error = exc
                if attempt_index >= self._max_repair_attempts:
                    raise SketchDslError(
                        "Local sketch brain failed strict validation after "
                        f"{attempt_index + 1} attempt(s): {exc}"
                    ) from exc
                continue

            return BrainSketch(
                request=cleaned,
                document=document,
                plan=plan,
                model_name=self._profile.name,
                attempts=attempt_index + 1,
            )

        raise AssertionError("Sketch repair loop exhausted unexpectedly.")

    def _messages_for_attempt(
        self,
        request: str,
        *,
        attempt_index: int,
        previous_error: SketchDslError | None,
    ) -> tuple[BrainMessage, ...]:
        user_prompt = "Create a simple vector sketch for this request:\n" + request
        if attempt_index == 0:
            return (
                BrainMessage(BrainMessageRole.SYSTEM, _SYSTEM_PROMPT),
                BrainMessage(BrainMessageRole.USER, user_prompt),
            )

        error_text = str(previous_error or "unknown strict-validation failure")
        repair_prompt = (
            user_prompt
            + "\n\nYour previous response was rejected by Qronos's strict sketch "
              "validator. Regenerate the entire document from scratch. "
              "Do not explain the error, do not use Markdown fences, and do not "
              "copy malformed syntax. Return exactly one complete JSON object "
              "matching the response schema.\nValidation error: "
            + error_text[:600]
        )
        return (
            BrainMessage(BrainMessageRole.SYSTEM, _SYSTEM_PROMPT),
            BrainMessage(BrainMessageRole.USER, repair_prompt),
        )
