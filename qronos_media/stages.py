"""The versioned processing graph (handoff s30, s31, s59) and its stages.

Each stage has a version. Raising a version, or a change in a stage's inputs
(a new sidecar subtitle beside the film), re-queues that stage and everything
downstream of it, and nothing upstream: new subtitle parsing never re-runs
shot detection.

A stage collects its result outside the database, then hands back a
``commit`` that the runner applies in the same transaction that marks the
stage COMPLETED, so a stage is either fully present or not at all. Long
stages (shot detection, keyframes) also record checkpoints between chunks and
resume from them after a pause, a cancel-and-retry or a crash.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable

from qronos_media.decoder_copy import sweep_stale_inputs, verified_decoder_input
from qronos_media.identity import FileState, HashCancelled, HashLimitExceeded, checked_asset_id, full_sha256
from qronos_media.paths import UnsafeMediaPath, owned_child, readable_media_file
from qronos_media.scope import OutOfScope, admit_sidecar
from qronos_media.probe import ProbeResult, probe
from qronos_media import asr
from qronos_media import semantic
from qronos_media import topics
from qronos_media import brain_labels
from qronos_media import visual_evidence
from qronos_media import image_embeddings
from qronos_media.resources import (
    BGE_M3, QWEN3_VL_4B, SIGLIP2_BASE_224_INT8, WHISPER_LARGE_V3_TURBO_Q5_0, ResourceRefused, StageNeeds,
)
from qronos_media.subtitles import (
    IMAGE_SUBTITLE_CODECS, MAX_SUBTITLE_BYTES, TEXT_SUBTITLE_CODECS, Cue, SubtitleError, decode_subtitle_bytes,
    find_sidecars, parse_srt, parse_subtitle_text,
)
from qronos_media.text import normalise_for_search
from qronos_media.tools import (
    ASS_DEMUXERS, ChildCancelled, ChildTerminationUnproven, MediaTools, MediaToolUnavailable, run_bounded,
)


class StageFailed(RuntimeError):
    """The stage cannot complete for this asset; the reason is stored."""


@dataclass(frozen=True)
class StageSpec:
    name: str
    version: int
    depends: tuple[str, ...]
    level: int
    needs: StageNeeds


STAGES: dict[str, StageSpec] = {
    spec.name: spec
    for spec in (
        StageSpec("metadata", 1, (), 0, StageNeeds("metadata")),
        StageSpec("subtitles", 1, ("metadata",), 1, StageNeeds("subtitles", disk_bytes=64 * 1024**2)),
        # Model-bearing: only the runtime-issued HostedModelAuthority grants it (never the standalone CLI).
        StageSpec("asr", asr.ASR_VERSION, ("subtitles",), 1,
                  StageNeeds("asr", disk_bytes=256 * 1024**2, model=WHISPER_LARGE_V3_TURBO_Q5_0)),
        # Model-bearing (bge-m3 on the runtime's CPU memory sidecar). Dialogue that ASR adds later requeues it.
        StageSpec("semantic", semantic.SEMANTIC_VERSION, ("subtitles",), 2,
                  StageNeeds("semantic", disk_bytes=64 * 1024**2, model=BGE_M3)),
        # Topic segments from the STORED bge-m3 window vectors (qronos_media.topics): no model call, CPU only.
        StageSpec("topics", topics.TOPICS_VERSION, ("semantic",), 2, StageNeeds("topics")),
        # Generated labels/summaries for the topic segments: ONE governed Fast-brain call per segment
        # (qronos_media.brain_gateway -> Orchestrator.answer_chat_only, which holds the brain's own ledger
        # reservation); the media lease here is the light worker lease. No runtime brain: unavailable.
        StageSpec("topic_labels", brain_labels.LABELS_VERSION, ("topics",), 4, StageNeeds("topic_labels")),
        StageSpec("scenes", 1, ("metadata",), 3, StageNeeds("scenes")),
        StageSpec("keyframes", 1, ("scenes",), 3, StageNeeds("keyframes", disk_bytes=256 * 1024**2)),
        # Model-bearing: the runtime's CONFIGURED Vision profile on its own brain runtime (qronos_media.vision_gateway),
        # under the runtime-issued HostedModelAuthority's lease for the declared QWEN3_VL_4B. No runtime Vision or a
        # mismatching profile: unavailable, never COMPLETED. Keyframes that change requeue it.
        StageSpec("visual_evidence", visual_evidence.VISUAL_VERSION, ("keyframes",), 5,
                  StageNeeds("visual_evidence", disk_bytes=32 * 1024**2, model=QWEN3_VL_4B)),
        # Model-bearing (the SAME bge-m3 on the runtime's CPU memory sidecar): vectors of the stored GENERATED
        # visual text, never of the image. Re-run whenever visual_evidence is; unchanged texts are cache hits.
        StageSpec("visual_semantic", visual_evidence.VISUAL_SEMANTIC_VERSION, ("visual_evidence",), 5,
                  StageNeeds("visual_semantic", disk_bytes=16 * 1024**2, model=BGE_M3)),
        # Model-bearing (the declared SigLIP 2 image tower, in process on the CPU; qronos_media.image_embeddings):
        # vectors of the stored keyframes' PIXELS, independent of Vision captions. Keyframes that change requeue it;
        # frames whose vector is already stored for the current fingerprint are not embedded again.
        StageSpec("image_embeddings", image_embeddings.STAGE_VERSION, ("keyframes",), 3,
                  StageNeeds("image_embeddings", disk_bytes=16 * 1024**2, model=SIGLIP2_BASE_224_INT8)),
    )
}

# Handoff capabilities that do not exist yet. Status reports them so nobody
# mistakes an empty result for "nothing there".
PENDING_CAPABILITIES: tuple[dict[str, object], ...] = (
    {"capability": "asr", "level": 1, "status": "implemented_unverified",
     "reason": "whisper.cpp large-v3-turbo q5_0 transcribes files without subtitles, only inside the Qronos runtime "
               "(shared governor and ledger) and only when the pinned whisper assets are provisioned; the standalone "
               "tool never runs it. Output is unverified, uncalibrated machine text; no accuracy is claimed and no "
               "real-model run has been accepted."},
    {"capability": "subtitle_asr_fusion", "level": 1, "status": "pending",
     "reason": "Agreement/uncertainty labels between subtitles and ASR (handoff s9) are not implemented."},
    {"capability": "semantic_dialogue", "level": 2, "status": "implemented_unverified",
     "reason": "bge-m3 (F16, 1024) windows over subtitle/ASR dialogue, only inside the Qronos runtime with its "
               "memory sidecar running (QRONOS_MEMORY_RUNTIME=1) and bge-m3 installed; scores are uncalibrated "
               "similarities; no real-model run has been accepted."},
    {"capability": "topic_retrieval", "level": 2, "status": "implemented_unverified",
     "reason": "Topic search over segments of one track's dialogue grouped by their stored bge-m3 vectors (stage "
               "'topics', no model call); a query vector needs the memory sidecar. Hits are always backed by indexed "
               "lines; expansion is pseudo-relevance feedback from those lines, shown as search hints, never facts; "
               "the rank score uses fixed weights and is uncalibrated. Segments carry no labels or summaries."},
    {"capability": "visual_semantic_text", "level": 3, "status": "implemented_unverified",
     "reason": "Cross-language bge-m3 retrieval over the stored GENERATED visual text (captions, text read, observations; stage 'visual_semantic'), only inside the Qronos runtime with its memory sidecar; text vectors, never image similarity; scores are uncalibrated; no real-model run has been accepted."},
    {"capability": "visual_embeddings", "level": 3, "status": "implemented_unverified",
     "reason": "Image similarity: SigLIP 2 base (Apache-2.0; int8 ONNX image and text towers in one space) embeds the PIXELS "
               "of the stored keyframes (stage 'image_embeddings') and a typed query, run in process by onnxruntime on "
               "the CPU only inside the Qronos runtime, and only when its three pinned files are provisioned (never "
               "downloaded or bundled). Scores are uncalibrated cosines, not identifications; tokenizer parity, quality "
               "(Persian included) and timing are unmeasured; no real-model run has been accepted."},
    {"capability": "vision_captions", "level": 4, "status": "implemented_unverified",
     "reason": "Captions, text read in the frame and visible observations for at most 24 stored keyframes per asset, from the runtime's configured Vision profile (stage 'visual_evidence'), only inside the Qronos runtime under the declared Vision model lease; generated, unverified model text with the frame time and SHA-256, never a fact or an identification; searched lexically as a separate channel; no real-model run has been accepted."},
    {"capability": "ocr", "level": 4, "status": "pending",
     "reason": "No OCR engine is specified. Text the Vision model reads in a frame is stored as model text in the visual evidence (with a second read at higher resolution for a few frames), not as OCR output."},
    {"capability": "topics", "level": 4, "status": "implemented_unverified",
     "reason": "Topic labels and summaries generated by the configured Fast brain through the runtime's governed call "
               "(stage 'topic_labels'), only inside the Qronos runtime; every kept label cites indexed lines, an "
               "answer citing anything else is rejected; generated, unverified model text, never a quotation or a "
               "fact; no real-model run has been accepted."},
    {"capability": "speaker_turns", "level": 5, "status": "pending", "reason": "Not implemented."},
    {"capability": "actions_motion", "level": 5, "status": "not_supported",
     "reason": "Sparse keyframes cannot establish actions or motion; no validated temporal model path exists."},
)

PAIR_MAX_GAP_MS = 2_000
SCENE_CHUNK_SECONDS = 600.0
SCENE_OVERLAP_SECONDS = 1.0
SCENE_THRESHOLD = 10.0
SCENE_EOF_SLACK_SECONDS = 0.5
MAX_SCENE_CHUNKS = 2_000  # 2,000 x 10 min: far beyond any film
MIN_SCENE_MS = 400
LONG_SCENE_MS = 20_000
MAX_KEYFRAMES_PER_ASSET = 3_000
SUBTITLE_STDOUT_LIMIT = 32 * 1024 * 1024
#: Sidecar reads are chunked so pause, Stop, the deadline, permission loss and
#: lease loss are observed between chunks (through the stage's watch).
SIDECAR_READ_CHUNK = 256 * 1024

_SCDET = re.compile(rb"lavfi\.scd\.score:\s*([0-9.]+),\s*lavfi\.scd\.time:\s*([0-9.]+)")
_PROGRESS_TIME = re.compile(rb"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")

Commit = Callable[[sqlite3.Connection], None]


def downstream(stage: str) -> list[str]:
    """``stage`` and everything that depends on it, in graph order."""
    affected = {stage}
    for spec in STAGES.values():
        if any(dep in affected for dep in spec.depends):
            affected.add(spec.name)
    return [name for name in STAGES if name in affected]


@dataclass
class StageContext:
    db: sqlite3.Connection
    asset_id: str
    files: list[Path]
    tools: MediaTools | None
    cache_dir: Path
    cache_quota_bytes: int
    cancelled: Callable[[], bool]
    renew: Callable[[], None]
    save_checkpoint: Callable[[dict], None]
    checkpoint: dict = field(default_factory=dict)
    # Uncached pause/cancel check for chunk boundaries; ``cancelled`` is the
    # cheap, rate-limited one polled while a child runs.
    interrupted: Callable[[], bool] = lambda: False
    #: The media index's own data folder: never an input.
    excluded: tuple = ()
    #: Re-admission of a copy right before a decoder opens it (scope and a
    #: fresh gate answer for its root). Production always sets it; None refuses.
    admit: Callable[[Path], Path] = None  # type: ignore[assignment]
    #: The lease this stage runs under (a model stage needs the runtime-issued ModelLease).
    lease: Callable[[], object] = lambda: None
    #: The runtime's bge-m3 embedder (qronos_media.semantic.MediaEmbedder) or None.
    embedder: object = None
    #: The memory-sidecar generation captured at admission (qronos_media.semantic.SidecarBinding) or None.
    sidecar_binding: object = None
    #: The runtime's governed Fast-brain gateway (qronos_media.brain_gateway.FastBrainGateway) or None.
    brain: object = None
    #: The runtime's Vision gateway (qronos_media.vision_gateway.VisionGateway) or None.
    vision: object = None
    #: The runtime's image encoder (qronos_media.image_embeddings.ImageEncoderProvider) or None.
    image_encoder: object = None

    def checkpoint_boundary(self) -> None:
        if self.interrupted():
            raise ChildCancelled("paused or cancelled at a checkpoint boundary")

    def require_tools(self) -> MediaTools:
        if self.tools is None:
            raise MediaToolUnavailable("ffmpeg/ffprobe are not provisioned (set QRONOS_MEDIA_FFMPEG_DIR)")
        return self.tools

    def primary_file(self) -> Path:
        """The copy a decoder may open: admitted again now, no link anywhere on its path.

        If no copy can be admitted now (its root was revoked or its approval
        expired, or it left scope), the stage is *refused*, not failed: the
        runner requeues it and says why.
        """
        if self.admit is None:
            raise StageFailed("no admission check was provided; refusing to open any copy")
        for path in self.files:
            try:
                return readable_media_file(self.admit(path))
            except (UnsafeMediaPath, OutOfScope):
                continue
        raise OutOfScope("no_admitted_copy")

    def reopen(self, path: Path) -> Path:
        """Fresh admission (scope + the copy's own root, asked now) before *each* decoder spawn."""
        if self.admit is None:
            raise StageFailed("no admission check was provided; refusing to open any copy")
        try:
            return readable_media_file(self.admit(path))
        except UnsafeMediaPath:
            raise OutOfScope("unsafe_path") from None

    def asset(self) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM assets WHERE asset_id = ?", (self.asset_id,)).fetchone()


# ------------------------------------------------------------------ metadata

def run_metadata(ctx: StageContext) -> Commit:
    result: ProbeResult = probe(ctx.require_tools(), ctx.primary_file(), cancelled=ctx.cancelled)

    def commit(db: sqlite3.Connection) -> None:
        db.execute(
            "UPDATE assets SET duration_ms = ?, start_offset_ms = ?, container = ?, title = ?, probe_json = ? "
            "WHERE asset_id = ?",
            (result.duration_ms, result.start_ms, result.container, result.title, json.dumps(result.as_json()),
             ctx.asset_id),
        )
        db.execute("DELETE FROM streams WHERE asset_id = ?", (ctx.asset_id,))
        db.executemany(
            "INSERT INTO streams(asset_id, idx, kind, codec, language, title, is_default, forced, channels, "
            "channel_layout, width, height, fps, start_ms, time_base) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (ctx.asset_id, s.index, s.kind, s.codec, s.language, s.title, int(s.default), int(s.forced),
                 s.channels, s.channel_layout, s.width, s.height, s.fps, s.start_ms, s.time_base)
                for s in result.streams
            ],
        )

    return commit


# ----------------------------------------------------------------- subtitles

def admitted_sidecars(video: Path, excluded: tuple) -> list:
    """Sidecars beside an admitted video that are themselves admitted (same folder, no link, not media state)."""
    found = []
    for sidecar in find_sidecars(video):
        try:
            admit_sidecar(video, sidecar.path, excluded)
        except OutOfScope:
            continue
        found.append(sidecar)
    return found


def subtitles_input_fingerprint(files: list[Path], excluded: tuple) -> str:
    """Changes when an admitted sidecar beside any admitted copy appears, changes or goes."""
    parts = []
    for video in files:
        for sidecar in admitted_sidecars(video, excluded):
            try:
                info = sidecar.path.stat()
            except OSError:
                continue
            parts.append(f"{os.path.normcase(str(sidecar.path))}|{info.st_size}|{info.st_mtime_ns}")
    return "s1:" + hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


@dataclass
class _Track:
    track_id: str
    source: str
    stream_index: int | None = None
    sidecar_path: str | None = None
    sidecar_sha256: str | None = None
    language: str | None = None
    title: str | None = None
    codec: str | None = None
    forced: bool = False
    encoding: str | None = None
    status: str = "indexed"
    detail: str | None = None
    cues: list[Cue] = field(default_factory=list)


def read_bounded(path: Path, limit: int, cancelled) -> bytes:
    """Read at most ``limit`` bytes in chunks, checking ``cancelled`` before each chunk.

    More than ``limit`` bytes (the file grew after its size was checked)
    refuses the file. Cancellation raises ChildCancelled, never a partial success.
    Reads go to the unbuffered file underneath ``open`` when there is one, so
    the operating system is not asked past ``limit + 1`` either (no read-ahead).
    """
    chunks: list[bytes] = []
    total = 0
    with open(path, "rb") as handle:
        reader = getattr(handle, "raw", handle)
        while True:
            if cancelled():
                raise ChildCancelled("subtitle read stopped")
            chunk = reader.read(min(SIDECAR_READ_CHUNK, limit + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise SubtitleError("subtitle file is too large")
            chunks.append(chunk)
    return b"".join(chunks)


def _extract_embedded(ctx: StageContext, path: Path, index: int, demuxers=None) -> list[Cue]:
    tools = ctx.require_tools()
    argv = tools.subtitle_argv(path, index) if demuxers is None else tools.subtitle_argv(path, index, demuxers=demuxers)
    result = run_bounded(
        argv, timeout_seconds=300.0, stdout_limit=SUBTITLE_STDOUT_LIMIT, stderr_limit=256 * 1024,
        cancelled=ctx.cancelled, env=tools.child_env(),
    )
    if result.returncode != 0:
        lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SubtitleError(lines[-1][:200] if lines else f"ffmpeg exit {result.returncode}")
    text, _ = decode_subtitle_bytes(result.stdout)
    return parse_srt(text)


def _refused_track(ctx: StageContext, sidecar, n: int, detail: str) -> "_Track":
    return _Track(f"{ctx.asset_id}:s-refused-{n}", "sidecar", sidecar_path=str(sidecar.path),
                  language=sidecar.language, codec=sidecar.path.suffix.lower().lstrip("."),
                  status="failed", detail=detail)


def _read_sidecar(ctx: StageContext, copy: Path, sidecar, n: int, seen_hashes: set[str]) -> "_Track | None":
    """One sidecar, as one observed and bounded handoff.

    1. Its size is checked first: over MAX_SUBTITLE_BYTES it is refused with
       no read and no decoder spawn.
    2. Its identity hash streams through ``full_sha256`` with the stage's
       cancellation, so pause, Stop, the deadline, permission loss and lease
       loss stop it between chunks (a typed ChildCancelled, never success).
       The hash read itself is bounded to MAX_SUBTITLE_BYTES + 1 bytes: a file
       that grew after the size check is refused there, with no parse or
       decoder open.
    3. SRT/VTT/ASS/SSA are then read in bounded chunks (at most the size
       limit) and must hash to that same digest, so the stored hash is of
       exactly the bytes used. SRT/VTT are parsed from those bytes. ASS/SSA are
       decoded by ffmpeg from a private, write-locked copy of exactly those
       bytes (``decoder_copy``), never from the user's file by name, after one
       more fresh admission made once the copy is ready; ffmpeg's output is bounded.
    4. The file must not have changed (size, mtime, file id) at the end.

    The hash and the read/decode are separate opens, so the copy and the
    sidecar are admitted again, with a fresh gate answer, right before the
    read/decode as well (the caller did so before the hash).
    """
    try:
        before = FileState.of(sidecar.path)
    except OSError as exc:
        return _refused_track(ctx, sidecar, n, f"unreadable: {type(exc).__name__}")
    if before.size > MAX_SUBTITLE_BYTES:
        return _refused_track(ctx, sidecar, n, f"too large: {before.size} bytes (limit {MAX_SUBTITLE_BYTES})")
    try:
        digest = full_sha256(sidecar.path, cancelled=ctx.cancelled, limit=MAX_SUBTITLE_BYTES)
    except HashCancelled:
        raise ChildCancelled("subtitle hash stopped") from None
    except HashLimitExceeded:
        return _refused_track(ctx, sidecar, n, f"too large: grew past {MAX_SUBTITLE_BYTES} bytes while hashed")
    except OSError as exc:
        return _refused_track(ctx, sidecar, n, f"unreadable: {type(exc).__name__}")
    try:
        grown = FileState.of(sidecar.path).size > MAX_SUBTITLE_BYTES
    except OSError:
        grown = True
    if grown:
        return _refused_track(ctx, sidecar, n, "too large or gone after hashing")
    if digest in seen_hashes:
        return None  # the same subtitle beside two copies of the same film
    seen_hashes.add(digest)
    track = _Track(
        track_id=f"{ctx.asset_id}:s{digest[:16]}", source="sidecar", sidecar_path=str(sidecar.path),
        sidecar_sha256=digest, language=sidecar.language, forced="forced" in sidecar.flags,
        codec=sidecar.path.suffix.lower().lstrip("."),
    )
    ctx.checkpoint_boundary()
    admit_sidecar(ctx.admit(copy), sidecar.path, ctx.excluded)  # fresh, right before the second open
    try:
        data = read_bounded(sidecar.path, MAX_SUBTITLE_BYTES, ctx.cancelled)
        if hashlib.sha256(data).hexdigest() != digest:
            raise SubtitleError("the subtitle file changed while it was read")
        if sidecar.path.suffix.lower() in {".ass", ".ssa"}:
            track.cues = _decode_verified(ctx, copy, sidecar, data, digest)
            track.encoding = "ffmpeg"
        else:
            text, track.encoding = decode_subtitle_bytes(data)
            track.cues = parse_subtitle_text(text, sidecar.path.suffix)
        if FileState.of(sidecar.path) != before:
            raise SubtitleError("the subtitle file changed while it was read")
    except (SubtitleError, MediaToolUnavailable, OSError) as exc:
        track.cues = []
        track.status, track.detail = "failed", str(exc)[:300]
    return track


def _decode_verified(ctx: StageContext, copy: Path, sidecar, data: bytes, digest: str) -> list[Cue]:
    """
    ASS/SSA: ffmpeg decodes a private copy of exactly the verified bytes.

    The copy is made, flushed and re-hashed first; only then is the original
    admitted again with a fresh gate answer, immediately before the decoder
    starts, so a refusal that lands while the copy is prepared stops it.
    """
    with verified_decoder_input(ctx.cache_dir, data, digest, sidecar.path.suffix) as private_copy:
        ctx.checkpoint_boundary()
        admit_sidecar(ctx.admit(copy), sidecar.path, ctx.excluded)  # fresh, right before the native handoff
        return _extract_embedded(ctx, private_copy, 0, demuxers=ASS_DEMUXERS)


def run_subtitles(ctx: StageContext) -> Commit:
    tracks: list[_Track] = []
    sweep_stale_inputs(ctx.cache_dir)  # private decoder copies a crash may have left
    video = ctx.primary_file()
    streams = ctx.db.execute(
        "SELECT idx, codec, language, title, forced FROM streams WHERE asset_id = ? AND kind = 'subtitle' ORDER BY idx",
        (ctx.asset_id,),
    ).fetchall()
    for row in streams:
        video = ctx.reopen(video)  # every embedded extraction reopens the video: admit it again now
        track = _Track(
            track_id=f"{ctx.asset_id}:e{row['idx']}", source="embedded", stream_index=row["idx"],
            language=row["language"], title=row["title"], codec=row["codec"], forced=bool(row["forced"]),
        )
        if row["codec"] in IMAGE_SUBTITLE_CODECS:
            track.status, track.detail = "unsupported_image", "image subtitles need OCR (pending)"
        elif row["codec"] in TEXT_SUBTITLE_CODECS:
            try:
                track.cues = _extract_embedded(ctx, video, int(row["idx"]))
                track.encoding = "utf-8"
            except (SubtitleError, MediaToolUnavailable) as exc:
                track.status, track.detail = "failed", str(exc)[:300]
        else:
            track.status, track.detail = "failed", f"unknown subtitle codec {row['codec']!r}"
        tracks.append(track)

    seen_hashes: set[str] = set()
    for copy in ctx.files:
        for sidecar in admitted_sidecars(ctx.admit(copy), ctx.excluded):
            # Each sidecar is its own read handoff: stop/pause/deadline/permission
            # (uncached), then the copy's root and the sidecar are admitted again now.
            ctx.checkpoint_boundary()
            admit_sidecar(ctx.admit(copy), sidecar.path, ctx.excluded)
            tracks.append(_read_sidecar(ctx, copy, sidecar, len(tracks), seen_hashes))
    tracks = [track for track in tracks if track is not None]

    def commit(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM dialogue WHERE asset_id = ?", (ctx.asset_id,))
        db.execute("DELETE FROM subtitle_tracks WHERE asset_id = ?", (ctx.asset_id,))
        for track in tracks:
            db.execute(
                "INSERT INTO subtitle_tracks(track_id, asset_id, source, stream_index, sidecar_path, sidecar_sha256, "
                "language, title, codec, forced, encoding, status, detail, cue_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (track.track_id, ctx.asset_id, track.source, track.stream_index, track.sidecar_path,
                 track.sidecar_sha256, track.language, track.title, track.codec, int(track.forced),
                 track.encoding, track.status, track.detail, len(track.cues)),
            )
            rows = []
            normalised = [normalise_for_search(cue.text) for cue in track.cues]
            for seq, (cue, norm) in enumerate(zip(track.cues, normalised)):
                if norm:
                    rows.append((ctx.asset_id, track.track_id, "cue", seq, cue.start_ms, cue.end_ms, cue.text, norm))
                if seq + 1 < len(track.cues):
                    following = track.cues[seq + 1]
                    if 0 <= following.start_ms - cue.end_ms <= PAIR_MAX_GAP_MS and norm and normalised[seq + 1]:
                        # A remembered line is often split across two cues.
                        rows.append((ctx.asset_id, track.track_id, "pair", seq, cue.start_ms, following.end_ms,
                                     f"{cue.text} {following.text}", f"{norm} {normalised[seq + 1]}"))
            db.executemany(
                "INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )

    return commit


# -------------------------------------------------------------------- scenes

def parse_scene_cuts(stderr: bytes, offset_seconds: float) -> list[tuple[int, float]]:
    cuts = []
    for score, seconds in _SCDET.findall(stderr):
        try:
            cuts.append((int(round((offset_seconds + float(seconds)) * 1000)), float(score)))
        except ValueError:
            continue
    return cuts


def scenes_from_cuts(cuts: list[tuple[int, float]], duration_ms: int) -> list[tuple[int, int, float | None]]:
    """Turn cut points into contiguous scenes covering ``[0, duration]``."""
    boundaries: list[tuple[int, float | None]] = [(0, None)]
    for at, score in sorted(cuts):
        if at - boundaries[-1][0] >= MIN_SCENE_MS and duration_ms - at >= MIN_SCENE_MS:
            boundaries.append((at, score))
    scenes = []
    for position, (start, score) in enumerate(boundaries):
        end = boundaries[position + 1][0] if position + 1 < len(boundaries) else duration_ms
        scenes.append((start, end, score))
    return scenes


def parse_decoded_seconds(stderr: bytes) -> float | None:
    """The last progress ``time=`` ffmpeg printed: how far this chunk decoded."""
    found = _PROGRESS_TIME.findall(stderr)
    if not found:
        return None
    hours, minutes, seconds = found[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def run_scenes(ctx: StageContext) -> Commit:
    """Shot boundaries on the asset timeline, chunked and checkpointed.

    Times from each chunk are relative to the chunk's ``-ss`` position, which
    ffmpeg measures from the container start, so ``chunk start + reported
    time`` is asset time. The end of the last scene is where decoding actually
    ended, not the container's duration claim.
    """
    has_video = ctx.db.execute(
        "SELECT 1 FROM streams WHERE asset_id = ? AND kind = 'video' LIMIT 1", (ctx.asset_id,)
    ).fetchone()
    if not has_video:
        return lambda db: db.execute("DELETE FROM scenes WHERE asset_id = ?", (ctx.asset_id,))
    tools = ctx.require_tools()
    video = ctx.primary_file()
    cuts = [tuple(item) for item in ctx.checkpoint.get("cuts", [])]
    start = float(ctx.checkpoint.get("next_start_ms", 0)) / 1000.0
    decoded_end = ctx.checkpoint.get("decoded_end_ms")
    for _ in range(MAX_SCENE_CHUNKS):
        if ctx.checkpoint.get("eof"):
            break
        ctx.checkpoint_boundary()
        ctx.renew()
        begin = max(0.0, start - SCENE_OVERLAP_SECONDS) if start > 0 else 0.0
        length = SCENE_CHUNK_SECONDS + (start - begin)
        video = ctx.reopen(video)  # each chunk is a new decoder open
        result = run_bounded(
            tools.scenes_argv(video, start_seconds=begin, length_seconds=length, threshold=SCENE_THRESHOLD),
            # A ceiling, not an estimate of decode speed.
            timeout_seconds=max(120.0, length * 3.0),
            stdout_limit=64 * 1024, stderr_limit=8 * 1024 * 1024,
            cancelled=ctx.cancelled, env=tools.child_env(),
        )
        if result.returncode != 0:
            lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
            raise StageFailed(f"shot detection failed: {lines[-1][:200] if lines else result.returncode}")
        for at, score in parse_scene_cuts(result.stderr, begin):
            if all(abs(at - known) > 200 for known, _ in cuts):
                cuts.append((at, score))
        decoded = parse_decoded_seconds(result.stderr)
        eof = decoded is None or decoded < length - SCENE_EOF_SLACK_SECONDS
        if decoded is not None:
            decoded_end = max(decoded_end or 0, int(round((begin + decoded) * 1000)))
        start += SCENE_CHUNK_SECONDS
        ctx.checkpoint = {"next_start_ms": int(start * 1000), "cuts": sorted(cuts),
                          "decoded_end_ms": decoded_end, "eof": eof}
        ctx.save_checkpoint(ctx.checkpoint)
    else:
        raise StageFailed("video did not end within the chunk limit")
    if not decoded_end:
        raise StageFailed("no video frames decoded")
    scenes = scenes_from_cuts(cuts, decoded_end)

    def commit(db: sqlite3.Connection) -> None:
        db.execute("UPDATE assets SET video_end_ms = ? WHERE asset_id = ?", (decoded_end, ctx.asset_id))
        db.execute("DELETE FROM scenes WHERE asset_id = ?", (ctx.asset_id,))
        db.executemany(
            "INSERT INTO scenes(asset_id, scene_index, start_ms, end_ms, cut_score) VALUES (?,?,?,?,?)",
            [(ctx.asset_id, index, s, e, score) for index, (s, e, score) in enumerate(scenes)],
        )

    return commit


# ----------------------------------------------------------------- keyframes

def keyframe_times(start_ms: int, end_ms: int) -> list[int]:
    """Adaptive v1 (handoff s15): middle; start/middle/end for long scenes."""
    middle = start_ms + (end_ms - start_ms) // 2
    if end_ms - start_ms < LONG_SCENE_MS:
        return [middle]
    return [start_ms + 1_000, middle, end_ms - 1_000]


def cache_bytes(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COALESCE(SUM(bytes), 0) FROM keyframes").fetchone()[0])


def run_keyframes(ctx: StageContext) -> Commit:
    scenes = ctx.db.execute(
        "SELECT scene_index, start_ms, end_ms FROM scenes WHERE asset_id = ? ORDER BY scene_index", (ctx.asset_id,)
    ).fetchall()
    directory = owned_child(ctx.cache_dir, "keyframes", checked_asset_id(ctx.asset_id))
    resume_from = int(ctx.checkpoint.get("next_scene", 0))
    ctx.db.execute("BEGIN IMMEDIATE")
    try:
        stale = ctx.db.execute(
            "SELECT path FROM keyframes WHERE asset_id = ? AND scene_index >= ?", (ctx.asset_id, resume_from)
        ).fetchall()
        ctx.db.execute("DELETE FROM keyframes WHERE asset_id = ? AND scene_index >= ?", (ctx.asset_id, resume_from))
        ctx.db.execute("COMMIT")
    except BaseException:
        ctx.db.execute("ROLLBACK")
        raise
    for row in stale:
        owned_child(directory, Path(row["path"]).name).unlink(missing_ok=True)
    # A decoder killed mid-write leaves a file with no row; remove anything
    # this stage would rewrite, by name, so the cache never holds orphans.
    if directory.is_dir():
        for leftover in directory.glob("*.jpg*"):
            match = re.fullmatch(r"(\d{6})_\d+\.jpg(\.part)?", leftover.name)
            if match is None or int(match.group(1)) >= resume_from:
                owned_child(directory, leftover.name).unlink(missing_ok=True)
    if not scenes:
        return lambda db: None
    tools = ctx.require_tools()
    video = ctx.primary_file()
    directory.mkdir(parents=True, exist_ok=True)
    produced = int(ctx.db.execute("SELECT COUNT(*) FROM keyframes WHERE asset_id = ?", (ctx.asset_id,)).fetchone()[0])
    for scene in scenes:
        index = scene["scene_index"]
        if index < resume_from:
            continue
        ctx.checkpoint_boundary()
        ctx.renew()
        rows = []
        for ordinal, at in enumerate(keyframe_times(scene["start_ms"], scene["end_ms"])):
            if produced >= MAX_KEYFRAMES_PER_ASSET:
                break
            if cache_bytes(ctx.db) >= ctx.cache_quota_bytes:
                raise StageFailed("keyframe cache quota reached")
            target = owned_child(directory, f"{index:06d}_{ordinal}.jpg")
            video = ctx.reopen(video)  # each frame is a new decoder open
            result = run_bounded(
                tools.keyframe_argv(video, time_seconds=at / 1000.0, output=target),
                timeout_seconds=60.0, stdout_limit=64 * 1024, stderr_limit=256 * 1024,
                cancelled=ctx.cancelled, env=tools.child_env(),
            )
            if result.returncode != 0 or not target.is_file():
                target.unlink(missing_ok=True)
                continue  # a frame that will not decode is skipped, not invented
            rows.append((ctx.asset_id, index, ordinal, at, str(target), target.stat().st_size))
            produced += 1
        ctx.db.execute("BEGIN IMMEDIATE")
        try:
            ctx.db.executemany(
                "INSERT INTO keyframes(asset_id, scene_index, ordinal, time_ms, path, bytes) VALUES (?,?,?,?,?,?)", rows
            )
            ctx.db.execute("COMMIT")
        except BaseException:
            ctx.db.execute("ROLLBACK")
            raise
        ctx.checkpoint = {"next_scene": index + 1}
        ctx.save_checkpoint(ctx.checkpoint)
    return lambda db: None


# ----------------------------------------------------------------------- asr

def run_asr(ctx: StageContext) -> Commit:
    """Chunked whisper.cpp transcription of one chosen audio stream (see :mod:`qronos_media.asr`)."""
    from core.config import CONFIG
    from qronos_media.model_authority import ModelLease

    lease = ctx.lease()
    if not isinstance(lease, ModelLease) or lease.spec is not WHISPER_LARGE_V3_TURBO_Q5_0:
        raise ResourceRefused("model_lease_required", "asr")
    if asr.skip_reason(ctx.db, ctx.asset_id) is not None:
        # Decided again under the lease: subtitle evidence is never overwritten.
        return _asr_cleared(ctx.asset_id)
    assets = asr.locate_whisper(CONFIG.paths.runtime_assets)  # AsrUnavailable: nothing starts
    tools = ctx.require_tools()
    choice = asr.choose_audio(ctx.db, ctx.asset_id)
    asset_sha = ctx.asset()["sha256"]
    key = asr.plan_key(asset_sha, choice, assets)
    stored = {row["chunk_index"]: row for row in ctx.db.execute(
        "SELECT chunk_index, segments_json, child_json FROM asr_chunks WHERE asset_id = ? AND plan_key = ? "
        "ORDER BY chunk_index", (ctx.asset_id, key))}
    chunks: list[tuple[int, list]] = []
    index, eof = 0, False
    while index in stored:  # resume: reuse every finished chunk, transcribe none twice
        row = stored[index]
        chunks.append((index, [tuple(s) for s in json.loads(row["segments_json"])]))
        eof = bool(json.loads(row["child_json"]).get("eof"))
        index += 1
        if eof:
            break
    video = ctx.primary_file()
    while not eof:
        if index >= asr.MAX_CHUNKS:
            raise StageFailed("audio did not end within the chunk limit")
        ctx.checkpoint_boundary()
        ctx.renew()
        wav, out_base, out_json = asr.owned_paths(ctx.cache_dir, ctx.asset_id, index)
        try:
            video = ctx.reopen(video)  # each chunk is a new decoder open: admitted again now
            start_ms = choice.offset_ms + index * asr.CHUNK_MS
            extracted = run_bounded(
                asr.launcher(tools.audio_chunk_argv(video, stream_index=choice.stream_index,
                                                    start_seconds=start_ms / 1000.0,
                                                    length_seconds=asr.CHUNK_MS / 1000.0, output=wav)),
                timeout_seconds=max(120.0, asr.CHUNK_MS / 1000.0), stdout_limit=64 * 1024,
                stderr_limit=256 * 1024, cancelled=ctx.cancelled, env=tools.child_env(),
            )
            if extracted.returncode != 0 or not wav.is_file():
                raise StageFailed(f"audio extraction failed for chunk {index}")
            length = asr.wav_duration_ms(wav)
            segments: list = []
            child_info: dict = {"eof": length < asr.CHUNK_MS - asr.CHUNK_EOF_SLACK_MS, "lengthMs": length}
            if length > 0:
                try:
                    result = run_bounded(
                        asr.launcher(asr.whisper_argv(assets, wav, out_base, asr.whisper_language(choice.language))),
                        timeout_seconds=max(300.0, length / 1000.0 * 10.0), stdout_limit=1024 * 1024,
                        stderr_limit=1024 * 1024, cancelled=ctx.cancelled, env=asr.whisper_env(assets),
                        limits=asr.ASR_CHILD_LIMITS, observer=lease, cwd=asr.whisper_cwd(assets),
                    )
                except ChildTerminationUnproven:
                    raise ResourceRefused("termination_unproven", f"asr chunk {index}") from None
                if result.returncode != 0 or not out_json.is_file():
                    raise StageFailed(f"speech recognition failed for chunk {index} (exit {result.returncode})")
                segments = asr.parse_whisper_json(out_json.read_bytes(), length)
                child_info.update({"pid": result.pid, "peakMemoryBytes": result.peak_memory_bytes})
            eof = bool(child_info["eof"])
            chunk_row = (ctx.asset_id, key, index, start_ms, length, json.dumps(segments), json.dumps(child_info),
                         time.time())

            def write(db: sqlite3.Connection, chunk_row: tuple = chunk_row) -> None:
                db.execute(
                    "INSERT OR REPLACE INTO asr_chunks(asset_id, plan_key, chunk_index, audio_offset_ms, length_ms, "
                    "segments_json, child_json, created_at) VALUES (?,?,?,?,?,?,?,?)", chunk_row)

            ctx.checkpoint = {"plan": key, "next_chunk": index + 1, "eof": eof}
            ctx.save_checkpoint(ctx.checkpoint, write)  # chunk result and checkpoint: one transaction
            chunks.append((index, segments))
            index += 1
        finally:
            for leftover in (wav, out_json):
                leftover.unlink(missing_ok=True)
    rows = asr.build_rows(ctx.asset_id, choice, chunks)
    record = asr.provenance(asset_sha, choice, assets, key, len(chunks))
    tid = asr.track_id(ctx.asset_id, choice)

    def commit(db: sqlite3.Connection) -> None:
        _delete_asr(db, ctx.asset_id)
        db.execute(
            "INSERT INTO subtitle_tracks(track_id, asset_id, source, stream_index, language, title, codec, forced, "
            "encoding, status, detail, cue_count, provenance_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, ctx.asset_id, "asr", choice.stream_index, choice.language, "ASR (unverified)", "whisper.cpp", 0,
             "utf-8", "indexed", record["label"], len(rows), json.dumps(record, sort_keys=True)),
        )
        db.executemany(
            "INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)
        requeue_semantic(db, ctx.asset_id)

    return commit


def _delete_asr(db: sqlite3.Connection, asset_id: str) -> None:
    """Only this asset's ASR track and rows; subtitle evidence is never touched."""
    db.execute("DELETE FROM dialogue WHERE track_id IN "
               "(SELECT track_id FROM subtitle_tracks WHERE asset_id = ? AND source = 'asr')", (asset_id,))
    db.execute("DELETE FROM subtitle_tracks WHERE asset_id = ? AND source = 'asr'", (asset_id,))
    requeue_semantic(db, asset_id)


def requeue_semantic(db: sqlite3.Connection, asset_id: str) -> None:
    """The asset's dialogue changed outside the subtitles stage (ASR): its semantic windows are rebuilt, and so is
    everything built on them (the topic segments), so nothing searchable outlives the dialogue it came from."""
    for stage in downstream("semantic"):
        db.execute("UPDATE stages SET state = 'QUEUED', checkpoint = NULL, error = NULL, "
                   "note = 'invalidated:dialogue', queued_at = ? WHERE asset_id = ? AND stage = ? "
                   "AND state != 'RUNNING'", (time.time(), asset_id, stage))


def _asr_cleared(asset_id: str) -> Commit:
    return lambda db: _delete_asr(db, asset_id)


# ------------------------------------------------------------------ semantic

def run_semantic(ctx: StageContext) -> Commit:
    """bge-m3 windows over the asset's indexed dialogue (see :mod:`qronos_media.semantic`)."""
    from qronos_media.model_authority import ModelLease

    lease = ctx.lease()
    if not isinstance(lease, ModelLease) or lease.spec is not BGE_M3:
        raise ResourceRefused("model_lease_required", "semantic")
    if ctx.embedder is None:
        raise ResourceRefused("model_unavailable", "no bge-m3 sidecar capability in this process")
    binding = ctx.sidecar_binding if ctx.sidecar_binding is not None else ctx.embedder.bind()
    # The slot belongs to the generation captured at admission (a reused resident slot of another generation is
    # refused here, before any request).
    lease.bind_capability(getattr(binding, "capability", None))
    ctx.primary_file()  # the asset's root is admitted now and bound: a revocation stops the work and the commit
    windows = semantic.build_windows(ctx.db, ctx.asset_id)
    keys = sorted({w.cache_key for w in windows})
    present: set[str] = set()
    for start in range(0, len(keys), 500):
        part = keys[start:start + 500]
        present |= {row[0] for row in ctx.db.execute(
            f"SELECT cache_key FROM semantic_cache WHERE cache_key IN ({','.join('?' for _ in part)})", part)}
    missing: dict[str, str] = {}
    for window in windows:
        if window.cache_key not in present:
            missing.setdefault(window.cache_key, window.text)
    todo = list(missing.items())
    done = 0
    for start in range(0, len(todo), semantic.EMBED_BATCH):
        batch = todo[start:start + semantic.EMBED_BATCH]
        ctx.checkpoint_boundary()  # Stop, Pause, deadline, permission, the person's foreground turn
        ctx.renew()
        try:
            vectors = ctx.embedder.embed([text for _, text in batch], on_dispatch=lease.mark_dispatched,
                                         binding=binding)
        except semantic.EmbeddingInvalid as exc:
            raise StageFailed(f"bge-m3 answer rejected: {exc}") from None
        except ResourceRefused as exc:
            if exc.reason == "embedding_request_failed":
                lease.mark_unresolved()  # the server may still be working on it: not proven drained
            raise
        done += len(batch)
        rows = [(key, semantic._fixed_profile()["model"], semantic.model_version(), semantic.DIMENSION, vector,
                 time.time()) for (key, _), vector in zip(batch, vectors)]

        def write(db: sqlite3.Connection, rows: list = rows) -> None:
            db.executemany("INSERT OR IGNORE INTO semantic_cache(cache_key, model, model_version, dim, vector, "
                           "created_at) VALUES (?,?,?,?,?,?)", rows)

        ctx.checkpoint = {"embedded": done, "of": len(todo)}
        ctx.save_checkpoint(ctx.checkpoint, write)  # vectors and checkpoint: one transaction (never searchable yet)

    def commit(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM semantic_windows WHERE asset_id = ?", (ctx.asset_id,))
        db.executemany(
            "INSERT INTO semantic_windows(asset_id, window_key, track_id, source, language, seq_from, seq_to, "
            "start_ms, end_ms, text, cache_key) SELECT ?,?,?,?,?,?,?,?,?,?,? "
            "WHERE EXISTS (SELECT 1 FROM semantic_cache WHERE cache_key = ?) "
            "AND EXISTS (SELECT 1 FROM subtitle_tracks WHERE track_id = ?)",
            [(ctx.asset_id, w.window_key, w.track_id, w.source, w.language, w.seq_from, w.seq_to, w.start_ms,
              w.end_ms, w.text, w.cache_key, w.cache_key, w.track_id) for w in windows])

    return commit


# ------------------------------------------------------------------ topics

def run_topics(ctx: StageContext) -> Commit:
    """Topic segments over the asset's searchable semantic windows (see :mod:`qronos_media.topics`)."""
    ctx.primary_file()  # the asset's root is admitted now and bound: a revocation stops the work and the commit
    segments = topics.build_segments(ctx.db, ctx.asset_id, check=ctx.checkpoint_boundary)
    window_count = sum(len(segment.window_keys) for segment in segments)
    record = json.dumps(topics.provenance(len(segments), window_count), sort_keys=True)

    def commit(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM topic_segments WHERE asset_id = ?", (ctx.asset_id,))
        db.executemany(
            "INSERT INTO topic_segments(asset_id, segment_key, track_id, source, language, start_ms, end_ms, "
            "window_keys, vector, provenance_json) SELECT ?,?,?,?,?,?,?,?,?,? "
            "WHERE EXISTS (SELECT 1 FROM subtitle_tracks WHERE track_id = ?)",
            [(ctx.asset_id, s.segment_key, s.track_id, s.source, s.language, s.start_ms, s.end_ms,
              json.dumps(list(s.window_keys)), s.vector, record, s.track_id) for s in segments])

    return commit


def run_topic_labels(ctx: StageContext) -> Commit:
    """Generated, evidence-cited labels for the asset's topic segments (see :mod:`qronos_media.brain_labels`)."""
    from qronos_media.brain_gateway import prompt_hash

    if ctx.brain is None:
        raise ResourceRefused("model_unavailable", "no governed Fast brain in this process")
    binding = ctx.brain.bind()  # ONE runtime brain for this run; every call checks it is still the current one
    ctx.primary_file()  # the asset's root is admitted now and bound: a revocation stops the work and the commit
    segments = ctx.db.execute(
        "SELECT segment_key, window_keys FROM topic_segments WHERE asset_id = ? ORDER BY start_ms, track_id LIMIT ?",
        (ctx.asset_id, brain_labels.MAX_SEGMENTS_PER_ASSET)).fetchall()
    done: dict = dict(ctx.checkpoint.get("labels", {})) if isinstance(ctx.checkpoint.get("labels"), dict) else {}
    results: dict[str, dict] = {}
    for segment in segments:
        lines = brain_labels.segment_lines(ctx.db, ctx.asset_id, json.loads(segment["window_keys"]))
        digest = brain_labels.input_hash(segment["segment_key"], lines)
        earlier = done.get(segment["segment_key"])
        if isinstance(earlier, dict) and earlier.get("inputSha256") == digest:
            results[segment["segment_key"]] = earlier  # resumed: the same input was already answered
            continue
        if not lines:
            results[segment["segment_key"]] = {"status": "rejected", "reason": "no_lines", "inputSha256": digest}
            continue
        ctx.checkpoint_boundary()  # Stop, Pause, deadline, permission, the person's foreground turn
        ctx.renew()
        user = brain_labels.label_prompt(lines)
        answer = ctx.brain.ask(binding, brain_labels.LABEL_SYSTEM, user)
        record = brain_labels.provenance(binding, prompt_digest=prompt_hash(brain_labels.LABEL_SYSTEM, user),
                                         input_digest=digest)
        try:
            parsed = brain_labels.parse_label(answer, lines)
            results[segment["segment_key"]] = {"status": "labelled", **parsed, "provenance": record,
                                               "inputSha256": digest}
        except brain_labels.LabelRejected as rejected:
            results[segment["segment_key"]] = {"status": "rejected", "reason": rejected.reason, "provenance": record,
                                               "inputSha256": digest}
        ctx.checkpoint = {"labels": results}
        ctx.save_checkpoint(ctx.checkpoint)  # resume never asks the brain twice for the same segment input

    def commit(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM topic_labels WHERE asset_id = ?", (ctx.asset_id,))
        rows = []
        for key, result in results.items():
            record = result.get("provenance") or {"kind": "topic_label", "generated": False, "verified": False,
                                                  "inputSha256": result.get("inputSha256")}
            labelled = result["status"] == "labelled"
            rows.append((ctx.asset_id, key, result["status"], result.get("label") if labelled else None,
                         result.get("summary") if labelled else None,
                         json.dumps(result.get("cites")) if labelled else None, result.get("reason"),
                         json.dumps(record, sort_keys=True), ctx.asset_id, key))
        # Only segments that still exist (the same keys the lines came from) get a row.
        db.executemany(
            "INSERT INTO topic_labels(asset_id, segment_key, status, label, summary, cites_json, reason, "
            "provenance_json) SELECT ?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM topic_segments WHERE asset_id = ? "
            "AND segment_key = ?)", rows)

    return commit


def topic_labels_skip_reason(db: sqlite3.Connection, asset_id: str) -> str | None:
    has_segments = db.execute("SELECT 1 FROM topic_segments WHERE asset_id = ? LIMIT 1", (asset_id,)).fetchone()
    return None if has_segments else "no_topic_segments"


def _delete_topic_labels(db: sqlite3.Connection, asset_id: str) -> None:
    db.execute("DELETE FROM topic_labels WHERE asset_id = ?", (asset_id,))


# ------------------------------------------------------------ visual evidence (qronos_media.visual_evidence)


def _frame_bytes(directory: Path, row: sqlite3.Row) -> bytes | str:
    """The stored keyframe's bytes from media's own cache, or why it cannot be used (never the person's file)."""
    try:
        path = owned_child(directory, Path(row["path"]).name)
    except UnsafeMediaPath:
        return "frame_path_unsafe"
    if os.path.normcase(str(path)) != os.path.normcase(str(Path(row["path"]))):
        return "frame_path_mismatch"
    try:
        data = path.read_bytes() if path.is_file() else b""
    except OSError:
        return "frame_unreadable"
    if not data:
        return "frame_missing"
    if len(data) != row["bytes"] or len(data) > visual_evidence.MAX_FRAME_BYTES:
        return "frame_changed"
    return data


def _text_check(ctx: StageContext, video: Path, row: sqlite3.Row, directory: Path, binding: object,
                lease: object) -> dict:
    """The selective deeper read: the same moment decoded once at TEXT_CHECK_WIDTH px, asked for its text only."""
    from core.vision_image import ImageUnusable, prepare_bytes

    if ctx.tools is None:
        return {"skipped": "decoder_unavailable"}
    directory.mkdir(parents=True, exist_ok=True)
    target = owned_child(directory, "check_%06d_%d.jpg" % (row["scene_index"], row["ordinal"]))
    try:
        opened = ctx.reopen(video)  # each decoder open is admitted again
        result = run_bounded(
            ctx.tools.keyframe_argv(opened, time_seconds=row["time_ms"] / 1000.0, output=target,
                                    width=visual_evidence.TEXT_CHECK_WIDTH),
            timeout_seconds=60.0, stdout_limit=64 * 1024, stderr_limit=256 * 1024,
            cancelled=ctx.cancelled, env=ctx.tools.child_env(),
        )
        if result.returncode != 0 or not target.is_file():
            return {"skipped": "decode_failed"}
        data = target.read_bytes()
    finally:
        target.unlink(missing_ok=True)
    try:
        image = prepare_bytes(data)
    except ImageUnusable:
        return {"skipped": "frame_unusable"}
    ctx.checkpoint_boundary()
    ctx.renew()
    answer = ctx.vision.look(binding, lease, image, visual_evidence.TEXT_INSTRUCTION, visual_evidence.TEXT_SCHEMA)
    try:
        return {"lines": visual_evidence.parse_text_check(answer),
                "frameSha256": hashlib.sha256(data).hexdigest(), "widthPx": visual_evidence.TEXT_CHECK_WIDTH}
    except visual_evidence.EvidenceRejected as rejected:
        return {"skipped": "rejected:" + rejected.reason}


def run_visual_evidence(ctx: StageContext) -> Commit:
    """Generated, frame-cited visual evidence for the asset's stored keyframes (see qronos_media.visual_evidence)."""
    from core.vision_image import ImageUnusable, prepare_bytes
    from qronos_media.model_authority import ModelLease

    lease = ctx.lease()
    if not isinstance(lease, ModelLease) or lease.spec is not QWEN3_VL_4B:
        raise ResourceRefused("model_lease_required", "visual_evidence")
    if ctx.vision is None:
        raise ResourceRefused("model_unavailable", "no runtime Vision in this process")
    binding = ctx.vision.bind()  # ONE runtime for this run; every request checks it is still the current one
    lease.bind_capability(binding.capability)  # the slot is freed only on THAT runtime's unload proof
    video = ctx.primary_file()  # the asset's root is admitted now and bound: a revocation stops the work and the commit
    asset = checked_asset_id(ctx.asset_id)
    keyframe_dir = owned_child(ctx.cache_dir, "keyframes", asset)
    check_dir = owned_child(ctx.cache_dir, "visual", asset)
    if check_dir.is_dir():  # a decoder killed mid-write leaves a file of no use: remove this stage's own leftovers
        for leftover in check_dir.glob("check_*.jpg*"):
            owned_child(check_dir, leftover.name).unlink(missing_ok=True)
    rows = ctx.db.execute("SELECT scene_index, ordinal, time_ms, path, bytes FROM keyframes WHERE asset_id = ?",
                          (ctx.asset_id,)).fetchall()
    frames = visual_evidence.select_frames(rows)
    done = ctx.checkpoint.get("frames") if isinstance(ctx.checkpoint.get("frames"), dict) else {}
    results: dict[str, dict] = {}
    checks = 0
    for row in frames:
        key = "%d:%d" % (row["scene_index"], row["ordinal"])
        base = {"timeMs": row["time_ms"], "sceneIndex": row["scene_index"], "ordinal": row["ordinal"]}
        data = _frame_bytes(keyframe_dir, row)
        if isinstance(data, str):  # no model call for a frame that is not exactly the stored one
            results[key] = {"status": "rejected", "reason": data, "frameSha256": "", **base}
            continue
        digest = hashlib.sha256(data).hexdigest()
        base["frameSha256"] = digest
        earlier = done.get(key)
        if isinstance(earlier, dict) and earlier.get("frameSha256") == digest:
            results[key] = earlier  # resumed: this exact frame was already answered
            checks += 1 if earlier.get("textCheck") else 0
            continue
        try:
            image = prepare_bytes(data)
        except ImageUnusable:
            results[key] = {"status": "rejected", "reason": "frame_unusable", **base}
            continue
        ctx.checkpoint_boundary()  # Stop, Pause, deadline, permission, the person's foreground turn
        ctx.renew()
        answer = ctx.vision.look(binding, lease, image, visual_evidence.VISUAL_INSTRUCTION,
                                 visual_evidence.VISUAL_SCHEMA)
        record = visual_evidence.provenance(binding, frame_sha256=digest, frame_bytes=len(data),
                                            time_ms=row["time_ms"], scene_index=row["scene_index"],
                                            ordinal=row["ordinal"])
        try:
            parsed = visual_evidence.parse_evidence(answer)
        except visual_evidence.EvidenceRejected as rejected:
            results[key] = {"status": "rejected", "reason": rejected.reason, **base, "provenance": record}
        else:
            check = None
            if parsed["visibleText"] and checks < visual_evidence.MAX_TEXT_CHECKS:
                checks += 1
                second = _text_check(ctx, video, row, check_dir, binding, lease)
                check = ({"reads": visual_evidence.compare_reads(parsed["visibleText"], second["lines"]),
                          "secondFrameSha256": second["frameSha256"], "widthPx": second["widthPx"]}
                         if "lines" in second else second)
                record = visual_evidence.provenance(binding, frame_sha256=digest, frame_bytes=len(data),
                                                    time_ms=row["time_ms"], scene_index=row["scene_index"],
                                                    ordinal=row["ordinal"], text_check=True)
            results[key] = {"status": "described", **parsed, **base, "textCheck": check, "provenance": record}
        ctx.checkpoint = {"frames": results}
        ctx.save_checkpoint(ctx.checkpoint)  # resume never asks again for the same frame bytes

    def commit(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM visual_evidence WHERE asset_id = ?", (ctx.asset_id,))
        rows_out = []
        for result in results.values():
            described = result["status"] == "described"
            record = result.get("provenance") or {"kind": "visual_evidence", "generated": False, "verified": False,
                                                  "visualVersion": visual_evidence.VISUAL_VERSION}
            lines = result.get("visibleText", []) if described else []
            observations = result.get("observations", []) if described else []
            rows_out.append((
                ctx.asset_id, result["sceneIndex"], result["ordinal"], result["timeMs"], result["frameSha256"],
                result["status"], result.get("caption") if described else None,
                json.dumps(lines, ensure_ascii=False) if described else None,
                json.dumps(observations, ensure_ascii=False) if described else None,
                json.dumps(result.get("textCheck"), ensure_ascii=False) if described and result.get("textCheck")
                else None,
                result.get("reason"), json.dumps(record, sort_keys=True, ensure_ascii=False),
                visual_evidence.search_text(result.get("caption"), lines, observations) if described else "",
                ctx.asset_id, result["sceneIndex"], result["ordinal"]))
        # Only frames whose keyframe row still exists get a row (the same rows the bytes were read from).
        db.executemany(
            "INSERT INTO visual_evidence(asset_id, scene_index, ordinal, time_ms, frame_sha256, status, caption, "
            "visible_text_json, observations_json, text_check_json, reason, provenance_json, norm_text) "
            "SELECT ?,?,?,?,?,?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM keyframes WHERE asset_id = ? "
            "AND scene_index = ? AND ordinal = ?)", rows_out)

    return commit


def visual_evidence_skip_reason(db: sqlite3.Connection, asset_id: str) -> str | None:
    has_frames = db.execute("SELECT 1 FROM keyframes WHERE asset_id = ? LIMIT 1", (asset_id,)).fetchone()
    return None if has_frames else "no_keyframes"


def _delete_visual_evidence(db: sqlite3.Connection, asset_id: str) -> None:
    db.execute("DELETE FROM visual_evidence WHERE asset_id = ?", (asset_id,))


def run_visual_semantic(ctx: StageContext) -> Commit:
    """bge-m3 vectors of the stored GENERATED visual text (see qronos_media.visual_evidence): text, never pixels."""
    from qronos_media.model_authority import ModelLease

    lease = ctx.lease()
    if not isinstance(lease, ModelLease) or lease.spec is not BGE_M3:
        raise ResourceRefused("model_lease_required", "visual_semantic")
    if ctx.embedder is None:
        raise ResourceRefused("model_unavailable", "no bge-m3 sidecar capability in this process")
    binding = ctx.sidecar_binding if ctx.sidecar_binding is not None else ctx.embedder.bind()
    lease.bind_capability(getattr(binding, "capability", None))
    ctx.primary_file()  # the asset's root is admitted now and bound: a revocation stops the work and the commit
    rows = ctx.db.execute("SELECT * FROM visual_evidence WHERE asset_id = ? AND status = 'described' ORDER BY time_ms",
                          (ctx.asset_id,)).fetchall()
    items = []
    for row in rows:
        lines = json.loads(row["visible_text_json"]) if row["visible_text_json"] else []
        seen = json.loads(row["observations_json"]) if row["observations_json"] else []
        text = visual_evidence.embedding_text(row["caption"], lines, seen)
        if text:
            items.append((row, text, semantic.cache_key(text)))
    keys = sorted({key for _, _, key in items})
    present: set[str] = set()
    for start in range(0, len(keys), 500):
        part = keys[start:start + 500]
        present |= {r[0] for r in ctx.db.execute(
            f"SELECT cache_key FROM semantic_cache WHERE cache_key IN ({','.join('?' for _ in part)})", part)}
    missing: dict[str, str] = {}
    for _, text, key in items:
        if key not in present:
            missing.setdefault(key, text)
    todo = list(missing.items())
    done = 0
    for start in range(0, len(todo), semantic.EMBED_BATCH):
        batch = todo[start:start + semantic.EMBED_BATCH]
        ctx.checkpoint_boundary()  # Stop, Pause, deadline, permission, the person's foreground turn
        ctx.renew()
        try:
            vectors = ctx.embedder.embed([text for _, text in batch], on_dispatch=lease.mark_dispatched,
                                         binding=binding)
        except semantic.EmbeddingInvalid as exc:
            raise StageFailed(f"bge-m3 answer rejected: {exc}") from None
        except ResourceRefused as exc:
            if exc.reason == "embedding_request_failed":
                lease.mark_unresolved()  # the server may still be working on it: not proven drained
            raise
        done += len(batch)
        cached = [(key, semantic._fixed_profile()["model"], semantic.model_version(), semantic.DIMENSION, vector,
                   time.time()) for (key, _), vector in zip(batch, vectors)]

        def write(db: sqlite3.Connection, cached: list = cached) -> None:
            db.executemany("INSERT OR IGNORE INTO semantic_cache(cache_key, model, model_version, dim, vector, "
                           "created_at) VALUES (?,?,?,?,?,?)", cached)

        ctx.checkpoint = {"embedded": done, "of": len(todo)}
        ctx.save_checkpoint(ctx.checkpoint, write)  # vectors and checkpoint: one transaction (never searchable yet)

    def commit(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM visual_semantic WHERE asset_id = ?", (ctx.asset_id,))
        # Only where the cache vector exists and the visual row is still the same described frame.
        db.executemany(
            "INSERT INTO visual_semantic(asset_id, scene_index, ordinal, time_ms, frame_sha256, cache_key, text, "
            "provenance_json) SELECT ?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM semantic_cache WHERE cache_key = ?) "
            "AND EXISTS (SELECT 1 FROM visual_evidence WHERE asset_id = ? AND scene_index = ? AND ordinal = ? "
            "AND frame_sha256 = ? AND status = 'described')",
            [(ctx.asset_id, row["scene_index"], row["ordinal"], row["time_ms"], row["frame_sha256"], key, text,
              json.dumps(visual_evidence.semantic_provenance(row, key, text), sort_keys=True, ensure_ascii=False),
              key, ctx.asset_id, row["scene_index"], row["ordinal"], row["frame_sha256"]) for row, text, key in items])

    return commit


def run_image_embeddings(ctx: StageContext) -> Commit:
    """Image vectors of the stored keyframes' PIXELS (see qronos_media.image_embeddings); never of a caption."""
    from qronos_media.model_authority import ModelLease

    lease = ctx.lease()
    if not isinstance(lease, ModelLease) or lease.spec is not SIGLIP2_BASE_224_INT8:
        raise ResourceRefused("model_lease_required", "image_embeddings")
    if ctx.image_encoder is None:
        raise ResourceRefused("model_unavailable", "no image encoder in this process")
    ctx.primary_file()  # the asset's root is admitted now and bound: a revocation stops the work and the commit
    current = image_embeddings.fingerprint()
    keyframe_dir = owned_child(ctx.cache_dir, "keyframes", checked_asset_id(ctx.asset_id))
    rows = ctx.db.execute("SELECT scene_index, ordinal, time_ms, path, bytes FROM keyframes WHERE asset_id = ? "
                          "ORDER BY scene_index, ordinal", (ctx.asset_id,)).fetchall()
    frames: list[tuple[sqlite3.Row, str, int]] = []
    for row in rows:  # first pass: which exact frame is stored (the bytes are not kept in memory)
        ctx.checkpoint_boundary()
        data = _frame_bytes(keyframe_dir, row)
        if not isinstance(data, str):
            frames.append((row, hashlib.sha256(data).hexdigest(), len(data)))
    digests = sorted({digest for _, digest, _ in frames})
    present: set[str] = set()
    for start in range(0, len(digests), 500):
        part = digests[start:start + 500]
        present |= {r[0] for r in ctx.db.execute(
            "SELECT frame_sha256 FROM image_vector_cache WHERE fingerprint = ? AND frame_sha256 IN (%s)"
            % ",".join("?" for _ in part), [current, *part])}
    todo: dict[str, sqlite3.Row] = {}
    for row, digest, _ in frames:
        if digest not in present:
            todo.setdefault(digest, row)  # the same frame twice is embedded once
    pending = list(todo.items())
    done = 0
    if pending:  # everything already stored (a resume, an unchanged asset): no model is loaded at all
        try:
            with ctx.image_encoder.open(lease, image=True) as encoder:
                for start in range(0, len(pending), image_embeddings.IMAGE_BATCH):
                    batch = pending[start:start + image_embeddings.IMAGE_BATCH]
                    ctx.checkpoint_boundary()  # Stop, Pause, deadline, permission, the person's foreground turn
                    ctx.renew()
                    arrays, kept = [], []
                    for digest, row in batch:
                        data = _frame_bytes(keyframe_dir, row)
                        if isinstance(data, str) or hashlib.sha256(data).hexdigest() != digest:
                            continue  # changed since the first pass: never embedded under the old SHA-256
                        try:
                            arrays.append(image_embeddings.preprocess_frame(data))
                        except image_embeddings.FrameUnusable:
                            continue
                        kept.append(digest)
                    if not arrays:
                        continue
                    try:
                        vectors = encoder.encode_frames(arrays)
                    except image_embeddings.EncoderOutputInvalid as exc:
                        raise StageFailed(f"image encoder answer rejected: {exc}") from None
                    done += len(kept)
                    cached = [(current, digest, image_embeddings.DIMENSION, vector, time.time())
                              for digest, vector in zip(kept, vectors)]

                    def write(db: sqlite3.Connection, cached: list = cached) -> None:
                        db.executemany("INSERT OR IGNORE INTO image_vector_cache(fingerprint, frame_sha256, dim, vector, "
                                       "created_at) VALUES (?,?,?,?,?)", cached)

                    ctx.checkpoint = {"embedded": done, "of": len(pending)}
                    ctx.save_checkpoint(ctx.checkpoint, write)  # vectors and checkpoint: one transaction (not searchable yet)
        except image_embeddings.EncoderRuntimeFailed as failed:
            # The runtime failed: the row ends FAILED and the runner carries on; no vector of this run is
            # committed (the lease and its sessions are already closed by the provider).
            raise StageFailed(f"image encoder failed: {failed.kind}") from None

    def commit(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM image_embeddings WHERE asset_id = ?", (ctx.asset_id,))
        # Only frames whose vector exists for the CURRENT fingerprint and whose keyframe row is still the one read.
        db.executemany(
            "INSERT INTO image_embeddings(asset_id, scene_index, ordinal, time_ms, frame_sha256, fingerprint, "
            "provenance_json) SELECT ?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM image_vector_cache WHERE fingerprint = ? "
            "AND frame_sha256 = ?) AND EXISTS (SELECT 1 FROM keyframes WHERE asset_id = ? AND scene_index = ? "
            "AND ordinal = ? AND bytes = ?)",
            [(ctx.asset_id, row["scene_index"], row["ordinal"], row["time_ms"], digest, current,
              json.dumps(image_embeddings.row_provenance(digest, size, row["time_ms"], row["scene_index"],
                                                         row["ordinal"]), sort_keys=True),
              current, digest, ctx.asset_id, row["scene_index"], row["ordinal"], size)
             for row, digest, size in frames])

    return commit


def image_embeddings_skip_reason(db: sqlite3.Connection, asset_id: str) -> str | None:
    stored = db.execute("SELECT 1 FROM keyframes WHERE asset_id = ? LIMIT 1", (asset_id,)).fetchone()
    return None if stored else "no_keyframes"


def _delete_image_embeddings(db: sqlite3.Connection, asset_id: str) -> None:
    db.execute("DELETE FROM image_embeddings WHERE asset_id = ?", (asset_id,))


def visual_semantic_skip_reason(db: sqlite3.Connection, asset_id: str) -> str | None:
    described = db.execute("SELECT 1 FROM visual_evidence WHERE asset_id = ? AND status = 'described' LIMIT 1",
                           (asset_id,)).fetchone()
    return None if described else "no_visual_evidence"


def _delete_visual_semantic(db: sqlite3.Connection, asset_id: str) -> None:
    db.execute("DELETE FROM visual_semantic WHERE asset_id = ?", (asset_id,))


def topics_skip_reason(db: sqlite3.Connection, asset_id: str) -> str | None:
    has_windows = db.execute("SELECT 1 FROM semantic_windows WHERE asset_id = ? LIMIT 1", (asset_id,)).fetchone()
    return None if has_windows else "no_semantic_windows"


def _delete_topics(db: sqlite3.Connection, asset_id: str) -> None:
    db.execute("DELETE FROM topic_segments WHERE asset_id = ?", (asset_id,))


def semantic_skip_reason(db: sqlite3.Connection, asset_id: str) -> str | None:
    has_dialogue = db.execute(
        "SELECT 1 FROM dialogue d JOIN subtitle_tracks t ON t.track_id = d.track_id WHERE d.asset_id = ? "
        "AND d.kind = 'cue' AND t.status = 'indexed' LIMIT 1", (asset_id,)).fetchone()
    return None if has_dialogue else "no_dialogue"


def _delete_semantic(db: sqlite3.Connection, asset_id: str) -> None:
    db.execute("DELETE FROM semantic_windows WHERE asset_id = ?", (asset_id,))
    _delete_topics(db, asset_id)  # segments never outlive the windows they are made of


#: Stages that decide, before any resource is reserved, that an asset needs nothing now (the skip note).
SKIPS: dict[str, Callable[[sqlite3.Connection, str], object]] = {"asr": asr.skip_reason,
                                                                  "semantic": semantic_skip_reason,
                                                                  "topics": topics_skip_reason,
                                                                  "topic_labels": topic_labels_skip_reason,
                                                                  "visual_evidence": visual_evidence_skip_reason,
                                                                  "visual_semantic": visual_semantic_skip_reason,
                                                                  "image_embeddings": image_embeddings_skip_reason}
#: What a skipped stage removes (rows from an earlier run that no longer apply), in the same transaction.
SKIP_CLEANUP: dict[str, Callable[[sqlite3.Connection, str], None]] = {"asr": _delete_asr,
                                                                       "semantic": _delete_semantic,
                                                                       "topics": _delete_topics,
                                                                       "topic_labels": _delete_topic_labels,
                                                                       "visual_evidence": _delete_visual_evidence,
                                                                       "visual_semantic": _delete_visual_semantic,
                                                                       "image_embeddings": _delete_image_embeddings}

RUNNERS: dict[str, Callable[[StageContext], Commit]] = {
    "metadata": run_metadata,
    "subtitles": run_subtitles,
    "asr": run_asr,
    "semantic": run_semantic,
    "topics": run_topics,
    "topic_labels": run_topic_labels,
    "scenes": run_scenes,
    "keyframes": run_keyframes,
    "visual_evidence": run_visual_evidence,
    "visual_semantic": run_visual_semantic,
    "image_embeddings": run_image_embeddings,
}
