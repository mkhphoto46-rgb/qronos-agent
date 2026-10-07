from __future__ import annotations

import re

from core.paint_draw_integration import MAX_DRAW_PROMPT_CHARS, PaintDrawAction


class PaintDrawParseError(ValueError):
    """Text was not one explicit natural-language request to draw in Paint."""


_FORBIDDEN_MARKERS = ("&&", "||", ";", ">", "<", "`", "$(", "\\", ".exe")
_PAINT_RE = re.compile(
    r"(?<!\w)(?:microsoft\s+paint|mspaint|paint|پینت|پاینت)(?!\w)",
    re.IGNORECASE,
)
_DRAW_RE = re.compile(
    r"(?<!\w)(?:draw|sketch)(?!\w)|(?:بکش(?:ید)?|رسم\s+کن|نقاشی\s+کن)",
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    value = str(text or "").replace("\u200c", " ")
    value = value.replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"\s+", " ", value).strip()


def parse_paint_draw_action(text: str) -> PaintDrawAction:
    cleaned = _normalise(text)
    lowered = cleaned.lower()
    if not cleaned:
        raise PaintDrawParseError("Paint drawing command is empty.")
    if len(cleaned) > MAX_DRAW_PROMPT_CHARS + 160:
        raise PaintDrawParseError("Paint drawing command is too long.")
    if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
        raise PaintDrawParseError(
            "Paths, executable names, shell operators, and command syntax are not accepted."
        )
    if not _PAINT_RE.search(cleaned):
        raise PaintDrawParseError("The request does not explicitly target Paint.")
    if not _DRAW_RE.search(cleaned):
        raise PaintDrawParseError("No explicit draw/sketch verb was found.")

    prompt = cleaned
    # Remove explicit Paint-location phrases first, preserving the subject/pose.
    prompt = re.sub(
        r"(?<!\w)(?:توی|تو|داخل|در|با)\s+"
        r"(?:microsoft\s+paint|mspaint|paint|پینت|پاینت)(?!\w)",
        " ",
        prompt,
        flags=re.IGNORECASE,
    )
    prompt = re.sub(
        r"(?<!\w)(?:in|inside|using|with)\s+(?:microsoft\s+)?paint(?!\w)",
        " ",
        prompt,
        flags=re.IGNORECASE,
    )
    # Support the common compound wording "Paint رو باز کن و ... بکش" without
    # treating arbitrary multi-action commands as shell-like execution.
    prompt = re.sub(
        r"(?<!\w)(?:microsoft\s+paint|mspaint|paint|پینت|پاینت)"
        r"\s+(?:رو|را)?\s*باز\s+کن\s*(?:و|بعد)?",
        " ",
        prompt,
        flags=re.IGNORECASE,
    )
    prompt = re.sub(
        r"(?<!\w)(?:open|launch)\s+(?:microsoft\s+)?paint\s*(?:and|then)?",
        " ",
        prompt,
        flags=re.IGNORECASE,
    )
    prompt = _PAINT_RE.sub(" ", prompt)
    prompt = _DRAW_RE.sub(" ", prompt)
    prompt = re.sub(
        r"(?<!\w)(?:لطفا|لطفاً|please|برام|برای\s+من)(?!\w)",
        " ",
        prompt,
        flags=re.IGNORECASE,
    )
    prompt = re.sub(r"\s+", " ", prompt).strip(" ،,:.!؟?-")
    if not prompt:
        raise PaintDrawParseError("Paint drawing subject/description is missing.")

    try:
        return PaintDrawAction(prompt=prompt)
    except ValueError as exc:
        raise PaintDrawParseError(str(exc)) from exc


PAINT_UNDO = "undo"
PAINT_UNDO_AMBIGUOUS = "ambiguous"

_UNDO_RE = re.compile(
    r"(?<!\w)(?:undo|revert)(?!\w)|(?:برگردون|برگردان|بازگردان|بازگردون|آندو)",
    re.IGNORECASE,
)


def classify_paint_undo_request(text: str) -> str:
    """
    Recognize an explicit request to undo Qronos's own Paint drawing.

    Returns ``PAINT_UNDO`` when the text names Paint and an undo verb,
    ``PAINT_UNDO_AMBIGUOUS`` when it also asks to draw (both must never run
    from one sentence), and ``""`` otherwise. Which drawing is reversed is
    decided by the undo journal, never by the sentence.
    """
    cleaned = _normalise(text)
    if not cleaned or len(cleaned) > MAX_DRAW_PROMPT_CHARS + 160:
        return ""
    if not _PAINT_RE.search(cleaned) or not _UNDO_RE.search(cleaned):
        return ""
    if _DRAW_RE.search(cleaned):
        return PAINT_UNDO_AMBIGUOUS
    return PAINT_UNDO
