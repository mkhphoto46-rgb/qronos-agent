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
        self.decision = verdict_decision
        self.paused = False
        self.session_result = session_result or BrokerResult(True, "performed", "done")
        self.session_error = session_error
        self.launcher_error = launcher_error

        def session_factory(launcher):
            harness = self

            class Session:
                def perform(self, action, request, grant, secret="", cancelled=lambda: False):
                    harness.performed.append((action, request, grant))
                    harness.secrets.append(secret)
                    harness.cancel_checks.append(cancelled)
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
    harness.begin()
    harness.coordinator.resolve(harness.token(), approved=True, secret="correct")
    (cancelled,) = harness.cancel_checks
    assert cancelled() is False
    harness.coordinator.discard("runtime_paused")
    assert cancelled() is True  # every later handoff in the session now refuses to send


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
    checks = iter([False, False, True])  # before launch, after launch, before send
    with pytest.raises(BrokerCancelled):
        BrokerSession(launcher, accept_timeout_seconds=20, reply_timeout_seconds=20).perform(
            action, request, grant, secret=SECRET, cancelled=lambda: next(checks))
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
