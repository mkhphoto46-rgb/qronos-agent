"""Round 30 repair of Main29 F1/F2: media approvals through the REAL bridge lifecycle, and the no-shell player.

F1: media approvals are their own ApprovalKind (MEDIA_ACTION) on the bridge's one shared ledger. The real Pause, Stop
and close paths of QronosRuntime revoke them without reading their payload, and the generic file-decision handler
refuses a media approval id (wrong kind, left untouched). Ordinary file approvals keep their own behaviour.
F2: a trusted player is an mpv/VLC ``.exe``; a script named like a player (.cmd, .bat, .com, ...) is never one.

Synthetic only: a real QronosRuntime under the suite's profile isolation, a monkeypatched media gate, the author's
world fixture, fake player catalogs and a recording launcher. No real player, model, server, archive or shell starts.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core import computer_executor
from core.actions import InvalidAction
from tests.test_media_product import Gate, add_and_scan, allow_everything, ask, world  # noqa: F401 (fixture)

ORIGINAL_OPEN = computer_executor.open_media_at_time  # each fake catalog records through the real argv builder


def media_results(events: list) -> list[dict]:
    return [json.loads(message) for event, _, message in events if event == "media_result"]


def wait_for(predicate, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def stand_in(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"stand-in, never executed")
    return path


def spec(app_id: str, executable: Path, name: str = "VLC media player"):
    return computer_executor.AppSpec(app_id=app_id, executable=str(executable), display_name=name)


def catalog(monkeypatch, entries: dict) -> list:
    """A fake trusted application catalog and a recording launcher (nothing is started)."""
    monkeypatch.setattr(computer_executor, "application_catalog", lambda refresh=False: dict(entries))
    launched: list = []
    monkeypatch.setattr(computer_executor, "open_media_at_time",
                        lambda s, path, seconds, launcher=None: ORIGINAL_OPEN(s, path, seconds,
                                                                              launcher=launched.append))
    return launched


# ------------------------------------------------------------ F1: the real bridge lifecycle

@pytest.fixture
def runtime_with_media(tmp_path, monkeypatch):
    from core.runtime_bridge import QronosRuntime
    from qronos_media import permissions

    monkeypatch.setenv("QRONOS_MEDIA_DATA_DIR", str(tmp_path / "media-data"))
    # roots.add is a Files READ request: the fixture gate's read decision makes it ask
    monkeypatch.setattr(permissions, "evaluate", Gate("require_ui_confirmation", read="require_ui_confirmation"))
    events: list = []
    runtime = QronosRuntime(notify=lambda event, status, message: events.append((event, status, message)))
    library = tmp_path / "library"
    library.mkdir()
    yield runtime, events, library
    host = getattr(runtime, "_media_host", None)
    if host is not None:
        host.close()


def media_action(runtime, events, request_id: str, **data) -> dict:
    from core import runtime_bridge

    runtime_bridge.handle_action(runtime, {"actionId": "qronos.media", "data": {"requestId": request_id, **data}})
    return wait_for(lambda: next((r for r in media_results(events) if r.get("requestId") == request_id), None))


def pending_root(runtime, events, library, request_id="d1") -> str:
    answer = media_action(runtime, events, request_id, op="roots.add", path=str(library), kind="folder", maxDepth=2)
    assert answer["status"] == "approval_required", answer
    return answer["approvalId"]


def test_control_pause_without_a_media_approval_completes(runtime_with_media):
    runtime, events, _ = runtime_with_media
    runtime.set_runtime_paused(True)
    assert runtime.runtime_paused and any(e[0] == "runtime_pause_changed" for e in events)


def test_pause_with_a_pending_media_approval_completes_and_revokes_it(runtime_with_media):
    from core.approval_ledger import ApprovalKind

    runtime, events, library = runtime_with_media
    approval = pending_root(runtime, events, library)
    assert [t.kind for t in runtime._approvals.open_tickets()] == [ApprovalKind.MEDIA_ACTION]
    runtime.set_runtime_paused(True)  # the bridge's Pause point finishes with a media ticket pending
    assert any(e[0] == "runtime_pause_changed" for e in events)
    assert runtime._approvals.open_tickets() == ()
    runtime.set_runtime_paused(False)
    late = media_action(runtime, events, "d2", op="decide", approvalId=approval, approved=True)
    assert late["ok"] is False and late["code"] == "stale_approval"
    assert media_action(runtime, events, "d3", op="roots.list")["roots"] == []


def test_stop_with_a_pending_media_approval_answers_and_revokes_it(runtime_with_media):
    runtime, events, library = runtime_with_media
    approval = pending_root(runtime, events, library)
    result = runtime.request_stop_response()
    assert isinstance(result, dict) and runtime._approvals.open_tickets() == ()
    late = media_action(runtime, events, "d2", op="decide", approvalId=approval, approved=True)
    assert late["ok"] is False and late["code"] == "stale_approval"


def test_close_with_a_pending_media_approval_completes(runtime_with_media):
    runtime, events, library = runtime_with_media
    pending_root(runtime, events, library)
    runtime.close()  # the bridge's shutdown point
    assert runtime._approvals.open_tickets() == ()


def test_a_media_approval_sent_as_a_file_decision_is_refused_and_left_for_media(runtime_with_media, monkeypatch):
    from qronos_media import permissions

    runtime, events, library = runtime_with_media
    approval = pending_root(runtime, events, library)
    for approved in (False, True):  # the desktop can send any token to the file-decision action
        runtime.resolve_file_action_decision(approval, approved=approved)
    stale = [json.loads(m) for e, _, m in events if e == "file_action_stale"]
    assert [s["actionToken"] for s in stale] == [approval, approval] and {s["reason"] for s in stale} == {"wrong_kind"}
    assert len(runtime._approvals.open_tickets()) == 1  # untouched: still the media question
    monkeypatch.setattr(permissions, "evaluate", Gate("allow", read="allow"))
    done = media_action(runtime, events, "d4", op="decide", approvalId=approval, approved=True)
    assert done["ok"] and done["status"] == "added", done


def test_pause_cancels_a_pending_file_action_and_a_media_approval_together(tmp_path, monkeypatch):
    from qronos_media import permissions
    from tests.test_activity_history import Harness

    monkeypatch.setenv("QRONOS_MEDIA_DATA_DIR", str(tmp_path / "media-data"))
    monkeypatch.setattr(permissions, "evaluate", Gate("require_ui_confirmation", read="require_ui_confirmation"))
    harness = Harness(tmp_path)
    runtime, events = harness.runtime, harness.events
    try:
        source = harness.source()
        runtime.run_text_turn(f'فایل "{source}" را در پوشه "{harness.backup}" کپی کن', "file-req")
        file_token = harness.payload("file_action_approval_required")["actionToken"]
        library = tmp_path / "library"
        library.mkdir()
        pending_root(runtime, events, library)
        runtime.set_runtime_paused(True)
        cancelled = [json.loads(m) for e, _, m in events if e == "file_action_cancelled"]
        assert [c["actionToken"] for c in cancelled] == [file_token]  # the file action keeps its own answer
        assert any(e[0] == "runtime_pause_changed" for e in events) and runtime._approvals.open_tickets() == ()
        assert not (harness.backup / source.name).exists()
    finally:
        host = getattr(runtime, "_media_host", None)
        if host is not None:
            host.close()


# ------------------------------------------------------------ F2: only an mpv/VLC .exe is a player

@pytest.mark.parametrize("name", ["mpv.cmd", "vlc.bat", "mpv.com", "VLC.CMD", "vlc.ps1", "mpv"])
def test_a_script_or_non_exe_named_like_a_player_is_never_a_player(tmp_path, monkeypatch, name):
    media = stand_in(tmp_path / "film.mkv")
    script = stand_in(tmp_path / "Programs" / name)
    monkeypatch.setattr(computer_executor, "application_catalog",
                        lambda refresh=False: {"p": spec("p", script, "Player")})
    assert computer_executor.trusted_media_players() == {}
    with pytest.raises(InvalidAction):  # also refused when handed straight to the argv builder
        computer_executor.media_player_argv(spec("p", script, "Player"), str(media), 1.0)



@pytest.mark.parametrize("name", ["mpv.cmd", "vlc.bat", "mpv.com"])
def test_the_argv_builder_refuses_a_script_even_if_the_catalog_filter_let_it_through(tmp_path, monkeypatch, name):
    # The second layer on its own: the builder re-checks the resolved executable, whatever the filter answered.
    media = stand_in(tmp_path / "film.mkv")
    script = spec("p", stand_in(tmp_path / "Programs" / name), "Player")
    monkeypatch.setattr(computer_executor, "trusted_media_players", lambda refresh=False: {"p": script})
    with pytest.raises(InvalidAction):
        computer_executor.media_player_argv(script, str(media), 1.0)

@pytest.mark.parametrize("name, option", [("vlc.exe", "--start-time=2.250"), ("mpv.exe", "--start=2.250"),
                                          ("VLC.EXE", "--start-time=2.250")])
def test_control_an_mpv_or_vlc_exe_is_a_player_with_the_exact_argv(tmp_path, monkeypatch, name, option):
    media = stand_in(tmp_path / "film.mkv")
    player = stand_in(tmp_path / "Programs" / name)
    good = spec("p", player, "Player")
    catalog(monkeypatch, {"p": good})
    assert set(computer_executor.trusted_media_players()) == {"p"}
    assert computer_executor.media_player_argv(good, str(media), 2.25) == [str(player.resolve()), option, str(media)]


def test_control_the_product_open_launches_only_the_catalog_exe_player(world, monkeypatch, tmp_path):
    asset = add_and_scan(world, monkeypatch)
    vlc = stand_in(tmp_path / "P" / "vlc.exe")
    script = stand_in(tmp_path / "S" / "mpv.cmd")
    launched = catalog(monkeypatch, {"a-script": spec("a-script", script, "mpv"), "vlc": spec("vlc", vlc)})
    allow_everything(monkeypatch)
    done = ask(world, "open.prepare", "c1", assetId=asset, startMs=1500)
    assert done["status"] == "launched" and launched == [
        [str(vlc.resolve()), "--start-time=1.500", str(world.library / "film.mkv")]]
    launched = catalog(monkeypatch, {"a-script": spec("a-script", script, "mpv")})  # only a script left
    assert ask(world, "open.prepare", "c2", assetId=asset, startMs=1500)["status"] == "no_player" and launched == []
