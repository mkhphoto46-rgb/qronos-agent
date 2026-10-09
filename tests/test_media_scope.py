"""Every read is admitted against its own root's current scope and a fresh gate answer;
strict verification is a bounded, owned operation.

Root findings M-P3/M-P4 and the Installer review (MR-1..3). A ``files`` row
is a record, not permission. These tests corrupt rows, narrow scopes, deny
the Files policy and let genuine ALWAYS_ASK grants expire, using the real
gate and approval ledger, and check that nothing outside the approved scope
is stat'ed for hashing, hashed, decoded or offered.

Production defaults are never changed here. Time-dependent rules (the
2-second in-flight revocation check, the gate's 180-second grant lifetime,
lease renewal, the verify deadline) are driven by a fake monotonic clock
that the streaming fake hash advances. Synthetic fixtures only.
"""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import time

import pytest

from core.activity_guard import ActivityMode, ResourcePressure
from core.approval_ledger import ApprovalLedger
from core.permission_control import set_permission_policy
from qronos_media import jobs as jobs_module
from qronos_media import service as service_module
from qronos_media import probe as probe_module
from qronos_media import stages as stages_module
from qronos_media.identity import FileState, HashCancelled
from qronos_media.jobs import REVOCATION_CHECK_SECONDS

#: The declared production contract, as a literal: the bounds below must not
#: scale with whatever the module constant happens to be.
DECLARED_REVOCATION_SECONDS = 2.0


def test_the_production_revocation_default_is_the_declared_one() -> None:
    assert REVOCATION_CHECK_SECONDS <= DECLARED_REVOCATION_SECONDS
from qronos_media.permissions import GatePermissions, MediaPermissionRefused
from qronos_media.resources import Lease, ResourceRefused, StandaloneCpuAuthority
from qronos_media.scope import OutOfScope, RootScope, admit, admit_sidecar
from qronos_media.service import MediaRequestError
from qronos_media.store import MediaStore
from tests.media.media_support import (  # noqa: F401
    FakePermissions, base_video, copy_video, make_service, quiet_activity, requires_ffmpeg, write_srt,
)


class FakeClock:
    """A monotonic clock the test moves; starts at the real value so gate grants line up."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


@pytest.fixture
def policy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QRONOS_PERMISSION_POLICY_PATH", str(tmp_path / "policy" / "permissions_v2.json"))
    monkeypatch.setenv("QRONOS_PERMISSION_AUDIT_PATH", str(tmp_path / "policy" / "audit.jsonl"))
    (tmp_path / "policy").mkdir()


def _approve(service, folder: Path, depth: int) -> dict:
    pending = service.add_root(str(folder), max_depth=depth)
    assert pending["status"] == "approval_required"
    return service.approve_root(pending["approvalId"], pending["decision"])


def _index(service) -> dict:
    report = {}
    for _ in range(5):
        report = service.index(max_seconds=60)
        if report["stopped"] != "time_budget":
            break
    return report


class Recorder:
    """Counts reads through the two hashing entry points; never changes results."""

    def __init__(self, monkeypatch) -> None:
        self.paths: list[str] = []
        for module in (jobs_module, service_module):
            real = module.full_sha256

            def counting(path, *args, _real=real, **kwargs):
                self.paths.append(str(path))
                return _real(path, *args, **kwargs)

            monkeypatch.setattr(module, "full_sha256", counting)


def _streaming_hash(monkeypatch, module, clock: FakeClock, on_chunk, *, seconds_per_chunk: float = 0.5):
    """A hash that streams 'chunks' until stopped; each chunk costs fake time."""
    chunks: list[str] = []

    def fake(path, *, cancelled=None):
        for n in range(10_000):
            on_chunk(n, path)
            if cancelled is not None and cancelled():
                raise HashCancelled(str(path))
            chunks.append(str(path))
            clock.advance(seconds_per_chunk)
        raise AssertionError("hashing was never stopped")

    monkeypatch.setattr(module, "full_sha256", fake)
    return chunks


# ------------------------------------------------------------- admission

def test_admission_rules_on_paths_alone(tmp_path: Path) -> None:
    root = RootScope(1, tmp_path / "films", "folder", 1)
    (tmp_path / "films" / "a" / "b").mkdir(parents=True)
    assert admit(root, tmp_path / "films" / "x.mkv", ()) == tmp_path / "films" / "x.mkv"
    assert admit(root, tmp_path / "films" / "a" / "x.mkv", ())
    cases = {
        tmp_path / "films" / "a" / "b" / "x.mkv": "beyond_depth",
        tmp_path / "elsewhere" / "x.mkv": "outside_root",
        tmp_path / "films-evil" / "x.mkv": "outside_root",  # prefix trick
        tmp_path / "films" / "x.txt": "not_media",
        str(tmp_path / "films") + "\\..\\x.mkv": "unsafe_path",
    }
    for path, reason in cases.items():
        with pytest.raises(OutOfScope) as refused:
            admit(root, path, ())
        assert refused.value.reason == reason, path
    with pytest.raises(OutOfScope) as state:
        admit(root, tmp_path / "films" / "a" / "x.mkv", (tmp_path / "films" / "a",))
    assert state.value.reason == "media_state"
    single = RootScope(2, tmp_path / "films" / "x.mkv", "file", 0)
    assert admit(single, tmp_path / "films" / "x.mkv", ())
    with pytest.raises(OutOfScope):
        admit(single, tmp_path / "films" / "y.mkv", ())


def test_sidecars_must_sit_beside_an_admitted_video(tmp_path: Path) -> None:
    video = tmp_path / "films" / "x.mkv"
    assert admit_sidecar(video, tmp_path / "films" / "x.fa.srt", ())
    for path, reason in ((tmp_path / "other" / "x.srt", "not_beside_video"),
                         (tmp_path / "films" / "x.exe", "not_subtitle")):
        with pytest.raises(OutOfScope) as refused:
            admit_sidecar(video, path, ())
        assert refused.value.reason == reason


# --------------------------------------------------------- MR-1..3 / M-P3

@requires_ffmpeg
def test_a_corrupted_row_outside_the_root_is_never_read(tmp_path, base_video, policy, monkeypatch) -> None:
    """Genuine ALWAYS_ASK grant for one folder; a row naming a file outside it, same root_id."""
    set_permission_policy("files", "ALWAYS_ASK")
    approved = copy_video(base_video, tmp_path / "approved" / "inside.mkv").parent
    outside = copy_video(base_video, tmp_path / "outside" / "secret.mkv")
    write_srt(outside.with_suffix(".srt"), [("00:00:00,500", "00:00:01,500", "zebra outside sentinel")])
    service = make_service(tmp_path / "data", permissions=GatePermissions(ledger=ApprovalLedger()))
    root_id = _approve(service, approved, 2)["rootId"]
    service.scan(root_id)
    _index(service)
    reads = Recorder(monkeypatch)
    state = FileState.of(outside)
    with sqlite3.connect(str(service.layout.database)) as raw:
        raw.execute("INSERT INTO files(path, root_id, size, mtime_ns, file_id, status, seen_at) "
                    "VALUES (?,?,?,?,?,'pending_identity',?)",
                    (str(outside), root_id, state.size, state.mtime_ns, state.file_id, time.time()))
    report = _index(service)
    row = service.store.db.execute("SELECT status, error, asset_id FROM files WHERE path = ?", (str(outside),)).fetchone()
    assert (row["status"], row["error"], row["asset_id"]) == ("missing", "out_of_scope", None)
    assert any(item.startswith(str(outside)) for item in report["outOfScope"])
    assert str(outside) not in reads.paths
    assert service.search_dialogue("zebra outside sentinel")["results"] == []
    service.close()


@requires_ffmpeg
def test_a_corrupted_row_reusing_an_indexed_asset_is_not_offered_or_verified(tmp_path, base_video, policy,
                                                                             monkeypatch) -> None:
    """The outside row claims the inside file's asset as 'present' (stage reuse, verify, playback)."""
    set_permission_policy("files", "ALLOW")
    approved = copy_video(base_video, tmp_path / "approved" / "inside.mkv").parent
    outside = copy_video(base_video, tmp_path / "outside" / "same-bytes.mkv")
    service = make_service(tmp_path / "data", permissions=GatePermissions())
    root_id = service.add_root(str(approved), max_depth=0)["rootId"]
    service.scan(root_id)
    _index(service)
    inside_row = service.store.db.execute("SELECT * FROM files").fetchone()
    state = FileState.of(outside)
    with sqlite3.connect(str(service.layout.database)) as raw:
        raw.execute(
            "INSERT INTO files(path, root_id, size, mtime_ns, file_id, asset_id, status, seen_at, hashed_size, "
            "hashed_mtime_ns, hashed_file_id) VALUES (?,?,?,?,?,?,'present',?,?,?,?)",
            (str(outside), root_id, state.size, state.mtime_ns, state.file_id, inside_row["asset_id"], time.time(),
             state.size, state.mtime_ns, state.file_id))
        raw.execute("DELETE FROM files WHERE path = ?", (inside_row["path"],))  # only the outside copy remains
    reads = Recorder(monkeypatch)
    verification = service.verify_asset(inside_row["asset_id"])
    assert verification["status"] == "no_copy" and verification["outOfScope"] == [str(outside)]
    assert reads.paths == []
    assert service.open_timestamp(inside_row["asset_id"], 0)["status"] == "not_verified"
    assert service.search_dialogue("I never asked for this")["results"] == []
    service.reindex(inside_row["asset_id"], "subtitles")
    assert _index(service)["completed"] == []
    service.close()


@requires_ffmpeg
def test_narrowing_a_root_on_re_approval_stops_reading_deeper_files(tmp_path, base_video, policy,
                                                                     monkeypatch) -> None:
    set_permission_policy("files", "ALWAYS_ASK")
    folder = tmp_path / "approved"
    deep = copy_video(base_video, folder / "season1" / "episode.mkv")
    service = make_service(tmp_path / "data", permissions=GatePermissions(ledger=ApprovalLedger()))
    root_id = _approve(service, folder, 1)["rootId"]
    service.scan(root_id)
    _index(service)
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    narrowed = _approve(service, folder, 0)
    assert narrowed["rootId"] == root_id and narrowed["outOfScope"] == 1
    write_srt(deep.with_suffix(".srt"), [("00:00:00,500", "00:00:01,500", "after narrowing line")])
    reads = Recorder(monkeypatch)
    summary = service.scan(root_id)[0]
    assert summary["truncated"] and summary["out_of_scope"] == 0  # already marked at approval time
    service.reindex(asset, "subtitles")
    _index(service)
    assert service.verify_asset(asset)["status"] == "no_copy"
    assert reads.paths == []
    assert service.search_dialogue("after narrowing line")["results"] == []
    assert tuple(service.store.db.execute("SELECT status, error FROM files").fetchone()) == ("missing", "out_of_scope")
    _approve(service, folder, 1)  # widening re-admits the file through a fresh scan and hash
    service.scan(root_id)
    assert _index(service)["hashed"] == 1
    service.close()


@requires_ffmpeg
def test_after_files_deny_strict_paths_read_nothing(tmp_path, base_video, policy, monkeypatch) -> None:
    set_permission_policy("files", "ALLOW")
    folder = copy_video(base_video, tmp_path / "selected" / "film.mkv").parent
    service = make_service(tmp_path / "data", permissions=GatePermissions())
    root_id = service.add_root(str(folder), max_depth=0)["rootId"]
    service.scan(root_id)
    _index(service)
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    set_permission_policy("files", "DENY")
    reads = Recorder(monkeypatch)
    verification = service.verify_asset(asset)
    assert verification["status"] == "refused" and verification["refusedRoots"] == {str(folder): "revoked"}
    assert service.open_timestamp(asset, 0)["status"] == "not_verified"
    with pytest.raises(MediaPermissionRefused):
        service.export_report(asset, str(tmp_path / "out.json"))  # the write gate refuses before any read
    assert reads.paths == [] and not (tmp_path / "out.json").exists()
    service.close()


# ------------------------------------------------- M-P4: fresh, bound, timed

@requires_ffmpeg
def test_a_deny_between_two_files_of_one_run_stops_the_second_read(tmp_path, base_video, policy,
                                                                    monkeypatch) -> None:
    """The run's 2 s answer cache must not carry a DENY past the next read handoff."""
    set_permission_policy("files", "ALLOW")
    folder = tmp_path / "selected"
    copy_video(base_video, folder / "a.mkv")
    second = folder / "b.mkv"
    second.write_bytes(base_video.read_bytes() + b"different bytes")
    service = make_service(tmp_path / "data", permissions=GatePermissions())
    service.scan(service.add_root(str(folder), max_depth=0)["rootId"])
    hashed: list[str] = []
    real = jobs_module.full_sha256

    def deny_after_first(path, **kwargs):
        hashed.append(str(path))
        digest = real(path, **kwargs)
        set_permission_policy("files", "DENY")  # the person denies right after the first file
        return digest

    monkeypatch.setattr(jobs_module, "full_sha256", deny_after_first)
    report = service.index(max_seconds=60)
    assert hashed == [str(folder / "a.mkv")]
    assert report["refusedRoots"] == {str(folder): "revoked"} and report["completed"] == []
    service.close()


@requires_ffmpeg
def test_files_deny_during_a_hash_stops_it_within_the_production_default(tmp_path, base_video, policy,
                                                                         monkeypatch) -> None:
    set_permission_policy("files", "ALLOW")
    clock = FakeClock()
    folder = copy_video(base_video, tmp_path / "selected" / "film.mkv").parent
    service = make_service(tmp_path / "data", permissions=GatePermissions(monotonic=clock), monotonic=clock)
    service.scan(service.add_root(str(folder), max_depth=0)["rootId"])
    denied_at: list[float] = []

    def on_chunk(n, path):
        if n == 4 and not denied_at:
            set_permission_policy("files", "DENY")
            denied_at.append(clock())

    chunks = _streaming_hash(monkeypatch, jobs_module, clock, on_chunk)
    report = service.index(max_seconds=600)
    assert report["stopped"] == "permission" and report["hashed"] == 0
    assert len(chunks) - 4 <= DECLARED_REVOCATION_SECONDS / 0.5 + 1  # stopped within the declared default
    assert clock() - denied_at[0] <= DECLARED_REVOCATION_SECONDS + 0.5
    service.close()


@requires_ffmpeg
def test_an_expired_ask_grant_stops_verification(tmp_path, base_video, policy, monkeypatch) -> None:
    """Genuine ALWAYS_ASK single-use approval with the gate's own lifetime; only time passes."""
    set_permission_policy("files", "ALWAYS_ASK")
    clock = FakeClock()
    permissions = GatePermissions(ledger=ApprovalLedger(), monotonic=clock)
    folder = copy_video(base_video, tmp_path / "selected" / "film.mkv").parent
    service = make_service(tmp_path / "data", permissions=permissions, monotonic=clock)
    root_id = _approve(service, folder, 0)["rootId"]
    service.scan(root_id)
    _index(service)
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    clock.advance(170.0)  # the approval is 170 s old; the gate's grant lives 180 s
    chunks = _streaming_hash(monkeypatch, service_module, clock, lambda n, path: None, seconds_per_chunk=1.0)
    result = service.verify_asset(asset, max_seconds=600)
    assert result["status"] == "incomplete" and result["reason"] == "permission"
    assert 10 <= len(chunks) <= 10 + DECLARED_REVOCATION_SECONDS + 1
    clock.advance(1.0)
    assert service.verify_asset(asset)["status"] == "refused"  # and nothing is read afterwards
    service.close()


def _two_roots(tmp_path, base_video, clock):
    """Two approved folders holding byte-identical copies; folder A approved 100 s before folder B."""
    permissions = GatePermissions(ledger=ApprovalLedger(), monotonic=clock)
    service = make_service(tmp_path / "data", permissions=permissions, monotonic=clock)
    folder_a = copy_video(base_video, tmp_path / "a_root" / "film.mkv").parent
    folder_b = copy_video(base_video, tmp_path / "b_root" / "film.mkv").parent
    root_a = _approve(service, folder_a, 0)["rootId"]
    clock.advance(100.0)
    root_b = _approve(service, folder_b, 0)["rootId"]
    service.scan(root_a)
    service.scan(root_b)
    _index(service)
    asset = service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"]
    return service, asset, folder_a, folder_b


@requires_ffmpeg
def test_a_live_grant_on_one_root_never_carries_another_root_s_copy(tmp_path, base_video, policy,
                                                                     monkeypatch) -> None:
    set_permission_policy("files", "ALWAYS_ASK")
    clock = FakeClock()
    service, asset, folder_a, folder_b = _two_roots(tmp_path, base_video, clock)
    clock.advance(70.0)  # A's grant is 170 s old (expires at 180); B's is 70 s old
    opened: list[str] = []
    chunks = _streaming_hash(monkeypatch, service_module, clock,
                             lambda n, path: opened.append(str(path)) if n == 0 else None, seconds_per_chunk=1.0)
    result = service.verify_asset(asset, max_seconds=600)
    assert opened == [str(folder_a / "film.mkv")]  # the copy under A was being read
    assert result["status"] == "incomplete" and result["reason"] == "permission"
    # It stopped when A's grant expired (10 s in), not when B's did (110 s in):
    # B's live grant never carried A's copy.
    assert len(chunks) <= 10 + DECLARED_REVOCATION_SECONDS + 1, len(chunks)
    service.close()


@requires_ffmpeg
def test_a_decoder_opens_only_a_copy_whose_own_root_is_allowed_now(tmp_path, base_video, policy,
                                                                    monkeypatch) -> None:
    set_permission_policy("files", "ALWAYS_ASK")
    clock = FakeClock()
    service, asset, folder_a, folder_b = _two_roots(tmp_path, base_video, clock)
    clock.advance(85.0)  # A expired (185 s), B live (85 s)
    decoded: list[str] = []
    real = stages_module.run_bounded

    def recording(argv, **kwargs):
        # Inputs only: keyframe output targets in the media cache are also file: arguments.
        decoded.extend(part for part in argv if part.startswith("file:") and part.lower().endswith(".mkv"))
        return real(argv, **kwargs)

    monkeypatch.setattr(stages_module, "run_bounded", recording)
    monkeypatch.setattr(probe_module, "run_bounded", recording)
    service.reindex(asset, "metadata")
    report = service.index(max_seconds=120)
    assert f"{asset}/metadata" in report["completed"]
    assert decoded and all(str(folder_b) in part for part in decoded)
    assert not any(str(folder_a) in part for part in decoded)
    service.close()


# ------------------------------------------------- read boundary (clarified)

@requires_ffmpeg
def test_an_explicit_appdata_or_external_selection_is_an_ordinary_root(tmp_path, base_video, policy) -> None:
    """The Library's write restriction is not a read policy: explicit selections anywhere index."""
    set_permission_policy("files", "ALLOW")
    under_appdata = copy_video(base_video, Path(os.environ["LOCALAPPDATA"]) / "Videos" / "clip.mkv").parent
    external = copy_video(base_video, tmp_path / "external-drive" / "Films" / "film.mkv").parent
    service = make_service(tmp_path / "data", permissions=GatePermissions())
    for folder in (under_appdata, external):
        added = service.add_root(str(folder), max_depth=0)
        assert added["status"] == "added"
        service.scan(added["rootId"])
    assert _index(service)["hashed"] == 2
    service.close()


@requires_ffmpeg
def test_the_media_index_never_reads_its_own_data_folder(tmp_path, base_video, policy) -> None:
    set_permission_policy("files", "ALLOW")
    parent = tmp_path / "library"
    copy_video(base_video, parent / "film.mkv")
    data = parent / "media-data"
    service = make_service(data, permissions=GatePermissions())
    planted = copy_video(base_video, data / "cache" / "planted.mkv")
    added = service.add_root(str(parent), max_depth=4)
    summary = service.scan(added["rootId"])[0]
    assert summary["skipped_media_state"] == 1
    assert [row[0] for row in service.store.db.execute("SELECT path FROM files")] == [str(parent / "film.mkv")]
    state = FileState.of(planted)
    with sqlite3.connect(str(service.layout.database)) as raw:
        raw.execute("INSERT INTO files(path, root_id, size, mtime_ns, file_id, status, seen_at) "
                    "VALUES (?,?,?,?,?,'pending_identity',?)",
                    (str(planted), added["rootId"], state.size, state.mtime_ns, state.file_id, time.time()))
    report = _index(service)
    assert any(item.startswith(str(planted)) for item in report["outOfScope"])
    with pytest.raises(MediaRequestError):
        service.add_root(str(data / "cache"))
    service.close()


# ------------------------------------------------- bounded strict verify

class RecordingAuthority(StandaloneCpuAuthority):
    def __init__(self, root: Path, *, refuse_renew: bool = False) -> None:
        super().__init__(root, activity=quiet_activity, reserve_bytes=0)
        self.refuse_renew = refuse_renew
        self.held: list[str] = []

    def reserve(self, task_id, needs):
        lease = super().reserve(task_id, needs)
        self.held.append(task_id)
        return lease

    def renew(self, lease: Lease) -> Lease:
        if self.refuse_renew:
            raise ResourceRefused("activity_mode", "gaming started")
        return super().renew(lease)

    def release(self, lease: Lease) -> None:
        self.held.remove(lease.task_id)


def _indexed_one(tmp_path, base_video, **kwargs):
    folder = copy_video(base_video, tmp_path / "selected" / "film.mkv").parent
    service = make_service(tmp_path / "data", **kwargs)
    service.scan(service.add_root(str(folder), max_depth=0)["rootId"])
    _index(service)
    return service, service.store.db.execute("SELECT asset_id FROM files").fetchone()["asset_id"], folder


@requires_ffmpeg
def test_verify_defers_to_the_resource_authority_without_reading(tmp_path, base_video, monkeypatch) -> None:
    authority = RecordingAuthority(tmp_path)
    service, asset, _ = _indexed_one(tmp_path, base_video, authority=authority)
    authority.activity = lambda: (ActivityMode.GAMING_ASSIST, ResourcePressure.NORMAL)
    reads = Recorder(monkeypatch)
    result = service.verify_asset(asset)
    assert result["status"] == "deferred" and "activity_mode" in result["reason"]
    assert reads.paths == [] and authority.held == []
    service.close()


@requires_ffmpeg
@pytest.mark.parametrize("trigger,reason", [("stop", "cancelled"), ("deadline", "time_budget"),
                                            ("pause", "paused"), ("revoke", "permission"),
                                            ("lease", "deferred:activity_mode")])
def test_verify_is_observed_while_it_hashes(tmp_path, base_video, monkeypatch, trigger, reason) -> None:
    """All production defaults; only the fake clock moves (1 s per streamed chunk)."""
    clock = FakeClock()
    permissions = FakePermissions()
    authority = RecordingAuthority(tmp_path, refuse_renew=(trigger == "lease"))
    service, asset, folder = _indexed_one(tmp_path, base_video, permissions=permissions, authority=authority,
                                          monotonic=clock)
    other = MediaStore(service.layout.database)
    stop = {"now": False}

    def on_chunk(n: int, path) -> None:
        if n == 3:
            if trigger == "stop":
                stop["now"] = True
            elif trigger == "pause":
                other.set_meta("paused", "1")
            elif trigger == "revoke":
                permissions.refuse_roots[str(folder)] = "revoked"

    chunks = _streaming_hash(monkeypatch, service_module, clock, on_chunk, seconds_per_chunk=1.0)
    result = service.verify_asset(asset, max_seconds=5 if trigger == "deadline" else 600,
                                  cancelled=lambda: stop["now"])
    assert result["status"] == "incomplete" and result["reason"] == reason, result
    assert result["verified"] == []  # claims nothing
    bounds = {"stop": 3, "pause": 3, "deadline": 5, "revoke": 3 + DECLARED_REVOCATION_SECONDS + 1,
              "lease": jobs_module.LEASE_RENEW_SECONDS + 1}
    assert len(chunks) <= bounds[trigger], (trigger, len(chunks))
    assert authority.held == []  # the lease is always released
    other.close()
    service.close()


@requires_ffmpeg
def test_verify_budget_is_bounded(tmp_path, base_video) -> None:
    service, asset, _ = _indexed_one(tmp_path, base_video)
    for bad in (0.0, 10_000.0, float("nan")):
        with pytest.raises(MediaRequestError):
            service.verify_asset(asset, max_seconds=bad)
    assert service.verify_asset(asset)["status"] == "verified"
    service.close()
