"""M-P5: every subtitle and decoder read is its own bounded, admitted handoff.

- Oversize SRT/VTT/ASS/SSA are refused from their size before any read or
  decoder spawn (zero-filled files just over the real 16 MiB default; never
  read in full by the code under test).
- Sidecar reads are chunked and stop between chunks for pause, Stop, the
  deadline, permission loss and lease loss; the stored hash is of the bytes
  actually parsed, and a file that changes while read is refused.
- Every decoder spawn and every sidecar admits its copy again, with a fresh
  gate answer for that copy's own root. A refusal there requeues the stage as
  ``refused`` (typed, with the reason), never a crash or a stuck RUNNING row.
- Work that finishes under a tripped watch (pause, revocation) is requeued,
  not committed.

Production limits and guards are unchanged; time is a fake monotonic clock
where it matters. Synthetic fixtures only.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.approval_ledger import ApprovalLedger
from core.permission_control import set_permission_policy
from qronos_media import stages as stages_module
from qronos_media.permissions import GatePermissions
from qronos_media.stages import SIDECAR_READ_CHUNK, read_bounded
from qronos_media.store import MediaStore
from qronos_media.subtitles import MAX_SUBTITLE_BYTES, SubtitleError
from qronos_media.tools import ChildCancelled
from tests.media.media_support import (  # noqa: F401
    FakePermissions, base_video, copy_video, make_service, requires_ffmpeg, write_srt,
)
from tests.test_media_scope import FakeClock, _approve, _index, policy  # noqa: F401


def _oversize(path: Path) -> Path:
    with open(path, "wb") as handle:
        handle.truncate(MAX_SUBTITLE_BYTES + 1)  # zero-filled; the code under test must not read it
    return path


def _stage(service, asset: str, stage: str) -> tuple:
    row = service.store.db.execute("SELECT state, note FROM stages WHERE asset_id = ? AND stage = ?",
                                   (asset, stage)).fetchone()
    return tuple(row)


# ------------------------------------------------------------ bounded read

def test_bounded_read_stops_between_chunks(tmp_path: Path) -> None:
    path = tmp_path / "big.srt"
    path.write_bytes(b"x" * (4 * SIDECAR_READ_CHUNK))
    calls = {"n": 0}

    def stop_on_third() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    with pytest.raises(ChildCancelled):
        read_bounded(path, MAX_SUBTITLE_BYTES, stop_on_third)
    assert calls["n"] == 3  # two chunks were read, the third was never requested
    with pytest.raises(SubtitleError, match="too large"):
        read_bounded(path, SIDECAR_READ_CHUNK, lambda: False)  # grew past the size it was admitted with
    assert read_bounded(path, MAX_SUBTITLE_BYTES, lambda: False) == path.read_bytes()


# ---------------------------------------------------------- oversize refusal

@requires_ffmpeg
def test_oversize_sidecars_are_refused_before_any_read_or_decode(tmp_path, base_video, monkeypatch) -> None:
    library = tmp_path / "library"
    copy_video(base_video, library / "Film.mkv")
    big = [_oversize(library / name) for name in ("Film.en.srt", "Film.fa.vtt", "Film.de.ass", "Film.fr.ssa")]
    write_srt(library / "Film.es.srt", [("00:00:00,500", "00:00:01,500", "hola pequeño")])
    reads: list[str] = []
    spawned: list[str] = []
    real_read, real_run = stages_module.read_bounded, stages_module.run_bounded

    def counting_read(path, *args, **kwargs):
        reads.append(Path(path).name)
        return real_read(path, *args, **kwargs)

    def recording_run(argv, **kwargs):
        spawned.append(" ".join(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(stages_module, "read_bounded", counting_read)
    monkeypatch.setattr(stages_module, "run_bounded", recording_run)
    service = make_service(tmp_path / "data")
    service.scan(service.add_root(str(library))["rootId"])
    _index(service)
    tracks = {Path(row["sidecar_path"]).name: dict(row) for row in service.store.db.execute(
        "SELECT * FROM subtitle_tracks WHERE source = 'sidecar'")}
    for path in big:
        assert tracks[path.name]["status"] == "failed" and "too large" in tracks[path.name]["detail"]
        assert tracks[path.name]["cue_count"] == 0
        assert path.name not in reads
        assert not any(path.name in argv for argv in spawned)
    assert reads == ["Film.es.srt"] and tracks["Film.es.srt"]["cue_count"] == 1
    assert service.search_dialogue("hola pequeño", mode="exact")["results"]
    service.close()


# ----------------------------------------------------- in-flight sidecar read

@requires_ffmpeg
def test_a_pause_during_a_sidecar_read_requeues_the_stage(tmp_path, base_video, monkeypatch) -> None:
    library = tmp_path / "library"
    copy_video(base_video, library / "Film.mkv")
    write_srt(library / "Film.en.srt", [("00:00:00,500", "00:00:01,500", "paused line")])
    clock = FakeClock()
    service = make_service(tmp_path / "data", monotonic=clock)
    service.scan(service.add_root(str(library))["rootId"])
    service.index(max_seconds=60, max_stages=1)  # metadata only
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    other = MediaStore(service.layout.database)
    real_read = stages_module.read_bounded

    def pausing_read(path, limit, cancelled):
        other.set_meta("paused", "1")  # another process pauses indexing as the read begins
        clock.advance(1.0)  # past the watch's real 0.5 s poll interval
        return real_read(path, limit, cancelled)

    monkeypatch.setattr(stages_module, "read_bounded", pausing_read)
    report = service.index(max_seconds=60)
    assert report["stopped"] == "paused"
    assert _stage(service, asset, "subtitles") == ("QUEUED", "paused")
    assert service.store.db.execute("SELECT COUNT(*) FROM subtitle_tracks").fetchone()[0] == 0
    other.close()
    service.close()


@requires_ffmpeg
def test_a_sidecar_changed_while_read_is_refused_and_its_hash_is_of_the_parsed_bytes(tmp_path, base_video,
                                                                                    monkeypatch) -> None:
    import hashlib

    library = tmp_path / "library"
    copy_video(base_video, library / "Film.mkv")
    stable = write_srt(library / "Film.en.srt", [("00:00:00,500", "00:00:01,500", "stable line")])
    moving = write_srt(library / "Film.fa.srt", [("00:00:00,500", "00:00:01,500", "moving line")])
    real_read = stages_module.read_bounded

    def appending_read(path, limit, cancelled):
        data = real_read(path, limit, cancelled)
        if Path(path).name == moving.name:
            with open(path, "ab") as handle:  # changed after the bytes were taken
                handle.write(b"\n2\n00:00:02,000 --> 00:00:03,000\nlate line\n")
        return data

    monkeypatch.setattr(stages_module, "read_bounded", appending_read)
    service = make_service(tmp_path / "data")
    service.scan(service.add_root(str(library))["rootId"])
    _index(service)
    tracks = {Path(r["sidecar_path"]).name: dict(r) for r in service.store.db.execute(
        "SELECT * FROM subtitle_tracks WHERE source = 'sidecar'")}
    assert tracks[stable.name]["sidecar_sha256"] == hashlib.sha256(stable.read_bytes()).hexdigest()
    assert tracks[moving.name]["status"] == "failed" and "changed" in tracks[moving.name]["detail"]
    assert service.search_dialogue("moving line", mode="exact")["results"] == []
    service.close()


# ---------------------------------------------------- per-handoff admission

@requires_ffmpeg
def test_a_grant_expiring_between_two_copies_refuses_the_stage_instead_of_crashing(tmp_path, base_video, policy,
                                                                                   monkeypatch) -> None:
    """Genuine ALWAYS_ASK approvals with the gate's own lifetime; B expires while A's sidecar is read."""
    set_permission_policy("files", "ALWAYS_ASK")
    clock = FakeClock()
    permissions = GatePermissions(ledger=ApprovalLedger(), monotonic=clock)
    service = make_service(tmp_path / "data", permissions=permissions, monotonic=clock)
    folder_a = copy_video(base_video, tmp_path / "a_root" / "film.mkv").parent
    folder_b = copy_video(base_video, tmp_path / "b_root" / "film.mkv").parent
    write_srt(folder_a / "film.en.srt", [("00:00:00,500", "00:00:01,500", "from a")])
    write_srt(folder_b / "film.fa.srt", [("00:00:00,500", "00:00:01,500", "from b")])
    root_b = _approve(service, folder_b, 0)["rootId"]   # approved first: expires first
    clock.advance(100.0)
    root_a = _approve(service, folder_a, 0)["rootId"]
    service.scan(root_a)
    service.scan(root_b)
    _index(service)
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    clock.advance(70.0)  # B's approval is 170 s old, A's 70 s
    read: list[str] = []
    real_read = stages_module.read_bounded

    def expiring_read(path, limit, cancelled):
        read.append(str(path))
        data = real_read(path, limit, cancelled)
        clock.advance(15.0)  # B's approval expires (185 s) while A's sidecar is read
        return data

    monkeypatch.setattr(stages_module, "read_bounded", expiring_read)
    service.reindex(asset, "subtitles")
    report = service.index(max_seconds=600)  # must return, not raise
    assert read == [str(folder_a / "film.en.srt")]  # B's sidecar was never read
    assert any(item.startswith(f"{asset}/subtitles:") for item in report["refused"])
    state, note = _stage(service, asset, "subtitles")
    assert state == "QUEUED" and note.startswith("refused:")
    service.close()


@requires_ffmpeg
def test_every_decoder_spawn_admits_its_copy_again(tmp_path, base_video, monkeypatch) -> None:
    """A revocation between two scene chunks stops the next spawn at once (no 2 s in-flight wait)."""
    library = copy_video(base_video, tmp_path / "library" / "film.mkv").parent
    permissions = FakePermissions()
    service = make_service(tmp_path / "data", permissions=permissions)
    service.scan(service.add_root(str(library))["rootId"])
    service.index(max_seconds=60, max_stages=2)  # metadata, subtitles
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    monkeypatch.setattr(stages_module, "SCENE_CHUNK_SECONDS", 1.5)  # several chunks (tuning, not a guard)
    spawns: list[str] = []
    real_run = stages_module.run_bounded

    def revoking_run(argv, **kwargs):
        if "scdet" in " ".join(argv):
            spawns.append(argv[argv.index("-ss") + 1])
            result = real_run(argv, **kwargs)
            permissions.refuse_roots[str(library)] = "revoked"  # right after the first chunk
            return result
        return real_run(argv, **kwargs)

    monkeypatch.setattr(stages_module, "run_bounded", revoking_run)
    report = service.index(max_seconds=60)
    assert spawns == ["0.000"]
    assert any(item.startswith(f"{asset}/scenes:") for item in report["refused"])
    assert _stage(service, asset, "scenes")[0] == "QUEUED"
    service.close()


@requires_ffmpeg
def test_each_sidecar_beside_one_copy_is_admitted_with_a_fresh_answer(tmp_path, base_video, policy,
                                                                       monkeypatch) -> None:
    """The approval expires 1 s into reading the first sidecar: inside the 2 s in-flight cache window,
    so only the fresh per-sidecar admission can stop the second read."""
    set_permission_policy("files", "ALWAYS_ASK")
    clock = FakeClock()
    permissions = GatePermissions(ledger=ApprovalLedger(), monotonic=clock)
    service = make_service(tmp_path / "data", permissions=permissions, monotonic=clock)
    folder = copy_video(base_video, tmp_path / "selected" / "film.mkv").parent
    write_srt(folder / "film.en.srt", [("00:00:00,500", "00:00:01,500", "first sidecar")])
    write_srt(folder / "film.fa.srt", [("00:00:00,500", "00:00:01,500", "second sidecar")])
    service.scan(_approve(service, folder, 0)["rootId"])
    _index(service)
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    # 0.5 s of the approval's lifetime left (measured from its recorded expiry, so the real
    # seconds spent indexing above do not matter).
    held = permissions._root_grants[str(folder)]
    clock.advance(held.expires_at - clock() - 0.5)
    read: list[str] = []
    real_read = stages_module.read_bounded

    def ageing_read(path, limit, cancelled):
        read.append(Path(path).name)
        data = real_read(path, limit, cancelled)
        clock.advance(1.0)  # expired now, 1 s after the stage's fresh answer
        return data

    monkeypatch.setattr(stages_module, "read_bounded", ageing_read)
    service.reindex(asset, "subtitles")
    report = service.index(max_seconds=600)
    assert read == ["film.en.srt"]
    assert any(item.startswith(f"{asset}/subtitles:") for item in report["refused"])
    service.close()


@requires_ffmpeg
@pytest.mark.parametrize("deny_point", ["after_first_sidecar", "after_first_hash"])
def test_a_real_files_deny_between_sidecar_opens_stops_the_next_open(tmp_path, base_video, policy, monkeypatch,
                                                                     deny_point) -> None:
    """Owner form of Installer r6 case (c), on this design's hooks (hash = full_sha256, read = read_bounded).
    A frozen clock makes every handoff instantaneous, so only a fresh ask at each open can see the DENY."""
    set_permission_policy("files", "ALLOW")
    clock = FakeClock()
    frozen = clock()
    clock_value = lambda: frozen  # noqa: E731  (never advances)
    service = make_service(tmp_path / "data", permissions=GatePermissions(monotonic=clock_value),
                           monotonic=clock_value)
    folder = copy_video(base_video, tmp_path / "selected" / "film.mkv").parent
    write_srt(folder / "film.en.srt", [("00:00:00,200", "00:00:00,900", "english")])
    write_srt(folder / "film.fa.srt", [("00:00:00,200", "00:00:00,900", "farsi")])
    service.scan(service.add_root(str(folder), max_depth=0)["rootId"])
    _index(service)
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    service.reindex(asset, "subtitles")
    opens: list[tuple[str, str]] = []
    denied = {"at": None}
    real_hash, real_read = stages_module.full_sha256, stages_module.read_bounded

    def deny() -> None:
        if denied["at"] is None:
            set_permission_policy("files", "DENY")
            denied["at"] = len(opens)

    def recording_hash(path, *args, **kwargs):
        opens.append(("hash", Path(path).name))
        digest = real_hash(path, *args, **kwargs)
        if deny_point == "after_first_hash":
            deny()
        return digest

    def recording_read(path, *args, **kwargs):
        opens.append(("read", Path(path).name))
        data = real_read(path, *args, **kwargs)
        if deny_point == "after_first_sidecar":
            deny()
        return data

    monkeypatch.setattr(stages_module, "full_sha256", recording_hash)
    monkeypatch.setattr(stages_module, "read_bounded", recording_read)
    report = service.index(max_seconds=60)  # must return: a typed stop, not an exception
    assert denied["at"] is not None
    assert opens[denied["at"]:] == [], opens
    assert report["stopped"] == "permission"
    assert _stage(service, asset, "subtitles")[0] == "QUEUED"
    service.close()


# ------------------------------------------------- nothing commits under a tripped watch

@requires_ffmpeg
@pytest.mark.parametrize("trip", ["pause", "deny"])
def test_work_finished_under_a_tripped_watch_is_requeued_not_committed(tmp_path, base_video, policy, monkeypatch,
                                                                       trip) -> None:
    """The trip lands while the stage's last decoder child runs, so no chunk boundary follows it:
    only the look immediately before commit can see it (Installer r6 R5)."""
    set_permission_policy("files", "ALLOW")
    folder = copy_video(base_video, tmp_path / "selected" / "film.mkv").parent
    service = make_service(tmp_path / "data", permissions=GatePermissions())
    service.scan(service.add_root(str(folder), max_depth=0)["rootId"])
    _index(service)
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    service.reindex(asset, "scenes")
    monkeypatch.setattr(stages_module, "SCENE_CHUNK_SECONDS", 600.0)  # one chunk: no boundary after it
    other = MediaStore(service.layout.database)  # "another process"
    real = stages_module.run_bounded

    def tripping(argv, **kwargs):
        result = real(argv, **kwargs)
        if "scdet" in " ".join(argv):
            if trip == "pause":
                other.set_meta("paused", "1")
            else:
                set_permission_policy("files", "DENY")
        return result

    monkeypatch.setattr(stages_module, "run_bounded", tripping)
    report = service.index(max_seconds=60)
    assert f"{asset}/scenes" not in report["completed"]
    assert report["stopped"] == ("paused" if trip == "pause" else "permission"), report
    assert _stage(service, asset, "scenes")[0] == "QUEUED"
    other.close()
    service.close()
