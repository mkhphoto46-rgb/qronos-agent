from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

from core.brain_runtime import BrainMessage, BrainMessageRole, BrainRuntime
from core.model_registry import ModelProfile, get_model
from core.vision_image import PreparedImage


MAX_CRITIC_ISSUES = 8
MAX_CRITIC_REPAIR_ATTEMPTS = 1
DEFAULT_CRITIC_MIN_CONFIDENCE = 0.72


class SketchCriticError(ValueError):
    """Visual critic output was malformed or unavailable."""


CRITIC_RESPONSE_FORMAT: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "verdict",
        "recognizable",
        "request_satisfied",
        "composition_coherent",
        "confidence",
        "summary",
        "issues",
    ],
    "properties": {
        "verdict": {"enum": ["pass", "fail"]},
        "recognizable": {"type": "boolean"},
        "request_satisfied": {"type": "boolean"},
        "composition_coherent": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string", "maxLength": 240},
        "issues": {
            "type": "array",
            "maxItems": MAX_CRITIC_ISSUES,
            "items": {"type": "string", "minLength": 1, "maxLength": 180},
        },
    },
}


@dataclass(frozen=True)
class SketchCritique:
    verdict: str
    recognizable: bool
    request_satisfied: bool
    composition_coherent: bool
    confidence: float
    summary: str
    issues: tuple[str, ...]
    model_name: str

    def passes(self, *, min_confidence: float = DEFAULT_CRITIC_MIN_CONFIDENCE) -> bool:
        return (
            self.verdict == "pass"
            and self.recognizable
            and self.request_satisfied
            and self.composition_coherent
            and self.confidence >= min_confidence
        )

    def feedback(self) -> str:
        problems = list(self.issues)
        if not self.recognizable:
            problems.append("The intended subject was not clearly recognizable.")
        if not self.request_satisfied:
            problems.append("One or more explicit requirements were missing or unclear.")
        if not self.composition_coherent:
            problems.append("Major parts were disconnected, misplaced, or compositionally incoherent.")
        if self.summary:
            problems.append(f"Critic summary: {self.summary}")
        # Preserve order but avoid repeated feedback.
        unique = []
        for item in problems:
            if item not in unique:
                unique.append(item)
        return "\n".join(f"- {item}" for item in unique[:10])


def parse_critic_json(text: str, *, model_name: str = "") -> SketchCritique:
    cleaned = str(text).strip()
    if not cleaned:
        raise SketchCriticError("Sketch critic output is empty.")
    try:
        raw = json.loads(
            cleaned,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except SketchCriticError:
        raise
    except json.JSONDecodeError as exc:
        raise SketchCriticError(f"Sketch critic output is not strict JSON: {exc}.") from exc

    if not isinstance(raw, dict):
        raise SketchCriticError("Sketch critic root must be an object.")
    required = {
        "verdict",
        "recognizable",
        "request_satisfied",
        "composition_coherent",
        "confidence",
        "summary",
        "issues",
    }
    _require_exact_keys(raw, required=required, where="root")

    verdict = raw["verdict"]
    if verdict not in {"pass", "fail"}:
        raise SketchCriticError("root.verdict must be 'pass' or 'fail'.")

    bool_fields = {}
    for key in ("recognizable", "request_satisfied", "composition_coherent"):
        if not isinstance(raw[key], bool):
            raise SketchCriticError(f"root.{key} must be boolean.")
        bool_fields[key] = raw[key]

    confidence = raw["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise SketchCriticError("root.confidence must be a finite number.")
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise SketchCriticError("root.confidence must be within 0.0..1.0.")

    summary = raw["summary"]
    if not isinstance(summary, str) or len(summary) > 240:
        raise SketchCriticError("root.summary must be a string up to 240 characters.")

    issues = raw["issues"]
    if not isinstance(issues, list) or len(issues) > MAX_CRITIC_ISSUES:
        raise SketchCriticError(
            f"root.issues must be an array of at most {MAX_CRITIC_ISSUES} strings."
        )
    parsed_issues = []
    for index, issue in enumerate(issues):
        if not isinstance(issue, str) or not issue.strip() or len(issue) > 180:
            raise SketchCriticError(
                f"root.issues[{index}] must be a non-empty string up to 180 characters."
            )
        parsed_issues.append(issue.strip())

    return SketchCritique(
        verdict=verdict,
        recognizable=bool_fields["recognizable"],
        request_satisfied=bool_fields["request_satisfied"],
        composition_coherent=bool_fields["composition_coherent"],
        confidence=confidence,
        summary=summary.strip(),
        issues=tuple(parsed_issues),
        model_name=model_name,
    )


class SketchVisualCritic:
    """Qwen-VL semantic gate over a rendered preview, before mouse execution."""

    def __init__(
        self,
        runtime: BrainRuntime,
        *,
        role: str = "vision",
        timeout_seconds: float = 45.0,
        max_repair_attempts: int = MAX_CRITIC_REPAIR_ATTEMPTS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        if (
            isinstance(max_repair_attempts, bool)
            or not isinstance(max_repair_attempts, int)
            or not (0 <= max_repair_attempts <= MAX_CRITIC_REPAIR_ATTEMPTS)
        ):
            raise ValueError(
                "max_repair_attempts must be an integer from 0 through "
                f"{MAX_CRITIC_REPAIR_ATTEMPTS}."
            )
        self._runtime = runtime
        self._profile: ModelProfile = get_model(role)
        self._timeout = float(timeout_seconds)
        self._max_repair_attempts = max_repair_attempts

    @property
    def model_name(self) -> str:
        return self._profile.name

    def critique(self, request: str, preview: PreparedImage) -> SketchCritique:
        cleaned = str(request).strip()
        if not cleaned:
            raise SketchCriticError("Critic request cannot be empty.")

        last_error: SketchCriticError | None = None
        generation = self._profile.generation_for("default")
        try:
            for attempt_index in range(self._max_repair_attempts + 1):
                prompt = _critic_prompt(
                    cleaned,
                    repair_error=last_error if attempt_index else None,
                )
                raw = self._runtime.chat(
                    self._profile.name,
                    messages=[
                        BrainMessage(
                            role=BrainMessageRole.USER,
                            content=prompt,
                            images=(preview,),
                        )
                    ],
                    think=False,
                    num_predict=max(generation.num_predict or 0, 256),
                    num_ctx=self._profile.context_tokens,
                    keep_alive="0",
                    response_format=CRITIC_RESPONSE_FORMAT,
                    timeout_seconds=self._timeout,
                )
                try:
                    return parse_critic_json(raw, model_name=self._profile.name)
                except SketchCriticError as exc:
                    last_error = exc
                    if attempt_index >= self._max_repair_attempts:
                        raise SketchCriticError(
                            "Visual critic failed strict validation "
                            f"after {attempt_index + 1} attempt(s): {exc}"
                        ) from exc
        finally:
            self._runtime.stop_model(self._profile.name)

        raise AssertionError("Unreachable visual critic state.")


def _critic_prompt(request: str, repair_error: SketchCriticError | None) -> str:
    prompt = f"""You are Qronos's visual safety gate for a simple black-line sketch.

User request:
{request}

Judge ONLY the attached preview. Do not plan a new drawing and do not invent
anything that is not visibly present.

PASS only when ALL are true:
1. A typical person could recognize the requested subject(s) from the line art.
2. Explicit requested relations or poses are visibly satisfied.
3. Major parts are placed coherently; there are no obviously detached or
   nonsensical large components.

Do not penalize crude artistic style, simple geometry, lack of color, or minor
proportion issues. Be strict about semantic recognizability and requested
relationships. Return only the required JSON.
"""
    if repair_error is not None:
        prompt += (
            "\nYour previous JSON response was invalid. Return the judgment again "
            f"from scratch. Validation error: {repair_error}"
        )
    return prompt


def _require_exact_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    where: str,
) -> None:
    actual = set(value)
    missing = required - actual
    unknown = actual - required
    if missing:
        raise SketchCriticError(f"{where} is missing keys: {sorted(missing)!r}.")
    if unknown:
        raise SketchCriticError(f"{where} contains unknown keys: {sorted(unknown)!r}.")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SketchCriticError(f"Duplicate JSON key is not allowed: {key!r}.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise SketchCriticError(f"Non-finite JSON constant is not allowed: {value}.")
