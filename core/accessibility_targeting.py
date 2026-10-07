from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Sequence

from core.brain_runtime import (
    BrainMessage,
    BrainMessageRole,
    BrainRuntime,
)
from core.model_registry import get_model
from core.windows_accessibility import (
    ActionableUiElement,
    actionable_elements_in_window,
)


class AccessibilityTargetingError(RuntimeError):
    pass


class AccessibilityTargetNotFound(AccessibilityTargetingError):
    pass


class AccessibilityTargetAmbiguous(AccessibilityTargetingError):
    pass


class AccessibilityTargetInvalid(AccessibilityTargetingError):
    pass


@dataclass(frozen=True)
class AccessibilityTarget:
    candidate_id: str
    confidence: float
    element: ActionableUiElement
    selection_source: str = "model"

    @property
    def screen_x(self) -> int:
        return self.element.center[0]

    @property
    def screen_y(self) -> int:
        return self.element.center[1]


EnumerateFn = Callable[
    [int],
    Sequence[ActionableUiElement],
]


def selection_schema(
    candidate_ids: Sequence[str],
) -> dict:
    ids = tuple(str(value) for value in candidate_ids)

    if not ids:
        raise ValueError(
            "selection_schema requires at least one candidate."
        )

    return {
        "type": "object",
        "required": [
            "status",
            "candidate_id",
            "confidence",
        ],
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    "selected",
                    "not_found",
                    "ambiguous",
                ],
            },
            "candidate_id": {
                "type": "string",
                "enum": [
                    "",
                    *ids,
                ],
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
        },
        "additionalProperties": False,
    }


def _semantic_text(value: object) -> str:
    """
    Conservative normalization for deterministic metadata matching.

    This does NOT attempt fuzzy semantic reasoning or translation.
    If platform metadata contains an explicit human-readable control name
    that appears in the request, we do not need a model to rediscover it.
    """

    value = str(value or "").casefold()

    value = re.sub(
        r"[_/\\:\-]+",
        " ",
        value,
    )

    value = re.sub(
        r"[^\w\s]+",
        " ",
        value,
        flags=re.UNICODE,
    )

    return " ".join(
        value.split()
    )


def _deterministic_exact_match(
    description: str,
    candidates: Sequence[
        tuple[str, ActionableUiElement]
    ],
) -> tuple[str, ActionableUiElement] | None:
    """
    Select a unique explicit platform name without involving a model.

    Examples:
      request contains "address bar" and UIA exposes NAME="Address Bar"
      request contains "search" and exactly one candidate NAME="Search"

    It intentionally refuses:
      - unnamed controls,
      - one-character/two-character labels,
      - duplicate matching names,
      - fuzzy guesses.

    Anything unclear falls through to the model selector.
    """

    request = _semantic_text(
        description
    )

    if not request:
        return None

    matches = []

    for candidate_id, item in candidates:

        name = _semantic_text(
            item.name
        )

        if len(name) < 3:
            continue

        # Explicit human-facing control name appears in the user's request.
        if name in request:
            matches.append(
                (
                    candidate_id,
                    item,
                )
            )

    if len(matches) == 1:
        return matches[0]

    return None


def _clip(value: str, limit: int = 160) -> str:
    clean = " ".join(
        str(value or "").split()
    )

    if len(clean) <= limit:
        return clean

    return clean[: limit - 1] + "?"


class AccessibilityTargetLocator:
    """
    Semantic selection over trusted UI Automation candidates.

    The model chooses an opaque candidate ID.

    It never creates coordinates, rectangles, HWNDs or automation metadata.
    """

    def __init__(
        self,
        runtime: BrainRuntime | None,
        *,
        enumerate_fn: EnumerateFn | None = None,
        min_confidence: float = 0.80,
        max_candidates: int = 64,
    ) -> None:
        self.runtime = runtime
        self.enumerate_fn = (
            enumerate_fn
            if enumerate_fn is not None
            else actionable_elements_in_window
        )

        self.min_confidence = float(
            min_confidence
        )

        self.max_candidates = int(
            max_candidates
        )

        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError(
                "min_confidence must be between 0 and 1."
            )

        if not 1 <= self.max_candidates <= 128:
            raise ValueError(
                "max_candidates must be between 1 and 128."
            )

    def candidates(
        self,
        hwnd: int,
        *,
        role: str | None = None,
    ) -> tuple[tuple[str, ActionableUiElement], ...]:

        requested_role = (
            str(role).strip().lower()
            if role is not None
            else ""
        )

        raw = tuple(
            self.enumerate_fn(int(hwnd))
        )

        filtered = []

        for item in raw:

            item_role = item.role

            if item_role == "other":
                continue

            if (
                requested_role
                and item_role != requested_role
            ):
                continue

            filtered.append(item)

        filtered = filtered[
            : self.max_candidates
        ]

        return tuple(
            (
                f"uia-{index:03d}",
                item,
            )
            for index, item in enumerate(
                filtered,
                start=1,
            )
        )

    def locate(
        self,
        description: str,
        hwnd: int,
        *,
        role: str | None = None,
    ) -> AccessibilityTarget:

        target = str(
            description or ""
        ).strip()

        if not target:
            raise ValueError(
                "Accessibility target description is required."
            )

        candidates = self.candidates(
            hwnd,
            role=role,
        )

        if not candidates:
            raise AccessibilityTargetNotFound(
                "No compatible UI Automation candidate exists."
            )

        candidate_map = dict(
            candidates
        )

        deterministic = _deterministic_exact_match(
            target,
            candidates,
        )

        if deterministic is not None:
            candidate_id, element = deterministic

            return AccessibilityTarget(
                candidate_id=candidate_id,
                confidence=1.0,
                element=element,
                selection_source="platform_metadata",
            )

        rows = []

        for candidate_id, item in candidates:

            rows.append(
                {
                    "candidate_id":
                        candidate_id,
                    "role":
                        item.role,
                    "name":
                        _clip(item.name),
                    "automation_id":
                        _clip(
                            item.automation_id
                        ),
                    "class_name":
                        _clip(
                            item.class_name
                        ),
                    "control_type":
                        item.control_type,
                    "value":
                        _clip(item.value),
                    "focusable":
                        item.focusable,
                    "value_pattern":
                        item.value_pattern,
                    "value_read_only":
                        item.value_read_only,
                    "text_pattern":
                        item.text_pattern,
                }
            )

        required_role = (
            str(role).strip().lower()
            if role is not None
            else "any compatible role"
        )

        prompt = (
            "You select exactly one real Windows UI control from a "
            "trusted candidate list.\n\n"
            f"Requested control: {target}\n"
            f"Required role: {required_role}\n\n"
            "Rules:\n"
            "- Select only a supplied candidate_id.\n"
            "- Never invent a control or coordinate.\n"
            "- Use semantic name, role, automation id, class and current value.\n"
            "- If none matches, return not_found.\n"
            "- If two or more remain genuinely plausible, return ambiguous.\n"
            "- Search boxes and address/location bars are different controls.\n\n"
            "Candidates:\n"
            + json.dumps(
                rows,
                ensure_ascii=False,
                indent=2,
            )
        )

        ids = tuple(
            candidate_map.keys()
        )

        if self.runtime is None:
            raise AccessibilityTargetAmbiguous(
                "Platform metadata did not resolve the target uniquely "
                "and no model fallback is available."
            )

        profile = get_model(
            "fast"
        )

        generation = profile.generation_for(
            "default"
        )

        try:
            raw = self.runtime.chat(
                profile.name,
                messages=(
                    BrainMessage(
                        role=BrainMessageRole.SYSTEM,
                        content=(
                            "Return only the requested structured "
                            "candidate-selection result."
                        ),
                    ),
                    BrainMessage(
                        role=BrainMessageRole.USER,
                        content=prompt,
                    ),
                ),
                think=generation.think,
                num_predict=96,
                num_ctx=profile.context_tokens,
                keep_alive=generation.keep_alive,
                response_format=selection_schema(
                    ids
                ),
                timeout_seconds=60.0,
            )
        finally:
            self.runtime.stop_model(
                profile.name
            )

        return self._parse(
            raw,
            candidate_map,
        )

    def _parse(
        self,
        raw: str,
        candidate_map: dict[
            str,
            ActionableUiElement,
        ],
    ) -> AccessibilityTarget:

        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AccessibilityTargetInvalid(
                "Candidate selector returned invalid JSON."
            ) from exc

        if not isinstance(value, dict):
            raise AccessibilityTargetInvalid(
                "Candidate selector response must be an object."
            )

        if set(value) != {
            "status",
            "candidate_id",
            "confidence",
        }:
            raise AccessibilityTargetInvalid(
                "Candidate selector response fields are invalid."
            )

        status = value["status"]
        candidate_id = value["candidate_id"]

        try:
            confidence = float(
                value["confidence"]
            )
        except (TypeError, ValueError) as exc:
            raise AccessibilityTargetInvalid(
                "Candidate confidence is invalid."
            ) from exc

        if not 0.0 <= confidence <= 1.0:
            raise AccessibilityTargetInvalid(
                "Candidate confidence is outside 0..1."
            )

        if status == "not_found":
            if candidate_id:
                raise AccessibilityTargetInvalid(
                    "not_found must not select a candidate."
                )

            raise AccessibilityTargetNotFound(
                "No accessibility candidate matched the request."
            )

        if status == "ambiguous":
            if candidate_id:
                raise AccessibilityTargetInvalid(
                    "ambiguous must not select a candidate."
                )

            raise AccessibilityTargetAmbiguous(
                "Accessibility candidates remained ambiguous."
            )

        if status != "selected":
            raise AccessibilityTargetInvalid(
                "Unknown candidate-selection status."
            )

        if candidate_id not in candidate_map:
            raise AccessibilityTargetInvalid(
                "Model selected a candidate ID that was not supplied."
            )

        if confidence < self.min_confidence:
            raise AccessibilityTargetAmbiguous(
                "Candidate selection confidence was below threshold."
            )

        return AccessibilityTarget(
            candidate_id=candidate_id,
            confidence=confidence,
            element=candidate_map[
                candidate_id
            ],
            selection_source="model",
        )
