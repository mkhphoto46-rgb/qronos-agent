"""Device Hub (core/device_hub.py): inventory, capabilities, typed workflows, approvals and the bridge lifecycle.

Only a recording fake spooler/driver: no real printer is enumerated, no job is touched and no device is driven.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import security.gate as gate_module
from core import device_hub as hub_module
from core.action_audit import ActionAuditLog, AuditEvent
from core.actions import ActionOutcome
from core.approval_ledger import ApprovalKind, ApprovalLedger
from core.print_workflow import PrinterState, PrintJobState
from security.gate import Verdict
from security.permissions import PermissionDecision

USER = "amina"


class FakeSpooler:
    """Stands for the Windows spooler and the drivers behind it; records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.queues = [
            PrinterState("Office Laser", "USB001", "HP LaserJet 400", 0, 0x40, 2, is_default=True),
            PrinterState("\\\\server\\Floor2", "IP_10.0.0.9", "Canon Generic PCL6", 0x80, 0x10, 0),
            PrinterState("Prusa 3D", "USB002", "Microsoft 3D Print Driver", 0, 0x40, 0),
            PrinterState("Odd Queue", "LPT1", "Broken Driver", 0, 0x40, 0),
        ]
        self.capability = {
            "Office Laser": {"copiesMax": 9, "color": False, "duplex": True, "collate": True,
                             "paperSizes": ["A4", "Letter"]},
            "\\\\server\\Floor2": {"copiesMax": 99, "color": True, "duplex": False, "collate": False,
                                   "paperSizes": ["A4"]},
        }
        self.job_list = {"Office Laser": [PrintJobState(7, "report.pdf", USER, 0x10, 1, 3),
                                          PrintJobState(8, "someone-else.docx", "other", 0, 0, 1)]}

    def printers(self):
        self.calls.append(("printers",))
        return tuple(self.queues)

    def capabilities(self, printer, port):
        self.calls.append(("capabilities", printer, port))
        if printer not in self.capability:
            raise OSError("driver did not answer")
        return dict(self.capability[printer])

    def jobs(self, printer):
        self.calls.append(("jobs", printer))
        return tuple(self.job_list.get(printer, ()))

    def control_own_job(self, printer, job_id, command):
        self.calls.append(("control", printer, job_id, command))

    def current_user(self):
        return USER


def policy(decision: PermissionDecision):
    def evaluate(request, audit=None):
        outcome = (ActionOutcome.SUCCEEDED if decision is PermissionDecision.ALLOW else ActionOutcome.REFUSED
                   if decision is PermissionDecision.DENY else ActionOutcome.AWAITING_APPROVAL)
        verdict = Verdict(request=request, decision=decision, outcome=outcome, reason="fixture policy")
        if audit is not None:
            audit(verdict)
        return verdict
    return evaluate


@pytest.fixture
def world(tmp_path, monkeypatch):
    spooler, events, prints = FakeSpooler(), [], []
    audit = ActionAuditLog(path=tmp_path / "audit.jsonl")
    state = {"paused": False}
    monkeypatch.setattr(gate_module, "evaluate", policy(PermissionDecision.ALLOW))
    hub = hub_module.DeviceHub(
        source=lambda: spooler, ledger=ApprovalLedger(), emit=lambda e, s, p: events.append((e, s, json.loads(
            json.dumps(p)))), paused=lambda: state["paused"],
        begin_print=lambda intent, rid: prints.append((intent, rid)) or "فایل و چاپگر بررسی می‌شوند",
        verify_secret=lambda secret: secret == "correct horse", audit=audit, run_async=lambda work: work())
    return type("World", (), {"hub": hub, "spooler": spooler, "events": events, "prints": prints, "audit": audit,
                              "state": state})


def ask(world, op: str, request_id: str, **fields) -> dict:
    world.hub.handle({"op": op, "requestId": request_id, **fields})
    return next(p for e, _, p in reversed(world.events) if e == hub_module.RESULT_EVENT and p["requestId"] == request_id)


def devices(world) -> dict[str, dict]:
    return {d["name"]: d for d in ask(world, "devices.list", f"l{len(world.events)}")["devices"]}


def test_the_inventory_is_the_spoolers_devices_with_driver_capabilities_and_honest_limits(world):
    answer = ask(world, "devices.list", "l1")
    found = {d["name"]: d for d in answer["devices"]}
    laser, floor2, prusa, odd = (found[name] for name in ("Office Laser", "\\\\server\\Floor2", "Prusa 3D", "Odd Queue"))
    assert laser["connection"] == "local" and laser["isDefault"] and laser["status"]["ready"]
    assert laser["capabilities"]["copiesMax"] == 9 and laser["capabilities"]["color"] is False
    assert laser["workflows"] == ["print_document", "jobs_view", "job_control_own"]
    assert floor2["connection"] == "network" and floor2["status"]["ready"] is False  # offline bit set
    assert prusa["kind"] == "3d_printer" and prusa["workflows"] == [] and prusa["unsupported"] == "3d_workflow_pending"
    assert ("capabilities", "Prusa 3D", "USB002") not in world.spooler.calls  # nothing claimed for 3D
    assert odd["unsupported"] == "driver_capabilities_unreadable" and "print_document" not in odd["workflows"]
    kinds = {item["kind"] for item in answer["unsupportedClasses"]}
    assert {"3d_printer", "scanner", "camera", "other_local_device"} <= kinds


def test_a_device_id_is_stable_for_the_same_queue_and_changes_when_the_queue_changes():
    a = PrinterState("Office Laser", "USB001", "HP", 0, 0, 0)
    assert hub_module.device_id(a) == hub_module.device_id(PrinterState("office laser", "usb001", "hp", 5, 1, 9))
    assert hub_module.device_id(a) != hub_module.device_id(PrinterState("Office Laser", "USB009", "HP", 0, 0, 0))


@pytest.mark.parametrize("data, code", [
    ("not a dict", "bad_request"),
    ({"op": "devices.list"}, "bad_request"),
    ({"op": "format_disk", "requestId": "x1"}, "unknown_op"),
    ({"op": "device.jobs", "requestId": "x2", "deviceId": "../../x"}, "bad_request"),
    ({"op": "job.control", "requestId": "x3", "deviceId": "prn-" + "0" * 20, "jobId": 7, "command": "delete_all"},
     "bad_request"),
    ({"op": "job.control", "requestId": "x4", "deviceId": "prn-" + "0" * 20, "jobId": True, "command": "cancel"},
     "bad_request"),
    ({"op": "print.request", "requestId": "x5", "deviceId": "prn-" + "0" * 20, "path": "relative.pdf"}, "bad_request"),
    ({"op": "print.request", "requestId": "x6", "deviceId": "prn-" + "0" * 20, "path": "C:\\a.pdf", "pages": "1;2"},
     "bad_request"),
    ({"op": "print.request", "requestId": "x7", "deviceId": "prn-" + "0" * 20, "path": "C:\\a.pdf", "copies": 0},
     "bad_request"),
    ({"op": "decide", "requestId": "x8", "approvalId": "short"}, "bad_request"),
])
def test_requests_are_validated_before_the_spooler_is_touched(world, data, code):
    world.hub.handle(data)
    answer = world.events[-1][2]
    assert answer["ok"] is False and answer["code"] == code
    assert world.spooler.calls == []


def test_paused_refuses_changes_but_still_lists(world):
    laser = devices(world)["Office Laser"]
    world.state["paused"] = True
    assert ask(world, "job.control", "p1", deviceId=laser["id"], jobId=7, command="cancel")["code"] == "paused"
    assert ask(world, "print.request", "p2", deviceId=laser["id"], path="C:\\docs\\a.pdf")["code"] == "paused"
    assert ask(world, "devices.list", "p3")["ok"] is True
    assert not [c for c in world.spooler.calls if c[0] == "control"] and world.prints == []


def test_only_your_own_job_can_be_changed_and_it_is_audited(world):
    laser = devices(world)["Office Laser"]
    jobs = ask(world, "device.jobs", "j1", deviceId=laser["id"])["jobs"]
    assert [(j["jobId"], j["own"]) for j in jobs] == [(7, True), (8, False)] and "printing" in jobs[0]["status"]
    assert ask(world, "job.control", "j2", deviceId=laser["id"], jobId=8, command="cancel")["code"] == "not_own_job"
    done = ask(world, "job.control", "j3", deviceId=laser["id"], jobId=7, command="cancel")
    assert done["status"] == "done" and ("control", "Office Laser", 7, hub_module.JOB_CONTROL_DELETE) in world.spooler.calls
    events = [(r.event, r.target, r.category, r.outcome) for r in world.audit.records()]
    assert (AuditEvent.COMPLETED, "printer:Office Laser/job:7", "device_control", "succeeded") in events
    assert ask(world, "job.control", "j4", deviceId=laser["id"], jobId=99, command="pause")["code"] == "job_gone"


def test_a_job_change_waits_for_a_single_use_approval_and_the_current_policy(world, monkeypatch):
    laser = devices(world)["Office Laser"]
    monkeypatch.setattr(gate_module, "evaluate", policy(PermissionDecision.REQUIRE_UI_CONFIRMATION))
    pending = ask(world, "job.control", "a1", deviceId=laser["id"], jobId=7, command="pause")
    assert pending["status"] == "approval_required" and pending["needsSecret"] is False
    assert not [c for c in world.spooler.calls if c[0] == "control"]  # nothing ran before the answer
    assert ask(world, "decide", "a2", approvalId=pending["approvalId"], approved=True)["status"] == "done"
    assert ask(world, "decide", "a3", approvalId=pending["approvalId"], approved=True)["code"] == "stale_approval"
    again = ask(world, "job.control", "a4", deviceId=laser["id"], jobId=7, command="resume")
    monkeypatch.setattr(gate_module, "evaluate", policy(PermissionDecision.DENY))  # tightened while open
    assert ask(world, "decide", "a5", approvalId=again["approvalId"], approved=True)["code"] == "denied"
    monkeypatch.setattr(gate_module, "evaluate", policy(PermissionDecision.REQUIRE_UI_CONFIRMATION))
    third = ask(world, "job.control", "a6", deviceId=laser["id"], jobId=7, command="cancel")
    assert ask(world, "decline", "a7", approvalId=third["approvalId"])["declined"] is True
    fourth = ask(world, "job.control", "a8", deviceId=laser["id"], jobId=7, command="cancel")
    world.spooler.job_list["Office Laser"][0] = PrintJobState(7, "another.pdf", USER, 0, 0, 1)  # replaced job
    assert ask(world, "decide", "a9", approvalId=fourth["approvalId"], approved=True)["code"] == "job_changed"
    assert [c for c in world.spooler.calls if c[0] == "control"] == [("control", "Office Laser", 7, 1)]


def test_a_typed_secret_policy_needs_the_right_secret_which_is_never_echoed(world, monkeypatch):
    laser = devices(world)["Office Laser"]
    monkeypatch.setattr(gate_module, "evaluate", policy(PermissionDecision.REQUIRE_TYPED_SECRET))
    for request_id, secret in (("s1", None), ("s3", "wrong horse")):
        pending = ask(world, "job.control", request_id, deviceId=laser["id"], jobId=7, command="cancel")
        assert pending["needsSecret"] is True
        fields = {} if secret is None else {"secret": secret}
        assert ask(world, "decide", request_id + "d", approvalId=pending["approvalId"], approved=True,
                   **fields)["code"] == "secret_rejected"
    assert not [c for c in world.spooler.calls if c[0] == "control"]
    pending = ask(world, "job.control", "s5", deviceId=laser["id"], jobId=7, command="cancel")
    assert ask(world, "decide", "s6", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["status"] == "done"
    assert "correct horse" not in json.dumps(world.events) and "wrong horse" not in json.dumps(world.events)


def test_printing_goes_through_the_reviewed_print_step_within_the_devices_limits(world):
    found = devices(world)
    laser, floor2, prusa, odd = (found[n] for n in ("Office Laser", "\\\\server\\Floor2", "Prusa 3D", "Odd Queue"))
    done = ask(world, "print.request", "r1", deviceId=floor2["id"], path="C:\\docs\\a.pdf", copies=2, color="color",
               pages="1-3,5")
    assert done["status"] == "print_requested"
    (intent, request_id), = world.prints
    assert (intent.file_reference, intent.printer, intent.copies, intent.color, intent.pages, request_id) == (
        "C:\\docs\\a.pdf", "\\\\server\\Floor2", 2, "color", "1-3,5", "r1")
    assert ask(world, "print.request", "r2", deviceId=laser["id"], path="C:\\a.pdf", copies=10)["code"] == "over_capability"
    assert ask(world, "print.request", "r3", deviceId=laser["id"], path="C:\\a.pdf", color="color")["code"] == \
        "over_capability"
    assert ask(world, "print.request", "r4", deviceId=prusa["id"], path="C:\\a.3mf")["code"] == "unsupported"
    assert ask(world, "print.request", "r5", deviceId=odd["id"], path="C:\\a.pdf")["code"] == "unsupported"
    assert ask(world, "print.request", "r6", deviceId="prn-" + "f" * 20, path="C:\\a.pdf")["code"] == "device_gone"
    assert len(world.prints) == 1


# ------------------------------------------------------------ the real bridge lifecycle with a pending device approval

@pytest.fixture
def runtime_with_hub(tmp_path, monkeypatch):
    from core.runtime_bridge import QronosRuntime

    spooler = FakeSpooler()
    monkeypatch.setattr(hub_module, "default_source", lambda: spooler)
    monkeypatch.setattr(gate_module, "evaluate", policy(PermissionDecision.REQUIRE_UI_CONFIRMATION))
    events: list = []
    runtime = QronosRuntime(notify=lambda event, status, message: events.append((event, status, message)))
    return runtime, events, spooler


def hub_results(events) -> list[dict]:
    return [json.loads(m) for e, _, m in events if e == hub_module.RESULT_EVENT]


def hub_action(runtime, events, request_id, **data) -> dict:
    from core import runtime_bridge

    runtime_bridge.handle_action(runtime, {"actionId": "qronos.device_hub", "data": {"requestId": request_id, **data}})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        found = [r for r in hub_results(events) if r.get("requestId") == request_id]
        if found:
            return found[-1]
        time.sleep(0.02)
    raise AssertionError(f"no answer for {request_id}")


def pending_device_approval(runtime, events) -> str:
    laser = next(d for d in hub_action(runtime, events, "b1", op="devices.list")["devices"] if d["name"] == "Office Laser")
    pending = hub_action(runtime, events, "b2", op="job.control", deviceId=laser["id"], jobId=7, command="cancel")
    assert pending["status"] == "approval_required"
    assert [t.kind for t in runtime._approvals.open_tickets()] == [ApprovalKind.DEVICE_ACTION]
    return pending["approvalId"]


@pytest.mark.parametrize("lifecycle", ["pause", "stop", "close"])
def test_pause_stop_and_close_complete_and_revoke_a_pending_device_approval(runtime_with_hub, lifecycle):
    runtime, events, spooler = runtime_with_hub
    approval = pending_device_approval(runtime, events)
    if lifecycle == "pause":
        runtime.set_runtime_paused(True)
        assert any(e[0] == "runtime_pause_changed" for e in events)
        runtime.set_runtime_paused(False)
    elif lifecycle == "stop":
        assert isinstance(runtime.request_stop_response(), dict)
    else:
        runtime.close()
        assert runtime._approvals.open_tickets() == ()
        return
    assert runtime._approvals.open_tickets() == ()
    late = hub_action(runtime, events, "b3", op="decide", approvalId=approval, approved=True)
    assert late["code"] == "stale_approval" and not [c for c in spooler.calls if c[0] == "control"]


def test_a_device_approval_sent_as_a_file_decision_is_refused_and_left_for_the_hub(runtime_with_hub):
    runtime, events, spooler = runtime_with_hub
    approval = pending_device_approval(runtime, events)
    runtime.resolve_file_action_decision(approval, approved=True)
    stale = [json.loads(m) for e, _, m in events if e == "file_action_stale"]
    assert stale and stale[-1]["reason"] == "wrong_kind" and len(runtime._approvals.open_tickets()) == 1
    done = hub_action(runtime, events, "b4", op="decide", approvalId=approval, approved=True)
    assert done["status"] == "done" and ("control", "Office Laser", 7, hub_module.JOB_CONTROL_DELETE) in spooler.calls
