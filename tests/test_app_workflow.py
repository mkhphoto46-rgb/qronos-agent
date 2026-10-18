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
    StepGuard,
    WindowEvidence,
    WorkflowApproval,
    WorkflowStopped,
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
