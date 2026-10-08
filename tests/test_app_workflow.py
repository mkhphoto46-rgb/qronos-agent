from __future__ import annotations

from dataclasses import replace

import pytest

from core.action_cancellation import (
    CANCEL_REASON_PAUSE,
    CANCEL_REASON_USER_STOP,
    CancellationToken,
)
from core.app_workflow import (
    STOP_APPROVAL_REVOKED,
    STOP_CANCELLED,
    STOP_EVIDENCE_EXPIRED,
    STOP_WINDOW_CHANGED,
    ApprovalMismatch,
    DuplicateSideEffect,
    SideEffectJournal,
    SideEffectState,
    StepGuard,
    WindowEvidence,
    WorkflowApproval,
    WorkflowError,
    WorkflowStopped,
    content_key,
    operation_key,
    plan_digest,
)
from core.window_binding import WindowBinder, WindowRect, WindowSnapshot, WindowTarget
from security.permissions import PermissionDecision


def snapshot(**changes) -> WindowSnapshot:
    base = WindowSnapshot(
        hwnd=4242,
        pid=900,
        thread_id=901,
        title="Telegram",
        process_path=r"C:\Apps\Telegram\Telegram.exe",
        process_start_time_100ns=1234567,
        window_rect=WindowRect(0, 0, 1000, 800),
        client_rect=WindowRect(8, 30, 992, 792),
        dpi=96,
        visible=True,
        minimized=False,
        foreground=True,
    )
    return replace(base, **changes)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def bound(current: dict) -> tuple[WindowBinder, object]:
    binder = WindowBinder(
        enumerate_fn=lambda: (current["window"],),
        snapshot_fn=lambda hwnd: current["window"] if current["window"].hwnd == hwnd else None,
    )
    target = WindowTarget(app_id="telegram", display_name="Telegram", process_names=("telegram.exe",))
    return binder, binder.bind(target)


PLAN = {"workflow": "telegram_download", "contact": "Sara Ahmadi", "attachment": "report.pdf"}


def guard_for(current, *, token=None, valid=lambda: True, clock=None, max_age=20.0):
    clock = clock or Clock()
    binder, binding = bound(current)
    evidence = WindowEvidence.from_binding(binding, clock=clock, capture_png=b"png-bytes")
    guard = StepGuard(
        token=token,
        approval_valid=valid,
        binder=binder,
        binding=binding,
        evidence=evidence,
        max_evidence_age=max_age,
        clock=clock,
    )
    return guard, evidence, clock


def test_checkpoint_passes_and_records_steps_while_everything_holds():
    current = {"window": snapshot()}
    guard, evidence, _ = guard_for(current)
    assert guard.checkpoint("search contact") is not None
    assert [step.step for step in guard.steps] == ["search contact"]
    assert evidence.capture_sha256 and "png" not in str(evidence.to_json())


@pytest.mark.parametrize("reason", [CANCEL_REASON_USER_STOP, CANCEL_REASON_PAUSE])
def test_stop_or_pause_stops_at_the_next_checkpoint(reason):
    current = {"window": snapshot()}
    token = CancellationToken()
    guard, _, _ = guard_for(current, token=token)
    guard.checkpoint("open chat")
    token.cancel(reason)
    with pytest.raises(WorkflowStopped) as stopped:
        guard.checkpoint("download")
    assert stopped.value.reason == STOP_CANCELLED
    assert [step.step for step in guard.steps] == ["open chat"]


def test_revoked_approval_stops_before_the_side_effect():
    current = {"window": snapshot()}
    state = {"open": True}
    guard, _, _ = guard_for(current, valid=lambda: state["open"])
    guard.checkpoint("inspect messages")
    state["open"] = False
    with pytest.raises(WorkflowStopped) as stopped:
        guard.checkpoint("download")
    assert stopped.value.reason == STOP_APPROVAL_REVOKED


@pytest.mark.parametrize("change", [
    {"pid": 901},
    {"process_start_time_100ns": 999},
    {"process_path": r"C:\Other\Telegram.exe"},
])
def test_replaced_process_behind_the_same_hwnd_stops(change):
    current = {"window": snapshot()}
    guard, _, _ = guard_for(current)
    current["window"] = snapshot(**change)
    with pytest.raises(WorkflowStopped) as stopped:
        guard.checkpoint("download")
    assert stopped.value.reason == STOP_WINDOW_CHANGED


def test_moved_window_stops_only_when_layout_matters():
    current = {"window": snapshot()}
    guard, _, _ = guard_for(current)
    current["window"] = snapshot(window_rect=WindowRect(50, 0, 1050, 800), client_rect=WindowRect(58, 30, 1042, 792))
    guard.checkpoint("read message list")  # semantic step: layout may change
    with pytest.raises(WorkflowStopped) as stopped:
        guard.checkpoint("click download", require_same_layout=True)
    assert stopped.value.reason == STOP_WINDOW_CHANGED


def test_closed_window_and_minimised_window_stop():
    current = {"window": snapshot()}
    guard, _, _ = guard_for(current)
    current["window"] = snapshot(minimized=True)
    with pytest.raises(WorkflowStopped):
        guard.checkpoint("download")
    current["window"] = snapshot(hwnd=1)
    with pytest.raises(WorkflowStopped):
        guard.checkpoint("download")


def test_old_evidence_expires():
    current = {"window": snapshot()}
    guard, _, clock = guard_for(current, max_age=5.0)
    clock.now += 6.0
    with pytest.raises(WorkflowStopped) as stopped:
        guard.checkpoint("download")
    assert stopped.value.reason == STOP_EVIDENCE_EXPIRED


def test_refreshed_evidence_must_be_the_same_window():
    current = {"window": snapshot()}
    guard, evidence, clock = guard_for(current, max_age=5.0)
    clock.now += 4.0
    guard.refresh_evidence(replace(evidence, observed_at=clock.now))
    clock.now += 4.0
    guard.checkpoint("download")
    with pytest.raises(WorkflowStopped):
        guard.refresh_evidence(replace(evidence, identity_token="another-process"))


def test_approval_binds_plan_decision_and_window():
    current = {"window": snapshot()}
    _, evidence, _ = guard_for(current)
    approval = WorkflowApproval(
        approval_id="appr-1",
        plan_digest=plan_digest(PLAN),
        decision=PermissionDecision.REQUIRE_UI_CONFIRMATION,
        evidence_identity_token=evidence.identity_token,
    )
    approval.require_for(plan=PLAN, required=PermissionDecision.REQUIRE_UI_CONFIRMATION, evidence=evidence)
    with pytest.raises(ApprovalMismatch):
        approval.require_for(plan={**PLAN, "contact": "Sara Ahmadi 2"},
                             required=PermissionDecision.REQUIRE_UI_CONFIRMATION, evidence=evidence)
    with pytest.raises(ApprovalMismatch):
        approval.require_for(plan=PLAN, required=PermissionDecision.REQUIRE_TYPED_SECRET, evidence=evidence)
    with pytest.raises(ApprovalMismatch):
        approval.require_for(plan=PLAN, required=PermissionDecision.REQUIRE_UI_CONFIRMATION,
                             evidence=replace(evidence, identity_token="other"))


def test_plan_digest_is_order_independent_and_rejects_non_json():
    assert plan_digest({"a": 1, "b": "x"}) == plan_digest({"b": "x", "a": 1})
    with pytest.raises(ValueError):
        plan_digest({"a": object()})
    with pytest.raises(ValueError):
        plan_digest({"a": float("nan")})
    with pytest.raises(ValueError):
        plan_digest({})


def test_journal_runs_one_attempt_once_even_after_restart(tmp_path):
    path = tmp_path / "side-effects.json"
    journal = SideEffectJournal(path)
    op = operation_key("appr-1", plan_digest(PLAN))
    content = content_key("print", "sha256-of-file", "Office Printer")
    journal.begin(op, content, "print report.pdf")
    journal.settle(op, SideEffectState.CONFIRMED, "job 17")
    reopened = SideEffectJournal(path)
    with pytest.raises(DuplicateSideEffect):
        reopened.begin(op, content)
    assert reopened.get(op).state is SideEffectState.CONFIRMED
    # A new approval for the same content after a confirmed result is a new request.
    reopened.begin(operation_key("appr-2", plan_digest(PLAN)), content)


def test_crash_after_begin_blocks_repeat_of_the_same_content(tmp_path):
    path = tmp_path / "side-effects.json"
    content = content_key("print", "sha", "Printer")
    SideEffectJournal(path).begin("op-1", content)
    # Simulated crash: the process never settled. A fresh process must not repeat.
    restarted = SideEffectJournal(path)
    assert [entry.state for entry in restarted.unresolved_for(content)] == [SideEffectState.SUBMITTING]
    with pytest.raises(DuplicateSideEffect):
        restarted.begin("op-2", content)


def test_uncertain_outcome_needs_acknowledgement_before_retry(tmp_path):
    journal = SideEffectJournal(tmp_path / "j.json")
    content = content_key("download", "attachment-id", "folder")
    journal.begin("op-1", content)
    journal.settle("op-1", SideEffectState.UNCERTAIN, "no file appeared in time")
    with pytest.raises(DuplicateSideEffect):
        journal.begin("op-2", content)
    with pytest.raises(WorkflowError):
        journal.settle("op-1", SideEffectState.NOT_SUBMITTED)
    journal.acknowledge("op-1", "user checked the folder")
    journal.begin("op-2", content)
    with pytest.raises(WorkflowError):
        journal.acknowledge("op-2")


def test_late_evidence_may_confirm_an_uncertain_outcome(tmp_path):
    journal = SideEffectJournal(tmp_path / "j.json")
    journal.begin("op-1", "content")
    journal.settle("op-1", SideEffectState.UNCERTAIN, "job not visible yet")
    journal.settle("op-1", SideEffectState.CONFIRMED, "job 12 found later")
    assert journal.unresolved_for("content") == ()
    with pytest.raises(WorkflowError):
        journal.settle("op-1", SideEffectState.CONFIRMED)


def test_not_submitted_releases_the_content(tmp_path):
    journal = SideEffectJournal(tmp_path / "j.json")
    journal.begin("op-1", "content")
    journal.settle("op-1", SideEffectState.NOT_SUBMITTED, "printer offline before submission")
    journal.begin("op-2", "content")


def test_corrupt_journal_fails_closed(tmp_path):
    path = tmp_path / "j.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(Exception):
        SideEffectJournal(path).begin("op", "content")


# ------------------------------------------------- journal concurrency (W1)

import json as _json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

from core.app_workflow import JournalUnavailable
from core.json_store import JsonStore

ROOT = Path(__file__).resolve().parents[1]


def test_two_journal_objects_cannot_both_begin_the_same_content(tmp_path, monkeypatch):
    path = tmp_path / "side-effects.json"
    first, second = SideEffectJournal(path), SideEffectJournal(path)
    content = content_key("print", "sha", "printer")
    barrier = threading.Barrier(2, timeout=1.0)
    real_load = JsonStore.load_payload

    def slow_load(self, default=None):
        payload = real_load(self, default)
        try:
            barrier.wait()  # the old code let both read the pre-begin state here
        except threading.BrokenBarrierError:
            pass
        return payload

    monkeypatch.setattr(JsonStore, "load_payload", slow_load)
    results = {}

    def attempt(name, journal, op):
        try:
            journal.begin(op, content)
            results[name] = "begun"
        except DuplicateSideEffect:
            results[name] = "refused"

    threads = [threading.Thread(target=attempt, args=("a", first, "op-1")),
               threading.Thread(target=attempt, args=("b", second, "op-2"))]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    monkeypatch.setattr(JsonStore, "load_payload", real_load)
    assert sorted(results.values()) == ["begun", "refused"]
    assert len(SideEffectJournal(path).unresolved_for(content)) == 1


_CHILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from core.app_workflow import SideEffectJournal, DuplicateSideEffect
    path, op, content, go = sys.argv[2:6]
    import os
    while not os.path.exists(go):
        time.sleep(0.001)
    try:
        SideEffectJournal(path).begin(op, content)
        print("BEGUN")
    except DuplicateSideEffect:
        print("REFUSED")
    except Exception as error:
        print("ERROR " + type(error).__name__ + " " + str(error)[:120])
""")


def test_separate_processes_begin_one_content_exactly_once(tmp_path):
    for round_number in range(6):
        folder = tmp_path / f"round-{round_number}"
        folder.mkdir()
        path, go = folder / "side-effects.json", folder / "go"
        content = content_key("print", "file-sha", "printer")
        children = [
            subprocess.Popen(
                [sys.executable, "-I", "-c", _CHILD, str(ROOT), str(path), f"op-{index}", content, str(go)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for index in range(3)
        ]
        go.write_text("go", encoding="utf-8")
        outputs = sorted(child.communicate(timeout=60)[0].strip() for child in children)
        assert outputs == ["BEGUN", "REFUSED", "REFUSED"], outputs
        assert len(SideEffectJournal(path).unresolved_for(content)) == 1


def test_lock_held_by_another_process_times_out_without_writing(tmp_path):
    path = tmp_path / "side-effects.json"
    SideEffectJournal(path).begin("op-0", "content-0")
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", textwrap.dedent("""
            import sys, time
            sys.path.insert(0, sys.argv[1])
            from core.app_workflow import SideEffectJournal
            with SideEffectJournal(sys.argv[2])._locked():
                print("HELD", flush=True)
                time.sleep(4)
        """), str(ROOT), str(path)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "HELD"
        with pytest.raises(JournalUnavailable, match="Another Qronos process"):
            SideEffectJournal(path, lock_timeout=0.3).begin("op-1", "content-1")
    finally:
        holder.wait(timeout=30)
    assert SideEffectJournal(path).get("op-1") is None


@pytest.mark.parametrize("entries", [
    [],
    {"x": 5},
    {"x": {"contentKey": "c"}},
    {"x": {"contentKey": "c", "state": "bogus"}},
    {"x": {"state": "uncertain"}},
    {"x": {"contentKey": "", "state": "uncertain"}},
    {"x": {"contentKey": "c", "state": "uncertain", "updatedAt": "yesterday"}},
])
def test_malformed_journal_entries_refuse_instead_of_reading_empty(tmp_path, entries):
    path = tmp_path / "side-effects.json"
    path.write_text(_json.dumps({"schemaVersion": 1, "payload": {"entries": entries}}), encoding="utf-8")
    with pytest.raises(JournalUnavailable):
        SideEffectJournal(path).begin("op-new", "c")


def test_truncated_or_newer_journal_refuses(tmp_path):
    path = tmp_path / "side-effects.json"
    path.write_text('{"schemaVersion": 1, "payload": {"entries": {', encoding="utf-8")
    with pytest.raises(JournalUnavailable):
        SideEffectJournal(path).begin("op", "c")
    path.write_text(_json.dumps({"schemaVersion": 99, "payload": {"entries": {}}}), encoding="utf-8")
    with pytest.raises(JournalUnavailable):
        SideEffectJournal(path).begin("op", "c")


def test_a_journal_that_disappears_after_use_is_not_treated_as_empty(tmp_path):
    path = tmp_path / "side-effects.json"
    journal = SideEffectJournal(path)
    journal.begin("op-a", "content")
    journal.settle("op-a", SideEffectState.UNCERTAIN)
    path.unlink()
    with pytest.raises(JournalUnavailable, match="missing"):
        SideEffectJournal(path).begin("op-b", "content")


def test_journal_requires_a_durable_path():
    with pytest.raises(ValueError):
        SideEffectJournal(None)
    with pytest.raises(ValueError):
        SideEffectJournal("  ")


def test_settled_attempts_are_never_evicted(tmp_path):
    path = tmp_path / "side-effects.json"
    entries = {
        f"op-{index}": {"contentKey": f"c-{index}", "state": "confirmed", "detail": "", "updatedAt": float(index)}
        for index in range(3000)
    }
    path.write_text(_json.dumps({"schemaVersion": 1, "payload": {"entries": entries}}), encoding="utf-8")
    journal = SideEffectJournal(path)
    journal.begin("op-new", "c-new")
    with pytest.raises(DuplicateSideEffect):
        journal.begin("op-0", "c-0")
    stored = _json.loads(path.read_text(encoding="utf-8"))["payload"]["entries"]
    assert len(stored) == 3001 and "op-0" in stored


def test_journal_in_a_linked_folder_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("Directory links are unavailable here.")
        import _winapi
        _winapi.CreateJunction(str(real), str(linked))
    with pytest.raises(JournalUnavailable, match="link"):
        SideEffectJournal(linked / "side-effects.json").begin("op", "c")


# ------------------------------------------------ interrupted attempts (W3)


def test_an_attempt_left_by_a_crashed_process_becomes_uncertain_and_can_be_released(tmp_path):
    path = tmp_path / "side-effects.json"
    crashed = subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent("""
            import os, sys
            sys.path.insert(0, sys.argv[1])
            from core.app_workflow import SideEffectJournal
            SideEffectJournal(sys.argv[2]).begin("op-crash", "content-x")
            os._exit(9)  # dies before settling, like a crash
        """), str(ROOT), str(path)],
        capture_output=True, text=True, timeout=60,
    )
    assert crashed.returncode == 9, crashed.stderr
    journal = SideEffectJournal(path)
    assert journal.get("op-crash").state is SideEffectState.SUBMITTING
    with pytest.raises(WorkflowError):
        journal.acknowledge("op-crash")
    assert journal.reconcile_interrupted() == ("op-crash",)
    assert [entry.operation_key for entry in journal.uncertain()] == ["op-crash"]
    with pytest.raises(DuplicateSideEffect):
        journal.begin("op-new", "content-x")
    journal.acknowledge("op-crash", "checked")
    journal.begin("op-new", "content-x")


def test_an_attempt_owned_by_a_running_process_is_not_reconciled(tmp_path):
    journal = SideEffectJournal(tmp_path / "side-effects.json")
    journal.begin("op-live", "content")
    assert journal.reconcile_interrupted() == ()
    assert journal.get("op-live").state is SideEffectState.SUBMITTING


def test_settle_if_unresolved_never_overrides_a_settled_outcome(tmp_path):
    journal = SideEffectJournal(tmp_path / "side-effects.json")
    journal.begin("op", "content")
    journal.settle("op", SideEffectState.CONFIRMED)
    assert journal.settle_if_unresolved("op", SideEffectState.UNCERTAIN) is False
    assert journal.get("op").state is SideEffectState.CONFIRMED
