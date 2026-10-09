"""
The Action Broker's verification, one refusal at a time, with a real gate
grant turned into a real approval envelope. No process, pipe or elevation.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from core import action_broker as broker
from core.action_broker import (
    PRODUCTION_ACTIONS,
    ActionRegistry,
    ActionSpec,
    BrokerRefusal,
    ConsumedApprovals,
    approval_mac,
    handle_request,
    parse_arguments,
    refuse_code_targets,
    verify_request,
)
from core.action_broker_client import TypedAction, build_approval
from core.actions import ActionRequest
from security import gate
from security.permissions import ActionCategory
from core.supervisor_ipc import new_key
from tests.fixtures.broker_child import registry as fixture_registry


def grant_for(request: ActionRequest):
    verdict = gate.evaluate(request)
    if verdict.needs_approval:
        return gate.issue_grant(request, verdict, approval=verdict.decision)
    return gate.issue_grant(request, verdict)


class Setup:
    def __init__(self, tmp_path: Path):
        self.work = tmp_path
        self.registry = fixture_registry(tmp_path)
        self.key = new_key()
        self.consumed = ConsumedApprovals(tmp_path / "consumed")

    def approved(self, name="alpha"):
        action = TypedAction("fixture.write_marker", {"name": name})
        request = ActionRequest(category=ActionCategory.CREATE_OR_EDIT_FILE, target=f"test marker {name}",
                                summary="Write a test marker file.")
        approval = build_approval(action, request, grant_for(request), self.key)
        return {"kind": action.kind, "params": dict(action.params)}, approval

    def verify(self, action, approval, **kwargs):
        return verify_request(action, approval, self.key, self.registry, self.consumed, **kwargs)


@pytest.fixture
def setup(tmp_path):
    return Setup(tmp_path)


def resign(setup: Setup, approval: dict) -> dict:
    approval = dict(approval)
    approval["mac"] = approval_mac(setup.key, approval)
    return approval


def test_an_exactly_approved_action_is_verified_once(setup):
    action, approval = setup.approved()
    verified = setup.verify(action, approval)
    assert verified.spec.kind == "fixture.write_marker" and verified.params == {"name": "alpha"}
    with pytest.raises(BrokerRefusal) as refusal:
        setup.verify(action, approval)
    assert refusal.value.outcome == "already_used"


def test_the_approval_must_come_from_this_session(setup):
    action, approval = setup.approved()
    with pytest.raises(BrokerRefusal) as refusal:
        verify_request(action, approval, new_key(), setup.registry, setup.consumed)
    assert refusal.value.outcome == "bad_signature"


@pytest.mark.parametrize("field, value", [("target", "test marker beta"), ("category", "delete_recoverable"),
                                          ("expiresAt", 9e12), ("actionId", "someone-else")])
def test_any_change_to_a_signed_approval_breaks_the_signature(setup, field, value):
    action, approval = setup.approved()
    with pytest.raises(BrokerRefusal) as refusal:
        setup.verify(action, {**approval, field: value})
    assert refusal.value.outcome == "bad_signature"


def test_a_different_action_than_the_approved_one_is_refused(setup):
    action, approval = setup.approved("alpha")
    with pytest.raises(BrokerRefusal) as refusal:
        setup.verify({"kind": action["kind"], "params": {"name": "beta"}}, approval)
    assert refusal.value.outcome == "not_approved"


def test_a_change_that_leaves_the_target_alone_is_still_refused_by_the_digest(setup):
    # Only the digest ties non-target parameters to the approval.
    spec = ActionSpec("fixture.sized_marker", ActionCategory.CREATE_OR_EDIT_FILE, {"name": str, "size": int},
                      lambda params: f"sized marker {params['name']}", lambda params: "done", "Sized marker.")
    registry = ActionRegistry([spec])
    request = ActionRequest(category=ActionCategory.CREATE_OR_EDIT_FILE, target="sized marker alpha",
                            summary="Sized marker.")
    approval = build_approval(TypedAction("fixture.sized_marker", {"name": "alpha", "size": 1}), request,
                              grant_for(request), setup.key)
    with pytest.raises(BrokerRefusal) as refusal:
        verify_request({"kind": "fixture.sized_marker", "params": {"name": "alpha", "size": 999}}, approval,
                       setup.key, registry, setup.consumed)
    assert refusal.value.outcome == "not_approved"
    assert verify_request({"kind": "fixture.sized_marker", "params": {"name": "alpha", "size": 1}}, approval,
                          setup.key, registry, setup.consumed).params["size"] == 1


def test_freshness(setup):
    action, approval = setup.approved()
    now = approval["issuedAt"]
    with pytest.raises(BrokerRefusal) as expired:
        setup.verify(action, approval, clock=lambda: approval["expiresAt"] + 1)
    assert expired.value.outcome == "expired"
    with pytest.raises(BrokerRefusal) as future:
        setup.verify(action, approval, clock=lambda: now - 60)
    assert future.value.outcome == "not_yet_valid"
    too_long = resign(setup, {**approval, "expiresAt": now + 3600})
    with pytest.raises(BrokerRefusal) as long_lived:
        setup.verify(action, too_long, clock=lambda: now)
    assert long_lived.value.outcome == "too_long"


def test_the_broker_never_trusts_the_runtime_about_the_target(setup):
    # A runtime with the key could sign anything; the broker still derives the
    # target from the parameters and compares.
    action, approval = setup.approved("alpha")
    lying = resign(setup, {**approval, "target": "C:\\Windows"})
    with pytest.raises(BrokerRefusal) as refusal:
        setup.verify(action, lying)
    assert refusal.value.outcome == "not_approved"


@pytest.mark.parametrize("params, outcome", [
    ({"name": "alpha", "extra": "x"}, "bad_parameters"),
    ({}, "bad_parameters"),
    ({"name": 7}, "bad_parameters"),
    ({"name": True}, "bad_parameters"),
    ({"name": "run.ps1"}, "code_target"),
    ({"name": "a && calc"}, "shell_text"),
    ({"name": "with space"}, "bad_parameters"),
])
def test_parameters_must_match_the_spec_exactly(setup, params, outcome):
    action = {"kind": "fixture.write_marker", "params": params}
    request = ActionRequest(category=ActionCategory.CREATE_OR_EDIT_FILE, target="test marker x",
                            summary="Write a test marker file.")
    approval = build_approval(TypedAction(action["kind"], params), request, grant_for(request), setup.key)
    with pytest.raises(BrokerRefusal) as refusal:
        setup.verify(action, approval)
    assert refusal.value.outcome == outcome


def test_an_unknown_kind_is_refused_and_production_holds_only_the_spooler_restart(setup):
    action, approval = setup.approved()
    with pytest.raises(BrokerRefusal) as refusal:
        verify_request(action, approval, setup.key, PRODUCTION_ACTIONS, setup.consumed)
    assert refusal.value.outcome == "unknown_action"
    assert PRODUCTION_ACTIONS.kinds() == ("printer.spooler_restart",)


def test_the_gate_is_asked_again_inside_the_broker(setup):
    action, approval = setup.approved()
    refused = gate.Verdict(request=None, decision=gate.PermissionDecision.DENY,  # type: ignore[arg-type]
                           outcome=gate.ActionOutcome.REFUSED, reason="forbidden by policy", policy=None)
    with pytest.raises(BrokerRefusal) as refusal:
        setup.verify(action, approval, gate_check=lambda request: refused)
    assert refusal.value.outcome == "forbidden"
    # The refused attempt did not spend the approval.
    assert setup.verify(action, approval).action_id == approval["actionId"]


@pytest.mark.parametrize("approval_patch", [
    {"nonce": "nothex"}, {"digest": "0" * 10}, {"issuedAt": "now"}, {"issuedAt": True},
])
def test_malformed_envelopes_are_refused(setup, approval_patch):
    action, approval = setup.approved()
    with pytest.raises(BrokerRefusal) as refusal:
        setup.verify(action, {**approval, **approval_patch})
    assert refusal.value.outcome == "malformed"
    missing = dict(approval)
    missing.pop("nonce")
    with pytest.raises(BrokerRefusal):
        setup.verify(action, missing)


def test_the_action_must_be_exactly_kind_and_params(setup):
    _, approval = setup.approved()
    for action in ({"kind": "fixture.write_marker"}, {"kind": "x", "params": {}, "shell": "cmd"}, ["x"]):
        with pytest.raises(BrokerRefusal):
            setup.verify(action, approval)


# --------------------------------------------------------------- registry


@pytest.mark.parametrize("kind", ["fixture.run_command", "system.shell", "power.exec", "x.powershell", "Bad"])
def test_command_like_kinds_cannot_be_registered(kind):
    with pytest.raises(ValueError):
        ActionRegistry([ActionSpec(kind, ActionCategory.CREATE_OR_EDIT_FILE, {}, lambda p: "t", lambda p: "", "s")])


@pytest.mark.parametrize("category", sorted(broker.FORBIDDEN_CATEGORIES, key=lambda c: c.value))
def test_forbidden_categories_cannot_be_registered(category):
    with pytest.raises(ValueError):
        ActionRegistry([ActionSpec("fixture.thing", category, {}, lambda p: "t", lambda p: "", "s")])


def test_registry_refuses_duplicates_and_non_scalar_fields():
    spec = ActionSpec("fixture.thing", ActionCategory.CREATE_OR_EDIT_FILE, {}, lambda p: "t", lambda p: "", "s")
    with pytest.raises(ValueError):
        ActionRegistry([spec, spec])
    with pytest.raises(ValueError):
        ActionRegistry([ActionSpec("fixture.thing", ActionCategory.CREATE_OR_EDIT_FILE, {"items": list},
                                   lambda p: "t", lambda p: "", "s")])


@pytest.mark.parametrize("value", ["C:/tools/x.EXE", "setup.msi", "a.bat ", "reg.reg", "cmd.exe /c dir",
                                   "x | y", "$(whoami)", "rundll32 thing", "powershell -enc"])
def test_code_and_shell_targets_are_refused(value):
    with pytest.raises(BrokerRefusal):
        refuse_code_targets(value)


# ------------------------------------------------------------ single use


def test_single_use_survives_a_new_broker_and_a_race(tmp_path):
    nonce = "ab" * 16
    assert ConsumedApprovals(tmp_path).claim(nonce)
    assert not ConsumedApprovals(tmp_path).claim(nonce)

    winners = []
    other = "cd" * 16
    threads = [threading.Thread(target=lambda: winners.append(ConsumedApprovals(tmp_path).claim(other)))
               for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert winners.count(True) == 1


def test_old_markers_are_pruned_but_fresh_ones_kept(tmp_path):
    journal = ConsumedApprovals(tmp_path, clock=lambda: 10_000.0)
    assert journal.claim("ef" * 16)
    marker = next(tmp_path.glob("*.used"))
    os.utime(marker, (100.0, 100.0))
    assert journal.claim("01" * 16)
    assert not marker.exists() and len(list(tmp_path.glob("*.used"))) == 1


def test_a_malformed_nonce_is_never_claimed(tmp_path):
    assert not ConsumedApprovals(tmp_path).claim("../escape")


# ------------------------------------------------------------- handling


def test_handling_performs_and_audits_without_secrets(setup):
    action, approval = setup.approved("gamma")
    records = []
    result = handle_request({"action": action, "approval": approval}, setup.key, setup.registry, setup.consumed,
                            audit=records.append)
    assert result == {"ok": True, "outcome": "performed", "detail": "marker gamma written"}
    assert (setup.work / "markers" / "gamma.done").read_text() == "performed"
    text = json.dumps(records)
    assert approval["mac"] not in text and setup.key.hex() not in text and "gamma" not in text
    assert records[0]["outcome"] == "performed" and records[0]["actionId"] == approval["actionId"]


def test_a_failing_action_reports_failure_without_its_message(setup):
    spec = ActionSpec("fixture.boom", ActionCategory.CREATE_OR_EDIT_FILE, {}, lambda p: "boom",
                      lambda p: (_ for _ in ()).throw(RuntimeError("C:/private/path")), "Boom.")
    registry = ActionRegistry([spec])
    request = ActionRequest(category=ActionCategory.CREATE_OR_EDIT_FILE, target="boom", summary="Boom.")
    approval = build_approval(TypedAction("fixture.boom", {}), request, grant_for(request), setup.key)
    result = handle_request({"action": {"kind": "fixture.boom", "params": {}}, "approval": approval},
                            setup.key, registry, setup.consumed)
    assert result["ok"] is False and result["outcome"] == "failed" and "private" not in result["detail"]


def test_broker_arguments_are_exactly_pipe_key_and_runtime_pid():
    good = ["--pipe", r"\\.\pipe\qronos-action-broker-" + "0" * 32, "--key", "ab" * 32, "--runtime-pid", "42"]
    assert parse_arguments(good)[2] == 42
    for bad in (good[:4], good + ["--extra"], ["--key", "ab" * 32, "--pipe", good[1], "--runtime-pid", "42"],
                good[:3] + ["xyz"] + good[4:], good[:5] + ["-1"]):
        with pytest.raises(ValueError):
            parse_arguments(bad)
