"""
The one production elevated action, ``printer.spooler_restart``.

Unit tests drive the Service Control Manager adapter with a fake advapi32 and
the coordinator with fake approval, launcher and session. Process tests run a
real, NON-elevated broker child (tests/fixtures/broker_child.py --spooler)
that carries the production spec exactly, with only ``perform`` replaced by a
marker write. Nothing is elevated, no UAC prompt is shown, and the real Print
Spooler is never queried, stopped or started.
"""

from __future__ import annotations

import ctypes
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from core import elevated_actions, spooler_service
from core.action_broker import (
    PRODUCTION_ACTIONS,
    ActionRegistry,
    BrokerRefusal,
    ConsumedApprovals,
    handle_request,
    verify_request,
)
from core.action_broker_client import (
    BrokerOutcomeUnknown,
    BrokerResult,
    BrokerSession,
    BrokerUnavailable,
    TypedAction,
    build_approval,
)
from core.actions import ActionOutcome, ActionRequest
from core.elevated_actions import (
    FA,
    KIND,
    PHASE,
    ElevatedActionCoordinator,
    ElevationUnavailable,
    parse_spooler_restart,
)
from core.spooler_service import (
    ERROR_DEPENDENT_SERVICES_RUNNING,
    RUNNING,
    SERVICE_DISPLAY,
    START_PENDING,
    STOP_PENDING,
    STOPPED,
    PAUSED,
    SpoolerRecovery,
    SpoolerRecoveryFailed,
    Win32ServiceControl,
)
from core.uac_launch import UacDeclined
from security import gate
from security.permissions import ActionCategory, PermissionDecision

SPEC = PRODUCTION_ACTIONS.get(KIND)


# ------------------------------------------------------------ the request


@pytest.mark.parametrize("text", [
    "Restart the print spooler",
    "please restart the Windows print spooler service",
    "سرویس چاپ را ری‌استارت کن",
    "سرویس چاپ رو دوباره راه‌اندازی کن",
    "اسپولر را راه اندازی مجدد کن",
])
def test_plain_requests_to_restart_the_spooler_are_recognised(text):
    assert parse_spooler_restart(text)


@pytest.mark.parametrize("text", [
    "print the spooler report",
    "restart my computer",
    "don't restart the print spooler",
    "سرویس چاپ را ری‌استارت نکن",
    "what is a print spooler?",
    "restart the print spooler " + "x" * 400,
    "",
    None,
])
def test_anything_else_is_not_this_action(text):
    assert not parse_spooler_restart(text)


# ------------------------------------------------- the broker's registry


def test_the_production_action_is_exact_and_has_nothing_to_choose():
    assert PRODUCTION_ACTIONS.kinds() == (KIND,)
    assert SPEC.fields == {} and SPEC.category is ActionCategory.DEVICE_CONTROL
    assert SPEC.target({}) == SERVICE_DISPLAY == "Windows Print Spooler service (Spooler)"
    assert spooler_service.SERVICE_NAME == "Spooler"


def envelope(params, key=b"k" * 32, decision=PermissionDecision.REQUIRE_TYPED_SECRET):
    request = ActionRequest(category=SPEC.category, target=SPEC.target({}), summary=SPEC.summary)
    outcome = ActionOutcome.AWAITING_APPROVAL if decision is not PermissionDecision.ALLOW else ActionOutcome.SUCCEEDED
    verdict = gate.Verdict(request=request, decision=decision, outcome=outcome, reason="test")
    grant = gate.issue_grant(request, verdict, approval=decision if verdict.needs_approval else None)
    return {"kind": KIND, "params": params}, build_approval(TypedAction(KIND, params), request, grant, key), verdict


def test_the_broker_accepts_exactly_the_approved_spooler_restart(tmp_path):
    action, approval, verdict = envelope({})
    verified = verify_request(action, approval, b"k" * 32, PRODUCTION_ACTIONS, ConsumedApprovals(tmp_path),
                              gate_check=lambda request: verdict)
    assert verified.spec.kind == KIND and verified.params == {}


@pytest.mark.parametrize("params", [{"service": "W32Time"}, {"name": "Spooler"}])
def test_the_service_can_never_be_chosen_by_the_caller(tmp_path, params):
    action, approval, verdict = envelope(params)
    with pytest.raises(BrokerRefusal) as refusal:
        verify_request(action, approval, b"k" * 32, PRODUCTION_ACTIONS, ConsumedApprovals(tmp_path),
                       gate_check=lambda request: verdict)
    assert refusal.value.outcome == "bad_parameters"


def test_a_failed_restart_reports_its_reviewed_message_and_nothing_else(tmp_path):
    def failing(params):
        raise SpoolerRecoveryFailed("The Print Spooler did not stop in time; nothing was started.")

    def crashing(params):
        raise OSError("C:\\secret\\path leaked?")

    for perform, expected in ((failing, "did not stop in time"), (crashing, "The action failed: OSError.")):
        action, approval, verdict = envelope({})
        registry = ActionRegistry([replace(SPEC, perform=perform)])
        result = handle_request({"action": action, "approval": approval}, b"k" * 32, registry,
                                ConsumedApprovals(tmp_path / perform.__name__), gate_check=lambda request: verdict)
        assert result["ok"] is False and result["outcome"] == "failed" and expected in result["detail"]
        assert "secret" not in result["detail"]


# ------------------------------------------------ the SCM adapter (fake)


class FakeControl:
    """Plays the Service Control Manager for one service."""

    def __init__(self, states, stop_error=0, start_error=0):
        self.states = list(states)
        self.stop_error = stop_error
        self.start_error = start_error
        self.calls = []

    def open(self):
        self.calls.append("open")
        return ("manager", "service")

    def state(self, service):
        assert service == "service"
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def stop(self, service):
        self.calls.append("stop")
        return self.stop_error

    def start(self, service):
        self.calls.append("start")
        return self.start_error

    def close(self, handles):
        self.calls.append("close")


def recovery(control, timeout=1.0):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    return SpoolerRecovery(control, clock=lambda: now[0], sleep=sleep, stop_timeout=timeout, start_timeout=timeout)


def test_a_running_spooler_is_stopped_then_started():
    control = FakeControl([RUNNING, STOP_PENDING, STOPPED, START_PENDING, RUNNING])
    assert "restarted and is running" in recovery(control).restart()
    assert control.calls == ["open", "stop", "start", "close"]


def test_a_stopped_spooler_is_only_started():
    control = FakeControl([STOPPED, STOPPED, RUNNING])
    recovery(control).restart()
    assert control.calls == ["open", "start", "close"]


@pytest.mark.parametrize("control, message, calls", [
    (FakeControl([RUNNING], stop_error=ERROR_DEPENDENT_SERVICES_RUNNING), "depend on", ["open", "stop", "close"]),
    (FakeControl([RUNNING], stop_error=5), "refused to stop", ["open", "stop", "close"]),
    (FakeControl([RUNNING, STOP_PENDING]), "did not stop in time; nothing was started", ["open", "stop", "close"]),
    (FakeControl([RUNNING, STOPPED], start_error=5), "refused to start", ["open", "stop", "start", "close"]),
    (FakeControl([RUNNING, STOPPED, STOPPED, START_PENDING]), "not running yet", ["open", "stop", "start", "close"]),
    (FakeControl([PAUSED]), "paused", ["open", "close"]),
])
def test_every_failure_says_how_far_it_got_and_nothing_is_retried(control, message, calls):
    with pytest.raises(SpoolerRecoveryFailed, match=message):
        recovery(control).restart()
    assert control.calls == calls


class FakeAdvapi:
    def __init__(self, manager=1, service=2):
        self.manager, self.service = manager, service
        self.calls = []

    def OpenSCManagerW(self, machine, database, access):
        self.calls.append(("OpenSCManagerW", machine, database, access))
        return self.manager

    def OpenServiceW(self, manager, name, access):
        self.calls.append(("OpenServiceW", manager, name, access))
        return self.service

    def CloseServiceHandle(self, handle):
        self.calls.append(("CloseServiceHandle", handle))
        return 1


def test_the_adapter_opens_only_the_spooler_with_query_stop_start_rights():
    api = FakeAdvapi()
    control = Win32ServiceControl(api, last_error=lambda: 0)
    handles = control.open()
    control.close(handles)
    assert api.calls == [
        ("OpenSCManagerW", None, None, spooler_service.SC_MANAGER_CONNECT),
        ("OpenServiceW", 1, "Spooler", spooler_service.SERVICE_QUERY_STATUS | spooler_service.SERVICE_STOP
         | spooler_service.SERVICE_START),
        ("CloseServiceHandle", 2), ("CloseServiceHandle", 1),
    ]


def test_an_unopenable_service_manager_or_service_fails_closed():
    with pytest.raises(SpoolerRecoveryFailed, match="Service Control Manager"):
        Win32ServiceControl(FakeAdvapi(manager=0), last_error=lambda: 5).open()
    api = FakeAdvapi(service=0)
    with pytest.raises(SpoolerRecoveryFailed, match="could not be opened"):
        Win32ServiceControl(api, last_error=lambda: 5).open()
    assert api.calls[-1] == ("CloseServiceHandle", 1)


def test_the_module_never_starts_a_program():
    source = Path(spooler_service.__file__).read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "ShellExecute", "sc.exe", "net.exe", "powershell", "Popen"):
        assert forbidden not in source.split('"""', 2)[2], forbidden


# --------------------------------------------------- the coordinator


class Harness:
    def __init__(self, verdict_decision=PermissionDecision.REQUIRE_TYPED_SECRET, secret_state="ready",
                 session_result=None, session_error=None, launcher_error=None, verify=True):
        self.events = []
        self.performed = []
        self.decision = verdict_decision
        self.paused = False
        self.session_result = session_result or BrokerResult(True, "performed", "done")
        self.session_error = session_error
        self.launcher_error = launcher_error

        def session_factory(launcher):
            harness = self

            class Session:
                def perform(self, action, request, grant):
                    harness.performed.append((action, request, grant))
                    assert isinstance(grant, gate.ActionGrant) and grant.action_id == request.action_id
                    if harness.session_error is not None:
                        raise harness.session_error
                    return harness.session_result

            return Session()

        def launcher_factory():
            if self.launcher_error is not None:
                raise self.launcher_error
            return object()

        self.coordinator = ElevatedActionCoordinator(
            notify=lambda kind, status, message: self.events.append((kind, status, json.loads(message))),
            verify_secret=(lambda secret: secret == "correct") if verify else None,
            secret_status=lambda: secret_state,
            paused=lambda: self.paused,
            launcher_factory=launcher_factory,
            session_factory=session_factory,
            gate_check=self.gate,
            run_async=lambda target: target(),
        )

    def gate(self, request):
        outcome = {
            PermissionDecision.ALLOW: ActionOutcome.SUCCEEDED,
            PermissionDecision.DENY: ActionOutcome.REFUSED,
        }.get(self.decision, ActionOutcome.AWAITING_APPROVAL)
        return gate.Verdict(request=request, decision=self.decision, outcome=outcome, reason="test")

    def begin(self):
        return self.coordinator.try_begin("restart the print spooler", "req-1")

    def token(self):
        return next(event[2]["actionToken"] for event in self.events if event[0] == "app_workflow_approval_required")

    def finished(self):
        return [event[2] for event in self.events if event[0] == "app_workflow_finished"]


def test_the_prompt_always_asks_for_the_typed_secret_even_if_settings_lowered_device_control():
    for decision in (PermissionDecision.ALLOW, PermissionDecision.REQUIRE_UI_CONFIRMATION,
                     PermissionDecision.REQUIRE_TYPED_SECRET):
        harness = Harness(verdict_decision=decision)
        assert harness.begin() == FA["asked"]
        prompt = harness.events[0][2]
        assert prompt["phase"] == PHASE and prompt["permission"] == "require_typed_secret"
        harness.coordinator.resolve(harness.token(), approved=True, secret="wrong")
        assert harness.events[-1][0] == "app_workflow_secret_rejected" and harness.performed == []
        harness.coordinator.resolve(harness.token(), approved=True, secret="correct")
        (action, request, grant), = harness.performed
        assert action == TypedAction(KIND, {}) and request.target == SERVICE_DISPLAY
        assert harness.finished()[-1]["outcome"] == "succeeded"


def test_one_approval_is_used_once():
    harness = Harness()
    harness.begin()
    token = harness.token()
    harness.coordinator.resolve(token, approved=True, secret="correct")
    harness.coordinator.resolve(token, approved=True, secret="correct")
    assert len(harness.performed) == 1
    assert harness.events[-1][0] == "app_workflow_stale"


@pytest.mark.parametrize("setup, reason", [
    (dict(verdict_decision=PermissionDecision.DENY), "forbidden"),
    (dict(secret_state="not_configured"), "secret_not_configured"),
    (dict(secret_state="locked"), "secret_locked"),
    (dict(verify=False), "no_secret_path"),
])
def test_refusals_come_before_any_prompt(setup, reason):
    harness = Harness(**setup)
    assert harness.begin() == FA[reason]
    assert not any(event[0] == "app_workflow_approval_required" for event in harness.events)


def test_declining_does_nothing():
    harness = Harness()
    harness.begin()
    harness.coordinator.resolve(harness.token(), approved=False)
    assert harness.performed == [] and harness.finished()[-1]["outcome"] == "refused"


@pytest.mark.parametrize("setup, outcome, message", [
    (dict(session_error=BrokerOutcomeUnknown("lost")), "uncertain", "unknown"),
    (dict(session_error=BrokerUnavailable("did not connect")), "refused", "unavailable"),
    (dict(launcher_error=ElevationUnavailable("source")), "refused", "not_installed"),
    (dict(session_result=BrokerResult(False, "failed", "did not stop in time")), "failed", "failed"),
    (dict(session_result=BrokerResult(False, "already_used", "x")), "refused", "refused"),
])
def test_every_outcome_is_reported_once_and_never_retried(setup, outcome, message):
    harness = Harness(**setup)
    harness.begin()
    harness.coordinator.resolve(harness.token(), approved=True, secret="correct")
    finished = harness.finished()
    assert len(finished) == 1 and finished[0]["outcome"] == outcome and finished[0]["message"] == FA[message]
    assert len(harness.performed) == (0 if "launcher_error" in setup else 1)


def test_a_declined_uac_prompt_is_reported_as_declined():
    declined = BrokerUnavailable("not started")
    declined.__cause__ = UacDeclined("declined")
    harness = Harness(session_error=declined)
    harness.begin()
    harness.coordinator.resolve(harness.token(), approved=True, secret="correct")
    assert harness.finished()[-1]["message"] == FA["uac_declined"]


def test_stop_pause_and_shutdown_revoke_open_prompts():
    harness = Harness()
    harness.begin()
    token = harness.token()
    assert harness.coordinator.discard("runtime_paused") == 1
    harness.coordinator.resolve(token, approved=True, secret="correct")
    assert harness.performed == [] and harness.events[-1][0] == "app_workflow_stale"


def test_pause_refuses_both_new_requests_and_answers():
    harness = Harness()
    harness.begin()
    harness.paused = True
    harness.coordinator.resolve(harness.token(), approved=True, secret="correct")
    assert harness.performed == []
    assert harness.coordinator.try_begin("restart the print spooler", "req-2") == FA["paused"]


def test_elevation_is_refused_from_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    with pytest.raises(ElevationUnavailable):
        elevated_actions.bundled_launcher()


# ------------------------------------------- real broker child (non-elevated)


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Broker sessions are Windows named pipes.")


@pytest.fixture
def child_launchers():
    from tests.test_action_broker_session import ChildLauncher

    made = []

    def make(work, *extra):
        launcher = ChildLauncher(work, extra=extra)
        made.append(launcher)
        return launcher

    yield make
    for launcher in made:
        launcher.reap()


@windows_only
def test_the_production_spec_runs_end_to_end_through_a_real_broker_process(tmp_path, child_launchers):
    launcher = child_launchers(tmp_path, "--spooler")
    events = []
    coordinator = ElevatedActionCoordinator(
        notify=lambda kind, status, message: events.append((kind, json.loads(message))),
        verify_secret=lambda secret: secret == "correct",
        secret_status=lambda: "ready",
        paused=lambda: False,
        launcher_factory=lambda: launcher,
        session_factory=lambda chosen: BrokerSession(chosen, accept_timeout_seconds=30, reply_timeout_seconds=30),
        run_async=lambda target: target(),
    )
    assert coordinator.try_begin("Restart the print spooler", "req-e2e") == FA["asked"]
    token = next(body["actionToken"] for kind, body in events if kind == "app_workflow_approval_required")
    coordinator.resolve(token, approved=True, secret="correct")
    finished = [body for kind, body in events if kind == "app_workflow_finished"]
    assert finished[-1]["outcome"] == "succeeded", finished
    assert (tmp_path / "markers" / "spooler.done").read_text(encoding="utf-8") == "performed"
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert audit[-1]["kind"] == KIND and audit[-1]["outcome"] == "performed"
    assert "params" not in audit[-1] and "mac" not in json.dumps(audit[-1])


@windows_only
def test_a_lost_answer_after_the_request_left_is_outcome_unknown(tmp_path, child_launchers):
    from tests.test_action_broker_session import marker_request

    launcher = child_launchers(tmp_path, "--vanish")
    action, request, grant = marker_request("vanish")
    with pytest.raises(BrokerOutcomeUnknown):
        BrokerSession(launcher, accept_timeout_seconds=30, reply_timeout_seconds=30).perform(action, request, grant)
    assert (tmp_path / "markers" / "vanish.done").exists()  # it did act: hence "unknown", never "refused"


# ------------------------------------------------------------ the bridge


def test_the_bridge_routes_the_request_its_decision_and_revocation_to_the_coordinator():
    import types

    import core.runtime_bridge as bridge

    harness = Harness()
    replies = []
    runtime = object.__new__(bridge.QronosRuntime)
    runtime._elevated_actions = harness.coordinator
    runtime._computer_actions = types.SimpleNamespace(stopped_since_turn_start=lambda: False)
    runtime._finish_text_file_action = lambda **reply: replies.append(reply)

    assert runtime._begin_text_elevated_action("Restart the print spooler", "req-b", 0.0)
    assert replies[-1]["response"] == FA["asked"]
    assert not runtime._begin_text_elevated_action("print my report", "req-c", 0.0)

    runtime.resolve_app_workflow_decision(harness.token(), approved=True, secret="correct")
    assert len(harness.performed) == 1

    runtime._begin_text_elevated_action("Restart the print spooler", "req-d", 0.0)
    revoked_before = harness.coordinator.discard  # the bridge's Stop/Pause path calls this
    assert revoked_before("runtime_paused") == 1


def test_the_suite_refuses_the_real_scm_and_uac_bindings():
    from core import uac_launch

    with pytest.raises(AssertionError, match="real Windows computer side effect"):
        Win32ServiceControl().open()
    with pytest.raises(AssertionError, match="real Windows computer side effect"):
        uac_launch.Win32()
