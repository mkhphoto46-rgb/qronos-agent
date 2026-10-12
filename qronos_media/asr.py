"""Speech recognition for a file without usable subtitles (handoff s8: subtitle first, ASR as fallback).

The model is fixed: whisper.cpp ``ggml-large-v3-turbo-q5_0`` through
``whisper-cli``, the same provisioned assets as Qronos voice
(``CONFIG.paths.runtime_assets/whisper``). Before any child starts, both files
are checked read-only against their pinned size and SHA-256; anything missing
or different is ``model_unavailable`` and nothing runs. Assets are never
executed for verification, searched for, or substituted.

How a run goes:

* **Trigger.** Only when the asset has no indexed subtitle cue. Subtitle
  evidence is never deleted or overwritten here; ASR rows are a separate track
  with ``source = 'asr'``.
* **Audio track.** One audio stream, chosen by an explicit, recorded rule (the
  default-flagged stream, else the lowest index) and addressed by its absolute
  stream index, never "the first audio".
* **Timeline.** The asset timeline is presentation time minus the container
  start (``assets.start_offset_ms``, the asset origin); ffmpeg ``-ss`` seeks on
  it. Chunks are planned on the chosen stream's own timeline: chunk *k* starts
  at ``(audioStart - assetOrigin) + k * CHUNK_MS``, where audio really exists,
  so a segment at ``s`` ms inside it is at
  ``k * CHUNK_MS + s + (audioStart - assetOrigin)`` on the asset timeline.
* **Chunks.** ffmpeg extracts that stream (16 kHz mono PCM WAV) into an owned
  cache file; whisper-cli transcribes it with a fixed argument list, inside the
  existing suspended-before-Job containment (declared 2 GiB, one process), as a
  child of the runtime-issued model lease, which counts it until its exit is
  proven. Each finished chunk's segments are stored together with the stage
  checkpoint in one transaction, so a pause, Stop, crash or retry resumes at the
  next chunk and never transcribes one twice.
* **Result.** One commit, together with the stage's COMPLETED mark, replaces
  only this asset's ASR track and rows. Rows go through the normal dialogue
  table, so both FTS indexes update in the same transaction. Provenance says
  model, asset SHA-256, stream, language, and that the text is unverified and
  uncalibrated machine transcription. No accuracy is claimed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import time
from typing import Callable, Sequence

from qronos_media.containment import ChildLimits
from qronos_media.paths import owned_child
from qronos_media.resources import WHISPER_LARGE_V3_TURBO_Q5_0, ModelAsset, ModelSpec, ResourceRefused
from qronos_media.text import normalise_for_search

ASR_VERSION = 1
CHUNK_MS = 300_000                 # five minutes of audio per whisper run
CHUNK_EOF_SLACK_MS = 500
MAX_CHUNKS = 600                   # 50 hours
MAX_SEGMENTS_PER_CHUNK = 4_000
MAX_SEGMENT_CHARS = 2_000
MAX_JSON_BYTES = 16 * 1024 * 1024
WHISPER_THREADS = 4
#: Declared, conservative ASR child profile: the decoder's 2 GiB, one process. Not widened.
ASR_CHILD_LIMITS = ChildLimits(memory_bytes=2 * 1024**3, max_processes=1)
SAMPLE_RATE = 16_000

_ISO639 = {
    "eng": "en", "fas": "fa", "per": "fa", "ara": "ar", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de",
    "spa": "es", "rus": "ru", "tur": "tr", "jpn": "ja", "kor": "ko", "zho": "zh", "chi": "zh", "ita": "it",
    "por": "pt", "hin": "hi", "urd": "ur", "nld": "nl", "dut": "nl", "pol": "pl", "ukr": "uk", "heb": "he",
}

#: Test seam only: how a fixed argv is launched (tests prepend a base-interpreter fixture runner).
launcher: Callable[[list[str]], list[str]] = lambda argv: argv


class AsrUnavailable(ResourceRefused):
    """The pinned whisper assets are not provisioned as specified; nothing was started."""

    def __init__(self, detail: str) -> None:
        super().__init__("model_unavailable", detail)


@dataclass(frozen=True)
class WhisperAssets:
    executable: Path
    weights: Path
    spec: ModelSpec

    def provenance(self) -> dict[str, object]:
        return {"model": self.spec.name, "modelSha256": self.spec.weights.sha256,
                "executableSha256": self.spec.executable.sha256, "runtime": self.spec.runtime,
                "companionsSha256": companions_digest(self.spec)}


def companions_digest(spec: ModelSpec) -> str:
    """One digest over the pinned libraries the executable loads (name and SHA-256 of each)."""
    lines = sorted(f"{asset.parts[-1]}:{asset.sha256}" for asset in spec.companions)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


_VERIFIED: dict[tuple, bool] = {}


def _verify(path: Path, asset: ModelAsset) -> None:
    try:
        info = os.stat(path)
    except OSError:
        raise AsrUnavailable(f"{Path(*asset.parts)} is not provisioned") from None
    if not os.path.isfile(path) or os.path.islink(path):
        raise AsrUnavailable(f"{Path(*asset.parts)} is not a regular file")
    if info.st_size != asset.size:
        raise AsrUnavailable(f"{Path(*asset.parts)} has {info.st_size} bytes, pinned {asset.size}")
    key = (str(path), info.st_size, info.st_mtime_ns, getattr(info, "st_ino", 0), asset.sha256)
    if _VERIFIED.get(key):
        return
    digest = hashlib.sha256()
    with open(path, "rb") as stream:  # read-only; the file is never executed here
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != asset.sha256:
        raise AsrUnavailable(f"{Path(*asset.parts)} does not match its pinned SHA-256")
    _VERIFIED[key] = True


def locate_whisper(runtime_assets: Path, spec: ModelSpec = WHISPER_LARGE_V3_TURBO_Q5_0) -> WhisperAssets:
    """The provisioned whisper-cli and model, verified by size and SHA-256 (read-only)."""
    executable = Path(runtime_assets, *spec.executable.parts)
    weights = Path(runtime_assets, *spec.weights.parts)
    _verify(executable, spec.executable)
    for companion in spec.companions:  # every library it loads is the pinned one (a planted DLL refuses)
        _verify(Path(runtime_assets, *companion.parts), companion)
    _verify(weights, spec.weights)
    return WhisperAssets(executable, weights, spec)


def whisper_language(tag: str | None) -> str:
    text = (tag or "").strip().lower()
    if len(text) == 2 and text.isalpha():
        return text
    return _ISO639.get(text, "auto")


def whisper_argv(assets: WhisperAssets, wav: Path, out_base: Path, language: str) -> list[str]:
    """The only whisper-cli command line: the contract's decoding options, JSON segment output."""
    lang = language if language == "auto" or (len(language) == 2 and language.isalpha()) else "auto"
    return [str(assets.executable), "-m", str(assets.weights), "-f", str(wav), "-l", lang,
            "-t", str(WHISPER_THREADS), "-fa", "-oj", "-of", str(out_base)]


def whisper_env(assets: WhisperAssets) -> dict[str, str]:
    env = {"PATH": str(assets.executable.parent)}
    if os.name == "nt":
        env["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
    return env


# ------------------------------------------------------------------ selection

@dataclass(frozen=True)
class AudioChoice:
    stream_index: int
    language: str | None
    default: bool
    rule: str
    audio_start_ms: int | None
    asset_origin_ms: int | None

    @property
    def offset_ms(self) -> int:
        """audioStart - assetOrigin: where the stream's first sample is on the asset timeline."""
        if self.audio_start_ms is None or self.asset_origin_ms is None:
            return 0
        return max(0, int(self.audio_start_ms) - int(self.asset_origin_ms))


def choose_audio(db: sqlite3.Connection, asset_id: str) -> AudioChoice | None:
    rows = db.execute(
        "SELECT idx, language, is_default, start_ms FROM streams WHERE asset_id = ? AND kind = 'audio' ORDER BY idx",
        (asset_id,),
    ).fetchall()
    if not rows:
        return None
    defaults = [row for row in rows if row["is_default"]]
    row, rule = (defaults[0], "default_flag") if defaults else (rows[0], "lowest_index_no_default")
    origin = db.execute("SELECT start_offset_ms FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    return AudioChoice(int(row["idx"]), row["language"], bool(row["is_default"]), rule,
                       row["start_ms"], origin["start_offset_ms"] if origin is not None else None)


def skip_reason(db: sqlite3.Connection, asset_id: str) -> str | None:
    """Why this asset needs no ASR now (decided before any model is reserved), or None."""
    has_cues = db.execute(
        "SELECT 1 FROM subtitle_tracks WHERE asset_id = ? AND source != 'asr' AND status = 'indexed' "
        "AND cue_count > 0 LIMIT 1", (asset_id,),
    ).fetchone()
    if has_cues:
        return "subtitles_present"
    if choose_audio(db, asset_id) is None:
        return "no_audio"
    return None


def plan_key(asset_sha: str, choice: AudioChoice, assets: WhisperAssets) -> str:
    material = json.dumps({"v": ASR_VERSION, "asset": asset_sha, "stream": choice.stream_index,
                           "audioStart": choice.audio_start_ms, "origin": choice.asset_origin_ms,
                           "model": assets.spec.weights.sha256, "exe": assets.spec.executable.sha256,
                           "runtime": assets.spec.runtime, "libs": companions_digest(assets.spec),
                           "chunkMs": CHUNK_MS}, sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()


# ------------------------------------------------------------------ chunk output

def wav_duration_ms(path: Path) -> int:
    """Length of a PCM WAV ffmpeg wrote (16-bit mono 16 kHz), from its data chunk; 0 when unreadable."""
    try:
        with open(path, "rb") as stream:
            header = stream.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return 0
            for _ in range(64):
                chunk = stream.read(8)
                if len(chunk) < 8:
                    return 0
                kind, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
                if kind == b"data":
                    if size in (0xFFFFFFFF, 0):  # streamed header: use the file size
                        size = max(0, os.path.getsize(path) - stream.tell())
                    return int(size * 1000 // (SAMPLE_RATE * 2))
                stream.seek(size + (size & 1), os.SEEK_CUR)
    except OSError:
        return 0
    return 0


def parse_whisper_json(data: bytes, length_ms: int) -> list[tuple[int, int, str]]:
    """Segments (start, end, text) relative to the chunk, bounded and validated."""
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("whisper output too large")
    document = json.loads(data.decode("utf-8", "replace"))
    items = document.get("transcription") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise ValueError("whisper output has no transcription list")
    if len(items) > MAX_SEGMENTS_PER_CHUNK:
        raise ValueError("too many segments in one chunk")
    segments = []
    ceiling = length_ms + CHUNK_EOF_SLACK_MS
    for item in items:
        offsets = item.get("offsets") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(offsets, dict) or not isinstance(text, str):
            raise ValueError("malformed whisper segment")
        start, end = offsets.get("from"), offsets.get("to")
        if not isinstance(start, int) or not isinstance(end, int) or not (0 <= start <= end <= ceiling):
            raise ValueError("whisper segment outside its chunk")
        text = text.strip()[:MAX_SEGMENT_CHARS]
        if text:
            segments.append((start, end, text))
    return segments


def asset_time_ms(chunk_index: int, segment_ms: int, choice: AudioChoice) -> int:
    return chunk_index * CHUNK_MS + segment_ms + choice.offset_ms


def track_id(asset_id: str, choice: AudioChoice) -> str:
    return f"{asset_id}:asr{choice.stream_index}"


def build_rows(asset_id: str, choice: AudioChoice, chunks: Sequence[tuple[int, list]]) -> list[tuple]:
    rows = []
    seq = 0
    tid = track_id(asset_id, choice)
    for index, segments in chunks:
        for start, end, text in segments:
            norm = normalise_for_search(text)
            if not norm:
                continue
            rows.append((asset_id, tid, "cue", seq, asset_time_ms(index, start, choice),
                         asset_time_ms(index, end, choice), text, norm))
            seq += 1
    return rows


def provenance(asset_sha: str, choice: AudioChoice, assets: WhisperAssets, key: str, chunks: int) -> dict:
    return {
        "source": "asr", "schema": 2, "asrVersion": ASR_VERSION, **assets.provenance(),
        "assetSha256": asset_sha, "audioStreamIndex": choice.stream_index, "audioLanguage": choice.language,
        "whisperLanguage": whisper_language(choice.language), "audioDefault": choice.default,
        "audioSelection": choice.rule, "audioStartMs": choice.audio_start_ms,
        "assetOriginMs": choice.asset_origin_ms, "offsetMs": choice.offset_ms, "chunkMs": CHUNK_MS,
        "chunks": chunks, "planKey": key,
        "verified": False, "calibrated": False,
        "label": "unverified machine transcription (whisper.cpp); no accuracy claim",
        "generatedAt": time.time(),
    }


def owned_paths(cache_dir: Path, asset_id: str, index: int) -> tuple[Path, Path, Path]:
    """(wav, whisper output base, whisper JSON) for one chunk, all checked owned names in the media cache."""
    folder = owned_child(cache_dir, "asr")
    folder.mkdir(parents=True, exist_ok=True)
    name = f"asr-{asset_id[:24]}-{index:04d}"
    return (owned_child(cache_dir, "asr", name + ".wav"), owned_child(cache_dir, "asr", name),
            owned_child(cache_dir, "asr", name + ".json"))
