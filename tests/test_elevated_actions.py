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
import os
import sys
import threading
import time
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
    BrokerCancelled,
    BrokerOutcomeUnknown,
    BrokerResult,
    BrokerSession,
    BrokerUnavailable,
    Cancellation,
    TypedAction,
    build_approval,
)
from core.actions import ActionOutcome, ActionRequest
from core.approval_ledger import ApprovalLedger
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


SECRET_OK = dict(secret="right", verify_secret=lambda secret: secret == "right")


def envelope(params, key=b"k" * 32, decision=PermissionDecision.REQUIRE_TYPED_SECRET):
    request = ActionRequest(category=SPEC.category, target=SPEC.target({}), summary=SPEC.summary)
    outcome = ActionOutcome.AWAITING_APPROVAL if decision is not PermissionDecision.ALLOW else ActionOutcome.SUCCEEDED
    verdict = gate.Verdict(request=request, decision=decision, outcome=outcome, reason="test")
    grant = gate.issue_grant(request, verdict, approval=decision if verdict.needs_approval else None)
    return {"kind": KIND, "params": params}, build_approval(TypedAction(KIND, params), request, grant, key), verdict


def test_the_broker_accepts_exactly_the_approved_spooler_restart(tmp_path):
    action, approval, verdict = envelope({})
    verified = verify_request(action, approval, b"k" * 32, PRODUCTION_ACTIONS, ConsumedApprovals(tmp_path),
                              gate_check=lambda request: verdict, **SECRET_OK)
    assert verified.spec.kind == KIND and verified.params == {}


@pytest.mark.parametrize("params", [{"service": "W32Time"}, {"name": "Spooler"}])
def test_the_service_can_never_be_chosen_by_the_caller(tmp_path, params):
    action, approval, verdict = envelope(params)
    with pytest.raises(BrokerRefusal) as refusal:
        verify_request(action, approval, b"k" * 32, PRODUCTION_ACTIONS, ConsumedApprovals(tmp_path),
                       gate_check=lambda request: verdict, **SECRET_OK)
    assert refusal.value.outcome == "bad_parameters"


def test_a_failed_restart_reports_its_reviewed_message_and_nothing_else(tmp_path):
    def failing(params):
        raise SpoolerRecoveryFailed("The Print Spooler did not stop in time; nothing was started.")

    def crashing(params):
        raise OSError("C:\\secret\\path leaked?")

    for perform, expected in ((failing, "did not stop in time"), (crashing, "The action failed: OSError.")):
        action, approval, verdict = envelope({})
        registry = ActionRegistry([replace(SPEC, perform=perform)])
        result = handle_request({"action": action, "approval": approval, "secret": "right"}, b"k" * 32, registry,
                                ConsumedApprovals(tmp_path / perform.__name__), gate_check=lambda request: verdict,
                                verify_secret=SECRET_OK["verify_secret"])
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
        self.secrets = []
        self.cancel_checks = []
        self.during = None  # called inside perform, while the action runs
        self.decision = verdict_decision
        self.paused = False
        self.session_result = session_result or BrokerResult(True, "performed", "done")
        self.session_error = session_error
        self.launcher_error = launcher_error

        def session_factory(launcher):
            harness = self

            class Session:
                def perform(self, action, request, grant, secret="", cancelled=lambda: False, cancellation=None):
                    harness.performed.append((action, request, grant))
                    harness.secrets.append(secret)
                    harness.cancel_checks.append(cancellation)
                    if harness.during is not None:
                        harness.during(cancellation)
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
        assert harness.secrets == ["correct"]  # forwarded for the broker's own check
        assert "correct" not in json.dumps([event[2] for event in harness.events])  # never in an event
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


def test_the_secret_never_appears_in_a_pending_approval_s_repr():
    from core.elevated_actions import PendingElevation

    pending = PendingElevation("req", ActionRequest(category=SPEC.category, target="t", summary="s"),
                               secret="hunter2", epoch=3)
    assert "hunter2" not in repr(pending)


def test_stop_after_approval_cancels_a_session_that_has_not_sent_anything():
    harness = Harness()
    seen = {}

    def stop_while_running(cancellation):
        seen["before"] = cancellation.cancelled()
        harness.coordinator.discard("runtime_paused")
        seen["after"] = cancellation.cancelled()
        seen["gates"] = (cancellation.release_key(), cancellation.commit())

    harness.during = stop_while_running
    harness.begin()
    harness.coordinator.resolve(harness.token(), approved=True, secret="correct")
    assert seen == {"before": False, "after": True, "gates": (False, False)}  # every later handoff refuses


def test_an_unexpected_error_still_ends_in_one_visible_result():
    harness = Harness(session_error=RuntimeError("boom"))
    harness.begin()
    harness.coordinator.resolve(harness.token(), approved=True, secret="correct")
    (finished,) = harness.finished()
    assert finished["outcome"] == "uncertain" and finished["message"] == FA["error"]


def test_elevation_is_refused_from_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    with pytest.raises(ElevationUnavailable):
        elevated_actions.bundled_launcher()


# ------------------------------------------- real broker child (non-elevated)


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Broker sessions are Windows named pipes.")


class BlockingLauncher:
    """Holds "UAC" open until released; then starts a real non-elevated broker child (or nothing)."""

    def __init__(self, inner=None):
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = []

    def launch(self, address, runtime_pid):
        self.entered.set()
        assert self.release.wait(20)
        if self.inner is not None:
            return self.inner.launch(address, runtime_pid)
        launcher = self

        class Process:
            pid = 999_999

            def wait(self, timeout):
                return None

            def close(self):
                launcher.closed.append(True)

        return Process()


def threaded_coordinator(launcher, events, session_timeout=20):
    from tests.test_action_broker_session import SECRET

    return ElevatedActionCoordinator(
        notify=lambda kind, status, message: events.append((kind, json.loads(message))),
        verify_secret=lambda secret: secret == SECRET,
        secret_status=lambda: "ready",
        paused=lambda: False,
        launcher_factory=lambda: launcher,
        session_factory=lambda chosen: BrokerSession(chosen, accept_timeout_seconds=session_timeout,
                                                     reply_timeout_seconds=session_timeout),
    )


def wait_finished(events, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        finished = [body for kind, body in events if kind == "app_workflow_finished"]
        if finished:
            return finished
        time.sleep(0.05)
    raise AssertionError(f"no result; saw {events}")


@windows_only
def test_stop_while_uac_is_pending_sends_nothing_even_after_the_prompt_is_accepted():
    from tests.test_action_broker_session import SECRET

    launcher = BlockingLauncher()
    events = []
    coordinator = threaded_coordinator(launcher, events)
    coordinator.try_begin("restart the print spooler", "req-cancel")
    token = next(body["actionToken"] for kind, body in events if kind == "app_workflow_approval_required")
    coordinator.resolve(token, approved=True, secret=SECRET)
    assert launcher.entered.wait(10)  # the "UAC prompt" is up
    coordinator.discard("runtime_paused")  # Stop/Pause now
    launcher.release.set()  # the person then accepts the prompt
    (finished,) = wait_finished(events)
    assert finished["outcome"] == "refused" and finished["message"] == FA["cancelled"]
    assert launcher.closed == [True]  # the broker's handle was released


@pytest.fixture
def child_launchers():
    from tests.test_action_broker_session import ChildLauncher

    made = []

    def make(work, *extra, environment=None):
        launcher = ChildLauncher(work, extra=extra, environment=environment)
        made.append(launcher)
        return launcher

    yield make
    for launcher in made:
        launcher.reap()


@windows_only
def test_the_production_spec_runs_end_to_end_through_a_real_broker_process(tmp_path, child_launchers):
    from tests.test_action_broker_session import SECRET

    launcher = child_launchers(tmp_path, "--spooler")
    events = []
    coordinator = threaded_coordinator(launcher, events, session_timeout=30)
    assert coordinator.try_begin("Restart the print spooler", "req-e2e") == FA["asked"]
    token = next(body["actionToken"] for kind, body in events if kind == "app_workflow_approval_required")
    coordinator.resolve(token, approved=True, secret=SECRET)
    finished = wait_finished(events)
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
    from tests.test_action_broker_session import SECRET

    with pytest.raises(BrokerOutcomeUnknown):
        BrokerSession(launcher, accept_timeout_seconds=30, reply_timeout_seconds=30).perform(
            action, request, grant, secret=SECRET)
    assert (tmp_path / "markers" / "vanish.done").exists()  # it did act: hence "unknown", never "refused"


@windows_only
def test_a_real_broker_started_after_stop_gets_no_key_and_does_nothing(tmp_path, child_launchers):
    from tests.test_action_broker_session import SECRET

    launcher = BlockingLauncher(inner=child_launchers(tmp_path, "--spooler"))
    events = []
    coordinator = threaded_coordinator(launcher, events)
    coordinator.try_begin("restart the print spooler", "req-late")
    token = next(body["actionToken"] for kind, body in events if kind == "app_workflow_approval_required")
    coordinator.resolve(token, approved=True, secret=SECRET)
    assert launcher.entered.wait(10)
    coordinator.discard("runtime_shutdown")
    launcher.release.set()  # the broker really starts now
    (finished,) = wait_finished(events)
    assert finished["outcome"] == "refused" and finished["message"] == FA["cancelled"]
    (broker,) = launcher.inner.processes
    assert broker.wait(30) != 0  # it found no key and gave up on its own
    assert not (tmp_path / "markers").exists()


@windows_only
def test_cancelled_just_before_sending_nothing_is_sent(tmp_path, child_launchers):
    from tests.test_action_broker_session import SECRET, marker_request

    launcher = child_launchers(tmp_path)
    action, request, grant = marker_request("late")

    class StopAtTheCommitPoint(Cancellation):
        """Stop lands exactly at the commit point: after the real hello, before the request."""

        def commit(self):
            self.cancel()
            return super().commit()

    with pytest.raises(BrokerCancelled, match="just before the request was sent"):
        BrokerSession(launcher, accept_timeout_seconds=20, reply_timeout_seconds=20).perform(
            action, request, grant, secret=SECRET, cancellation=StopAtTheCommitPoint())
    (broker,) = launcher.processes
    assert broker.wait(30) != 0
    assert not (tmp_path / "markers").exists()


def hostile_request(launcher, secret: str) -> dict:
    """
    Play a hostile local "runtime": its own pipe, its own key, a perfectly
    formed envelope MAC'd under that key, a (simulated) UAC launch of the
    broker — and whatever secret it likes. Returns the broker's answer.
    """
    import secrets as random
    import time as clock

    from core.action_broker import FROM_RUNTIME, TO_RUNTIME, action_digest, approval_mac
    from core.supervisor_ipc import ChannelServer, new_key

    key = new_key()
    server = ChannelServer("action-broker", key, receive=TO_RUNTIME, send=FROM_RUNTIME)
    try:
        broker = launcher.launch(server.address, os.getpid())
        channel = server.accept(20, expected_pid=broker.pid, release_key=True)
        assert channel.receive(20)["kind"] == "hello"
        now = clock.time()
        approval = {"actionId": "forged", "category": "device_control", "target": SERVICE_DISPLAY,
                    "digest": action_digest(KIND, {}), "issuedAt": now, "expiresAt": now + 60,
                    "nonce": random.token_hex(16)}
        approval["mac"] = approval_mac(key, approval)
        channel.send("request", action={"kind": KIND, "params": {}}, approval=approval, secret=secret)
        reply = channel.receive(20)
        channel.close()
        return reply
    finally:
        server.close()


@windows_only
def test_a_non_qronos_process_cannot_get_an_elevated_action_without_the_typed_secret(tmp_path, child_launchers):
    for guess in ("", "1234", "password"):
        work = tmp_path / f"guess-{len(guess)}"
        work.mkdir()
        reply = hostile_request(child_launchers(work, "--spooler"), guess)
        assert reply["ok"] is False and reply["outcome"] == "secret_rejected", reply
        assert not (work / "markers").exists()


@windows_only
def test_a_launcher_cannot_point_the_broker_at_a_secret_store_it_chose(tmp_path, child_launchers):
    """
    The legitimate profile's secret is A. The launcher sets
    QRONOS_APPROVAL_SECRET_PATH and QRONOS_DATA_ROOT to its own store with
    secret B. The broker child runs the PRODUCTION store check; only the
    known Local AppData folder is redirected to a test directory.
    """
    from core.approval_secret import ApprovalSecretStore

    legit = tmp_path / "known-local-appdata"
    (legit / "Qronos").mkdir(parents=True)
    ApprovalSecretStore(legit / "Qronos" / "approval_secret.sqlite3",
                        audit_path=tmp_path / "legit-audit.jsonl").set_secret("legitimate owner secret A")
    evil = tmp_path / "evil" / "approval_secret.sqlite3"
    evil.parent.mkdir()
    ApprovalSecretStore(evil, audit_path=tmp_path / "evil-audit.jsonl").set_secret("attacker chosen secret B")
    environment = {
        "QRONOS_TEST_KNOWN_LOCALAPPDATA": str(legit),
        "QRONOS_APPROVAL_SECRET_PATH": str(evil),
        "QRONOS_DATA_ROOT": str(evil.parent),
        "QRONOS_PERMISSION_AUDIT_PATH": str(tmp_path / "evil-chosen-audit.jsonl"),
    }
    for secret, outcome in (("attacker chosen secret B", "secret_rejected"), ("legitimate owner secret A", "performed")):
        work = tmp_path / outcome
        work.mkdir()
        launcher = child_launchers(work, "--spooler", "--profile-store", environment=environment)
        reply = hostile_request(launcher, secret)
        assert reply["outcome"] == outcome, reply
        assert (work / "markers").exists() is (outcome == "performed")
    assert not (tmp_path / "evil-chosen-audit.jsonl").exists()  # nothing the launcher chose was used


def test_the_broker_profile_comes_from_the_known_folder_not_the_environment(monkeypatch, tmp_path):
    from core import action_broker

    with pytest.raises(AssertionError, match="real Windows computer side effect"):
        action_broker.installed_profile()  # the suite refuses the real known folder
    monkeypatch.setattr(action_broker, "_known_local_appdata", lambda: tmp_path)
    monkeypatch.setenv("QRONOS_DATA_ROOT", str(tmp_path / "chosen"))
    monkeypatch.setenv("QRONOS_APPROVAL_SECRET_PATH", str(tmp_path / "chosen.sqlite3"))
    assert action_broker.installed_profile() == tmp_path / "Qronos"
    assert action_broker.default_consumed_directory() == tmp_path / "Qronos" / "ActionBroker" / "consumed"


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


# ------------------------------------------------- Stop races at the real boundaries (R-P3a)


class BarrierLedger(ApprovalLedger):
    """The real ledger, able to pause just AFTER a real issue or take, until released."""

    def __init__(self):
        super().__init__(ttl_seconds=elevated_actions.APPROVAL_TTL_SECONDS)
        self.pause = None  # "issue" | "take"
        self.reached = threading.Event()
        self.release = threading.Event()

    def _barrier(self, step):
        if self.pause == step:
            self.reached.set()
            assert self.release.wait(10), "the test never released the barrier"

    def issue(self, *args, **kwargs):
        ticket = super().issue(*args, **kwargs)
        self._barrier("issue")
        return ticket

    def take(self, *args, **kwargs):
        ticket = super().take(*args, **kwargs)
        self._barrier("take")
        return ticket


class Sends:
    """Records everything that could leave toward a broker. Each list must stay empty."""

    def __init__(self, monkeypatch):
        from core import action_broker_client

        self.sessions, self.launches, self.servers = [], [], []
        real_server = action_broker_client.ChannelServer
        sends = self

        def server(*args, **kwargs):
            sends.servers.append(args[0] if args else "?")  # a key would be released on this server
            return real_server(*args, **kwargs)

        monkeypatch.setattr(action_broker_client, "ChannelServer", server)

        class Launcher:
            def launch(self, address, runtime_pid):
                sends.launches.append(address)
                raise AssertionError("no broker may be launched after Stop")

        self.launcher = Launcher()

        def session(launcher):
            sends.sessions.append(launcher)
            return BrokerSession(launcher, accept_timeout_seconds=2, reply_timeout_seconds=2)

        self.session_factory = session

    def none(self):
        return self.sessions == [] and self.launches == [] and self.servers == []


@pytest.fixture
def real_secret(tmp_path):
    """The real verifier contract over a disposable store with a test secret."""
    from core.approval_secret import ApprovalSecretStore
    from core.library_executor import WorkflowSecretCheck

    store = ApprovalSecretStore(tmp_path / "approval_secret.sqlite3", audit_path=tmp_path / "audit.jsonl")
    store.set_secret("disposable test secret 1")
    return WorkflowSecretCheck(store), "disposable test secret 1"


def race_coordinator(ledger, sends, check, events, scheduled, verify=None):
    return ElevatedActionCoordinator(
        notify=lambda kind, status, message: events.append((kind, json.loads(message))),
        verify_secret=verify or check.verify,
        secret_status=check.status,
        paused=lambda: False,
        launcher_factory=lambda: sends.launcher,
        session_factory=sends.session_factory,
        gate_check=lambda request: gate.Verdict(request=request, decision=PermissionDecision.REQUIRE_TYPED_SECRET,
                                                outcome=ActionOutcome.AWAITING_APPROVAL, reason="test"),
        run_async=scheduled.append,  # the action's thread starts when the test says so
        ledger=ledger,
    )


def prompt_token(events):
    return next(body["actionToken"] for kind, body in events if kind == "app_workflow_approval_required")


def finished(events):
    return [body for kind, body in events if kind == "app_workflow_finished"]


def test_stop_just_after_a_genuine_approval_is_taken_releases_nothing(monkeypatch, real_secret):
    """
    The R-P3a boundary: resolve has consumed the real ticket (correct secret,
    real ledger) and pauses right there; Stop runs; resolve resumes; then the
    action's thread starts. Stop must win: no session, no server (so no key
    released), no launch, and exactly one honest terminal event.
    """
    check, secret = real_secret
    ledger, sends, events, scheduled = BarrierLedger(), Sends(monkeypatch), [], []
    coordinator = race_coordinator(ledger, sends, check, events, scheduled)
    coordinator.try_begin("restart the print spooler", "req-race")
    token = prompt_token(events)

    ledger.pause = "take"
    resolving = threading.Thread(target=lambda: coordinator.resolve(token, approved=True, secret=secret))
    resolving.start()
    assert ledger.reached.wait(10)  # the ticket is consumed; resolve is paused right after it
    stopping = threading.Thread(target=lambda: coordinator.discard("runtime_stopped"))
    stopping.start()
    stopping.join(0.3)
    stop_waited_for_consumption = stopping.is_alive()
    ledger.release.set()
    resolving.join(10)
    stopping.join(10)
    assert not resolving.is_alive() and not stopping.is_alive()
    for start in scheduled:  # the action's thread starts after Stop
        start()

    assert sends.none(), (sends.sessions, sends.launches, sends.servers)
    (result,) = finished(events)
    assert result["outcome"] == "refused" and result["message"] == FA["cancelled"]
    assert [kind for kind, _ in events].count("app_workflow_finished") == 1
    assert stop_waited_for_consumption  # Stop is serialized with consumption, not interleaved


def test_stop_while_a_prompt_is_being_issued_leaves_no_usable_approval(monkeypatch, real_secret):
    check, secret = real_secret
    ledger, sends, events, scheduled = BarrierLedger(), Sends(monkeypatch), [], []
    coordinator = race_coordinator(ledger, sends, check, events, scheduled)
    ledger.pause = "issue"
    beginning = threading.Thread(target=lambda: coordinator.try_begin("restart the print spooler", "req-issue"))
    beginning.start()
    assert ledger.reached.wait(10)
    stopping = threading.Thread(target=lambda: coordinator.discard("runtime_stopped"))
    stopping.start()
    ledger.release.set()
    beginning.join(10)
    stopping.join(10)
    token = prompt_token(events)
    kinds = [kind for kind, body in events if body.get("actionToken") == token]
    assert kinds[-1] == "app_workflow_stale"  # the desktop's last word on it is "stale", whatever the order
    coordinator.resolve(token, approved=True, secret=secret)
    for start in scheduled:
        start()
    assert sends.none() and finished(events) == []


def test_stop_during_secret_verification_refuses_the_answer(monkeypatch, real_secret):
    check, secret = real_secret
    ledger, sends, events, scheduled = BarrierLedger(), Sends(monkeypatch), [], []
    verifying, release = threading.Event(), threading.Event()

    def slow_verify(candidate):
        verifying.set()
        assert release.wait(10)
        return check.verify(candidate)  # the real verifier says yes

    coordinator = race_coordinator(ledger, sends, check, events, scheduled, verify=slow_verify)
    coordinator.try_begin("restart the print spooler", "req-verify")
    token = prompt_token(events)
    resolving = threading.Thread(target=lambda: coordinator.resolve(token, approved=True, secret=secret))
    resolving.start()
    assert verifying.wait(10)
    coordinator.discard("runtime_stopped")  # revokes the still-open ticket
    release.set()
    resolving.join(10)
    for start in scheduled:
        start()
    assert sends.none() and scheduled == [] and finished(events) == []
    assert events[-1][0] == "app_workflow_stale"


def test_stop_after_the_approval_but_before_its_thread_starts_cancels_it(monkeypatch, real_secret):
    check, secret = real_secret
    ledger, sends, events, scheduled = BarrierLedger(), Sends(monkeypatch), [], []
    coordinator = race_coordinator(ledger, sends, check, events, scheduled)
    coordinator.try_begin("restart the print spooler", "req-start")
    coordinator.resolve(prompt_token(events), approved=True, secret=secret)
    assert len(scheduled) == 1
    coordinator.discard("runtime_stopped")
    scheduled[0]()
    assert sends.none()
    (result,) = finished(events)
    assert result["message"] == FA["cancelled"]


def test_a_prompt_issued_after_stop_still_works(monkeypatch, real_secret):
    """The fix must not make Stop poison approvals given after it."""
    check, secret = real_secret
    ledger, sends, events, scheduled = BarrierLedger(), Sends(monkeypatch), [], []
    coordinator = race_coordinator(ledger, sends, check, events, scheduled)
    coordinator.discard("runtime_stopped")
    coordinator.try_begin("restart the print spooler", "req-after")
    coordinator.resolve(prompt_token(events), approved=True, secret=secret)
    scheduled[0]()  # it really proceeds to a launch (refused here only by the recording launcher)
    assert len(sends.sessions) == 1 and len(sends.launches) == 1
    (result,) = finished(events)
    assert result["outcome"] == "refused" and result["message"] == FA["unavailable"]


# ------------------------------------- D1: Stop while the session waits for the broker
#
# The real coordinator (production run_async, ledger and gate), the real
# BrokerSession and ChannelServer on private random pipes. The "broker" is a
# thread of this process driven by the fake launcher below, so the real pipe
# process-id checks see this process. No UAC, SCM, child process or
# production mutex is involved, and nothing is performed anywhere.


class ThreadBrokerLauncher:
    """Fake UAC launcher: starts ``peer(address)`` on a thread; the process handle counts closes."""

    def __init__(self, peer):
        self.peer = peer
        self.closes = 0
        self.threads = []

    def launch(self, address, runtime_pid):
        assert runtime_pid == os.getpid()
        thread = threading.Thread(target=self.peer, args=(address,), daemon=True)
        thread.start()
        self.threads.append(thread)
        launcher = self

        class Process:
            pid = os.getpid()

            def wait(self, timeout):
                return 0

            def close(self):
                launcher.closes += 1

        return Process()


class ListenerProbe:
    def __init__(self):
        self.servers = []
        self.waiting = threading.Event()


@pytest.fixture
def listening(monkeypatch):
    """Records each session's ChannelServer and signals when the session starts waiting in accept."""
    from core import action_broker_client

    real = action_broker_client.ChannelServer
    probe = ListenerProbe()

    class Recording(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            probe.servers.append(self)

        def accept(self, *args, **kwargs):
            probe.waiting.set()
            return real.accept(self, *args, **kwargs)

    monkeypatch.setattr(action_broker_client, "ChannelServer", Recording)
    return probe


def d1_coordinator(launcher, events, session_factory=BrokerSession):
    from tests.test_action_broker_session import SECRET

    return ElevatedActionCoordinator(
        notify=lambda kind, status, message: events.append((kind, json.loads(message))),
        verify_secret=lambda secret: secret == SECRET,
        secret_status=lambda: "ready",
        paused=lambda: False,
        launcher_factory=lambda: launcher,
        session_factory=session_factory,
    )


def d1_approve(coordinator, events, request_id):
    from tests.test_action_broker_session import SECRET

    assert coordinator.try_begin("restart the print spooler", request_id) == FA["asked"]
    token = next(body["actionToken"] for kind, body in events
                 if kind == "app_workflow_approval_required" and body["requestId"] == request_id)
    coordinator.resolve(token, approved=True, secret=SECRET)


def d1_finished(events, request_id, within):
    """The terminal events for ``request_id`` once the first arrives within ``within`` seconds, else []."""
    def terminal():
        return [body for kind, body in events if kind == "app_workflow_finished" and body["requestId"] == request_id]

    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if terminal():
            time.sleep(0.2)  # a second terminal event would show up here
            return terminal()
        time.sleep(0.01)
    return []


def assert_listener_gone(server):
    from core.supervisor_ipc import _deadline, _open_checked

    assert server._closed
    with pytest.raises(Exception):
        _open_checked(server.address, os.getpid(), None, _deadline(1)).close()


@windows_only
def test_stop_while_waiting_for_the_broker_to_connect_releases_no_key_to_a_late_peer(listening):
    from core.action_broker import FROM_RUNTIME, TO_RUNTIME
    from core.supervisor_ipc import ChannelError, connect_for_key

    go, seen = threading.Event(), {}

    def late_broker(address):
        assert go.wait(20)
        try:
            channel, key = connect_for_key(address, receive=FROM_RUNTIME, send=TO_RUNTIME,
                                           expected_server_pid=os.getpid(), timeout=3)
        except ChannelError as error:
            seen["refused"] = type(error).__name__
            return
        seen["key"] = key
        channel.send("hello", pid=os.getpid(), protocol=1)
        try:
            seen["request"] = channel.receive(3)["kind"]
        except ChannelError:
            pass
        channel.close()

    launcher, events = ThreadBrokerLauncher(late_broker), []
    coordinator = d1_coordinator(launcher, events)  # production session defaults (120 s accept)
    d1_approve(coordinator, events, "req-late")
    assert listening.waiting.wait(10)  # UAC returned; the session waits for the broker
    coordinator.discard("runtime_stopped")
    stopped = time.monotonic()
    finished = d1_finished(events, "req-late", within=2.0)
    elapsed = time.monotonic() - stopped
    go.set()  # the broker finishes starting and tries to connect, after Stop
    launcher.threads[0].join(10)

    assert [(f["outcome"], f["message"]) for f in finished] == [("refused", FA["cancelled"])], finished
    assert elapsed < 2.0 and "no key was released" in finished[0]["detail"]
    assert "key" not in seen and "request" not in seen and seen.get("refused"), seen
    (server,) = listening.servers
    assert_listener_gone(server)
    assert launcher.closes == 1
    assert coordinator.try_begin("restart the print spooler", "req-after") == FA["asked"]  # not busy


@windows_only
def test_stop_while_waiting_for_a_broker_that_never_connects_ends_at_once_at_production_defaults(listening):
    launcher, events = ThreadBrokerLauncher(lambda address: None), []  # UAC accepted; no broker ever connects
    coordinator = d1_coordinator(launcher, events)  # accept timeout 120 s: not what ends the wait
    d1_approve(coordinator, events, "req-silent")
    assert listening.waiting.wait(10)
    coordinator.discard("runtime_paused")
    stopped = time.monotonic()
    finished = d1_finished(events, "req-silent", within=2.0)
    assert time.monotonic() - stopped < 2.0
    assert [(f["outcome"], f["message"]) for f in finished] == [("refused", FA["cancelled"])], finished
    (server,) = listening.servers
    assert_listener_gone(server)
    assert launcher.closes == 1
    assert coordinator.try_begin("restart the print spooler", "req-after") == FA["asked"]


@windows_only
def test_stop_during_the_handshake_after_the_key_ends_promptly_and_sends_no_request(listening):
    from core.supervisor_ipc import _deadline, _open_checked

    took_key, done, seen = threading.Event(), threading.Event(), {"messages": []}

    def stalling_broker(address):
        connection = _open_checked(address, os.getpid(), None, _deadline(5))
        try:
            while not done.is_set():
                if connection.poll(0.05):
                    seen["messages"].append(connection.recv_bytes(256))
                    if len(seen["messages"]) == 2:  # the key, then the challenge it never answers
                        took_key.set()
        except (EOFError, OSError):
            seen["closed"] = True
        finally:
            connection.close()

    launcher, events = ThreadBrokerLauncher(stalling_broker), []
    coordinator = d1_coordinator(launcher, events)
    d1_approve(coordinator, events, "req-stall")
    assert took_key.wait(10)
    coordinator.discard("runtime_stopped")
    stopped = time.monotonic()
    finished = d1_finished(events, "req-stall", within=2.0)
    assert time.monotonic() - stopped < 2.0
    done.set()
    launcher.threads[0].join(5)
    assert [(f["outcome"], f["message"]) for f in finished] == [("refused", FA["cancelled"])], finished
    assert "the key had already been released to the launched broker" in finished[0]["detail"]
    assert len(seen["messages"]) == 2 and seen.get("closed")  # nothing after the challenge: no request
    assert launcher.closes == 1
    assert_listener_gone(listening.servers[0])


@windows_only
def test_stop_while_waiting_for_the_hello_ends_promptly_and_sends_no_request(listening):
    from core.action_broker import FROM_RUNTIME, TO_RUNTIME
    from core.supervisor_ipc import ChannelError, connect_for_key

    connected, done, seen = threading.Event(), threading.Event(), {}

    def silent_broker(address):
        channel, _ = connect_for_key(address, receive=FROM_RUNTIME, send=TO_RUNTIME, expected_server_pid=os.getpid())
        connected.set()
        assert done.wait(10)
        try:
            seen["request"] = channel.receive(2)["kind"]
        except ChannelError as error:
            seen["after"] = type(error).__name__
        channel.close()

    launcher, events = ThreadBrokerLauncher(silent_broker), []
    coordinator = d1_coordinator(launcher, events)
    d1_approve(coordinator, events, "req-mute")
    assert connected.wait(10)
    coordinator.discard("runtime_stopped")
    stopped = time.monotonic()
    finished = d1_finished(events, "req-mute", within=2.0)
    assert time.monotonic() - stopped < 2.0
    done.set()
    launcher.threads[0].join(5)
    assert [(f["outcome"], f["message"]) for f in finished] == [("refused", FA["cancelled"])], finished
    assert "request" not in seen and seen.get("after"), seen
    assert launcher.closes == 1


@windows_only
def test_stop_after_the_commit_point_cancels_nothing_and_the_real_result_is_reported(listening):
    from core.action_broker import FROM_RUNTIME, TO_RUNTIME
    from core.supervisor_ipc import connect_for_key

    got_request, answer, seen = threading.Event(), threading.Event(), {}

    def acting_broker(address):
        channel, _ = connect_for_key(address, receive=FROM_RUNTIME, send=TO_RUNTIME, expected_server_pid=os.getpid())
        channel.send("hello", pid=os.getpid(), protocol=1)
        seen["request"] = channel.receive(10)["kind"]
        got_request.set()
        assert answer.wait(10)
        channel.send("result", ok=True, outcome="performed", detail="fixture: recorded, nothing restarted")
        channel.close()

    launcher, events = ThreadBrokerLauncher(acting_broker), []
    coordinator = d1_coordinator(launcher, events)
    d1_approve(coordinator, events, "req-committed")
    assert got_request.wait(10)
    coordinator.discard("runtime_stopped")  # too late: the request was committed and sent
    answer.set()
    finished = d1_finished(events, "req-committed", within=10.0)
    assert seen["request"] == "request"
    assert [(f["outcome"], f["message"]) for f in finished] == [("succeeded", FA["done"])], finished
    assert launcher.closes == 1


def test_the_cancellation_orders_key_release_and_commit_against_stop():
    first = Cancellation()
    assert first.cancel() is True
    assert first.release_key() is False and first.commit() is False and first.cancelled() is True

    second = Cancellation()
    assert second.release_key() is True and second.key_released  # the key went first
    assert second.cancel() is True  # ordered after the release: it still stops the request
    assert second.commit() is False

    third = Cancellation()
    assert third.release_key() is True and third.commit() is True
    assert third.cancel() is False and third.cancelled() is False  # committed: nothing to cancel

    woken = []
    fourth = Cancellation()
    with fourth.waking(lambda: woken.append("now")):
        fourth.cancel()
    with fourth.waking(lambda: woken.append("at once")):
        pass
    assert woken == ["now", "at once"]

    paused = Cancellation(also=lambda: True)
    assert paused.cancelled() and not paused.release_key() and not paused.commit()


@windows_only
def test_stop_between_the_peer_check_and_the_key_release_writes_no_key_byte(listening):
    """Stop lands after the broker connected and passed the PID check, just before the key would leave."""
    from core.supervisor_ipc import _deadline, _open_checked

    done, seen = threading.Event(), {"messages": []}
    holder = {}

    def recording_broker(address):
        connection = _open_checked(address, os.getpid(), None, _deadline(5))
        try:
            while not done.is_set():
                if connection.poll(0.05):
                    seen["messages"].append(connection.recv_bytes(256))
        except (EOFError, OSError):
            seen["closed"] = True
        finally:
            connection.close()

    def stop_during_the_pid_check(connection):
        # The session's PID query runs right before the key release: Stop exactly here.
        holder["coordinator"].discard("runtime_stopped")
        return os.getpid()

    launcher, events = ThreadBrokerLauncher(recording_broker), []
    coordinator = d1_coordinator(launcher, events,
                                 session_factory=lambda chosen: BrokerSession(chosen, peer_pid=stop_during_the_pid_check))
    holder["coordinator"] = coordinator
    d1_approve(coordinator, events, "req-gate")
    finished = d1_finished(events, "req-gate", within=10.0)
    deadline = time.monotonic() + 5
    while not seen.get("closed") and time.monotonic() < deadline:
        time.sleep(0.02)
    done.set()
    launcher.threads[0].join(5)
    assert [(f["outcome"], f["message"]) for f in finished] == [("refused", FA["cancelled"])], finished
    assert "no key was released" in finished[0]["detail"]
    assert seen["messages"] == [] and seen.get("closed")  # closed unanswered: not one key byte
    assert launcher.closes == 1
