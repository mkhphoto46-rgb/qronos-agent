"""One asset timeline for cuts, subtitles and seeks; and search that stays bounded.

Timeline fixtures are generated: the container starts at 10 s, the frame rate
changes at the cut (10 then 25 fps), and the only colour change is at 2.0 s of
playback. Expected values are those generated or authored times, measured on
these fixtures only.
"""
from __future__ import annotations

from pathlib import Path
import time

import pytest

from qronos_media import search as search_module
from qronos_media import stages as stages_module
from qronos_media.search import MAX_CANDIDATE_WORDS, MAX_WORD_CHARS, search_dialogue, similarity
from qronos_media.store import MediaStore
from tests.media.media_support import (  # noqa: F401
    CUT_MS, EMBEDDED_CUES, TOOLS, copy_video, make_service, make_video, requires_ffmpeg, write_srt,
)


@pytest.fixture(scope="module")
def shifted_video(tmp_path_factory) -> Path:
    if TOOLS is None:
        pytest.skip(requires_ffmpeg.kwargs["reason"])
    directory = tmp_path_factory.mktemp("media-shifted")
    subtitle = write_srt(directory / "embedded.srt", EMBEDDED_CUES)
    return make_video(directory / "shifted.mkv", embedded_srt=subtitle, rates=(10, 25), start_offset=10.0)


def _index_one(tmp_path: Path, video: Path, sidecar: str | None = None):
    library = tmp_path / "library"
    film = copy_video(video, library / "Shifted.mkv")
    if sidecar is not None:
        (library / "Shifted.en.srt").write_text(sidecar, encoding="utf-8")
    service = make_service(tmp_path / "data")
    service.add_root(str(library))
    service.scan()
    report = service.index(max_seconds=120)
    assert report["failed"] == [], report
    asset = service.store.db.execute("SELECT * FROM assets").fetchone()
    return service, film, asset


# ---------------------------------------------------------------- timeline

@requires_ffmpeg
def test_shifted_vfr_file_is_measured_on_the_asset_timeline(tmp_path: Path, shifted_video: Path) -> None:
    service, _, asset = _index_one(tmp_path, shifted_video)
    assert asset["start_offset_ms"] == 10_000
    assert abs(asset["video_end_ms"] - 4_000) <= 100  # measured, whatever the container's duration claim says
    scenes = service.store.db.execute(
        "SELECT start_ms, end_ms FROM scenes WHERE asset_id = ? ORDER BY scene_index", (asset["asset_id"],)).fetchall()
    assert [s["start_ms"] for s in scenes][0] == 0 and len(scenes) == 2
    assert abs(scenes[1]["start_ms"] - CUT_MS) <= 100
    assert scenes[1]["end_ms"] == asset["video_end_ms"]
    frames = service.get_scene(asset["asset_id"], 1)["keyframes"]
    assert frames and scenes[1]["start_ms"] <= frames[0]["time_ms"] < scenes[1]["end_ms"]
    streams = {row["kind"]: row for row in service.store.db.execute(
        "SELECT kind, start_ms, time_base FROM streams WHERE asset_id = ?", (asset["asset_id"],))}
    assert streams["video"]["start_ms"] == 10_000 and streams["video"]["time_base"] == "1/1000"
    service.close()


@requires_ffmpeg
def test_embedded_cues_and_seeks_share_the_asset_timeline(tmp_path: Path, shifted_video: Path) -> None:
    service, _, asset = _index_one(tmp_path, shifted_video)
    hit = service.search_dialogue("I never asked for this", mode="exact")["results"][0]
    assert (hit["startMs"], hit["endMs"]) == (2_200, 3_500)  # not 12 200: the container start is removed
    assert hit["timeline"].startswith("asset") and "normalised" in hit["timing"]
    assert hit["scene"]["scene_index"] == 1
    plan = service.open_timestamp(asset["asset_id"], hit["startMs"])
    assert plan["startSeconds"] == 2.2 and plan["hashVerified"]
    service.close()


@requires_ffmpeg
def test_chunking_does_not_move_the_cut(tmp_path: Path, shifted_video: Path, monkeypatch) -> None:
    monkeypatch.setattr(stages_module, "SCENE_CHUNK_SECONDS", 1.5)
    service, _, asset = _index_one(tmp_path, shifted_video)
    starts = [row[0] for row in service.store.db.execute(
        "SELECT start_ms FROM scenes WHERE asset_id = ? ORDER BY scene_index", (asset["asset_id"],))]
    assert len(starts) == 2 and abs(starts[1] - CUT_MS) <= 100
    assert abs(asset["video_end_ms"] - 4_000) <= 100
    service.close()


@requires_ffmpeg
def test_sidecar_offsets_are_kept_as_authored_and_labelled(tmp_path: Path, shifted_video: Path) -> None:
    late = "1\n00:00:03,200 --> 00:00:04,500\nI never asked for this\n"  # the same line, one second later
    service, _, _ = _index_one(tmp_path, shifted_video, sidecar=late)
    hits = service.search_dialogue("I never asked for this", mode="exact")["results"]
    by_source = {hit["track"]["source"]: hit for hit in hits}
    assert by_source["embedded"]["startMs"] == 2_200 and by_source["sidecar"]["startMs"] == 3_200
    assert "not verified" in by_source["sidecar"]["timing"] and "this exact cut" in by_source["sidecar"]["timing"]
    service.close()


def test_scene_parsing_uses_the_last_progress_time() -> None:
    stderr = (b"frame=   10 fps=0.0 q=-0.0 size=N/A time=00:00:01.00 bitrate=N/A\r"
              b"[Parsed_scdet_1 @ 0] lavfi.scd.score: 15.625, lavfi.scd.time: 0.5\n"
              b"frame=   60 fps=0.0 q=-0.0 Lsize=N/A time=01:02:03.48 bitrate=N/A\n")
    assert stages_module.parse_decoded_seconds(stderr) == pytest.approx(3723.48)
    assert stages_module.parse_scene_cuts(stderr, 1.5) == [(2_000, 15.625)]
    assert stages_module.parse_decoded_seconds(b"no progress") is None


# ---------------------------------------------------------- search bounds

def _bulk_index(path: Path, texts: list[str]) -> MediaStore:
    from qronos_media.text import normalise_for_search

    store = MediaStore(path)
    with store.transaction() as db:
        db.execute("INSERT INTO assets(asset_id, sha256, size, created_at, updated_at) "
                   "VALUES ('a_00000000000000000000000000000001', 'h', 1, 0, 0)")
        db.execute("INSERT INTO subtitle_tracks(track_id, asset_id, source, status) "
                   "VALUES ('t', 'a_00000000000000000000000000000001', 'sidecar', 'indexed')")
        db.executemany(
            "INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
            "VALUES ('a_00000000000000000000000000000001', 't', 'cue', ?, ?, ?, ?, ?)",
            [(i, i * 1000, i * 1000 + 900, text, normalise_for_search(text)) for i, text in enumerate(texts)],
        )
    return store


def test_common_term_queries_return_within_budget(tmp_path: Path) -> None:
    texts = ["the the the the the the the the"] * 20_000 + ["they thought the theatre was there"]
    store = _bulk_index(tmp_path / "i.sqlite3", texts)
    for mode in ("exact", "fuzzy", "auto"):
        started = time.monotonic()
        result = search_dialogue(store.db, "the the the the", mode=mode, budget_seconds=0.5)
        elapsed = time.monotonic() - started
        assert elapsed < 3.0, (mode, elapsed)
        assert result["status"] in {"complete", "partial"}
        if result["status"] == "partial":
            assert result["stoppedBy"] in {"time_budget", "work_budget"} or "common_terms" in result["notes"]
        assert all(hit["confidence"] == "uncalibrated" for hit in result["results"])
    store.close()


def test_long_repetitive_cue_text_is_bounded() -> None:
    query = " ".join(["word"] * 40)
    text = " ".join(["word" * 50] * 1_000)
    started = time.monotonic()
    score = similarity(query, text)
    assert time.monotonic() - started < 2.0
    assert 0.0 <= score <= 1.0
    assert MAX_CANDIDATE_WORDS <= 64 and MAX_WORD_CHARS <= 64


def test_exhausted_work_budget_is_a_typed_partial_result(tmp_path: Path, monkeypatch) -> None:
    texts = [f"he left when i was {n} years old" for n in range(500)]
    store = _bulk_index(tmp_path / "i.sqlite3", texts)
    monkeypatch.setattr(search_module, "MAX_RATIO_CALLS", 3)
    result = search_dialogue(store.db, "he left when I was nine", mode="fuzzy")
    assert result["status"] == "partial" and result["stoppedBy"] == "work_budget"
    store.close()


def test_budget_must_be_bounded(tmp_path: Path) -> None:
    store = _bulk_index(tmp_path / "i.sqlite3", ["x y z"])
    for bad in (0.0, 60.0, float("nan")):
        with pytest.raises(search_module.SearchError):
            search_dialogue(store.db, "x y z", budget_seconds=bad)
    assert search_dialogue(store.db, "x y z")["status"] == "complete"
    store.close()


def test_evidence_for_many_copies_is_bounded(tmp_path: Path) -> None:
    store = _bulk_index(tmp_path / "i.sqlite3", ["he left when i was nine"])
    with store.transaction() as db:
        db.execute("INSERT INTO roots(root_id, path, kind, approval_action_id, approval_decision, approved_at, "
                   "max_depth) VALUES (1, ?, 'folder', 'a', 'allow', 0, 1)", (str(tmp_path),))
        db.executemany(
            "INSERT INTO files(path, root_id, size, mtime_ns, file_id, asset_id, status, seen_at) "
            "VALUES (?, 1, 1, 1, 1, 'a_00000000000000000000000000000001', 'present', 0)",
            [(str(tmp_path / f"missing-copy-{n:03d}.mkv"),) for n in range(200)],
        )
    hit = search_dialogue(store.db, "left when i was nine", mode="exact")["results"][0]
    assert len(hit["paths"]) + len(hit["stalePaths"]) == search_module.MAX_EVIDENCE_PATHS
    assert hit["pathsTruncated"] and not hit["available"]
    store.close()


def test_a_database_locked_by_another_connection_is_a_typed_partial(tmp_path: Path) -> None:
    import sqlite3

    from qronos_media.store import open_read_only

    path = tmp_path / "i.sqlite3"
    _bulk_index(path, ["he left when i was nine"]).close()
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("PRAGMA locking_mode = EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("UPDATE meta SET value = value WHERE key = 'schema_version'")
    try:
        reader = open_read_only(path)
        started = time.monotonic()
        result = search_dialogue(reader, "he left when i was nine", budget_seconds=0.3)
        assert time.monotonic() - started < 3.0
        assert result["status"] == "partial" and result["stoppedBy"] == "busy" and result["results"] == []
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 5000  # restored afterwards
        reader.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
