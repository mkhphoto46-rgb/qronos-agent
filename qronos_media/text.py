"""One normal form for dialogue, applied identically to the index and the query.

Built on :func:`core.persian_text.normalise`, so Arabic letter variants,
Persian/Arabic-Indic digits, diacritics, tatweel, invisible bidi marks and
ZWNJ spelling differences all compare equal. On top of that: case folding,
punctuation to spaces, and subtitle markup removed.
"""
from __future__ import annotations

import re
import unicodedata

from core.persian_text import normalise as persian_normalise

_TAGS = re.compile(r"<[^>]{0,200}>|\{\\[^}]{0,200}\}")
_ASS_BREAKS = re.compile(r"\\[Nnh]")


def clean_cue_text(text: str) -> str:
    """Display text: markup and ASS line breaks removed, lines joined."""
    without = _TAGS.sub("", _ASS_BREAKS.sub(" ", text))
    return " ".join(line.strip() for line in without.splitlines() if line.strip())


def normalise_for_search(text: str) -> str:
    """Lower-case, punctuation-free, Persian-normalised words joined by spaces."""
    base = persian_normalise(clean_cue_text(text), for_search=True).casefold()
    # Compatibility decomposition folds presentation forms and ligatures, and
    # dropping combining marks makes "café"/"cafe" and "آ"/"ا" meet.
    base = unicodedata.normalize("NFKD", base)
    chars = []
    for ch in base:
        category = unicodedata.category(ch)
        if category[0] == "M":
            continue
        if category[0] in ("L", "N"):
            chars.append(ch)
        elif ch == "'" or ch == "’":
            continue  # "I'll" and "Ill" should meet; apostrophes are inconsistent in subtitles
        else:
            chars.append(" ")
    return unicodedata.normalize("NFC", " ".join("".join(chars).split()))


def tokens(text: str) -> list[str]:
    return normalise_for_search(text).split()
