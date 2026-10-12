from __future__ import annotations

from dataclasses import dataclass
import re

from core.persian_text import normalise


_WORD_RE = re.compile(r"[A-Za-z0-9\u0600-\u06FF]+")


@dataclass(frozen=True)
class RoutingInput:
    """Immutable, pre-normalised text shared by routing analyzers."""

    raw_text: str
    normalized_text: str
    lower_text: str
    words: tuple[str, ...]
    char_count: int
    word_count: int

    @classmethod
    def from_text(cls, text: str) -> "RoutingInput":
        raw = text or ""
        normalized = normalise(raw)
        lowered = normalized.lower()
        words = tuple(_WORD_RE.findall(lowered))

        return cls(
            raw_text=raw,
            normalized_text=normalized,
            lower_text=lowered,
            words=words,
            char_count=len(normalized),
            word_count=len(words),
        )