"""Deterministic Persian/English media requests for the brain and voice turns (no model, no guess).

    «کلید» را در ویدیوهام پیدا کن        در فیلم‌ها دنبال «قطار ساحلی» بگرد (معنایی)
    search my videos for "the old key"    find "the train" in my movies
    وضعیت فهرست ویدیوها                     media index status

A request names the media library (video/film/media words) AND asks to search or for status. The search phrase is
taken verbatim from quotes (« », " ", “ ”) or, in English, after "for"; without a phrase nothing is guessed. The
result is one of the typed :mod:`qronos_media.product` operations; the caller runs exactly that.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from qronos_media.product import MAX_QUERY_CHARS


@dataclass(frozen=True)
class MediaSearchIntent:
    query: str
    mode: str  # "exact", "fuzzy" or "semantic"


@dataclass(frozen=True)
class MediaStatusIntent:
    pass


MediaIntent = MediaSearchIntent | MediaStatusIntent

_MEDIA = re.compile(r"(?:\bvideos?\b|\bmovies?\b|\bfilms?\b|\bmedia\b|ویدیو|ویدئو|فیلم|رسانه)", re.IGNORECASE)
_SEARCH = re.compile(r"(?:\bsearch\b|\bfind\b|\blook for\b|بگرد|جستجو|جست‌وجو|جست و جو|پیدا کن|پیدا کنید)",
                     re.IGNORECASE)
_STATUS = re.compile(r"(?:\bstatus\b|\bprogress\b|وضعیت|پیشرفت)", re.IGNORECASE)
_INDEX = re.compile(r"(?:\bindex\b|\bindexing\b|\blibrary\b|فهرست|نمایه|کتابخانه)", re.IGNORECASE)
_SEMANTIC = re.compile(r"(?:\bsemantic\b|\bsimilar\b|\bmeaning\b|معنایی|مشابه|به معنی)", re.IGNORECASE)
_FUZZY = re.compile(r"(?:\bfuzzy\b|\bapproximate\b|تقریبی)", re.IGNORECASE)
_MINE = re.compile(r"\bmy\s+(?:videos?|movies?|films?|media)\b", re.IGNORECASE)
_QUOTED = re.compile(r"[«\"“]([^«»\"“”]{1,1200})[»\"”]")
_ENGLISH_FOR = re.compile(r"\b(?:search|look)\b.*?\bfor\s+(.{1,1200}?)\s*[.?!]?\s*$", re.IGNORECASE)


def parse_media_intent(text: str) -> MediaIntent | None:
    if not isinstance(text, str) or not text.strip() or len(text) > 4 * MAX_QUERY_CHARS:
        return None
    if not _MEDIA.search(text):
        return None
    if _SEARCH.search(text):
        quoted = _QUOTED.search(text)
        phrase = quoted.group(1) if quoted else None
        if phrase is None and _MINE.search(text):  # unquoted only for "... my videos/movies ..." in English
            match = _ENGLISH_FOR.search(text)
            phrase = match.group(1) if match else None
            if phrase is not None:
                phrase = re.sub(r"\s+in\s+my\s+(?:videos?|movies?|films?|media)\b.*$", "", phrase, flags=re.IGNORECASE)
        if phrase is None or not phrase.strip() or len(phrase) > MAX_QUERY_CHARS:
            return None
        mode = "semantic" if _SEMANTIC.search(text) else "fuzzy" if _FUZZY.search(text) else "exact"
        return MediaSearchIntent(phrase.strip(), mode)
    if _STATUS.search(text) and _INDEX.search(text):
        return MediaStatusIntent()
    return None


def clock(ms: object) -> str:
    try:
        total = max(0, int(ms)) // 1000
    except (TypeError, ValueError):
        return "?"
    return f"{total // 3600:d}:{total // 60 % 60:02d}:{total % 60:02d}"


def spoken_answer(intent: MediaIntent, result: dict | None) -> str:
    """A short Persian answer from the typed result (never more than what the result says)."""
    if result is None:
        return "جست‌وجوی رسانه به‌موقع جواب نداد؛ وضعیت را در بخش رسانه ببینید."
    if not result.get("ok"):
        code = result.get("code", "refused")
        if code == "paused":
            return "Qronos متوقف است؛ کارهای رسانه بعد از ادامه انجام می‌شود."
        return f"درخواست رسانه انجام نشد ({code})."
    if isinstance(intent, MediaStatusIntent):
        store = (result.get("status") or {}).get("store") or {}
        assets = store.get("assets")
        running = (result.get("status") or {}).get("running")
        state = "در حال فهرست‌سازی است" if running else "الان کاری در جریان نیست"
        return f"{assets if assets is not None else 'هنوز هیچ'} ویدیو در فهرست است؛ {state}."
    if intent.mode == "semantic":
        block = result.get("semantic") or {}
        items = block.get("results") or []
        exact = (result.get("exact") or {}).get("results") or []
        if not items:
            reason = block.get("reason") or block.get("status")
            return (f"جست‌وجوی معنایی در دسترس نیست ({reason}); {len(exact)} نتیجهٔ دقیق پیدا شد."
                    if block.get("status") == "unavailable" else f"نتیجهٔ معنایی پیدا نشد؛ {len(exact)} نتیجهٔ دقیق.")
    else:
        items = (result.get("results") or {}).get("results") or []
    if not items:
        return "در ویدیوهای فهرست‌شده چیزی پیدا نشد."
    first = items[0]
    title = first.get("title") or "ویدیو"
    return f"{len(items)} نتیجه پیدا شد؛ اولی در «{title}» دقیقهٔ {clock(first.get('startMs'))}. جزئیات در بخش رسانه است."
