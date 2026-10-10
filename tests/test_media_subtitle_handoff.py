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
- The sidecar hash read itself never asks for a byte past the cap + 1: a
  sidecar that grows at or during its hash is refused there (round 9).
- ASS/SSA are decoded from a private, write-locked copy of exactly the
  verified capped bytes, never from the user's file by name; bounded reads
  never ask the operating system past the cap + 1 (round 13).

Production limits and guards are unchanged; time is a fake monotonic clock
where it matters. Synthetic fixtures only.
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
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


# ---------------------------------------- the sidecar hash read itself is byte-bounded (round 9, R1)

_ASS = (
    "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
    "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
    "MarginR, MarginV, Encoding\n"
    "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n"
    "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:00.20,0:00:00.90,Default,,0,0,0,,small ass line\n"
)


class _CountingHandle:
    """Wraps the REAL file object identity.full_sha256 opened; records every request and delivered byte.

    ``furthest`` is the furthest file offset any read asked for (bytes already delivered + bytes requested):
    the bound is that no read ever asks for a byte beyond ``limit + 1``. ``requested`` (a sum over short
    reads) is kept as evidence only."""

    def __init__(self, real, record: dict, after_first_read) -> None:
        self._real, self._record, self._after_first_read = real, record, after_first_read

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._real.close()

    def read(self, size=-1):
        asked = size if size is not None and size >= 0 else 1 << 62
        self._record["requested"] += asked
        self._record["furthest"] = max(self._record["furthest"], self._record["delivered"] + asked)
        data = self._real.read(size)
        self._record["delivered"] += len(data)
        self._record["reads"] += 1
        if self._record["reads"] == 1 and self._after_first_read is not None:
            self._after_first_read()
        return data


def _watch_hash_opens(monkeypatch, target: Path, grow_to: int, when: str) -> dict:
    """Every open identity makes of ``target`` is counted; the file grows (sparse) AT the hash open or
    IN FLIGHT after the first chunk. Below the cap when its size was checked."""
    from qronos_media import identity as identity_module

    record = {"opens": 0, "requested": 0, "furthest": 0, "delivered": 0, "reads": 0}

    def grow() -> None:
        os.truncate(target, grow_to)

    def counting_open(path, mode="r", *args, **kwargs):
        if Path(path) == target:
            record["opens"] += 1
            if when == "at_handoff":
                grow()
            return _CountingHandle(open(path, mode, *args, **kwargs), record,
                                   grow if when == "in_flight" else None)
        return open(path, mode, *args, **kwargs)

    monkeypatch.setattr(identity_module, "open", counting_open, raising=False)
    return record


@requires_ffmpeg
@pytest.mark.parametrize("suffix", [".srt", ".ass"])
@pytest.mark.parametrize("when", ["at_handoff", "in_flight"])
def test_a_sidecar_that_grows_during_its_hash_is_refused_within_cap_plus_one_bytes(tmp_path, base_video, monkeypatch,
                                                                                  suffix, when) -> None:
    import hashlib

    library = tmp_path / "library"
    copy_video(base_video, library / "Film.mkv")
    write_srt(library / "Film.en.srt", [("00:00:00,500", "00:00:01,500", "small srt line")])
    (library / "Film.ja.ass").write_text(_ASS, encoding="utf-8")
    grower = library / f"Film.grow{suffix}"
    if suffix == ".srt":
        write_srt(grower, [("00:00:00,500", "00:00:01,500", "growing line")])
    else:
        grower.write_text(_ASS, encoding="utf-8")
    service = make_service(tmp_path / "data")
    service.scan(service.add_root(str(library))["rootId"])
    _index(service)
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    assert grower.stat().st_size < MAX_SUBTITLE_BYTES

    service.reindex(asset, "subtitles")
    record = _watch_hash_opens(monkeypatch, grower, 2 * MAX_SUBTITLE_BYTES, when)
    reads, spawned = [], []
    real_read, real_run = stages_module.read_bounded, stages_module.run_bounded

    def counting_read(path, *args, **kwargs):
        reads.append(Path(path).name)
        return real_read(path, *args, **kwargs)

    def recording_run(argv, **kwargs):
        spawned.append(" ".join(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(stages_module, "read_bounded", counting_read)
    monkeypatch.setattr(stages_module, "run_bounded", recording_run)
    report = service.index(max_seconds=60)
    tracks = {Path(row["sidecar_path"]).name: dict(row) for row in service.store.db.execute(
        "SELECT * FROM subtitle_tracks WHERE source = 'sidecar'")}

    assert record["opens"] == 1  # the hash opened it once; nothing reopened it
    assert record["furthest"] <= MAX_SUBTITLE_BYTES + 1, record
    assert record["delivered"] <= MAX_SUBTITLE_BYTES + 1, record
    assert tracks[grower.name]["status"] == "failed" and "too large" in tracks[grower.name]["detail"]
    assert tracks[grower.name]["cue_count"] == 0
    assert grower.name not in reads and not any(grower.name in argv for argv in spawned)
    # The run is clean and the unchanged small sidecars keep their exact digest and cues.
    assert f"{asset}/subtitles" in report["completed"] and _stage(service, asset, "subtitles")[0] == "COMPLETED"
    for name in ("Film.en.srt", "Film.ja.ass"):
        assert tracks[name]["status"] != "failed" and tracks[name]["cue_count"] == 1, tracks[name]
        assert tracks[name]["sidecar_sha256"] == hashlib.sha256((library / name).read_bytes()).hexdigest()
    service.close()


def test_the_bounded_hash_is_exact_up_to_the_limit_and_requests_at_most_limit_plus_one(tmp_path, monkeypatch) -> None:
    import hashlib

    from qronos_media import identity as identity_module
    from qronos_media.identity import HashCancelled, HashLimitExceeded, full_sha256

    exact = tmp_path / "exact.bin"
    exact.write_bytes(os.urandom(1000))
    over = tmp_path / "over.bin"
    over.write_bytes(os.urandom(5000))
    record = {"opens": 0, "requested": 0, "furthest": 0, "delivered": 0, "reads": 0}

    def counting_open(path, mode="r", *args, **kwargs):
        record["opens"] += 1
        return _CountingHandle(open(path, mode, *args, **kwargs), record, None)

    monkeypatch.setattr(identity_module, "open", counting_open, raising=False)
    assert full_sha256(exact, limit=1000) == hashlib.sha256(exact.read_bytes()).hexdigest()
    assert record["furthest"] <= 1001
    for key in ("requested", "furthest", "delivered", "reads"):
        record[key] = 0
    with pytest.raises(HashLimitExceeded):
        full_sha256(over, limit=1000)
    assert record["furthest"] <= 1001 and record["delivered"] <= 1001, record
    with pytest.raises(HashCancelled):
        full_sha256(exact, limit=1000, cancelled=lambda: True)
    # No limit: the video identity semantics, a whole multi-chunk file.
    big = tmp_path / "video-like.bin"
    big.write_bytes(os.urandom(5 * 1024 * 1024))
    assert full_sha256(big) == hashlib.sha256(big.read_bytes()).hexdigest()


# ------------------------------- ASS/SSA decode only the verified capped bytes (round 13, Installer r9 F1)

class _RecordingRaw(io.RawIOBase):
    """The REAL FileIO under the buffered reader ``open`` returns; records every raw (OS-level) read."""

    def __init__(self, raw: io.FileIO, log: list) -> None:
        self._raw, self._log = raw, log

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        offset = self._raw.tell()
        count = self._raw.readinto(buffer)
        self._log.append({"offset": offset, "requested": len(buffer), "delivered": count or 0})
        return count

    def close(self) -> None:
        self._raw.close()
        super().close()


def _raw_open(target: Path, log: list):
    """An ``open`` stand-in: the target is opened exactly as open(path, "rb") would, raw reads recorded."""
    def opener(path, mode="r", *args, **kwargs):
        if Path(path) == target and mode == "rb":
            return io.BufferedReader(_RecordingRaw(io.FileIO(path, "rb"), log), io.DEFAULT_BUFFER_SIZE)
        return open(path, mode, *args, **kwargs)
    return opener


def _decoder_spawns(monkeypatch, cache_dir: Path, on_spawn=None) -> list:
    """Record every ffmpeg spawn: its input path, whether it is a private copy, its size and digest at spawn."""
    real_run = stages_module.run_bounded
    spawns: list = []

    def recording_run(argv, **kwargs):
        inputs = [part[len("file:"):] for part in argv if part.startswith("file:")]
        for item in inputs:
            path = Path(item)
            private = path.parent == cache_dir / "subtitle-input"
            spawns.append({"input": item, "private": private, "size": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if private else None})
            if on_spawn is not None and private:
                on_spawn(path)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(stages_module, "run_bounded", recording_run)
    return spawns


def _subtitle_fixture(tmp_path, base_video, name: str, content: str, **service_options):
    library = tmp_path / "library"
    copy_video(base_video, library / "Film.mkv")
    (library / name).write_bytes(content.encode("utf-8"))
    service = make_service(tmp_path / "data", **service_options)
    service.scan(service.add_root(str(library))["rootId"])
    _index(service)
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    service.reindex(asset, "subtitles")
    return service, asset, library / name


def _admissions_of(monkeypatch, target: Path, on_call) -> list:
    """Count fresh sidecar admissions of ``target``: 1 listing, 2 before the hash, 3 before the read, 4 before ffmpeg."""
    calls: list = []
    real_admit = stages_module.admit_sidecar

    def counting_admit(video, raw, excluded):
        admitted = real_admit(video, raw, excluded)
        if Path(raw) == target:
            calls.append(len(calls) + 1)
            on_call(len(calls))
        return admitted

    monkeypatch.setattr(stages_module, "admit_sidecar", counting_admit)
    return calls


def _track(service, path: Path) -> dict:
    row = service.store.db.execute("SELECT status, detail, cue_count, sidecar_sha256 FROM subtitle_tracks "
                                   "WHERE sidecar_path = ?", (str(path),)).fetchone()
    return dict(row) if row else {}


def _private_copies(service) -> list:
    folder = service.layout.cache / "subtitle-input"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


@requires_ffmpeg
@pytest.mark.parametrize("suffix", [".ass", ".ssa"])
def test_an_ass_or_ssa_sidecar_growing_after_its_hash_never_reaches_the_decoder(tmp_path, base_video, monkeypatch,
                                                                              suffix) -> None:
    service, asset, sidecar = _subtitle_fixture(tmp_path, base_video, f"Film.big{suffix}", _ASS)
    admissions = _admissions_of(monkeypatch, sidecar,
                                lambda n: os.truncate(sidecar, 2 * MAX_SUBTITLE_BYTES) if n == 3 else None)
    raw_reads: list = []
    monkeypatch.setattr(stages_module, "open", _raw_open(sidecar, raw_reads), raising=False)
    spawns = _decoder_spawns(monkeypatch, service.layout.cache)
    report = service.index(max_seconds=120)
    track = _track(service, sidecar)
    furthest = max((r["offset"] + r["requested"] for r in raw_reads), default=0)
    delivered = sum(r["delivered"] for r in raw_reads)
    assert admissions == [1, 2, 3], admissions  # it grew AFTER the bounded hash, at the read's fresh admission
    assert [s for s in spawns if Path(s["input"]).resolve() == sidecar.resolve()] == [], "the decoder was handed the user's file by name"
    assert [s for s in spawns if s["size"] > MAX_SUBTITLE_BYTES] == [], spawns
    assert furthest <= MAX_SUBTITLE_BYTES + 1 and delivered <= MAX_SUBTITLE_BYTES + 1, (furthest, delivered)
    assert track["status"] == "failed" and "too large" in track["detail"] and track["cue_count"] == 0, track
    assert f"{asset}/subtitles" in report["completed"] and _private_copies(service) == []
    service.close()


@requires_ffmpeg
def test_a_same_size_substitution_between_hash_and_read_is_refused_before_any_decoder(tmp_path, base_video,
                                                                                    monkeypatch) -> None:
    service, asset, sidecar = _subtitle_fixture(tmp_path, base_video, "Film.sub.ass", _ASS)
    substitute = _ASS.replace("small ass line", "other ass line")
    assert len(substitute) == len(_ASS)
    _admissions_of(monkeypatch, sidecar,
                   lambda n: sidecar.write_bytes(substitute.encode("utf-8")) if n == 3 else None)
    spawns = _decoder_spawns(monkeypatch, service.layout.cache)
    report = service.index(max_seconds=120)
    track = _track(service, sidecar)
    sidecar_spawns = [s for s in spawns if s["private"] or Path(s["input"]).resolve() == sidecar.resolve()]
    assert sidecar_spawns == [], "a decoder ran for a substituted sidecar"
    assert track["status"] == "failed" and "changed" in track["detail"], track
    assert f"{asset}/subtitles" in report["completed"] and _private_copies(service) == []
    service.close()


@requires_ffmpeg
def test_the_decoder_reads_only_the_verified_bytes_even_if_the_original_changes_at_spawn(tmp_path, base_video,
                                                                                         monkeypatch) -> None:
    service, asset, sidecar = _subtitle_fixture(tmp_path, base_video, "Film.swap.ass", _ASS)
    verified = hashlib.sha256(_ASS.encode("utf-8")).hexdigest()
    swapped = _ASS.replace("small ass line", "swapped line!!")

    def swap_original(private: Path) -> None:
        sidecar.write_bytes(swapped.encode("utf-8"))  # the user's file changes while the decoder starts

    spawns = _decoder_spawns(monkeypatch, service.layout.cache, on_spawn=swap_original)
    report = service.index(max_seconds=120)
    track = _track(service, sidecar)
    private = [s for s in spawns if s["private"]]
    assert len(private) == 1 and private[0]["sha256"] == verified, spawns  # exactly the hashed bytes
    assert track["status"] == "failed" and "changed" in track["detail"] and track["cue_count"] == 0, track
    assert f"{asset}/subtitles" in report["completed"] and _private_copies(service) == []
    service.close()


@requires_ffmpeg
@pytest.mark.skipif(os.name != "nt", reason="the write lock is a Windows share mode")
def test_the_private_decoder_copy_cannot_be_written_replaced_or_deleted_while_decoded(tmp_path, base_video,
                                                                                    monkeypatch) -> None:
    service, asset, sidecar = _subtitle_fixture(tmp_path, base_video, "Film.lock.ssa", _ASS)
    attempts: dict = {}
    other = tmp_path / "other.ssa"
    other.write_text("x", encoding="utf-8")

    def tamper(private: Path) -> None:
        for name, action in (("write", lambda: open(private, "r+b").close()),
                             ("append", lambda: open(private, "ab").close()),
                             ("replace", lambda: os.replace(other, private)),
                             ("delete", lambda: os.remove(private))):
            try:
                action()
                attempts[name] = "allowed"
            except PermissionError:
                attempts[name] = "refused"

    spawns = _decoder_spawns(monkeypatch, service.layout.cache, on_spawn=tamper)
    report = service.index(max_seconds=120)
    track = _track(service, sidecar)
    assert attempts == {"write": "refused", "append": "refused", "replace": "refused", "delete": "refused"}, attempts
    assert len([s for s in spawns if s["private"]]) == 1
    assert track["status"] == "indexed" and track["cue_count"] == 1, track
    assert track["sidecar_sha256"] == hashlib.sha256(_ASS.encode("utf-8")).hexdigest()
    assert f"{asset}/subtitles" in report["completed"] and _private_copies(service) == []
    service.close()


@requires_ffmpeg
def test_a_files_deny_after_the_read_stops_before_the_decoder_handoff(tmp_path, base_video, policy,
                                                                     monkeypatch) -> None:
    set_permission_policy("files", "ALLOW")
    clock = FakeClock()
    frozen = clock()
    service, asset, sidecar = _subtitle_fixture(tmp_path, base_video, "Film.deny.ass", _ASS,
                                                permissions=GatePermissions(monotonic=lambda: frozen),
                                                monotonic=lambda: frozen)
    real_read = stages_module.read_bounded

    def read_then_deny(path, *args, **kwargs):
        data = real_read(path, *args, **kwargs)
        if Path(path) == sidecar:
            set_permission_policy("files", "DENY")  # lands after the read, before the native handoff
        return data

    monkeypatch.setattr(stages_module, "read_bounded", read_then_deny)
    spawns = _decoder_spawns(monkeypatch, service.layout.cache)
    report = service.index(max_seconds=60)
    assert [s for s in spawns if s["private"]] == [], "the decoder ran after a real Files DENY"
    assert report["stopped"] == "permission" and _stage(service, asset, "subtitles")[0] == "QUEUED", report
    assert f"{asset}/subtitles" not in report["completed"] and _private_copies(service) == []
    service.close()


@requires_ffmpeg
def test_private_copies_left_by_a_crash_are_swept_and_nothing_else_is_touched(tmp_path, base_video) -> None:
    service, asset, sidecar = _subtitle_fixture(tmp_path, base_video, "Film.ok.ass", _ASS)
    folder = service.layout.cache / "subtitle-input"
    folder.mkdir(parents=True, exist_ok=True)
    stale = folder / f"sub-{'a' * 32}.ass"
    stale.write_text("left by a crash", encoding="utf-8")
    unrelated = folder / "keep-me.txt"
    unrelated.write_text("not ours to delete", encoding="utf-8")
    report = service.index(max_seconds=120)
    assert not stale.exists() and unrelated.exists()
    assert _track(service, sidecar)["status"] == "indexed" and f"{asset}/subtitles" in report["completed"]
    assert _private_copies(service) == ["keep-me.txt"]
    service.close()


@pytest.mark.parametrize("length", [2 * MAX_SUBTITLE_BYTES, MAX_SUBTITLE_BYTES + 1, MAX_SUBTITLE_BYTES])
def test_bounded_reads_never_ask_the_operating_system_past_cap_plus_one(tmp_path, monkeypatch, length) -> None:
    from qronos_media import identity as identity_module
    from qronos_media.identity import HashLimitExceeded, full_sha256

    path = tmp_path / "grown.ass"
    path.write_bytes(b"")
    os.truncate(path, length)
    outcomes, metrics = {}, {}
    for name, module, call in (
            ("hash", identity_module, lambda: full_sha256(path, limit=MAX_SUBTITLE_BYTES)),
            ("read", stages_module, lambda: read_bounded(path, MAX_SUBTITLE_BYTES, lambda: False))):
        log: list = []
        monkeypatch.setattr(module, "open", _raw_open(path, log), raising=False)
        try:
            call()
            outcomes[name] = "accepted"
        except (HashLimitExceeded, SubtitleError):
            outcomes[name] = "refused"
        metrics[name] = (max(r["offset"] + r["requested"] for r in log), sum(r["delivered"] for r in log))
    expected = "refused" if length > MAX_SUBTITLE_BYTES else "accepted"
    assert outcomes == {"hash": expected, "read": expected}, outcomes
    for name, (furthest, delivered) in metrics.items():
        assert furthest <= MAX_SUBTITLE_BYTES + 1 and delivered <= MAX_SUBTITLE_BYTES + 1, (name, furthest, delivered)


@requires_ffmpeg
@pytest.mark.parametrize("suffix", [".srt", ".ass"])
def test_a_same_stat_content_substitution_after_the_hash_is_refused_by_content(tmp_path, base_video, monkeypatch,
                                                                              suffix) -> None:
    """Size, mtime and file id unchanged; only the bytes differ. Only the content digest can see it."""
    original = "1\n00:00:00,500 --> 00:00:01,500\nsmall srt line\n\n" if suffix == ".srt" else _ASS
    service, asset, sidecar = _subtitle_fixture(tmp_path, base_video, f"Film.stat{suffix}", original)
    substitute = original.replace("small", "SMALL").encode("utf-8")
    assert len(substitute) == len(original.encode("utf-8"))
    before = os.stat(sidecar)

    def substitute_in_place(n: int) -> None:
        if n == 3:  # after the bounded hash, at the read's fresh admission
            with open(sidecar, "r+b") as handle:  # same file id
                handle.write(substitute)
            os.utime(sidecar, ns=(before.st_atime_ns, before.st_mtime_ns))  # same mtime
            after = os.stat(sidecar)
            assert (after.st_size, after.st_mtime_ns, after.st_ino) == (before.st_size, before.st_mtime_ns,
                                                                         before.st_ino)

    _admissions_of(monkeypatch, sidecar, substitute_in_place)
    spawns = _decoder_spawns(monkeypatch, service.layout.cache)
    report = service.index(max_seconds=120)
    track = _track(service, sidecar)
    assert track["status"] == "failed" and "changed" in track["detail"] and track["cue_count"] == 0, track
    assert [s for s in spawns if s["private"]] == []
    texts = [row[0] for row in service.store.db.execute("SELECT text FROM dialogue WHERE asset_id = ?", (asset,))]
    assert not any("SMALL" in text for text in texts), texts
    assert f"{asset}/subtitles" in report["completed"] and _private_copies(service) == []
    service.close()


@requires_ffmpeg
def test_a_files_deny_while_the_private_copy_is_prepared_stops_before_the_decoder(tmp_path, base_video, policy,
                                                                                  monkeypatch) -> None:
    """Deterministic seam: the DENY lands inside the copy's write/flush, after the read's admission."""
    from qronos_media import decoder_copy

    set_permission_policy("files", "ALLOW")
    clock = FakeClock()
    frozen = clock()
    service, asset, sidecar = _subtitle_fixture(tmp_path, base_video, "Film.prep.ass", _ASS,
                                                permissions=GatePermissions(monotonic=lambda: frozen),
                                                monotonic=lambda: frozen)
    real_write = decoder_copy._LockedFile.write_all
    landed: list = []

    def write_then_deny(self, data):
        real_write(self, data)
        set_permission_policy("files", "DENY")  # while the 16 MiB-capped copy is being prepared
        landed.append(len(data))

    monkeypatch.setattr(decoder_copy._LockedFile, "write_all", write_then_deny)
    spawns = _decoder_spawns(monkeypatch, service.layout.cache)
    report = service.index(max_seconds=60)
    assert landed, "the seam was not reached"
    assert [s for s in spawns if s["private"]] == [], "the decoder started after a DENY during copy preparation"
    assert report["stopped"] == "permission" and _stage(service, asset, "subtitles")[0] == "QUEUED", report
    assert _private_copies(service) == []
    service.close()


@requires_ffmpeg
@pytest.mark.skipif(os.name != "nt", reason="Windows folder semantics")
def test_the_path_ffmpeg_reopens_cannot_be_rebound_while_it_decodes(tmp_path, base_video, monkeypatch) -> None:
    service, asset, sidecar = _subtitle_fixture(tmp_path, base_video, "Film.ns.ass", _ASS)
    outcomes: dict = {}

    def rebind(private: Path) -> None:
        folders = {"subtitle-input": private.parent, "cache": private.parent.parent,
                   "media data root": service.layout.root}
        for name, folder in folders.items():
            try:
                os.rename(folder, folder.with_name(folder.name + "-moved"))
                outcomes[name] = "allowed"
                os.rename(folder.with_name(folder.name + "-moved"), folder)
            except PermissionError:
                outcomes[name] = "refused"

    spawns = _decoder_spawns(monkeypatch, service.layout.cache, on_spawn=rebind)
    report = service.index(max_seconds=120)
    track = _track(service, sidecar)
    assert outcomes == {"subtitle-input": "refused", "cache": "refused", "media data root": "refused"}, outcomes
    assert len([s for s in spawns if s["private"]]) == 1 and track["status"] == "indexed", track
    assert f"{asset}/subtitles" in report["completed"] and _private_copies(service) == []
    service.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows share modes")
def test_the_sweep_never_deletes_a_copy_held_by_a_live_owner_and_says_so(tmp_path) -> None:
    from qronos_media.decoder_copy import INPUT_DIR, sweep_stale_inputs
    from tests.fixtures.child_python import child_python

    cache = tmp_path / "cache"
    (cache / INPUT_DIR).mkdir(parents=True)
    held = cache / INPUT_DIR / f"sub-{'c' * 32}.ass"
    stale = cache / INPUT_DIR / f"sub-{'d' * 32}.ssa"
    stale.write_bytes(b"stale")
    executable, environment = child_python()
    holder = subprocess.Popen(
        [executable, "-I", "-c",
         "import sys; sys.path[:0] = sys.argv[2:3]; from pathlib import Path\n"
         "from qronos_media.decoder_copy import _LockedFile\n"
         "locked = _LockedFile(Path(sys.argv[1])); locked.write_all(b'live'); print('held', flush=True)\n"
         "sys.stdin.read(); locked.close()",
         str(held), str(Path(stages_module.__file__).parents[1])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=environment)
    try:
        assert holder.stdout.readline().strip() == b"held"
        counts = sweep_stale_inputs(cache)
        assert counts == {"removed": 1, "busy": 1}, counts
        assert held.exists() and not stale.exists()
    finally:
        holder.stdin.close()
        holder.wait(10)
    assert sweep_stale_inputs(cache) == {"removed": 1, "busy": 0} and not held.exists()


def test_cleanup_never_deletes_a_foreign_file_that_took_the_copy_s_name(tmp_path, monkeypatch) -> None:
    from qronos_media import decoder_copy

    data = b"[Script Info]\n"
    foreign = tmp_path / "foreign.ass"
    foreign.write_bytes(b"not ours")
    real_close = decoder_copy._LockedFile.close
    seen: dict = {}

    def close_then_swap(self):
        real_close(self)
        if "path" in seen and not seen.get("swapped"):
            os.replace(foreign, seen["path"])  # another file takes the name once the handle is gone
            seen["swapped"] = True

    monkeypatch.setattr(decoder_copy._LockedFile, "close", close_then_swap)
    with pytest.raises(decoder_copy.DecoderInputError, match="another file"):
        with decoder_copy.verified_decoder_input(tmp_path / "cache", data, hashlib.sha256(data).hexdigest(),
                                                 ".ass") as path:
            seen["path"] = path
    assert seen["path"].read_bytes() == b"not ours"  # the foreign file was left alone


def test_a_copy_that_is_not_where_it_was_created_is_refused_and_removed(tmp_path, monkeypatch) -> None:
    from qronos_media import decoder_copy

    data = b"[Script Info]\n"
    monkeypatch.setattr(decoder_copy, "_same_path", lambda final, intended: False)
    entered = []
    with pytest.raises(decoder_copy.DecoderInputError, match="not where it was created"):
        with decoder_copy.verified_decoder_input(tmp_path / "cache", data, hashlib.sha256(data).hexdigest(),
                                                 ".ass") as path:
            entered.append(path)
    assert entered == [] and list((tmp_path / "cache" / decoder_copy.INPUT_DIR).iterdir()) == []
