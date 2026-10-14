"""The media index end to end on generated containers: scan, identity, stages, search, control.

Expected times are the fixture's own authored or generated values: subtitle
cues as written, and the colour change at CUT_MS. Nothing here measures or
claims accuracy on real films.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

import pytest

from core.activity_guard import ActivityMode, ResourcePressure
from qronos_media import stages as stages_module
from qronos_media import tools as tools_module
from qronos_media.jobs import RunnerLock
from qronos_media.permissions import MediaPermissionRefused
from qronos_media.resources import StageNeeds, StandaloneCpuAuthority, ResourceRefused
from qronos_media.service import MediaRequestError
from qronos_media.store import MediaStore
from tests.media.media_support import (  # noqa: F401
    CUT_MS, TOOLS, FakePermissions, base_video, copy_video, make_service, quiet_activity, requires_ffmpeg, write_srt,
)

pytestmark = requires_ffmpeg


def _library(tmp_path: Path, base: Path) -> Path:
    library = tmp_path / "library"
    copy_video(base, library / "Film One.mkv")
    copy_video(base, library / "nested" / "deeper" / "Film One (copy).mkv")
    (library / "notes.txt").write_text("not a video", encoding="utf-8")
    (library / "Film One.fa.vtt").write_text(
        "WEBVTT\n\n00:00:00.400 --> 00:00:01.500\nمن هرگز این را نخواستم\n\n"
        "00:00:02.000 --> 00:00:03.000\nپدرش او را ترک کرد\n", encoding="utf-8")
    return library


def _indexed(tmp_path: Path, base: Path, **kwargs):
    library = _library(tmp_path, base)
    service = make_service(tmp_path / "data", **kwargs)
    service.add_root(str(library))
    service.scan()
    report = service.index(max_seconds=120)
    return service, library, report


def _states(service, asset_id: str) -> dict[str, str]:
    return {row[0]: row[1] for row in service.store.db.execute(
        "SELECT stage, state FROM stages WHERE asset_id = ?", (asset_id,))}


def _only_asset(service) -> str:
    rows = service.store.db.execute("SELECT asset_id FROM assets").fetchall()
    assert len(rows) == 1
    return rows[0][0]


# --------------------------------------------------------------- pipeline

def test_duplicates_become_one_asset_and_every_stage_completes(tmp_path: Path, base_video: Path) -> None:
    service, library, report = _indexed(tmp_path, base_video)
    asset = _only_asset(service)
    assert report["hashed"] == 2 and report["failed"] == [] and report["stopped"] == "idle"
    # Current graph (rounds 24/26): ASR completes as a skip because subtitles exist; semantic needs the runtime's
    # bge-m3 embedder, which this service does not have, so it stays queued as unavailable (never reserved).
    assert sorted(report["completed"]) == sorted(f"{asset}/{s}" for s in ("metadata", "subtitles", "asr", "scenes",
                                                                          "keyframes"))
    notes = {row[0]: (row[1], row[2]) for row in service.store.db.execute(
        "SELECT stage, state, note FROM stages WHERE asset_id = ?", (asset,))}
    assert notes["asr"] == ("COMPLETED", "skipped:subtitles_present")
    assert notes["semantic"] == ("QUEUED", "unavailable:model_unavailable")
    # visual_evidence needs the runtime's Vision, which this service does not have either (never reserved);
    # visual_semantic waits for it.
    assert notes["visual_evidence"] == ("QUEUED", "unavailable:model_unavailable")
    assert notes["visual_semantic"][0] == "QUEUED"
    assert report["refused"] == [f"{asset}/semantic:model_unavailable", f"{asset}/visual_evidence:model_unavailable"]
    files = service.get_asset(asset)["files"]
    assert {Path(f["path"]).name for f in files} == {"Film One.mkv", "Film One (copy).mkv"}
    tracks = {t["track_id"].split(":")[1][0]: t for t in service.get_asset(asset)["subtitleTracks"]}
    assert tracks["e"]["language"] == "fa" and tracks["e"]["cue_count"] == 2
    assert tracks["s"]["language"] == "fa" and tracks["s"]["cue_count"] == 2
    service.close()


def test_shot_boundary_and_keyframes_match_the_generated_cut(tmp_path: Path, base_video: Path) -> None:
    service, _, _ = _indexed(tmp_path, base_video)
    asset = _only_asset(service)
    scenes = service.store.db.execute(
        "SELECT scene_index, start_ms, end_ms FROM scenes WHERE asset_id = ? ORDER BY scene_index", (asset,)).fetchall()
    assert len(scenes) == 2
    assert scenes[0]["start_ms"] == 0 and abs(scenes[0]["end_ms"] - CUT_MS) <= 100
    assert scenes[1]["start_ms"] == scenes[0]["end_ms"]
    frames = service.get_scene(asset, 1)["keyframes"]
    assert len(frames) == 1 and scenes[1]["start_ms"] <= frames[0]["time_ms"] < scenes[1]["end_ms"]
    assert Path(frames[0]["path"]).read_bytes()[:2] == b"\xff\xd8"  # JPEG
    assert service.get_scene(asset, 1)["visualDescription"] is None
    service.close()


def test_exact_search_returns_hash_bound_time_ranged_evidence(tmp_path: Path, base_video: Path) -> None:
    service, _, _ = _indexed(tmp_path, base_video)
    result = service.search_dialogue("I never asked for this", mode="exact")
    assert len(result["results"]) == 1
    hit = result["results"][0]
    sha = service.store.db.execute("SELECT sha256 FROM assets").fetchone()[0]
    assert (hit["sha256"], hit["startMs"], hit["endMs"], hit["match"], hit["unit"]) == (sha, 2_200, 3_500, "exact", "cue")
    assert hit["track"]["source"] == "embedded" and hit["track"]["language"] == "fa"
    assert hit["confidence"] == "uncalibrated" and hit["available"] and len(hit["paths"]) == 2
    assert hit["scene"]["scene_index"] == 1
    # Round 32 stale-claim correction (documented): semantic dialogue is implemented, only not part of this call.
    assert "semantic_dialogue" in result["notInThisCall"] and "semantic_dialogue" not in result["pending"]
    persian = service.search_dialogue("نخواستم", mode="exact")["results"]
    assert [(r["startMs"], r["track"]["source"]) for r in persian] == [(400, "sidecar")]
    service.close()


def test_fuzzy_search_tolerates_misremembered_wording(tmp_path: Path, base_video: Path) -> None:
    service, _, _ = _indexed(tmp_path, base_video)
    assert service.search_dialogue("I never ask for this", mode="exact")["results"] == []
    hits = service.search_dialogue("I never ask for this", mode="auto")["results"]
    assert hits and hits[0]["match"] == "fuzzy" and hits[0]["startMs"] == 2_200
    assert 0.6 <= hits[0]["lexicalScore"] < 1.0 and hits[0]["scoreKind"] == "similarity"
    persian = service.search_dialogue("پدرش ترک کرد", mode="fuzzy")["results"]
    assert persian and persian[0]["startMs"] == 2_000
    assert service.search_dialogue("completely unrelated words", mode="auto")["results"] == []
    service.close()


def test_a_line_split_across_two_cues_is_found_as_a_pair(tmp_path: Path, base_video: Path) -> None:
    library = tmp_path / "library"
    copy_video(base_video, library / "Split.mkv")
    write_srt(library / "Split.en.srt", [("00:00:01,000", "00:00:02,000", "I will never"),
                                          ("00:00:02,300", "00:00:03,000", "leave you behind")])
    service = make_service(tmp_path / "data")
    service.add_root(str(library))
    service.scan()
    service.index(max_seconds=120)
    hits = service.search_dialogue("never leave you", mode="exact", language="en")["results"]
    assert [(h["unit"], h["startMs"], h["endMs"]) for h in hits] == [("pair", 1_000, 3_000)]
    service.close()


def test_search_never_decodes_or_writes(tmp_path: Path, base_video: Path, monkeypatch) -> None:
    service, _, _ = _indexed(tmp_path, base_video)
    database = service.layout.database
    before = service.store.db.execute("SELECT stage, state, attempts FROM stages ORDER BY stage").fetchall()

    def refuse(*args, **kwargs):
        raise AssertionError("search started a child process")

    monkeypatch.setattr(tools_module, "run_bounded", refuse)
    monkeypatch.setattr(stages_module, "run_bounded", refuse)
    for mode in ("exact", "fuzzy", "auto"):
        service.search_dialogue("never asked", mode=mode)
    after = service.store.db.execute("SELECT stage, state, attempts FROM stages ORDER BY stage").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert database.is_file()
    service.close()


# ---------------------------------------------------------- incremental

def test_new_sidecar_requeues_only_subtitles_and_replaces_its_text(tmp_path: Path, base_video: Path) -> None:
    service, library, _ = _indexed(tmp_path, base_video)
    asset = _only_asset(service)
    sidecar = library / "Film One.fa.vtt"
    sidecar.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nکاملاً متن تازه\n", encoding="utf-8")
    summary = service.scan()[0]
    # subtitles and its dependents only (asr, semantic, topics, topic_labels); shots and keyframes are not touched
    assert summary["requeued_stages"] == 5
    assert _states(service, asset) == {"metadata": "COMPLETED", "subtitles": "QUEUED", "asr": "QUEUED",
                                       "semantic": "QUEUED", "topics": "QUEUED", "topic_labels": "QUEUED",
                                       "scenes": "COMPLETED", "keyframes": "COMPLETED",
                                       "visual_evidence": "QUEUED", "visual_semantic": "QUEUED"}  # no Vision here
    report = service.index(max_seconds=120)
    assert report["completed"] == [f"{asset}/subtitles", f"{asset}/asr"]  # asr: a skip (subtitles present)
    assert service.search_dialogue("نخواستم", mode="exact")["results"] == []
    assert service.search_dialogue("متن تازه", mode="exact")["results"][0]["startMs"] == 1_000
    service.close()


def test_stage_version_change_requeues_it_and_its_dependents_only(tmp_path: Path, base_video: Path, monkeypatch) -> None:
    service, _, _ = _indexed(tmp_path, base_video)
    asset = _only_asset(service)
    bumped = stages_module.StageSpec("scenes", 2, ("metadata",), 3, StageNeeds("scenes"))
    monkeypatch.setitem(stages_module.STAGES, "scenes", bumped)
    with service.store.transaction() as db:
        from qronos_media.jobs import ensure_stages
        requeued = ensure_stages(db, asset)
    assert requeued == ["scenes", "keyframes", "visual_evidence", "visual_semantic"]
    assert _states(service, asset)["subtitles"] == "COMPLETED"
    service.close()


def test_swapped_file_with_copied_size_and_mtime_is_rehashed(tmp_path: Path, base_video: Path) -> None:
    service, library, _ = _indexed(tmp_path, base_video)
    old_asset = _only_asset(service)
    film = library / "Film One.mkv"
    original = film.stat()
    replacement = tmp_path / "replacement.mkv"
    data = bytearray(film.read_bytes())
    data[len(data) // 2] ^= 0xFF  # an interior change a head/middle/tail sample could miss
    replacement.write_bytes(bytes(data))
    os.replace(replacement, film)
    os.utime(film, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert film.stat().st_size == original.st_size and film.stat().st_mtime_ns == original.st_mtime_ns
    summary = service.scan()[0]
    assert summary["changed"] == 1
    hit = service.search_dialogue("I never asked for this", mode="exact")["results"][0]
    assert hit["assetId"] == old_asset and [Path(p).name for p in hit["paths"]] == ["Film One (copy).mkv"]
    service.index(max_seconds=120)
    assets = {row[0] for row in service.store.db.execute("SELECT asset_id FROM assets")}
    assert len(assets) == 2 and old_asset in assets
    service.close()


def test_file_changed_after_hashing_never_feeds_the_old_asset(tmp_path: Path, base_video: Path) -> None:
    library = tmp_path / "library"
    film = copy_video(base_video, library / "Only.mkv")
    service = make_service(tmp_path / "data")
    service.add_root(str(library))
    service.scan()
    assert service.index(max_seconds=120, max_stages=0)["hashed"] == 1
    asset = _only_asset(service)
    film.write_bytes(film.read_bytes() + b"\0")  # changes after the hash, before any stage
    report = service.index(max_seconds=120)
    assert report["identityChanged"] == [f"{asset}/metadata"]
    assert _states(service, asset)["metadata"] == "QUEUED"
    row = service.store.db.execute("SELECT status, asset_id FROM files").fetchone()
    assert tuple(row) == ("pending_identity", None)
    report = service.index(max_seconds=120)
    assert report["hashed"] == 1
    new_asset = next(row[0] for row in service.store.db.execute("SELECT asset_id FROM assets WHERE asset_id != ?",
                                                                   (asset,)))
    assert sorted(report["completed"]) == sorted(f"{new_asset}/{s}" for s in ("metadata", "subtitles", "asr",
                                                                              "scenes", "keyframes"))
    assert _states(service, asset)["metadata"] == "QUEUED"  # the old hash got nothing
    service.close()


def test_file_changed_while_a_stage_decodes_is_discarded(tmp_path: Path, base_video: Path, monkeypatch) -> None:
    library = tmp_path / "library"
    film = copy_video(base_video, library / "Only.mkv")
    service = make_service(tmp_path / "data")
    service.add_root(str(library))
    service.scan()
    real = stages_module.RUNNERS["metadata"]

    def tampering(ctx):
        commit = real(ctx)
        film.write_bytes(film.read_bytes() + b"\0")
        return commit

    monkeypatch.setitem(stages_module.RUNNERS, "metadata", tampering)
    report = service.index(max_seconds=120)
    asset = _only_asset(service)
    assert report["identityChanged"] == [f"{asset}/metadata"] and report["completed"] == []
    assert service.store.db.execute("SELECT duration_ms FROM assets").fetchone()[0] is None
    assert service.store.db.execute("SELECT COUNT(*) FROM streams").fetchone()[0] == 0
    service.close()


# --------------------------------------------------------------- control

def test_pause_mid_stage_keeps_checkpoint_and_resume_continues(tmp_path: Path, base_video: Path, monkeypatch) -> None:
    library = tmp_path / "library"
    copy_video(base_video, library / "Only.mkv")
    service = make_service(tmp_path / "data")
    service.add_root(str(library))
    service.scan()
    monkeypatch.setattr(stages_module, "SCENE_CHUNK_SECONDS", 1.5)
    starts: list[str] = []
    pauses = {"left": 1}  # one pause, in the first run; the resumed run must complete
    real = stages_module.run_bounded
    other = MediaStore(service.layout.database)  # "another process" holding its own connection

    def recording(argv, **kwargs):
        if "scdet" in " ".join(argv):
            starts.append(argv[argv.index("-ss") + 1])
            if len(starts) == 2 and pauses["left"]:
                pauses["left"] -= 1
                other.set_meta("paused", "1")
        return real(argv, **kwargs)

    monkeypatch.setattr(stages_module, "run_bounded", recording)
    report = service.index(max_seconds=120)
    asset = _only_asset(service)
    assert report["stopped"] == "paused"
    row = service.store.db.execute(
        "SELECT state, checkpoint, note FROM stages WHERE asset_id = ? AND stage = 'scenes'", (asset,)).fetchone()
    assert row["state"] == "QUEUED" and row["note"] == "paused"
    # The pause landed while chunk 2 decoded; that chunk finished and was
    # checkpointed, and the next chunk boundary stopped the stage.
    assert json.loads(row["checkpoint"])["next_start_ms"] == 3_000
    assert service.index()["stopped"] == "paused"
    other.set_meta("paused", "0")
    starts.clear()
    report = service.index(max_seconds=120)
    assert f"{asset}/scenes" in report["completed"]
    # Resumed at the checkpoint (3.0 s) minus the 1 s overlap, never from 0;
    # a further chunk may probe past the end to confirm the video ended.
    assert starts[0] == "2.000" and "0.000" not in starts
    cuts = [r[0] for r in service.store.db.execute("SELECT start_ms FROM scenes WHERE asset_id = ?", (asset,))]
    assert len(cuts) == 2 and abs(cuts[1] - CUT_MS) <= 100
    other.close()
    service.close()


def test_cancel_and_retry_a_job(tmp_path: Path, base_video: Path) -> None:
    library = tmp_path / "library"
    copy_video(base_video, library / "Only.mkv")
    service = make_service(tmp_path / "data")
    service.add_root(str(library))
    service.scan()
    service.index(max_seconds=120, max_stages=1)
    asset = _only_asset(service)
    assert service.cancel_job(asset, "scenes")["state"] == "CANCELLED"
    report = service.index(max_seconds=120)
    assert f"{asset}/scenes" not in report["completed"] and _states(service, asset)["keyframes"] == "QUEUED"
    assert service.retry(asset, "scenes")["state"] == "QUEUED"
    report = service.index(max_seconds=120)
    assert {f"{asset}/scenes", f"{asset}/keyframes"} <= set(report["completed"])
    with pytest.raises(MediaRequestError):
        service.cancel_job(asset, "no-such-stage")
    service.close()


def test_crashed_runner_rows_are_recovered(tmp_path: Path, base_video: Path) -> None:
    service, _, _ = _indexed(tmp_path, base_video)
    asset = _only_asset(service)
    with service.store.transaction() as db:
        db.execute("UPDATE stages SET state = 'RUNNING' WHERE asset_id = ? AND stage = 'keyframes'", (asset,))
    report = service.index(max_seconds=120)
    assert report["recoveredInterrupted"] == 1 and report["completed"] == [f"{asset}/keyframes"]
    service.close()


def test_second_runner_is_told_busy(tmp_path: Path, base_video: Path) -> None:
    service, _, _ = _indexed(tmp_path, base_video)
    with RunnerLock(service.layout.lock):
        assert service.index()["stopped"] == "busy"
    assert service.index()["stopped"] == "idle"
    service.close()


def test_gaming_mode_defers_indexing_and_gpu_is_never_granted(tmp_path: Path, base_video: Path) -> None:
    library = _library(tmp_path, base_video)
    gaming = lambda: (ActivityMode.GAMING_ASSIST, ResourcePressure.NORMAL)  # noqa: E731
    authority = StandaloneCpuAuthority(tmp_path, activity=gaming, reserve_bytes=0)
    service = make_service(tmp_path / "data", authority=authority)
    service.add_root(str(library))
    service.scan()
    report = service.index(max_seconds=30)
    assert report["stopped"] == "deferred" and "activity_mode" in report["detail"] and report["hashed"] == 0
    assert service.store.db.execute("SELECT COUNT(*) FROM stages WHERE state = 'RUNNING'").fetchone()[0] == 0
    calm = StandaloneCpuAuthority(tmp_path, activity=quiet_activity, reserve_bytes=0)
    with pytest.raises(ResourceRefused) as refused:
        calm.reserve("t", StageNeeds("vision_captions", gpu=True))
    assert refused.value.reason == "requires_qronos_governor"
    service.close()


def test_keyframe_cache_quota_fails_only_that_stage(tmp_path: Path, base_video: Path) -> None:
    service, _, _ = _indexed(tmp_path, base_video, cache_quota_bytes=1)
    asset = _only_asset(service)
    states = _states(service, asset)
    assert states["keyframes"] == "FAILED" and states["subtitles"] == "COMPLETED" and states["scenes"] == "COMPLETED"
    error = service.store.db.execute(
        "SELECT error FROM stages WHERE asset_id = ? AND stage = 'keyframes'", (asset,)).fetchone()[0]
    assert "quota" in error
    assert service.search_dialogue("never asked")["results"]  # the transcript index is unaffected
    service.close()


def test_revoked_root_stops_scan_and_index(tmp_path: Path, base_video: Path) -> None:
    library = _library(tmp_path, base_video)
    permissions = FakePermissions()
    service = make_service(tmp_path / "data", permissions=permissions)
    service.add_root(str(library))
    permissions.refuse_roots[str(library)] = "revoked"
    assert service.scan() == [{"root_id": 1, "root": str(library), "refused": "revoked", "detail": "revoked"}]
    with service.store.transaction() as db:  # files a previous scan found
        db.execute("INSERT INTO files(path, root_id, size, mtime_ns, file_id, status, seen_at) "
                   "VALUES (?, 1, 1, 1, 1, 'pending_identity', 0)", (str(library / "Film One.mkv"),))
    report = service.index(max_seconds=30)
    assert report["hashed"] == 0 and report["refusedRoots"] == {str(library): "revoked"}
    service.close()


def test_removing_a_root_forgets_its_assets_and_cache(tmp_path: Path, base_video: Path) -> None:
    service, _, _ = _indexed(tmp_path, base_video)
    asset = _only_asset(service)
    keyframes = service.layout.cache / "keyframes" / asset
    assert any(keyframes.iterdir())
    result = service.remove_root(1)
    assert result["removedAssets"] == [asset] and not keyframes.exists()
    assert service.status()["assets"] == 0
    assert service.search_dialogue("never asked")["results"] == []
    with pytest.raises(MediaRequestError):
        service.get_asset(asset)
    service.close()


# --------------------------------------------------------------- outputs

def test_reports_come_from_the_index_and_are_escaped(tmp_path: Path, base_video: Path, monkeypatch) -> None:
    library = tmp_path / "library"
    copy_video(base_video, library / "Evil.mkv")
    write_srt(library / "Evil.en.srt", [("00:00:01,000", "00:00:02,000", "<script>alert(1)</script>"),
                                         ("00:00:02,500", "00:00:03,000", "=HYPERLINK(\"http://x\")")])
    service = make_service(tmp_path / "data")
    service.add_root(str(library))
    service.scan()
    service.index(max_seconds=120)
    asset = _only_asset(service)
    monkeypatch.setattr(tools_module, "run_bounded", lambda *a, **k: (_ for _ in ()).throw(AssertionError("decode")))
    monkeypatch.setattr(stages_module, "run_bounded", lambda *a, **k: (_ for _ in ()).throw(AssertionError("decode")))
    html = Path(service.generate_report(asset, fmt="html")["path"]).read_text(encoding="utf-8")
    assert "<script>" not in html and 'lang="fa" dir="rtl"' in html
    csv_text = Path(service.generate_report(asset, fmt="csv", kind="transcript")["path"]).read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in csv_text and "\n=HYPERLINK" not in csv_text
    report = json.loads(Path(service.generate_report(asset, fmt="json")["path"]).read_text(encoding="utf-8"))
    assert report["asset"]["sha256"] == service.store.db.execute("SELECT sha256 FROM assets").fetchone()[0]
    assert {p["capability"] for p in report["levels"]["pending"]} >= {"asr", "visual_embeddings", "actions_motion"}
    assert all(f["matchesHash"] for f in report["files"])
    assert len(report["scenes"]) == 2 and report["dialogue"]
    service.close()


def test_export_report_is_a_gated_new_file(tmp_path: Path, base_video: Path) -> None:
    permissions = FakePermissions()
    service, _, _ = _indexed(tmp_path, base_video, permissions=permissions)
    asset = _only_asset(service)
    destination = tmp_path / "out" / "report.json"
    destination.parent.mkdir()
    permissions.refuse_exports = True
    with pytest.raises(MediaPermissionRefused, match="denied"):
        service.export_report(asset, str(destination))
    assert not destination.exists()
    permissions.refuse_exports = False
    written = service.export_report(asset, str(destination))
    assert json.loads(destination.read_text(encoding="utf-8"))["asset"]["assetId"] == asset
    import hashlib
    assert written["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    with pytest.raises(MediaRequestError):
        service.export_report(asset, str(destination))  # never overwrites
    with pytest.raises(MediaRequestError):
        service.export_report(asset, str(tmp_path / "out" / "report.txt"))
    service.close()


def test_open_timestamp_plans_only_verified_copies(tmp_path: Path, base_video: Path) -> None:
    service, library, _ = _indexed(tmp_path, base_video)
    asset = _only_asset(service)
    player = tmp_path / "players" / "mpv.exe"
    player.parent.mkdir()
    player.write_bytes(b"")  # never executed; the plan is only built
    plan = service.open_timestamp(asset, 2_200, player=str(player))
    assert plan["status"] == "planned" and plan["launch"] == "requires_qronos_approved_executor"
    assert plan["argv"][:2] == [str(player), "--start=2.200"]
    with pytest.raises(MediaRequestError):
        service.open_timestamp(asset, 0, player=str(tmp_path / "players" / "cmd.exe"))
    for film in library.rglob("*.mkv"):
        film.write_bytes(film.read_bytes() + b"\0")
    refused = service.open_timestamp(asset, 2_200)
    assert refused["status"] == "not_verified" and "path" not in refused
    assert refused["verification"]["status"] == "no_copy"  # stat no longer matches: nothing was hashed
    service.close()


def test_status_lists_pending_capabilities_honestly(tmp_path: Path, base_video: Path) -> None:
    service, _, _ = _indexed(tmp_path, base_video)
    status = service.status()
    pending = {item["capability"]: item["status"] for item in status["pending"]}
    assert pending["visual_embeddings"] == "implemented_unverified"  # SigLIP 2 frame vectors (round 79)
    assert pending["actions_motion"] == "not_supported" and pending["asr"] == "implemented_unverified"
    assert pending["semantic_dialogue"] == "implemented_unverified" and pending["topics"] == "implemented_unverified"
    assert status["stages"]["keyframes"] == {"COMPLETED": 1} and status["toolsAvailable"]
    service.close()


# --------------------------------------------------- owned-path deletion

@pytest.mark.skipif(os.name != "nt", reason="junction fixture")
def test_corrupted_asset_id_never_drives_a_delete_outside_the_cache(tmp_path: Path, base_video: Path) -> None:
    service, library, _ = _indexed(tmp_path, base_video)
    sentinel = tmp_path / "data" / "sentinel"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("keep", encoding="utf-8")
    with service.store.transaction() as db:
        db.execute("INSERT INTO assets(asset_id, sha256, size, created_at, updated_at) "
                   "VALUES ('../../sentinel', 'evil', 1, 0, 0)")
        db.execute("INSERT INTO files(path, root_id, size, mtime_ns, file_id, asset_id, status, seen_at) "
                   "VALUES (?, 1, 1, 1, 1, '../../sentinel', 'present', 0)", (str(library / "ghost.mkv"),))
    with pytest.raises(MediaRequestError, match="nothing was removed"):
        service.remove_root(1)
    assert (sentinel / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert len(service.list_roots()) == 1  # the index was not touched either
    service.close()


@pytest.mark.skipif(os.name != "nt", reason="junction fixture")
def test_junction_swapped_into_the_cache_is_not_followed_by_delete(tmp_path: Path, base_video: Path) -> None:
    import _winapi

    service, _, _ = _indexed(tmp_path, base_video)
    asset = _only_asset(service)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    frames = service.layout.cache / "keyframes" / asset
    shutil.rmtree(frames)
    _winapi.CreateJunction(str(outside), str(frames))
    with pytest.raises(MediaRequestError, match="nothing was removed"):
        service.remove_root(1)
    assert (outside / "keep.txt").exists()
    service.close()


@pytest.mark.skipif(os.name != "nt", reason="junction fixture")
def test_data_folder_behind_a_junction_is_refused(tmp_path: Path) -> None:
    import _winapi

    from qronos_media.paths import UnsafeMediaPath

    real = tmp_path / "real"
    real.mkdir()
    _winapi.CreateJunction(str(real), str(tmp_path / "linked"))
    with pytest.raises(UnsafeMediaPath):
        make_service(tmp_path / "linked" / "data", tools=None)
    assert not (real / "data").exists()


# ------------------------------------------------- in-flight observation

def test_pause_is_observed_while_a_file_is_hashing(tmp_path: Path, base_video: Path, monkeypatch) -> None:
    from qronos_media import jobs as jobs_module
    from qronos_media.identity import HashCancelled

    library = tmp_path / "library"
    copy_video(base_video, library / "Only.mkv")
    service = make_service(tmp_path / "data")
    service.add_root(str(library))
    service.scan()
    other = MediaStore(service.layout.database)

    def hashing(path, *, cancelled):
        other.set_meta("paused", "1")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cancelled():
                raise HashCancelled(str(path))
            time.sleep(0.05)
        raise AssertionError("pause was not observed during hashing")

    monkeypatch.setattr(jobs_module, "full_sha256", hashing)
    report = service.index(max_seconds=60)
    assert report["stopped"] == "paused" and report["hashed"] == 0
    assert service.store.db.execute("SELECT status FROM files").fetchone()[0] == "pending_identity"
    other.close()
    service.close()


def test_permission_revoked_mid_decode_stops_and_requeues(tmp_path: Path, base_video: Path, monkeypatch) -> None:
    from qronos_media import jobs as jobs_module

    library = tmp_path / "library"
    copy_video(base_video, library / "Only.mkv")
    permissions = FakePermissions()
    service = make_service(tmp_path / "data", permissions=permissions)
    service.add_root(str(library))
    service.scan()
    monkeypatch.setattr(stages_module, "SCENE_CHUNK_SECONDS", 1.5)
    real = stages_module.run_bounded
    chunks = []

    def revoking(argv, **kwargs):
        if "scdet" in " ".join(argv):
            chunks.append(argv)
            if len(chunks) == 2:
                permissions.refuse_roots[str(library)] = "revoked"
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not kwargs["cancelled"]():
                    time.sleep(0.05)
                raise tools_module.ChildCancelled("revoked during decode")
        return real(argv, **kwargs)

    monkeypatch.setattr(stages_module, "run_bounded", revoking)
    report = service.index(max_seconds=120)
    asset = _only_asset(service)
    assert report["stopped"] == "permission"
    row = service.store.db.execute(
        "SELECT state, note, checkpoint FROM stages WHERE asset_id = ? AND stage = 'scenes'", (asset,)).fetchone()
    assert row["state"] == "QUEUED" and row["note"] == "permission" and row["checkpoint"]
    service.close()


def test_watch_reports_time_budget_and_lost_lease(tmp_path: Path) -> None:
    from qronos_media.jobs import StageRunner, Watch

    class Refusing:
        def renew(self, lease):
            raise ResourceRefused("activity_mode", "gaming")

    store = MediaStore(tmp_path / "i.sqlite3")
    runner = StageRunner(store, tools=None, authority=Refusing(), permissions=FakePermissions(),
                         cache_dir=tmp_path, cache_quota_bytes=1)
    late = Watch(runner, deadline=time.monotonic() - 1, holder=[None], roots=set())
    assert late.now() and late.reason == "time_budget"
    lease = Watch(runner, deadline=time.monotonic() + 60, holder=[None], roots=set())
    lease._renewed -= 3600
    assert lease.now() and lease.reason == "deferred:activity_mode"
    store.close()


# ----------------------------------------------------- strict evidence

def test_verify_asset_rehashes_and_export_requires_a_matching_copy(tmp_path: Path, base_video: Path) -> None:
    permissions = FakePermissions()
    service, library, _ = _indexed(tmp_path, base_video, permissions=permissions)
    asset = _only_asset(service)
    first = service.verify_asset(asset)
    assert first["status"] == "verified" and len(first["verified"]) == 1  # one proven copy is enough
    permissions.ask = True
    destination = tmp_path / "report.json"
    pending = service.export_report(asset, str(destination))
    assert pending["status"] == "approval_required" and not destination.exists()
    written = service.approve_export(pending["approvalId"], pending["decision"])
    assert written["status"] == "written" and destination.is_file()
    with pytest.raises(MediaPermissionRefused):
        service.approve_export(pending["approvalId"], pending["decision"])  # single use
    for film in library.rglob("*.mkv"):
        data = bytearray(film.read_bytes())
        data[-1] ^= 0xFF
        stat = film.stat()
        film.write_bytes(bytes(data))
        os.utime(film, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same size, same mtime, same file id
    after = service.verify_asset(asset)  # only the re-hash notices
    assert after["status"] == "mismatch" and after["verified"] == [] and len(after["mismatched"]) == 2
    permissions.ask = False
    refused = service.export_report(asset, str(tmp_path / "second.json"))
    assert refused["status"] == "not_verified" and not (tmp_path / "second.json").exists()
    assert service.open_timestamp(asset, 0)["status"] == "not_verified"
    service.close()
