"""Round 32: the media product route (qronos_media.product + host product calls + bridge action + intents).

Fixtures only: synthetic dialogue rows, the fake owned sidecar of test_media_semantic, fake permissions or the real
GatePermissions on a private ApprovalLedger with a monkeypatched gate, a fake player catalog and a recording
launcher. No real player, model, server, archive or device.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from core.approval_ledger import ApprovalLedger
from core.resource_governor import ResourceGovernor
from core.resource_ownership import ResourceLedger
from qronos_media import product as product_module
from qronos_media.intents import MediaSearchIntent, MediaStatusIntent, parse_media_intent, spoken_answer
from qronos_media.product import MediaProduct
from tests.media.media_support import quiet_activity
from tests.test_media_semantic import CUES_EN, EMBEDDERS, QUIET, FakeSidecar


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict]] = []
        self.changed = threading.Condition()

    def __call__(self, event: str, status: str, payload: dict) -> None:
        json.dumps(payload)  # every answer is JSON-serialisable for the runtime event
        with self.changed:
            self.items.append((event, status, payload))
            self.changed.notify_all()

    def result(self, request_id: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                for event, _, payload in self.items:
                    if event == "media_result" and payload.get("requestId") == request_id:
                        return payload
                left = deadline - time.monotonic()
                assert left > 0, f"no answer for {request_id}"
                self.changed.wait(left)


class Gate:
    """Monkeypatched security.gate.evaluate for qronos_media.permissions: Files reads get ``read``, every other
    category (open, write) gets ``decision``."""

    def __init__(self, decision: str, read: str = "allow") -> None:
        self.decision = decision
        self.read = read

    def __call__(self, request, audit=None):
        from core.actions import ActionOutcome
        from security.gate import Verdict
        from security.permissions import ActionCategory, PermissionDecision

        decision = PermissionDecision(self.read if request.category is ActionCategory.READ_NON_SENSITIVE
                                      else self.decision)
        outcome = (ActionOutcome.SUCCEEDED if decision is PermissionDecision.ALLOW else ActionOutcome.REFUSED
                   if decision is PermissionDecision.DENY else ActionOutcome.AWAITING_APPROVAL)
        return Verdict(request=request, decision=decision, outcome=outcome, reason="fixture policy")


@pytest.fixture
def world(tmp_path, monkeypatch):
    from core import local_ipc
    from qronos_media.host import build_bridge_media_host

    monkeypatch.setenv("QRONOS_MEDIA_DATA_DIR", str(tmp_path / "media-data"))
    sidecar = FakeSidecar()
    monkeypatch.setattr(local_ipc, "request_json", sidecar)
    approvals = ApprovalLedger()
    governor, ledger = ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None), ResourceLedger()
    secrets = type("Secrets", (), {"verify": staticmethod(lambda secret: secret == "correct horse")})()
    host = build_bridge_media_host(governor=governor, ledger=ledger, foreground_busy=lambda: False,
                                   paused=lambda: False, approvals=approvals, audit=None, activity=quiet_activity,
                                   embedding_owner=lambda: None, secrets=secrets)
    events, paused = Events(), [False]
    route = MediaProduct(host, events, paused=lambda: paused[0])
    library = tmp_path / "library"
    library.mkdir()
    (library / "film.mkv").write_bytes(b"synthetic container stand-in")
    yield type("World", (), {"host": host, "route": route, "events": events, "paused": paused, "library": library,
                             "approvals": approvals, "sidecar": sidecar, "tmp": tmp_path})
    host.close()


def ask(world, op: str, request_id: str, **fields) -> dict:
    world.route.handle({"op": op, "requestId": request_id, **fields})
    return world.events.result(request_id)


def allow_everything(monkeypatch, decision: str = "allow", read: str | None = None) -> None:
    from qronos_media import permissions

    monkeypatch.setattr(permissions, "evaluate", Gate(decision, read if read is not None else
                                                      ("allow" if decision == "allow" else decision)))


def seed_dialogue(world) -> str:
    """Synthetic indexed dialogue on the one real asset (written through the worker, like any product call)."""
    done = threading.Event()
    holder = {}

    def write(service):
        asset = service.store.db.execute("SELECT asset_id FROM assets").fetchone()[0]
        with service.store.transaction() as tx:
            tx.execute("INSERT INTO subtitle_tracks(track_id, asset_id, source, stream_index, language, status, "
                       "cue_count) VALUES (?, ?, 'embedded', 2, 'eng', 'indexed', ?)",
                       (f"{asset}:e2", asset, len(CUES_EN)))
            for seq, text in enumerate(CUES_EN):
                tx.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                           "VALUES (?, ?, 'cue', ?, ?, ?, ?, ?)",
                           (asset, f"{asset}:e2", seq, 10_000 * seq + 500, 10_000 * seq + 4_000, text, text.lower()))
        holder["asset"] = asset
        return {"ok": True}

    world.host.call("seed", write, lambda result: done.set())
    assert done.wait(20)
    return holder["asset"]


def add_and_scan(world, monkeypatch) -> str:
    allow_everything(monkeypatch)
    added = ask(world, "roots.add", "r1", path=str(world.library), kind="folder", maxDepth=2)
    assert added["ok"] and added["status"] == "added", added
    scanned = ask(world, "scan", "s1")
    assert scanned["ok"] and scanned["index"]["accepted"], scanned
    deadline = time.monotonic() + 30
    while True:
        status = ask(world, "status", f"st{time.monotonic_ns()}")["status"]
        if (status.get("store") or {}).get("assets") and not status["running"] and not status["queued"]:
            break
        assert time.monotonic() < deadline, status
        time.sleep(0.05)
    return seed_dialogue(world)


# ------------------------------------------------------------------ validation and bounds

@pytest.mark.parametrize("data, code", [
    ("not an object", "bad_request"),
    ({"op": "status"}, "bad_request"),  # no requestId
    ({"op": "status", "requestId": "has space"}, "bad_request"),
    ({"op": "format_disk", "requestId": "x1"}, "unknown_op"),
    ({"op": "search", "requestId": "x2", "query": ""}, "bad_request"),
    ({"op": "search", "requestId": "x3", "query": "a" * 1201}, "bad_request"),
    ({"op": "search", "requestId": "x4", "query": "key", "mode": "sql"}, "bad_request"),
    ({"op": "search", "requestId": "x5", "query": "key", "limit": 51}, "bad_request"),
    ({"op": "search", "requestId": "x6", "query": "key", "limit": True}, "bad_request"),
    ({"op": "roots.add", "requestId": "x7", "path": "C:\\x", "maxDepth": 65}, "bad_request"),
    ({"op": "job.retry", "requestId": "x8", "assetId": "../../etc", "stage": "hash"}, "bad_request"),
    ({"op": "job.retry", "requestId": "x9", "assetId": "a_" + "0" * 32, "stage": "drop_table"}, "bad_request"),
    ({"op": "open.prepare", "requestId": "y1", "assetId": "a_" + "0" * 32, "startMs": -1}, "bad_request"),
    ({"op": "report.generate", "requestId": "y2", "assetId": "a_" + "0" * 32, "format": "exe"}, "bad_request"),
    ({"op": "report.generate", "requestId": "y5", "assetId": "a_" + "0" * 32, "format": "pdf", "kind": "summary"},
     "bad_request"),
    ({"op": "report.export", "requestId": "y6", "assetId": "a_" + "0" * 32, "format": "pdf", "kind": ["full"],
      "destination": "C:\\x.pdf"}, "bad_request"),
    ({"op": "report.preview", "requestId": "y7", "assetId": "a_" + "0" * 32, "kind": "topics"}, "bad_request"),
    ({"op": "decide", "requestId": "y3", "approvalId": "unknown-approval-id", "approved": True}, "stale_approval"),
    ({"op": "decide", "requestId": "y4", "approvalId": "short"}, "bad_request"),
])
def test_requests_are_validated_before_anything_runs(world, data, code):
    world.route.handle(data)
    answer = world.events.items[-1][2]
    assert answer["ok"] is False and answer["code"] == code, answer
    assert world.host.status()["workerStarted"] is False  # nothing reached the worker


def test_paused_refuses_work_but_still_answers_reads(world):
    world.paused[0] = True
    assert ask(world, "index.start", "p1")["code"] == "paused"
    assert ask(world, "roots.add", "p2", path="C:\\anything")["code"] == "paused"
    assert ask(world, "status", "p3")["ok"] is True
    caps = ask(world, "capabilities", "p4")
    statuses = {c["capability"]: c["status"] for c in caps["capabilities"]}
    assert statuses["asr"] == "implemented_unverified" and statuses["topics"] == "implemented_unverified"
    assert statuses["ocr"] == "pending" and statuses["actions_motion"] == "not_supported"


# ------------------------------------------------------------------ the workflow

def test_add_root_scan_index_and_search_through_the_one_host(world, monkeypatch):
    asset = add_and_scan(world, monkeypatch)
    exact = ask(world, "search", "q1", query="the train to the coast", mode="exact")
    hits = exact["results"]["results"]
    assert exact["ok"] and hits and hits[0]["assetId"] == asset and hits[0]["startMs"] == 30_500
    assert "semantic_dialogue" in exact["results"]["notInThisCall"]
    semantic = ask(world, "search", "q2", query="key", mode="semantic")
    assert semantic["ok"] and semantic["semantic"]["status"] == "unavailable"
    assert semantic["semantic"]["reason"] == "model_unavailable" and "exact" in semantic  # honest, lexical kept
    detail = ask(world, "asset.detail", "d1", assetId=asset)
    assert detail["ok"] and detail["asset"]["asset_id"] == asset and "stages" in detail["asset"]
    assert ask(world, "asset.priority", "pr1", assetId=asset, priority=50)["ok"]
    retried = ask(world, "job.reindex", "j1", assetId=asset, stage="semantic")
    assert retried["ok"] and retried["assetId"] == asset
    assert world.host.authority.ledger.active() == ()  # no model was reserved without an owner


def test_search_answers_while_an_index_pass_is_running(world, monkeypatch):
    asset = add_and_scan(world, monkeypatch)
    started, release = threading.Event(), threading.Event()

    def slow(service):
        started.set()
        release.wait(20)  # stands for a long index pass holding the writer worker
        return {"ok": True}

    world.host.call("slow", slow, lambda result: None)
    assert started.wait(10)
    try:
        t0 = time.monotonic()
        answer = ask(world, "search", "q3", query="the train to the coast", mode="exact", limit=5)
        assert answer["ok"] and answer["results"]["results"][0]["assetId"] == asset
        assert time.monotonic() - t0 < 10  # the query thread did not wait for the writer
    finally:
        release.set()


def test_approval_required_root_is_single_use_secret_bound_and_never_echoed(world, monkeypatch):
    allow_everything(monkeypatch, "require_typed_secret")
    pending = ask(world, "roots.add", "a1", path=str(world.library))
    assert pending["ok"] and pending["status"] == "approval_required" and pending["needsSecret"] is True
    approval = pending["approvalId"]
    wrong = ask(world, "decide", "a2", approvalId=approval, approved=True, secret="wrong guess")
    assert wrong["ok"] is False and wrong["code"] == "secret_rejected"
    again = ask(world, "decide", "a3", approvalId=approval, approved=True, secret="correct horse")
    assert again["code"] == "stale_approval"  # the identity was spent by the wrong answer
    fresh = ask(world, "roots.add", "a4", path=str(world.library))
    done = ask(world, "decide", "a5", approvalId=fresh["approvalId"], approved=True, secret="correct horse")
    assert done["ok"] and done["status"] == "added", done
    flat = json.dumps([payload for _, _, payload in world.events.items])
    assert "correct horse" not in flat and "wrong guess" not in flat


def test_a_declined_or_revoked_approval_runs_nothing(world, monkeypatch):
    from core.approval_ledger import ApprovalKind

    allow_everything(monkeypatch, "require_ui_confirmation")
    pending = ask(world, "roots.add", "b1", path=str(world.library))
    assert ask(world, "decline", "b2", approvalId=pending["approvalId"])["declined"] is True
    assert ask(world, "decide", "b3", approvalId=pending["approvalId"], approved=True)["code"] == "stale_approval"
    revoked = ask(world, "roots.add", "b4", path=str(world.library))
    # what the bridge's Pause/Stop/close revoke (tests/test_media_approval_lifecycle.py drives the real ones)
    world.approvals.revoke_kinds((ApprovalKind.MEDIA_ACTION,), "paused")
    late = ask(world, "decide", "b5", approvalId=revoked["approvalId"], approved=True)
    assert late["ok"] is False and late["code"] == "stale_approval"
    assert ask(world, "roots.list", "b6")["roots"] == []


def test_open_at_time_uses_only_the_trusted_catalog_player_after_verification(world, monkeypatch, tmp_path):
    from core import computer_executor

    asset = add_and_scan(world, monkeypatch)
    player = tmp_path / "Programs" / "vlc.exe"
    player.parent.mkdir()
    player.write_bytes(b"stand-in")
    spec = computer_executor.AppSpec(app_id="vlc", executable=str(player), display_name="VLC media player")
    monkeypatch.setattr(computer_executor, "application_catalog", lambda refresh=False: {"vlc": spec})
    launched = []
    real_open = computer_executor.open_media_at_time
    monkeypatch.setattr(computer_executor, "open_media_at_time",
                        lambda s, path, seconds, launcher=None: real_open(s, path, seconds,
                                                                          launcher=launched.append))
    allow_everything(monkeypatch, "require_ui_confirmation", read="deny")  # Files read revoked: refused first
    revoked = ask(world, "open.prepare", "o0", assetId=asset, startMs=30_500)
    assert revoked["status"] == "not_verified" and revoked["verification"]["status"] == "refused" and launched == []
    allow_everything(monkeypatch, "require_ui_confirmation", read="allow")
    pending = ask(world, "open.prepare", "o1", assetId=asset, startMs=30_500)
    assert pending["status"] == "approval_required" and pending["preview"]["player"] == "VLC media player", \
        json.dumps(pending)[:1500]
    assert launched == []  # nothing runs before the approval
    done = ask(world, "decide", "o2", approvalId=pending["approvalId"], approved=True)
    assert done["ok"] and done["status"] == "launched", done
    (argv,) = launched
    assert argv == [str(player.resolve()), "--start-time=30.500", str(world.library / "film.mkv")]
    # a replaced copy is refused at decision time: re-verified, nothing launched
    again = ask(world, "open.prepare", "o3", assetId=asset, startMs=1000)
    (world.library / "film.mkv").write_bytes(b"a different cut entirely")
    refused = ask(world, "decide", "o4", approvalId=again["approvalId"], approved=True)
    assert refused["status"] == "not_verified" and len(launched) == 1


def test_the_executor_refuses_anything_but_a_catalog_player_and_a_real_file(tmp_path, monkeypatch):
    from core import computer_executor
    from core.actions import InvalidAction

    media = tmp_path / "film.mkv"
    media.write_bytes(b"x")
    notepad = tmp_path / "notepad.exe"
    notepad.write_bytes(b"x")
    vlc = tmp_path / "vlc.exe"
    vlc.write_bytes(b"x")
    good = computer_executor.AppSpec(app_id="vlc", executable=str(vlc), display_name="VLC")
    other = computer_executor.AppSpec(app_id="notepad", executable=str(notepad), display_name="Notepad")
    monkeypatch.setattr(computer_executor, "application_catalog",
                        lambda refresh=False: {"vlc": good, "notepad": other})
    assert set(computer_executor.trusted_media_players()) == {"vlc"}
    for spec, path, seconds in ((other, str(media), 1), (good, "relative.mkv", 1), (good, str(tmp_path / "no.mkv"), 1),
                                (good, str(media), -1), (good, str(media), float("nan")),
                                (computer_executor.AppSpec(app_id="vlc", executable=str(vlc), display_name="forged"),
                                 str(media), 1)):
        with pytest.raises(InvalidAction):
            computer_executor.media_player_argv(spec, path, seconds)


def test_reports_generate_with_honest_capabilities_and_export_needs_approval(world, monkeypatch, tmp_path):
    asset = add_and_scan(world, monkeypatch)
    html = ask(world, "report.generate", "g1", assetId=asset, format="html")
    text = Path(html["report"]["path"]).read_text(encoding="utf-8")
    implemented, missing = text.split("هنوز در دسترس نیست", 1)
    assert "asr: implemented_unverified" in implemented and "asr:" not in missing
    assert "ocr: pending" in missing
    allow_everything(monkeypatch, "require_ui_confirmation", read="allow")
    destination = tmp_path / "out" / "report.csv"
    destination.parent.mkdir()
    pending = ask(world, "report.export", "e1", assetId=asset, destination=str(destination), format="csv")
    assert pending["status"] == "approval_required" and not destination.exists()
    done = ask(world, "decide", "e2", approvalId=pending["approvalId"], approved=True)
    assert done["ok"] and done["status"] == "written" and destination.exists(), done


def test_a_pdf_report_is_generated_previewed_and_exported_through_the_same_gate(world, monkeypatch, tmp_path):
    asset = add_and_scan(world, monkeypatch)
    assert ask(world, "capabilities", "c1")["reports"] == {"formats": ["json", "html", "csv", "pdf"],
                                                           "kinds": ["short", "full", "transcript", "evidence"]}
    made = ask(world, "report.generate", "g1", assetId=asset, format="pdf", kind="transcript")
    assert made["ok"] and made["report"]["kind"] == "transcript", made
    assert Path(made["report"]["path"]).read_bytes().startswith(b"%PDF-")
    world.paused[0] = True  # a preview only reads the index: allowed while paused, like search
    view = ask(world, "report.preview", "p1", assetId=asset, kind="short")
    world.paused[0] = False
    assert view["ok"] and view["preview"]["kind"] == "short" and "limitations" in view["preview"], view
    allow_everything(monkeypatch, "require_ui_confirmation", read="allow")
    destination = tmp_path / "out" / "report.pdf"
    destination.parent.mkdir()
    pending = ask(world, "report.export", "e1", assetId=asset, destination=str(destination), format="pdf",
                  kind="full")
    assert pending["status"] == "approval_required" and not destination.exists(), pending
    done = ask(world, "decide", "e2", approvalId=pending["approvalId"], approved=True)
    assert done["ok"] and done["status"] == "written" and done["format"] == "pdf", done
    assert destination.read_bytes().startswith(b"%PDF-") and done["kind"] == "full"
    again = ask(world, "decide", "e3", approvalId=pending["approvalId"], approved=True)
    assert again["ok"] is False and again["code"] == "stale_approval"


def test_stop_reaches_the_same_host_and_queued_calls_are_answered(world):
    started, release = threading.Event(), threading.Event()
    world.host.call("hold", lambda s: (started.set(), release.wait(10), {"ok": True})[2], lambda r: None)
    assert started.wait(10)
    world.route.handle({"op": "roots.list", "requestId": "z1"})
    world.route.handle({"op": "index.stop", "requestId": "z2"})
    assert world.events.result("z2")["stopped"] is True
    assert world.events.result("z1")["code"] == "stopped"  # never runs after the Stop, but is answered
    assert world.host.authority.yield_reason() == "user_stop"
    release.set()


# ------------------------------------------------------------------ brain / voice

@pytest.mark.parametrize("text, expected", [
    ("در ویدیوهام «کلید قدیمی» را پیدا کن", MediaSearchIntent("کلید قدیمی", "exact")),
    ("در فیلم‌ها دنبال «قطار ساحلی» بگرد، معنایی", MediaSearchIntent("قطار ساحلی", "semantic")),
    ("search my videos for the old key", MediaSearchIntent("the old key", "exact")),
    ('find "the train" in my movies, fuzzy', MediaSearchIntent("the train", "fuzzy")),
    ("وضعیت فهرست ویدیوها", MediaStatusIntent()),
    ("search for movies about space", None),  # not a request about the person's library
    ("find a movie to watch tonight", None),
    ("وضعیت هوا چطوره", None),
    ("یک فیلم خوب پیشنهاد بده", None),
])
def test_media_intents_are_deterministic_and_never_guess(text, expected):
    assert parse_media_intent(text) == expected


def test_the_spoken_answer_says_only_what_the_result_says():
    hit = {"title": "Harbour", "startMs": 30_500}
    assert "1 نتیجه" in spoken_answer(MediaSearchIntent("x", "exact"),
                                      {"ok": True, "results": {"results": [hit]}})
    assert "0:00:30" in spoken_answer(MediaSearchIntent("x", "exact"), {"ok": True, "results": {"results": [hit]}})
    assert "در دسترس نیست" in spoken_answer(MediaSearchIntent("x", "semantic"),
                                            {"ok": True, "semantic": {"status": "unavailable", "results": [],
                                                                      "reason": "model_unavailable"},
                                             "exact": {"results": []}})
    assert "به‌موقع" in spoken_answer(MediaStatusIntent(), None)
    assert "متوقف" in spoken_answer(MediaStatusIntent(), {"ok": False, "code": "paused"})


def test_the_bridge_routes_the_media_action_and_the_text_turn_to_the_product(monkeypatch):
    from core import runtime_bridge

    handled = []
    fake = type("Product", (), {"handle": lambda self, data, reply=None: (handled.append(data), reply and reply(
        {"ok": True, "results": {"results": []}}))})()
    runtime = type("Runtime", (), {"media_product": lambda self: fake, "runtime_paused": True})()
    monkeypatch.setattr(runtime_bridge, "emit", lambda *a, **k: None)
    runtime_bridge.handle_action(runtime, {"actionId": "qronos.media", "data": {"op": "status", "requestId": "w1"}})
    assert handled == [{"op": "status", "requestId": "w1"}]  # reached the product even while paused (it decides)
    answer = runtime_bridge.QronosRuntime._run_media_intent(runtime, MediaSearchIntent("key", "exact"))
    assert handled[-1]["op"] == "search" and handled[-1]["query"] == "key" and "پیدا نشد" in answer
