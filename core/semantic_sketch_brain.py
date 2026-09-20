from __future__ import annotations

from dataclasses import dataclass

from core.brain_runtime import BrainMessage, BrainMessageRole, BrainRuntime
from core.model_registry import ModelProfile, get_model
from core.semantic_sketch import (
    SEMANTIC_SKETCH_RESPONSE_FORMAT,
    SemanticSketchDocument,
    SemanticSketchError,
    parse_semantic_model_json,
)


MAX_SEMANTIC_REQUEST_CHARS = 1200
MAX_SEMANTIC_REPAIR_ATTEMPTS = 1
DEFAULT_SEMANTIC_ROLE = "fast"
DEFAULT_SEMANTIC_EFFORT = "default"
MIN_SEMANTIC_NUM_PREDICT = 1024
_REASONING_BUDGET_ERROR = "full generation budget for reasoning"


_SYSTEM_PROMPT = """You are Qronos's semantic line-art planner.

Describe the requested simple sketch as a small categorical scene graph. You are
NOT allowed to choose x/y coordinates or numeric geometry. Qronos will place and
render the parts deterministically.

The scene has an IMPLICIT INVISIBLE ROOT. Represent it with exactly one part:
- id: scene_root
- primitive: group
- relation: scene_center
- target: __scene__
- placement: center
- scale: large
- proportion: default
- direction: auto
This scene_root is a structural container only and is NEVER drawn. Do not use a
visible ellipse/triangle/rectangle as the root.

Stage 4B.3K uses a FLAT VISIBLE scene graph:
- scene_root is the ONLY allowed group and the ONLY invisible part.
- EVERY other part MUST be directly drawable: ellipse, rectangle, triangle, line,
  or arc. Never create body/group, legs/group, wheels/group, object/group, or any
  other nested group.
- Every major visible component needed to recognize the requested sketch must be
  represented by its own drawable part. Plural/aggregate concepts such as legs,
  wheels, arms, branches, windows, etc. must be decomposed into concrete visible
  parts when they matter to the request.
- Direct drawable children of scene_root use relation=inside.
- Attach/contain details directly to visible drawable parents. Use
  above/below/left_of/right_of for relationships between separate objects.
- object_id is metadata only. Give all parts of one requested object the same
  short ASCII object_id; use different object_id values for distinct requested
  objects. scene_root uses object_id=__scene__.

Each part has ONLY categorical semantic fields:
- id: unique short ASCII identifier.
- object_id: metadata identifier for the requested object this visible part belongs to.
- primitive: group ONLY for scene_root; otherwise ellipse, rectangle, triangle,
  line, or arc.
- relation: scene_center for scene_root only; otherwise inside, attached_top,
  attached_bottom, attached_left, attached_right, above, below, left_of, right_of.
- target: ALWAYS a string. Use exactly __scene__ only for scene_root; otherwise
  use another part id from this document.
- placement: center/top/bottom/left/right/top_left/top_right/bottom_left/bottom_right.
- scale: tiny/small/medium/large.
- proportion: default/tall/wide/thin.
- direction: auto/horizontal/vertical/up/down/left/right/up_left/up_right/
  down_left/down_right. Use explicit upward/downward directions when the user's
  requested pose depends on them.

Also output a requirements array. Requirements are a machine-checkable summary
of the user's EXPLICIT semantic constraints. Every requirement has:
- subject: exact id of a part in parts.
- primitive: any/group/ellipse/rectangle/triangle/line/arc.
- relation: any/attached/inside/above/below/left_of/right_of.
- target: __any__ or an exact part id.
- direction: any/upward/downward/leftward/rightward/horizontal/vertical.

Examples of requirement logic (not shape templates):
- if the user explicitly asks for an upward/raised part, create a requirement
  whose subject is that part and direction=upward.
- if the user explicitly says X is above Y, require subject=X, relation=above, target=Y.
- if the request only requires an object to exist, use relation=any, target=__any__,
  direction=any.

Rules:
- Requirements must refer to ids that actually exist in parts.
- Never use primitive=group in requirements; requirements describe visible parts.
- The graph itself must satisfy every requirement before preview is allowed.
- Use semantic attachment, containment, and relative placement instead of coordinates.
- Prefer 4..16 meaningful VISIBLE parts; more parts do not automatically improve the sketch.
- For limbs/branches/handles use line parts attached to a visible drawable parent.
- A hand/end-cap attached to a line should attach to the endpoint matching its
  attached_top/bottom/left/right relation.
- Never emit raw points, coordinates, aspect ratios, dimensions, angles, lengths,
  screen locations, mouse actions, code, or prose.
- Output only JSON matching the supplied schema.
"""


@dataclass(frozen=True)
class SemanticBrainSketch:
    request: str
    document: SemanticSketchDocument
    model_name: str
    attempts: int


class SemanticSketchPlanner:
    """Semantic-brain request -> strict scene graph, without coordinates.

    The realtime default intentionally uses the fast brain. The heavier role remains
    available explicitly for offline experiments, but is not required by the live
    sketch path.
    """

    def __init__(
        self,
        runtime: BrainRuntime,
        *,
        role: str = DEFAULT_SEMANTIC_ROLE,
        effort: str = DEFAULT_SEMANTIC_EFFORT,
        timeout_seconds: float = 90.0,
        max_repair_attempts: int = MAX_SEMANTIC_REPAIR_ATTEMPTS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        if (
            isinstance(max_repair_attempts, bool)
            or not isinstance(max_repair_attempts, int)
            or not (0 <= max_repair_attempts <= MAX_SEMANTIC_REPAIR_ATTEMPTS)
        ):
            raise ValueError(
                "max_repair_attempts must be an integer from 0 through "
                f"{MAX_SEMANTIC_REPAIR_ATTEMPTS}."
            )
        self._runtime = runtime
        self._profile: ModelProfile = get_model(role)
        self._effort = effort
        self._timeout = float(timeout_seconds)
        self._max_repair_attempts = max_repair_attempts

    @property
    def model_name(self) -> str:
        return self._profile.name

    def plan(
        self,
        request: str,
        *,
        visual_feedback: str = "",
    ) -> SemanticBrainSketch:
        cleaned = str(request).strip()
        if not cleaned:
            raise SemanticSketchError("Semantic sketch request cannot be empty.")
        if len(cleaned) > MAX_SEMANTIC_REQUEST_CHARS:
            raise SemanticSketchError(
                f"Semantic sketch request exceeds {MAX_SEMANTIC_REQUEST_CHARS} characters."
            )

        feedback = str(visual_feedback).strip()
        generation = self._profile.generation_for(self._effort)
        num_predict = max(generation.num_predict or 0, MIN_SEMANTIC_NUM_PREDICT)
        last_error: SemanticSketchError | None = None

        try:
            for attempt_index in range(self._max_repair_attempts + 1):
                messages = self._messages_for_attempt(
                    cleaned,
                    visual_feedback=feedback,
                    attempt_index=attempt_index,
                    previous_error=last_error,
                )
                raw = self._chat_with_reasoning_fallback(
                    messages=messages,
                    think=generation.think,
                    num_predict=num_predict,
                )
                try:
                    document = parse_semantic_model_json(raw)
                except SemanticSketchError as exc:
                    last_error = exc
                    if attempt_index >= self._max_repair_attempts:
                        raise SemanticSketchError(
                            "Semantic sketch generation failed strict validation "
                            f"after {attempt_index + 1} attempt(s): {exc}"
                        ) from exc
                    continue

                return SemanticBrainSketch(
                    request=cleaned,
                    document=document,
                    model_name=self._profile.name,
                    attempts=attempt_index + 1,
                )
        finally:
            self._runtime.stop_model(self._profile.name)

        raise AssertionError("Unreachable semantic sketch planner state.")


    def _chat_with_reasoning_fallback(
        self,
        *,
        messages: list[BrainMessage],
        think: bool | str,
        num_predict: int,
    ) -> str:
        """Run one bounded model call, retrying only reasoning-budget exhaustion."""

        def invoke(reasoning: bool | str) -> str:
            return self._runtime.chat(
                self._profile.name,
                messages=messages,
                think=reasoning,
                num_predict=num_predict,
                num_ctx=self._profile.context_tokens,
                # The vision critic runs next, so never keep the semantic planner resident.
                keep_alive="0",
                response_format=SEMANTIC_SKETCH_RESPONSE_FORMAT,
                timeout_seconds=self._timeout,
            )

        try:
            return invoke(think)
        except RuntimeError as exc:
            if think is False or _REASONING_BUDGET_ERROR not in str(exc).lower():
                raise
            # Optional reasoning-capable profiles can still be supplied explicitly.
            # If one consumes the whole budget on hidden reasoning, retry exactly once
            # with reasoning disabled instead of escalating global model settings.
            return invoke(False)

    @staticmethod
    def _messages_for_attempt(
        request: str,
        *,
        visual_feedback: str,
        attempt_index: int,
        previous_error: SemanticSketchError | None,
    ) -> list[BrainMessage]:
        user = f"Requested sketch:\n{request}"
        if visual_feedback:
            user += (
                "\n\nA visual critic rejected the previous candidate. "
                "Regenerate the semantic scene graph from scratch while addressing "
                "these problems:\n"
                f"{visual_feedback}"
            )
        if attempt_index and previous_error is not None:
            user += (
                "\n\nYour previous response was rejected before rendering. "
                "Regenerate the entire JSON document from scratch.\n"
                f"Validation error: {previous_error}\n"
                "Wire-format reminder: include requirements plus parts. Use exactly one invisible "
                "scene_root group with scene_center/target='__scene__'/object_id='__scene__'. "
                "No other group is allowed: every non-root part must be a directly drawable "
                "ellipse/rectangle/triangle/line/arc. Direct drawable children of scene_root "
                "must use relation='inside'. Give parts of the same requested object the same "
                "object_id. Every requirement subject/target id must exist and the graph must satisfy the "
                "required relation/direction. All "
                "geometry hints are categorical: never output aspect ratios, coordinates, "
                "dimensions, angles, or lengths."
            )
        return [
            BrainMessage(role=BrainMessageRole.SYSTEM, content=_SYSTEM_PROMPT),
            BrainMessage(role=BrainMessageRole.USER, content=user),
        ]
