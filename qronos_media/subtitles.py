"""Subtitle cues from sidecar files and embedded text streams (handoff s8, s10).

Parsing is pure Python and tolerant: real subtitle files have stray BOMs,
mixed line endings, missing indices, dot or comma milliseconds and legacy
Windows-1256 Persian. A cue that cannot be timed is dropped, never guessed.
Image-based subtitle streams (PGS, VobSub, DVB) need OCR and are reported as
unsupported rather than silently skipped.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

from qronos_media.text import clean_cue_text

MAX_SUBTITLE_BYTES = 16 * 1024 * 1024
MAX_CUES = 200_000
MAX_CUE_CHARS = 2_000
MAX_SIBLINGS = 10_000

TEXT_SUBTITLE_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "microdvd", "subviewer"})
IMAGE_SUBTITLE_CODECS = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvb_teletext"})

_LANGUAGE_ALIASES = {
    "fa": "fa", "fas": "fa", "per": "fa", "farsi": "fa", "persian": "fa",
    "en": "en", "eng": "en", "english": "en",
    "ar": "ar", "ara": "ar", "arabic": "ar",
    "de": "de", "deu": "de", "ger": "de", "fr": "fr", "fra": "fr", "fre": "fr",
    "es": "es", "spa": "es", "tr": "tr", "tur": "tr", "ru": "ru", "rus": "ru",
}
_FLAG_TAGS = {"forced", "sdh", "cc", "hi"}

_SRT_TIME = re.compile(
    r"(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)
_VTT_TIME = re.compile(
    r"(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})\.(\d{1,3})\s+-->\s+(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})\.(\d{1,3})"
)


class SubtitleError(ValueError):
    """A subtitle source could not be read."""


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class SidecarSubtitle:
    path: Path
    language: str | None
    flags: tuple[str, ...]


def normalise_language(value: str | None) -> str | None:
    if not value:
        return None
    key = value.strip().lower()
    if not key or key in {"und", "unknown", "zxx", "mis"}:
        return None
    return _LANGUAGE_ALIASES.get(key, key if re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})?", key) else None)


def decode_subtitle_bytes(data: bytes) -> tuple[str, str]:
    """Decode subtitle bytes; returns (text, encoding used)."""
    if len(data) > MAX_SUBTITLE_BYTES:
        raise SubtitleError("subtitle file is too large")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    # Not UTF-8. Older Persian and Arabic subtitles are overwhelmingly
    # Windows-1256; Western ones Windows-1252. Pick by which decoding yields
    # Arabic-script letters, and say it was a guess.
    arabic = data.decode("cp1256", "replace")
    letters = [ch for ch in arabic if ch.isalpha()]
    share = sum(1 for ch in letters if "؀" <= ch <= "ۿ") / max(1, len(letters))
    if share >= 0.3:
        return arabic, "cp1256-guessed"
    return data.decode("cp1252", "replace"), "cp1252-guessed"


def _ms(h: str | None, m: str, s: str, frac: str) -> int:
    millis = int(frac.ljust(3, "0")[:3])
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + millis


def _cue(start: int, end: int, lines: list[str]) -> Cue | None:
    text = clean_cue_text("\n".join(lines))[:MAX_CUE_CHARS]
    if end <= start or not text:
        return None
    return Cue(start, end, text)


def _blocks(text: str) -> list[list[str]]:
    blocks, current = [], []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip():
            current.append(line.rstrip())
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for block in _blocks(text.lstrip("﻿")):
        for position, line in enumerate(block[:3]):
            match = _SRT_TIME.search(line)
            if match:
                g = match.groups()
                cue = _cue(_ms(*g[0:4]), _ms(*g[4:8]), block[position + 1:])
                if cue:
                    cues.append(cue)
                break
        if len(cues) >= MAX_CUES:
            break
    return sorted(cues, key=lambda cue: (cue.start_ms, cue.end_ms))


def parse_vtt(text: str) -> list[Cue]:
    body = text.lstrip("﻿")
    if not body.startswith("WEBVTT"):
        raise SubtitleError("not a WebVTT file")
    cues: list[Cue] = []
    for block in _blocks(body)[1:]:
        if block[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        for position, line in enumerate(block[:2]):
            match = _VTT_TIME.search(line)
            if match:
                g = match.groups()
                cue = _cue(_ms(*g[0:4]), _ms(*g[4:8]), block[position + 1:])
                if cue:
                    cues.append(cue)
                break
        if len(cues) >= MAX_CUES:
            break
    return sorted(cues, key=lambda cue: (cue.start_ms, cue.end_ms))


def parse_subtitle_text(text: str, suffix: str) -> list[Cue]:
    kind = suffix.lower()
    if kind == ".vtt":
        return parse_vtt(text)
    if kind == ".srt":
        return parse_srt(text)
    raise SubtitleError(f"unsupported subtitle format {suffix}")


def read_subtitle_file(path: Path) -> tuple[list[Cue], str]:
    size = path.stat().st_size
    if size > MAX_SUBTITLE_BYTES:
        raise SubtitleError("subtitle file is too large")
    with open(path, "rb") as handle:
        data = handle.read(MAX_SUBTITLE_BYTES + 1)
    text, encoding = decode_subtitle_bytes(data)
    return parse_subtitle_text(text, path.suffix), encoding


def find_sidecars(video: Path) -> list[SidecarSubtitle]:
    """Subtitle files beside ``video`` named ``<stem>[.<tags>].<ext>``.

    Tags such as ``fa``, ``eng`` or ``forced`` become language and flags.
    Links are not followed and the directory listing is bounded.
    """
    stem = video.stem.casefold()
    found: list[SidecarSubtitle] = []
    try:
        with os.scandir(video.parent) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_SIBLINGS:
                    break
                name = entry.name
                suffix = os.path.splitext(name)[1].lower()
                if suffix not in {".srt", ".vtt", ".ass", ".ssa"}:
                    continue
                base = name[: -len(suffix)]
                if base.casefold() != stem and not base.casefold().startswith(stem + "."):
                    continue
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                tags = [tag.lower() for tag in base[len(stem):].split(".") if tag]
                # Only known language names count: a free-form tag such as
                # "cut" or "hi" (hearing impaired) must not become a language.
                language = next((_LANGUAGE_ALIASES[tag] for tag in tags if tag in _LANGUAGE_ALIASES), None)
                flags = tuple(sorted({tag for tag in tags if tag in _FLAG_TAGS}))
                found.append(SidecarSubtitle(Path(entry.path), language, flags))
    except OSError:
        return []
    return sorted(found, key=lambda item: str(item.path).casefold())
