"""Reviewed deterministic Paint drawings: a house and a few simple shapes (D4).

Most drawing requests need a reference image, and Qronos has no local image
generator wired yet, so those requests stop safely before Paint is touched.
A small set of plain requests does not need one: "draw a house", "draw a
circle", "یک مثلث بکش" and so on. Each of those has a fixed, reviewed vector
plan here. The plan still goes through every existing rail: a rendered
preview approved by the visual critic, vision-grounded canvas and tool state,
bounded strokes inside the exact canvas, and deterministic plus semantic
post-draw verification. Nothing here clicks or calls a model.

Matching is exact and conservative. After removing articles, "simple"/"ساده"
and politeness words, the remaining subject must be exactly one known name.
"a red house", "a house and a tree" or "a cat" are not templates; they take
the reference-image path, which refuses while no reference is available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from core.shape_planner import ShapePlan, ShapePlanner


@dataclass(frozen=True)
class PaintTemplate:
    name: str
    #: The subject the visual critic is asked to recognise.
    subject: str
    build: Callable[[], ShapePlan]


def house() -> ShapePlan:
    """A square body, a pitched roof and a door: three closed strokes."""
    body = ((0.22, 0.48), (0.78, 0.48), (0.78, 0.88), (0.22, 0.88), (0.22, 0.48))
    roof = ((0.16, 0.50), (0.50, 0.14), (0.84, 0.50), (0.16, 0.50))
    door = ((0.44, 0.88), (0.44, 0.66), (0.56, 0.66), (0.56, 0.88))
    return ShapePlan.from_strokes((body, roof, door), name="house")


TEMPLATES: dict[str, PaintTemplate] = {
    template.name: template
    for template in (
        PaintTemplate("house", "a simple house", house),
        PaintTemplate("square", "a square", lambda: _named(ShapePlanner.rectangle(margin=0.2), "square")),
        PaintTemplate(
            "rectangle",
            "a rectangle",
            lambda: ShapePlanner.polyline(
                ((0.12, 0.3), (0.88, 0.3), (0.88, 0.7), (0.12, 0.7)), close=True, name="rectangle"
            ),
        ),
        PaintTemplate("circle", "a circle", ShapePlanner.circle),
        PaintTemplate("triangle", "a triangle", ShapePlanner.triangle),
        PaintTemplate("star", "a five-pointed star", ShapePlanner.star),
        PaintTemplate("line", "a straight horizontal line", ShapePlanner.line),
        PaintTemplate("arrow", "an arrow pointing right", ShapePlanner.arrow),
    )
}

#: Every accepted phrase, normalised, mapped to one template.
_NAMES: dict[str, str] = {
    "house": "house", "home": "house", "خانه": "house", "خونه": "house",
    "square": "square", "مربع": "square",
    "rectangle": "rectangle", "مستطیل": "rectangle",
    "circle": "circle", "دایره": "circle",
    "triangle": "triangle", "مثلث": "triangle",
    "star": "star", "ستاره": "star",
    "line": "line", "straight line": "line", "خط": "line", "خط صاف": "line",
    "arrow": "arrow", "فلش": "arrow", "پیکان": "arrow",
}

_FILLER = re.compile(
    r"(?<!\w)(?:a|an|the|one|simple|basic|plain|small|"
    r"یک|یه|ساده|یک\s+عدد|یه\s+دونه|رو|را)(?!\w)",
    re.IGNORECASE,
)


def _named(plan: ShapePlan, name: str) -> ShapePlan:
    return ShapePlan(strokes=plan.strokes, name=name)


def _normalise(text: str) -> str:
    value = str(text or "").replace("‌", " ").replace("ي", "ی").replace("ك", "ک")
    value = re.sub(r"[\s،,.!؟?:;\-]+", " ", value).strip().casefold()
    value = _FILLER.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


def match_paint_template(prompt: str) -> PaintTemplate | None:
    """The one reviewed template a request names exactly, or None."""
    name = _NAMES.get(_normalise(prompt))
    return TEMPLATES[name] if name is not None else None


class TemplateSketchRejected(RuntimeError):
    """The visual critic did not recognise the template's rendered preview."""


def approve_template_sketch(request: str, template: PaintTemplate, critic, *, renderer=None):
    """Render the template, require the visual critic's approval, freeze the plan.

    Returns the same ``VisualApprovedSketch`` capability the reference path
    produces, so the adapter and the verifier treat both identically. The
    critic is the caller's (the Vision model in production, a fake in tests).
    """
    from core.sketch_approval import VisualApprovedSketch
    from core.sketch_preview import SketchPreviewRenderer

    plan = template.build()
    preview = (renderer or SketchPreviewRenderer()).render(plan)
    critique = critic.critique(template.subject, preview.image)
    if not critique.passes():
        raise TemplateSketchRejected(
            f"The {template.name} preview did not pass the visual check: "
            + (critique.summary or "not approved")
        )
    return VisualApprovedSketch(
        # The critic checks the result against the template's own subject.
        request=template.subject,
        plan=plan,
        plan_digest=preview.plan_digest,
        preview_digest=preview.image_digest,
        preview_image=preview.image,
        critic_model=critique.model_name,
        critic_confidence=critique.confidence,
        candidate_attempts=1,
        critic_summary=critique.summary,
    )
