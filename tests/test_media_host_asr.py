"""The in-process media host and the chunked whisper.cpp fallback (fixtures only).

No real whisper, model, GPU, audio device or user media is used. ffmpeg and
whisper-cli are stood in for by base-interpreter fixture runners that write
synthetic WAV and JSON files; they are started through the product's own
``run_bounded`` inside the real Windows Job containment, so process identity,
teardown and exit proof are real. The "video" is a small file under pytest's
tmp_path whose content tells the fake ffmpeg how much audio exists and where.
The governor and the ledger are the real classes; the bridge, the Files
permission and the activity reading are fakes. The asset pins are never
executed; the whisper "assets" here are dummy files reached through a seam.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from core.activity_guard import ActivityMode, ResourcePressure
from core.resource_governor import DEFAULT_LEASE_SECONDS, ResourceGovernor, Weight
from core.resource_guard import SystemStatus
from core.resource_ownership import ResourceLedger
from qronos_media import asr
from qronos_media.containment import ChildLimits
from qronos_media.resources import (
    WHISPER_LARGE_V3_TURBO_Q5_0, GovernorAuthority, ModelSpec, ResourceRefused, StageNeeds, StandaloneCpuAuthority,
)
from qronos_media.service import MediaService
from qronos_media.stages import STAGES
from qronos_media.tools import MediaTools, run_bounded
from tests.fixtures.child_python import child_python
from tests.media.media_support import FakePermissions, quiet_activity

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Job containment and the bridge host are Windows paths")

QUIET = SystemStatus(cpu_usage_percent=5.0, ram_usage_percent=20.0, ram_used_gb=8.0, ram_total_gb=64.0)
PYTHON = child_python()[0]
CHUNK_MS = 10_000
_real_locate = asr.locate_whisper  # before any test seam replaces it

FAKE_FFMPEG = r'''
import json, struct, sys
from pathlib import Path
argv = sys.argv[1:]
def after(flag):
    return argv[argv.index(flag) + 1]
ss, t = float(after("-ss")), float(after("-t"))
source = Path(after("-i")[len("file:"):])
out = Path(argv[-1][len("file:"):])
spec = json.loads(source.read_text(encoding="utf-8"))
start, end = spec["audioOffsetMs"], spec["audioOffsetMs"] + spec["audioMs"]
produced = max(0, min(ss * 1000 + t * 1000, end) - max(ss * 1000, start))
size = int(produced) * 32
with open(out, "wb") as stream:
    stream.write(b"RIFF" + struct.pack("<I", 36 + size) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16))
    stream.write(b"data" + struct.pack("<I", size) + bytes(size))
with open(str(source) + ".ffmpeg.log", "a", encoding="utf-8") as log:
    log.write(json.dumps({"ss": after("-ss"), "t": after("-t"), "map": after("-map"), "out": out.name}) + "\n")
'''

FAKE_WHISPER = r'''
import json, os, struct, subprocess, sys, time
from pathlib import Path
argv = sys.argv[1:]
def after(flag):
    return argv[argv.index(flag) + 1]
wav, base, model = Path(after("-f")), Path(after("-of")), Path(after("-m"))
control_path = model.parent / "control.json"
control = json.loads(control_path.read_text(encoding="utf-8")) if control_path.exists() else {}
index = int(wav.stem.rsplit("-", 1)[1])
markers = Path(control.get("markers", str(model.parent)))
(markers / f"whisper-{index}-{os.getpid()}.started").write_text(str(os.getpid()), encoding="utf-8")
with open(model.parent / "whisper.log", "a", encoding="utf-8") as log:
    log.write(json.dumps({"chunk": index, "pid": os.getpid(), "argv": argv}) + "\n")
if control.get("grandchild_on") == index:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (markers / f"grandchild-{child.pid}.pid").write_text(str(child.pid), encoding="utf-8")
if control.get("sleep_on") == index:
    time.sleep(float(control.get("sleep", 30)))
with open(wav, "rb") as stream:
    stream.seek(40)
    size = struct.unpack("<I", stream.read(4))[0]
length = size // 32
segments = [{"offsets": {"from": 1000, "to": 2500}, "text": f" chunk {index} opening line"}]
if length > 6000:
    segments.append({"offsets": {"from": 5000, "to": 6000}, "text": f" chunk {index} second line"})
Path(str(base) + ".json").write_text(json.dumps({"transcription": segments}), encoding="utf-8")
'''


# ------------------------------------------------------------------ fixtures


class FakeBridgeState:
    def __init__(self) -> None:
        self.foreground = False
        self.paused = False


def make_authority(tmp_path: Path, state: FakeBridgeState | None = None, governor=None, ledger=None):
    from qronos_media.model_authority import issue_hosted_authority

    state = state or FakeBridgeState()
    governor = governor or ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None)
    ledger = ledger or ResourceLedger()
    authority = issue_hosted_authority(governor=governor, ledger=ledger, disk_root=tmp_path,
                                       foreground_busy=lambda: state.foreground, paused=lambda: state.paused,
                                       activity=quiet_activity, reserve_bytes=0)
    return authority, state


@pytest.fixture()
def runners(tmp_path: Path, monkeypatch):
    """The fake ffmpeg/whisper runners plus dummy whisper assets reached through the locate seam."""
    folder = tmp_path / "runners"
    folder.mkdir()
    (folder / "ffmpeg_runner.py").write_text(FAKE_FFMPEG, encoding="utf-8")
    (folder / "whisper_runner.py").write_text(FAKE_WHISPER, encoding="utf-8")
    assets_dir = tmp_path / "assets"
    (assets_dir / "models").mkdir(parents=True)
    exe = assets_dir / "whisper-cli.exe"
    exe.write_bytes(b"not executed")
    weights = assets_dir / "models" / "model.bin"
    weights.write_bytes(b"dummy weights")

    def launch(argv: list[str]) -> list[str]:
        runner = "whisper_runner.py" if argv[0] == str(exe) else "ffmpeg_runner.py"
        return [PYTHON, "-B", "-I", str(folder / runner), *argv]

    monkeypatch.setattr(asr, "launcher", launch)
    monkeypatch.setattr(asr, "locate_whisper",
                        lambda runtime_assets, spec=WHISPER_LARGE_V3_TURBO_Q5_0: asr.WhisperAssets(exe, weights, spec))
    monkeypatch.setattr(asr, "CHUNK_MS", CHUNK_MS)
    return {"assets": assets_dir, "exe": exe, "weights": weights, "control": assets_dir / "models" / "control.json",
            "whisper_log": assets_dir / "models" / "whisper.log"}


def prepared_service(tmp_path: Path, authority, *, audio_ms: int, origin_ms: int, audio_start_ms: int,
                     subtitles: bool = False, audio: bool = True):
    """A store whose one asset has metadata and subtitles done and only ``asr`` queued (no decoder needed)."""
    library = tmp_path / "library"
    library.mkdir()
    video = library / "film.mkv"
    video.write_text(json.dumps({"audioMs": audio_ms, "audioOffsetMs": audio_start_ms - origin_ms}),
                     encoding="utf-8")
    tools = MediaTools(Path(PYTHON), Path(PYTHON))
    service = MediaService(tmp_path / "media-data", tools=tools, authority=authority,
                           permissions=FakePermissions())
    service.add_root(str(library), kind="folder", max_depth=2)
    service.scan()
    service.index(max_seconds=30, max_stages=0)  # hash only: creates the asset and queues its stages
    db = service.store.db
    asset_id = db.execute("SELECT asset_id FROM assets").fetchone()[0]
    with service.store.transaction() as tx:
        tx.execute("UPDATE assets SET start_offset_ms = ? WHERE asset_id = ?", (origin_ms, asset_id))
        if audio:
            tx.execute("INSERT INTO streams(asset_id, idx, kind, codec, language, is_default, forced, start_ms) "
                       "VALUES (?, 1, 'audio', 'aac', 'eng', 1, 0, ?)", (asset_id, audio_start_ms))
        if subtitles:
            tx.execute("INSERT INTO subtitle_tracks(track_id, asset_id, source, stream_index, status, cue_count) "
                       "VALUES (?, ?, 'embedded', 2, 'indexed', 1)", (f"{asset_id}:e2", asset_id))
            tx.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                       "VALUES (?, ?, 'cue', 0, 100, 900, 'Real subtitle', 'real subtitle')", (asset_id, f"{asset_id}:e2"))
        tx.execute("UPDATE stages SET state = 'COMPLETED' WHERE asset_id = ? AND stage != 'asr'", (asset_id,))
    return service, asset_id, video


def ffmpeg_calls(video: Path) -> list[dict]:
    log = Path(str(video) + ".ffmpeg.log")
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def whisper_calls(runners) -> list[dict]:
    log = runners["whisper_log"]
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def asr_rows(service, asset_id: str) -> list[tuple]:
    return [tuple(row) for row in service.store.db.execute(
        "SELECT d.start_ms, d.end_ms, d.text FROM dialogue d JOIN subtitle_tracks t ON t.track_id = d.track_id "
        "WHERE d.asset_id = ? AND t.source = 'asr' ORDER BY d.seq", (asset_id,))]


def stage_row(service, asset_id: str, stage: str = "asr") -> sqlite3.Row:
    return service.store.db.execute("SELECT * FROM stages WHERE asset_id = ? AND stage = ?",
                                    (asset_id, stage)).fetchone()


# ---------------------------------------------------------- authority rules


def test_the_standalone_and_governor_authorities_refuse_every_model_bearing_stage(tmp_path):
    asr_needs = STAGES["asr"].needs
    assert asr_needs.model is WHISPER_LARGE_V3_TURBO_Q5_0 and not asr_needs.gpu  # CPU whisper is still a model
    standalone = StandaloneCpuAuthority(tmp_path, activity=quiet_activity, reserve_bytes=0)
    with pytest.raises(ResourceRefused) as refused:
        standalone.reserve("media:asr:a", asr_needs)
    assert refused.value.reason == "requires_qronos_governor"
    governor = ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None)
    hosted_light = GovernorAuthority(governor, tmp_path, activity=quiet_activity, reserve_bytes=0)
    with pytest.raises(ResourceRefused) as refused:
        hosted_light.reserve("media:asr:a", asr_needs)
    assert refused.value.reason == "requires_trusted_model_authority" and governor.active() == ()


def test_the_model_capability_is_an_issued_object_never_a_forgeable_value(tmp_path):
    from qronos_media.host import MediaHost
    from qronos_media.model_authority import HostedModelAuthority

    with pytest.raises(TypeError):
        HostedModelAuthority(object(), governor=None, ledger=None, disk_root=tmp_path,
                             foreground_busy=lambda: False, paused=lambda: False, activity=quiet_activity)
    for forged in ({"model": "whisper", "vramMb": 1}, True, "--allow-model", object()):
        with pytest.raises(TypeError):
            MediaHost(forged, data_dir=lambda: tmp_path, tools=lambda: None, permissions=lambda: None)
    authority, _ = make_authority(tmp_path)
    look_alike = ModelSpec(**{field: getattr(WHISPER_LARGE_V3_TURBO_Q5_0, field)
                              for field in ("name", "vram_mb", "ram_mb", "executable", "weights")})
    with pytest.raises(ResourceRefused) as refused:
        authority.reserve("media:asr:x", StageNeeds("asr", model=look_alike))
    assert refused.value.reason == "undeclared_model"
    assert authority.governor.active() == () and authority.ledger.active() == ()


def test_one_model_slot_counted_in_the_ledger_and_as_a_managed_heavy_reservation(tmp_path):
    clock = [1000.0]
    governor = ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None, clock=lambda: clock[0])
    authority, _ = make_authority(tmp_path, governor=governor)
    lease = authority.reserve("media:asr:a", STAGES["asr"].needs)
    [owned] = authority.ledger.active()
    assert owned.workload == f"media-model:{WHISPER_LARGE_V3_TURBO_Q5_0.name}" and owned.budget.vram_mb == 2048
    assert governor.heavy_owner() == "media:asr:a"
    clock[0] += DEFAULT_LEASE_SECONDS * 20  # the lease clock is never authority to free a model slot
    assert governor.heavy_owner() == "media:asr:a" and len(authority.ledger.active()) == 1
    with pytest.raises(ResourceRefused) as refused:
        authority.reserve("media:asr:b", STAGES["asr"].needs)
    assert refused.value.reason == "model_busy"  # no second, independent model load
    assert not governor.request("queue:heavy", Weight.HEAVY).granted
    authority.release(lease)
    assert governor.active() == () and authority.ledger.active() == ()


@pytest.mark.parametrize("why", ["foreground", "paused", "user_stop", "activity_mode"])
def test_media_yields_before_the_assistant_and_never_reserves_then(tmp_path, why):
    state = FakeBridgeState()
    activity = quiet_activity
    if why == "activity_mode":
        activity = lambda: (ActivityMode.GAMING_ASSIST, ResourcePressure.NORMAL)  # noqa: E731
    from qronos_media.model_authority import issue_hosted_authority

    authority = issue_hosted_authority(governor=ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None),
                                       ledger=ResourceLedger(), disk_root=tmp_path,
                                       foreground_busy=lambda: state.foreground, paused=lambda: state.paused,
                                       activity=activity, reserve_bytes=0)
    state.foreground = why == "foreground"
    state.paused = why == "paused"
    if why == "user_stop":
        authority.preempt("user_stop")
    with pytest.raises(ResourceRefused) as refused:
        authority.reserve("media:asr:a", STAGES["asr"].needs)
    assert refused.value.reason == why
    assert authority.governor.active() == () and authority.ledger.active() == ()


# ------------------------------------------------- spawn handoff and exit proof


def _marker_child(tmp_path: Path, seconds: float = 30.0) -> list[str]:
    marker = tmp_path / f"marker-{time.monotonic_ns()}"
    code = f"import os,time; open({str(marker)!r},'w').write(str(os.getpid())); time.sleep({seconds})"
    return [PYTHON, "-B", "-I", "-c", code], marker


def test_a_release_during_the_spawn_handoff_frees_nothing_until_the_handoff_ends(tmp_path):
    authority, _ = make_authority(tmp_path)
    lease = authority.reserve("media:asr:a", STAGES["asr"].needs)
    lease.begin_spawn()  # a spawn is in its handoff: counted before any process exists
    authority.release(lease)
    assert authority.governor.heavy_owner() == "media:asr:a" and len(authority.ledger.active()) == 1
    with pytest.raises(ResourceRefused) as refused:
        authority.reserve("media:asr:b", STAGES["asr"].needs)
    assert refused.value.reason == "termination_unproven"
    lease.spawn_failed()  # no process was created: now, and only now, it is free
    assert authority.governor.active() == () and authority.ledger.active() == ()


def test_a_released_lease_cannot_start_a_child(tmp_path):
    authority, _ = make_authority(tmp_path)
    lease = authority.reserve("media:asr:a", STAGES["asr"].needs)
    authority.release(lease)
    argv, marker = _marker_child(tmp_path, 1)
    with pytest.raises(ResourceRefused) as refused:
        run_bounded(argv, timeout_seconds=10, stdout_limit=1024, stderr_limit=1024, observer=lease,
                    limits=asr.ASR_CHILD_LIMITS)
    assert refused.value.reason == "lease_released"
    time.sleep(0.3)
    assert not marker.exists() and lease.children == []


def test_a_release_while_the_child_runs_tears_it_down_and_frees_only_after_proof(tmp_path):
    authority, _ = make_authority(tmp_path)
    lease = authority.reserve("media:asr:a", STAGES["asr"].needs)
    argv, marker = _marker_child(tmp_path)
    outcome: dict = {}

    def run() -> None:
        try:
            run_bounded(argv, timeout_seconds=60, stdout_limit=1024, stderr_limit=1024, observer=lease,
                        limits=asr.ASR_CHILD_LIMITS)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcome["error"] = type(exc).__name__

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    [record] = lease.children
    assert record.pid == int(marker.read_text()) and record.creation_time > 0  # exact child identity
    authority.release(lease)
    worker.join(30)
    assert record.child.poll() is not None and record.proven
    assert authority.governor.active() == () and authority.ledger.active() == ()
    events = [e["event"] for e in authority.events]
    assert events.index("child_end") < events.index("released") or \
        events.index("termination_unproven") < events.index("released")


# ---------------------------------------------------------------- the stage


def test_asr_runs_only_under_the_hosted_lease_with_exact_argv_and_corrected_timestamps(tmp_path, runners):
    authority, _ = make_authority(tmp_path)
    service, asset_id, video = prepared_service(tmp_path, authority, audio_ms=25_000, origin_ms=1_500,
                                                audio_start_ms=2_700)
    report = service.index(max_seconds=120, max_stages=5)
    assert f"{asset_id}/asr" in report["completed"], report
    offset = 2_700 - 1_500
    assert [call["ss"] for call in ffmpeg_calls(video)] == [f"{(offset + k * CHUNK_MS) / 1000:.3f}" for k in range(3)]
    assert {call["map"] for call in ffmpeg_calls(video)} == {"0:1"}  # the chosen stream by absolute index
    calls = whisper_calls(runners)
    assert [c["chunk"] for c in calls] == [0, 1, 2]
    first = calls[0]["argv"]
    assert first[1:3] == ["-m", str(runners["weights"])] and first[first.index("-l") + 1] == "en"
    assert first[-4:-1] == ["-fa", "-oj", "-of"] and "-t" in first
    rows = asr_rows(service, asset_id)
    assert rows[0] == (1_000 + offset, 2_500 + offset, "chunk 0 opening line")
    assert (CHUNK_MS + 1_000 + offset, CHUNK_MS + 2_500 + offset, "chunk 1 opening line") in rows
    track = service.store.db.execute("SELECT provenance_json FROM subtitle_tracks WHERE source = 'asr'").fetchone()
    record = json.loads(track[0])
    assert record["verified"] is False and record["calibrated"] is False and record["schema"] == 2
    assert record["assetSha256"] == service.store.db.execute("SELECT sha256 FROM assets").fetchone()[0]
    assert (record["audioStreamIndex"], record["offsetMs"], record["audioSelection"]) == (1, offset, "default_flag")
    fts = service.store.db.execute("SELECT count(*) FROM dialogue_fts WHERE dialogue_fts MATCH 'opening'").fetchone()
    assert fts[0] == 3
    assert authority.governor.active() == () and authority.ledger.active() == ()


def test_foreground_preempts_the_running_chunk_then_exit_proof_then_release_then_resume_without_duplicates(
        tmp_path, runners):
    authority, state = make_authority(tmp_path)
    service, asset_id, video = prepared_service(tmp_path, authority, audio_ms=25_000, origin_ms=0, audio_start_ms=0)
    markers = tmp_path / "markers"
    markers.mkdir()
    runners["control"].write_text(json.dumps({"sleep_on": 1, "sleep": 60, "markers": str(markers)}),
                                  encoding="utf-8")

    spoke: list[float] = []

    def turn_starts() -> None:
        deadline = time.monotonic() + 60
        while not list(markers.glob("whisper-1-*.started")) and time.monotonic() < deadline:
            time.sleep(0.02)
        spoke.append(time.monotonic())
        state.foreground = True  # the person speaks: media must yield

    helper = threading.Thread(target=turn_starts)
    helper.start()
    report = service.index(max_seconds=120, max_stages=5)
    helper.join(60)
    assert report["stopped"] == "deferred" and "foreground" in report["detail"], report
    row = stage_row(service, asset_id)
    assert row["state"] == "QUEUED" and json.loads(row["checkpoint"])["next_chunk"] == 1
    events = list(authority.events)
    kinds = [e["event"] for e in events]
    ended = [e for e in events if e["event"] == "child_end"]
    assert len(ended) == 2 and all(e["proven"] for e in ended)
    assert kinds.index("released") > max(i for i, k in enumerate(kinds) if k == "child_end")
    assert authority.governor.active() == () and authority.ledger.active() == ()
    assert asr_rows(service, asset_id) == []  # no partial result committed
    sleeping_pid = int(next(markers.glob("whisper-1-*.started")).read_text())
    assert any(e["pid"] == sleeping_pid and e["proven"] for e in ended)  # the preempted child's exit was proven
    stopped_at = next(e["at"] for e in ended if e["pid"] == sleeping_pid)
    assert stopped_at - spoke[0] < 5.0, "media did not yield promptly to the foreground turn"

    state.foreground = False
    runners["control"].write_text(json.dumps({"markers": str(markers)}), encoding="utf-8")
    report = service.index(max_seconds=120, max_stages=5)
    assert f"{asset_id}/asr" in report["completed"], report
    assert [c["chunk"] for c in whisper_calls(runners)] == [0, 1, 1, 2]  # chunk 0 was never transcribed twice
    rows = asr_rows(service, asset_id)
    assert len(rows) == len(set(rows)) == 5 and sorted(rows) == rows


def test_an_unproved_termination_stays_busy_and_counted_until_the_job_is_proven_empty(tmp_path, runners, monkeypatch):
    import qronos_media.tools as tools_module

    class StubbornJob(tools_module.Containment):
        allow_terminate = False

        def terminate(self) -> None:  # simulates a failed TerminateJobObject
            if StubbornJob.allow_terminate:
                super().terminate()

    monkeypatch.setattr(tools_module, "Containment", StubbornJob)
    monkeypatch.setattr(asr, "ASR_CHILD_LIMITS", ChildLimits(memory_bytes=2 * 1024**3, max_processes=2))
    authority, state = make_authority(tmp_path)
    service, asset_id, _ = prepared_service(tmp_path, authority, audio_ms=15_000, origin_ms=0, audio_start_ms=0)
    markers = tmp_path / "markers"
    markers.mkdir()
    runners["control"].write_text(json.dumps({"grandchild_on": 0, "sleep_on": 0, "sleep": 60,
                                              "markers": str(markers)}), encoding="utf-8")

    def turn_starts() -> None:
        deadline = time.monotonic() + 60
        while not list(markers.glob("grandchild-*.pid")) and time.monotonic() < deadline:
            time.sleep(0.02)
        state.foreground = True

    helper = threading.Thread(target=turn_starts)
    helper.start()
    try:
        report = service.index(max_seconds=120, max_stages=5)
        helper.join(60)
        assert report["stopped"] == "deferred", report
        slot = authority._slot
        assert slot is not None and slot.closed, list(authority.events)
        assert authority.status()["modelSlot"]["state"] == "termination_unproven"
        assert authority.governor.heavy_owner() == slot.task_id and len(authority.ledger.active()) == 1
        state.foreground = False
        with pytest.raises(ResourceRefused) as refused:
            authority.reserve("media:asr:other", STAGES["asr"].needs)
        assert refused.value.reason == "termination_unproven"
        assert stage_row(service, asset_id)["state"] == "QUEUED"
        StubbornJob.allow_terminate = True  # the job can now really be terminated
        assert authority.reconcile() is True
        assert authority.governor.active() == () and authority.ledger.active() == ()
    finally:
        StubbornJob.allow_terminate = True
        slot = authority._slot
        for record in (slot.children if slot else []):
            if record.containment is not None:
                record.containment.close()  # KILL_ON_JOB_CLOSE: no survivor either way


def test_valid_subtitles_mean_no_asr_and_no_reservation_and_evidence_untouched(tmp_path, runners):
    authority, _ = make_authority(tmp_path)
    service, asset_id, video = prepared_service(tmp_path, authority, audio_ms=25_000, origin_ms=0,
                                                audio_start_ms=0, subtitles=True)
    report = service.index(max_seconds=60, max_stages=5)
    assert f"{asset_id}/asr" in report["completed"]
    assert stage_row(service, asset_id)["note"] == "skipped:subtitles_present"
    assert ffmpeg_calls(video) == [] and whisper_calls(runners) == []
    assert list(authority.events) == []  # nothing was reserved
    texts = [r[0] for r in service.store.db.execute("SELECT text FROM dialogue WHERE asset_id = ?", (asset_id,))]
    assert texts == ["Real subtitle"]


def test_the_standalone_tool_never_transcribes_and_other_stages_carry_on(tmp_path, runners):
    authority = StandaloneCpuAuthority(tmp_path, activity=quiet_activity, reserve_bytes=0)
    service, asset_id, video = prepared_service(tmp_path, authority, audio_ms=25_000, origin_ms=0, audio_start_ms=0)
    report = service.index(max_seconds=60, max_stages=5)
    assert any(item.endswith("asr:requires_qronos_governor") for item in report["refused"]), report
    assert report["stopped"] == "idle"
    assert stage_row(service, asset_id)["note"] == "unavailable:requires_qronos_governor"
    assert ffmpeg_calls(video) == [] and whisper_calls(runners) == []


def test_unprovisioned_whisper_assets_are_model_unavailable_with_zero_spawns(tmp_path, runners, monkeypatch):
    monkeypatch.setattr(asr, "locate_whisper", _real_locate)  # the real check, on an unprovisioned folder
    authority, _ = make_authority(tmp_path)
    service, asset_id, video = prepared_service(tmp_path, authority, audio_ms=25_000, origin_ms=0, audio_start_ms=0)
    report = service.index(max_seconds=60, max_stages=5)
    assert stage_row(service, asset_id)["note"] == "unavailable:model_unavailable", report
    assert ffmpeg_calls(video) == [] and whisper_calls(runners) == []
    assert authority.governor.active() == () and authority.ledger.active() == ()


def test_pinned_asset_verification_reads_size_and_hash_and_never_executes(tmp_path):
    spec = WHISPER_LARGE_V3_TURBO_Q5_0
    with pytest.raises(asr.AsrUnavailable):
        _real_locate(tmp_path, spec)  # nothing provisioned
    exe = Path(tmp_path, *spec.executable.parts)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x" * spec.executable.size)  # right size, wrong bytes
    with pytest.raises(asr.AsrUnavailable) as refused:
        _real_locate(tmp_path, spec)
    assert "SHA-256" in str(refused.value)


# ------------------------------------------------------- store admission


def test_the_host_admits_the_actual_store_folder_before_anything_opens(tmp_path):
    from core.startup_admission import StartupRefused
    from qronos_media.host import MediaHost

    authority, _ = make_authority(tmp_path)
    data = tmp_path / "Media"
    seen: list = []

    def refuse(root):
        seen.append(root)
        raise StartupRefused("store", "refused for the test")

    host = MediaHost(authority, data_dir=lambda: data, tools=lambda: None, permissions=lambda: FakePermissions(),
                     admit_store=refuse)
    try:
        assert host.submit_index(max_seconds=5, max_stages=1)["accepted"]
        deadline = time.monotonic() + 20
        while host.status()["lastReport"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert host.status()["lastReport"]["stopped"] == "store_admission"
        assert seen == [data] and not data.exists()  # nothing created, no SQLite opened
    finally:
        host.close()


STORE_FILE = "media-index.sqlite3"  # MediaLayout.database's name
_SQLITE_OPENS: list = []
_COUNTING: list = []


def _count_sqlite(event, args) -> None:
    if _COUNTING and event == "sqlite3.connect":
        _SQLITE_OPENS.append(args[0] if args else None)


sys.addaudithook(_count_sqlite)


def _host_report(host) -> dict:
    deadline = time.monotonic() + 30
    while host.status()["lastReport"] is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return host.status()["lastReport"]


@pytest.mark.parametrize("linked", ["database", "wal_side_file"])
def test_a_hard_linked_index_database_in_an_ordinary_folder_is_refused_before_any_sqlite_open(tmp_path, linked):
    """Root round-24 source check: the folder alone passes the one-name rule; the ACTUAL database file must not."""
    from core import startup_admission
    from qronos_media.host import MediaHost, media_store_admission

    installed = startup_admission._known_local_appdata() / "Qronos" / "Media"
    installed.mkdir(parents=True)
    installed_db = installed / STORE_FILE
    db = sqlite3.connect(installed_db)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.commit()
    db.close()
    (installed / (STORE_FILE + "-wal")).write_bytes(b"installed wal bytes")
    before = {path.name: path.read_bytes() for path in installed.iterdir()}
    outside = tmp_path / "ordinary-folder"
    outside.mkdir()
    if linked == "database":
        os.link(installed_db, outside / STORE_FILE)
    else:
        (outside / STORE_FILE).write_bytes(b"")
        os.link(installed / (STORE_FILE + "-wal"), outside / (STORE_FILE + "-wal"))
    authority, _ = make_authority(tmp_path)
    host = MediaHost(authority, data_dir=lambda: outside, tools=lambda: None, permissions=lambda: FakePermissions(),
                     admit_store=media_store_admission)
    startup_admission._reset_for_tests()
    _SQLITE_OPENS.clear()
    _COUNTING.append(1)
    try:
        assert host.submit_index(max_seconds=5, max_stages=1)["accepted"]
        report = _host_report(host)
    finally:
        _COUNTING.clear()
        host.close()
        startup_admission._reset_for_tests()
    assert report["stopped"] == "store_admission" and report["code"] == "store", report
    assert _SQLITE_OPENS == []  # nothing opened, not even read-only
    assert {path.name: path.read_bytes() for path in installed.iterdir()} == before
    assert os.stat(installed_db).st_nlink == (2 if linked == "database" else 1)


def test_an_ordinary_fresh_index_folder_is_admitted_and_opened(tmp_path):
    from core import startup_admission
    from qronos_media.host import MediaHost, media_store_admission

    folder = tmp_path / "fresh-media"
    authority, state = make_authority(tmp_path)
    host = MediaHost(authority, data_dir=lambda: folder, tools=lambda: None, permissions=lambda: FakePermissions(),
                     admit_store=media_store_admission)
    startup_admission._reset_for_tests()
    try:
        assert host.submit_index(max_seconds=5, max_stages=1)["accepted"]
        report = _host_report(host)
        assert report["stopped"] == "idle", report
        assert host.status()["storeOpen"] and (folder / STORE_FILE).is_file()
    finally:
        host.close()
        startup_admission._reset_for_tests()


def test_store_admission_binds_the_resolved_folder(tmp_path):
    from core import startup_admission
    from core.startup_admission import StartupRefused

    root = tmp_path / "installed"
    root.mkdir()
    seams = dict(installed=True, windows=True, data_root=root, version="0.1.0", recover=lambda v: {"ok": True},
                 confirm=lambda v: {"ok": True}, admit=lambda r: "admitted")
    startup_admission._reset_for_tests()
    try:
        assert startup_admission.enter_for_store("media", "QRONOS_MEDIA_DATA_DIR", root / "Media", **seams)
        with pytest.raises(StartupRefused) as refused:
            startup_admission.enter_for_store("media", "QRONOS_MEDIA_DATA_DIR", tmp_path / "elsewhere", **seams)
        assert refused.value.code == "store"
    finally:
        startup_admission._reset_for_tests()
    # A source process whose media folder leads INTO installed data is admitted on that root.
    known_root = startup_admission._known_local_appdata() / "Qronos"
    known_root.mkdir(parents=True)
    admitted: list = []
    try:
        startup_admission.enter_for_store(
            "media", "QRONOS_MEDIA_DATA_DIR", known_root / "Media", installed=False, windows=True,
            environ={"LOCALAPPDATA": str(tmp_path / "env-local")}, version="0.1.0",
            recover=lambda v: {"ok": True}, confirm=lambda v: {"ok": True}, admit=lambda r: admitted.append(r) or "a")
        assert [os.path.normcase(str(p)) for p in admitted] == [os.path.normcase(str(known_root))]
    finally:
        startup_admission._reset_for_tests()


def test_the_cli_refuses_before_opening_when_admission_refuses(tmp_path, monkeypatch, capsys):
    from core.startup_admission import STARTUP_REFUSED_EXIT, StartupRefused
    import qronos_media.host as host_module
    from qronos_media import cli

    def refuse(root):
        raise StartupRefused("update_in_progress")

    monkeypatch.setattr(host_module, "media_store_admission", refuse)
    target = tmp_path / "cli-data"
    assert cli.main(["--data-dir", str(target), "roots"]) == STARTUP_REFUSED_EXIT
    assert json.loads(capsys.readouterr().out)["code"] == "update_in_progress"
    assert not target.exists()


# --------------------------------------------------------------- the bridge


def test_the_bridge_host_uses_the_queue_governor_the_shared_ledger_and_the_bridge_approvals(tmp_path, monkeypatch):
    from core import runtime_bridge
    from core.approval_ledger import ApprovalLedger
    from core.resource_ownership import GLOBAL_RESOURCE_LEDGER

    monkeypatch.setenv("QRONOS_MEDIA_DATA_DIR", str(tmp_path / "media"))
    governor = ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None)

    class Bridge:
        def __init__(self) -> None:
            self._media_host = None
            self._media_host_lock = threading.Lock()
            self._approvals = ApprovalLedger()
            self.action_audit = None
            self.runtime_paused = False
            self.busy = False
            self.scheduler = type("Scheduler", (), {"governor": governor})()

        def ensure_scheduler(self):
            return self.scheduler

        def _media_foreground_busy(self) -> bool:
            return self.busy

        def _memory_sidecar_owner(self):  # round 26: the bridge also hands over its memory sidecar owner
            return None

        def _memory_sidecar_generation(self):  # round 32: the atomic owner + generation pair replaces it
            return None

    bridge = Bridge()
    host = runtime_bridge.QronosRuntime.ensure_media_host(bridge)
    try:
        assert host is runtime_bridge.QronosRuntime.ensure_media_host(bridge)  # one host
        assert host.authority.governor is governor and host.authority.ledger is GLOBAL_RESOURCE_LEDGER
        assert host._permissions().ledger is bridge._approvals
        bridge.busy = True  # (read before any activity reading)
        assert host.authority.yield_reason() == "foreground"
        assert host.status()["workerStarted"] is False  # nothing runs until asked
    finally:
        host.close()


def test_importing_the_media_host_or_the_bridge_starts_nothing(tmp_path):
    code = (
        "import sys, threading\n"
        "import core.activity_guard as guard\n"
        "made = []\n"
        "original = guard.ActivityGuard.__init__\n"
        "guard.ActivityGuard.__init__ = lambda self, *a, **k: (made.append(1), original(self, *a, **k))[1]\n"
        "import core.runtime_bridge\n"
        "print('media-imported-by-bridge', 'qronos_media' in sys.modules)\n"
        "import qronos_media.host, qronos_media.model_authority, qronos_media.asr, qronos_media.stages\n"
        "print('threads', threading.active_count(), 'guards', len(made))\n"
    )
    executable, environment = child_python()
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "QRONOS_RUN_LIVE_DEVICE_TESTS": "0",
                        "QRONOS_OLLAMA_AUTO_START": "0"})
    done = subprocess.run([executable, "-B", "-c", code], env=environment, capture_output=True, timeout=120,
                          cwd=str(Path(__file__).resolve().parents[1]))
    out = done.stdout.decode()
    assert done.returncode == 0, done.stderr.decode()[-2000:]
    assert "media-imported-by-bridge False" in out and "threads 1 guards 0" in out, out


# ------------------------------------------------------------ schema 2


V1_TABLES = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE assets (asset_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, size INTEGER NOT NULL,
  duration_ms INTEGER, start_offset_ms INTEGER, video_end_ms INTEGER, container TEXT, title TEXT, probe_json TEXT,
  priority INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE subtitle_tracks (track_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  source TEXT NOT NULL CHECK (source IN ('embedded', 'sidecar')), stream_index INTEGER, sidecar_path TEXT,
  sidecar_sha256 TEXT, language TEXT, title TEXT, codec TEXT, forced INTEGER NOT NULL DEFAULT 0, encoding TEXT,
  status TEXT NOT NULL CHECK (status IN ('indexed', 'unsupported_image', 'failed')), detail TEXT,
  cue_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE dialogue (unit_id INTEGER PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  track_id TEXT NOT NULL REFERENCES subtitle_tracks(track_id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('cue', 'pair')), seq INTEGER NOT NULL, start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL, text TEXT NOT NULL, norm_text TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '1');
INSERT INTO assets(asset_id, sha256, size, created_at, updated_at) VALUES ('a1', 's1', 1, 0, 0);
INSERT INTO subtitle_tracks(track_id, asset_id, source, status, cue_count) VALUES ('a1:e1', 'a1', 'embedded', 'indexed', 1);
INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text)
  VALUES ('a1', 'a1:e1', 'cue', 0, 0, 1, 'kept line', 'kept line');
"""


def test_a_schema_1_store_migrates_once_and_a_newer_one_is_refused_untouched(tmp_path):
    from qronos_media.store import MediaStore, StoreTooNew

    path = tmp_path / "v1.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(V1_TABLES)
    db.close()
    store = MediaStore(path)
    try:
        assert store.get_meta("schema_version") == "4"  # schema 4 (topic segments) migrates a schema-1 file too
        assert store.db.execute("SELECT count(*) FROM topic_segments").fetchone()[0] == 0
        assert store.db.execute("SELECT text FROM dialogue").fetchone()[0] == "kept line"
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        with store.transaction() as tx:
            tx.execute("INSERT INTO subtitle_tracks(track_id, asset_id, source, status, provenance_json) "
                       "VALUES ('a1:asr1', 'a1', 'asr', 'indexed', '{}')")
        assert store.db.execute("SELECT count(*) FROM dialogue_fts WHERE dialogue_fts MATCH 'kept'").fetchone()[0] == 0
    finally:
        store.close()
    newer = tmp_path / "v5.sqlite3"
    db = sqlite3.connect(newer)
    db.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                     "INSERT INTO meta VALUES ('schema_version', '5');")
    db.close()
    before = newer.read_bytes()
    with pytest.raises(StoreTooNew):
        MediaStore(newer)
    assert newer.read_bytes() == before
