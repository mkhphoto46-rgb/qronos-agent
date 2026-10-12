from __future__ import annotations

import re
from typing import Callable

from core.computer_executor import (
    ComputerAction,
    ComputerOperation,
    KNOWN_APPS,
)


class ComputerActionParseError(ValueError):
    """A COMPUTER-routed request was not a safe typed Windows action."""


AppResolver = Callable[[str], str]

_ENGLISH_OPEN = re.compile(r"(?<!\w)(open|launch|start|run)(?!\w)", re.IGNORECASE)
_ENGLISH_CLOSE = re.compile(r"(?<!\w)(close|quit|exit)(?!\w)", re.IGNORECASE)
_ENGLISH_MINIMIZE = re.compile(r"(?<!\w)(minimize|minimise)(?!\w)", re.IGNORECASE)
_ENGLISH_MAXIMIZE = re.compile(r"(?<!\w)(maximize|maximise)(?!\w)", re.IGNORECASE)
_ENGLISH_FOCUS = re.compile(r"(?<!\w)(focus|foreground)(?!\w)", re.IGNORECASE)
_ENGLISH_DRAW_HOUSE = re.compile(
    r"(?<!\w)(?:draw|sketch)\s+(?:a\s+)?(?:basic\s+|simple\s+)?house(?!\w)",
    re.IGNORECASE,
)

_PERSIAN_OPEN_PHRASES = ("باز کن", "بازش کن", "اجرا کن", "راه بنداز", "راه بینداز", "راه اندازی کن")
_PERSIAN_CLOSE_PHRASES = ("ببند", "ببندش", "بندازش", "خارج شو از")
_PERSIAN_MINIMIZE_PHRASES = ("مینیمایز کن", "مینیمایزش کن", "کوچیکش کن", "کوچک کن")
_PERSIAN_MAXIMIZE_PHRASES = ("ماکسیمایز کن", "ماکسیمایزش کن", "بزرگش کن")
_PERSIAN_FOCUS_PHRASES = ("فوکوس کن", "بیار جلو", "بیارش جلو", "برو روی")
_PERSIAN_DRAW_HOUSE_PHRASES = (
    "خونه بکش", "خانه بکش", "یه خونه بکش", "یک خانه بکش",
    "خونه ساده بکش", "خانه ساده بکش", "خونه رسم کن", "خانه رسم کن",
)

_FORBIDDEN_MARKERS = ("&&", "||", ";", ">", "<", "`", "$(", "\\", "/", ":", ".exe")

_LOCATION_CUES = (
    "desktop", "downloads", "documents", "drive", "folder", "directory", "file", "note", "document",
    "دسکتاپ", "دسکتاب", "دانلود", "دانلودز", "داکیومنتس", "اسناد", "درایو", "فولدر", "پوشه", "فایل", "نوت", "یادداشت", "داکیومنت", "دایکیومنت",
)


def _normalise(text: str) -> str:
    value = str(text or "").replace("\u200c", " ")
    value = value.replace("ي", "ی").replace("ك", "ک")
    value = value.replace("ڈ", "د")
    return re.sub(r"\s+", " ", value).strip().lower()


def _contains_alias(text: str, alias: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(_normalise(alias)) + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _operation(text: str) -> ComputerOperation | None:
    if _ENGLISH_DRAW_HOUSE.search(text) or (
        ("خونه" in text or "خانه" in text)
        and ("بکش" in text or "رسم کن" in text)
    ):
        return ComputerOperation.DRAW_BASIC_HOUSE
    checks = (
        (ComputerOperation.CLOSE_APP, _ENGLISH_CLOSE, _PERSIAN_CLOSE_PHRASES),
        (ComputerOperation.MINIMIZE_WINDOW, _ENGLISH_MINIMIZE, _PERSIAN_MINIMIZE_PHRASES),
        (ComputerOperation.MAXIMIZE_WINDOW, _ENGLISH_MAXIMIZE, _PERSIAN_MAXIMIZE_PHRASES),
        (ComputerOperation.FOCUS_WINDOW, _ENGLISH_FOCUS, _PERSIAN_FOCUS_PHRASES),
        (ComputerOperation.OPEN_APP, _ENGLISH_OPEN, _PERSIAN_OPEN_PHRASES),
    )
    for operation, english, persian in checks:
        if english.search(text) or any(phrase in text for phrase in persian):
            return operation
    return None


def _has_contextual_file_or_location_target(text: str) -> bool:
    return any(_contains_alias(text, cue) for cue in _LOCATION_CUES)


def _strip_operation(text: str) -> str:
    value = text
    for regex in (_ENGLISH_OPEN, _ENGLISH_CLOSE, _ENGLISH_MINIMIZE, _ENGLISH_MAXIMIZE, _ENGLISH_FOCUS, _ENGLISH_DRAW_HOUSE):
        value = regex.sub(" ", value)
    for phrase in (*_PERSIAN_OPEN_PHRASES, *_PERSIAN_CLOSE_PHRASES, *_PERSIAN_MINIMIZE_PHRASES, *_PERSIAN_MAXIMIZE_PHRASES, *_PERSIAN_FOCUS_PHRASES):
        value = value.replace(phrase, " ")
    value = re.sub(r"(?<!\w)(?:رو|را|برام|لطفا|لطفاً|please|the|app|application)(?!\w)", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"(?<!\w)(?:برنامه|نرم افزار|نرم‌افزار)(?!\w)", " ", value)
    value = re.sub(r"(?<!\w)(?:یه|یک|ساده|خونه|خانه|بکش|رسم|کن)(?!\w)", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _static_app_id(cleaned: str) -> str | None:
    matched: list[str] = []
    for app_id, spec in KNOWN_APPS.items():
        aliases = (spec.display_name, *spec.aliases)
        if any(_contains_alias(cleaned, alias) for alias in aliases):
            matched.append(app_id)
    matched = sorted(set(matched))
    if len(matched) > 1:
        raise ComputerActionParseError("One Windows action may target only one known application.")
    return matched[0] if matched else None


def parse_computer_action(text: str, *, app_resolver: AppResolver | None = None) -> ComputerAction:
    cleaned = _normalise(text)
    if not cleaned:
        raise ComputerActionParseError("Computer command is empty.")
    if any(marker in cleaned for marker in _FORBIDDEN_MARKERS):
        raise ComputerActionParseError("Paths, executable names, shell operators, and command syntax are not accepted.")

    operation = _operation(cleaned)
    if operation is None:
        raise ComputerActionParseError("No supported Windows-control verb was found.")

    app_id = _static_app_id(cleaned)

    if operation is ComputerOperation.DRAW_BASIC_HOUSE:
        if app_id != "paint":
            raise ComputerActionParseError(
                "Drawing the basic house template is restricted to Paint."
            )
        return ComputerAction(operation=operation, app_id="paint")

    # Preserve the four reviewed built-ins as direct application targets.  In
    # particular, Persian "نوت پد" contains the ordinary file cue "نوت" and
    # must not be stolen by the file resolver.  File Explorer is the exception:
    # when another file/folder/drive target is present, the resolver owns it.
    if app_id is not None and app_id != "file_explorer":
        return ComputerAction(operation=operation, app_id=app_id)

    if _has_contextual_file_or_location_target(cleaned):
        if app_id == "file_explorer":
            remainder = _strip_operation(cleaned)
            if remainder in {_normalise(a) for a in KNOWN_APPS["file_explorer"].aliases}:
                return ComputerAction(operation=operation, app_id=app_id)
        raise ComputerActionParseError("A file/folder/drive resolver owns this request.")

    if app_id is None and app_resolver is not None:
        target = _strip_operation(cleaned)
        if not target:
            raise ComputerActionParseError("Application name is missing.")
        try:
            app_id = app_resolver(target)
        except Exception as exc:
            raise ComputerActionParseError(str(exc)) from exc

    if app_id is None:
        raise ComputerActionParseError("The requested application is not in the active application catalog.")

    return ComputerAction(operation=operation, app_id=app_id)
