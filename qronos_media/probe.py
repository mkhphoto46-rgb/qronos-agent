"""Container metadata from ffprobe (handoff s7), parsed defensively.

ffprobe's JSON comes from a native parser reading an untrusted file, so every
field is type-checked and bounded before it reaches the database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Any, Callable

from qronos_media.subtitles import IMAGE_SUBTITLE_CODECS, TEXT_SUBTITLE_CODECS, normalise_language
from qronos_media.tools import MediaTools, MediaToolError, run_bounded

PROBE_TIMEOUT_SECONDS = 60.0
PROBE_STDOUT_LIMIT = 4 * 1024 * 1024
MAX_STREAMS = 256


class ProbeFailed(MediaToolError):
    """ffprobe could not read the file as a supported container."""


@dataclass(frozen=True)
class StreamInfo:
    index: int
    kind: str
    codec: str | None
    language: str | None
    title: str | None
    default: bool
    forced: bool
    channels: int | None = None
    channel_layout: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    start_ms: int | None = None
    time_base: str | None = None

    @property
    def subtitle_kind(self) -> str | None:
        if self.kind != "subtitle":
            return None
        if self.codec in TEXT_SUBTITLE_CODECS:
            return "text"
        if self.codec in IMAGE_SUBTITLE_CODECS:
            return "image"
        return "unknown"


@dataclass(frozen=True)
class ProbeResult:
    """Container facts as ffprobe reports them.

    ``start_ms`` is the container start time. Qronos's asset timeline is
    presentation time minus that start, which is how ffmpeg normalises cut
    times, extracted subtitle times and ``-ss`` seeks. ``duration_ms`` is the
    container's claim only; some files report their *end* time there, so the
    scenes stage measures where the video actually ends.
    """

    container: str | None
    duration_ms: int | None
    title: str | None
    bit_rate: int | None
    streams: tuple[StreamInfo, ...] = field(default_factory=tuple)
    start_ms: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "startMs": self.start_ms,
            "durationMs": self.duration_ms,
            "title": self.title,
            "bitRate": self.bit_rate,
            "streams": [stream.__dict__ for stream in self.streams],
        }


def _text(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:limit] or None


def _int(value: Any, low: int = 0, high: int = 10**15) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _seconds_ms(value: Any) -> int | None:
    try:
        seconds = float(str(value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0 or seconds > 10 * 24 * 3600:
        return None
    return int(round(seconds * 1000))


def _signed_ms(value: Any) -> int | None:
    try:
        seconds = float(str(value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or abs(seconds) > 10 * 24 * 3600:
        return None
    return int(round(seconds * 1000))


def _rate(value: Any) -> float | None:
    text = _text(value, 32)
    if not text or "/" not in text:
        return None
    num, _, den = text.partition("/")
    try:
        rate = float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        return None
    return round(rate, 3) if math.isfinite(rate) and 0 < rate < 1000 else None


def parse_probe_json(raw: bytes | str) -> ProbeResult:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProbeFailed("ffprobe output is not JSON") from exc
    if not isinstance(data, dict):
        raise ProbeFailed("ffprobe output has no object")
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    tags = fmt.get("tags") if isinstance(fmt.get("tags"), dict) else {}
    streams = []
    for item in (data.get("streams") or [])[:MAX_STREAMS]:
        if not isinstance(item, dict):
            continue
        index = _int(item.get("index"), 0, 4096)
        kind = _text(item.get("codec_type"), 16)
        if index is None or kind not in {"video", "audio", "subtitle", "data", "attachment"}:
            continue
        stags = item.get("tags") if isinstance(item.get("tags"), dict) else {}
        disposition = item.get("disposition") if isinstance(item.get("disposition"), dict) else {}
        streams.append(StreamInfo(
            index=index,
            kind=kind,
            codec=_text(item.get("codec_name"), 64),
            language=normalise_language(_text(stags.get("language"), 16)),
            title=_text(stags.get("title")),
            default=disposition.get("default") == 1,
            forced=disposition.get("forced") == 1,
            channels=_int(item.get("channels"), 0, 64) if kind == "audio" else None,
            channel_layout=_text(item.get("channel_layout"), 32) if kind == "audio" else None,
            width=_int(item.get("width"), 0, 100_000) if kind == "video" else None,
            height=_int(item.get("height"), 0, 100_000) if kind == "video" else None,
            fps=_rate(item.get("avg_frame_rate")) or _rate(item.get("r_frame_rate")) if kind == "video" else None,
            start_ms=_signed_ms(item.get("start_time")),
            time_base=_text(item.get("time_base"), 32),
        ))
    if not any(stream.kind in {"video", "audio"} for stream in streams):
        raise ProbeFailed("no audio or video stream")
    return ProbeResult(
        container=_text(fmt.get("format_name"), 64),
        duration_ms=_seconds_ms(fmt.get("duration")),
        title=_text(tags.get("title")),
        bit_rate=_int(fmt.get("bit_rate")),
        streams=tuple(sorted(streams, key=lambda stream: stream.index)),
        start_ms=_signed_ms(fmt.get("start_time")),
    )


def probe(tools: MediaTools, path: Path, *, cancelled: Callable[[], bool] | None = None) -> ProbeResult:
    result = run_bounded(
        tools.probe_argv(path),
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        stdout_limit=PROBE_STDOUT_LIMIT,
        stderr_limit=64 * 1024,
        cancelled=cancelled,
        env=tools.child_env(),
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ProbeFailed(f"ffprobe exit {result.returncode}: {detail[-1][:200] if detail else 'no detail'}")
    return parse_probe_json(result.stdout)
