from __future__ import annotations

import re


_STANDALONE_FOUR_RE = re.compile(
    r"^\s*چهار\s*\.?\s*$"
)


def normalise_spoken_text(text: str) -> str:
    """
    Convert display text into TTS-only text without changing conversation text.

    This first production rule is deliberately narrow and evidence-based:
    the current Persian Chatterbox model mispronounces an isolated "چهار"
    (with or without a final period), while the same word is pronounced
    correctly when it has natural sentence context.

    Do not globally replace occurrences of "چهار". Sentences such as
    "چهار گزینه داریم." must remain unchanged.
    """
    spoken = re.sub(
        r"\s+",
        " ",
        (text or "").strip(),
    )

    if not spoken:
        return ""

    if _STANDALONE_FOUR_RE.fullmatch(spoken):
        return "می‌شود چهار."

    return spoken
