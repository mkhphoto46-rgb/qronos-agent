"""Device Hub (core/device_hub.py): inventory, capabilities, typed workflows, approvals and the bridge lifecycle.

Only a recording fake spooler/driver: no real printer is enumerated, no job is touched and no device is driven.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import security.gate as gate_module
from core import device_hub as hub_module
from core.action_audit import ActionAuditLog, AuditEvent
from core.actions import ActionOutcome
from core.approval_ledger import ApprovalKind, ApprovalLedger
from core.print_workflow import PrinterState, PrintJobState
from core import power_wake
from core.power_wake import ArmResult, WakeCapability
from core.wia_imaging import ImagingDevice
from core.camera_capture import CameraDevice, CameraError, Frame as CameraFrame
from security.gate import Verdict
from security.permissions import ActionCategory, PermissionDecision
from tests.threemf_fixture import (CORE_2013, CORE_2015, MATERIAL_EXT, REL_MODEL, model_xml,
                                  relationships, write_3mf)

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
            PrinterState("Workshop", "USB003", "Vendor Additive Driver", 0, 0x40, 0),  # no "3D" anywhere
        ]
        self.proven_3d = {"Workshop": {
            "buildVolumeMicrons": {"width": 250000, "depth": 210000, "height": 220000},
            "threeMfVersion": CORE_2015, "threeMfExtensions": [MATERIAL_EXT]}}
        self.submitted: list[tuple] = []
        self.submit_error: Exception | None = None
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

    def current_machines(self):
        return ("THIS-PC", "this-pc", "this-pc.example.lan")

    def capabilities_3d(self, printer):
        self.calls.append(("capabilities_3d", printer))
        if printer == "Odd Queue":
            raise OSError("Print Ticket provider did not answer")
        found = self.proven_3d.get(printer)
        return dict(found) if found else None

    def submit_3d(self, printer, job_name, package):
        self.calls.append(("submit_3d", printer, job_name))
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append((printer, job_name, package))
        self.job_list.setdefault(printer, []).append(PrintJobState(41, job_name, USER, 0x8, 0, 0))


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
    # A 3D-looking name without driver proof: not a 3D workflow, and never document printing either.
    assert prusa["kind"] == "3d_printer" and prusa["workflows"] == [] and prusa["unsupported"] == \
        "3d_capability_unproven" and prusa["capabilities"] == {}
    assert ("capabilities", "Prusa 3D", "USB002") not in world.spooler.calls
    assert odd["unsupported"] == "driver_capabilities_unreadable" and "print_document" not in odd["workflows"]
    kinds = {item["kind"] for item in answer["unsupportedClasses"]}
    assert {"camera", "other_local_device"} <= kinds and "3d_printer" not in kinds
    assert "scanner" not in kinds  # WIA 2.0 scanners are supported now (imaging.list)


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


def test_own_means_this_user_from_this_computer_not_a_same_named_user_elsewhere(world):
    found = devices(world)
    laser, floor2 = found["Office Laser"], found["\\\\server\\Floor2"]
    world.spooler.job_list["Office Laser"] += [
        PrintJobState(9, "remote.pdf", USER.upper(), 0, 0, 1, machine="\\\\OTHER-PC"),   # same name, other PC
        PrintJobState(10, "near.pdf", USER, 0, 0, 1, machine="\\\\THIS-PC-2"),       # not a substring match
        PrintJobState(11, "mine.pdf", USER, 0, 0, 1, machine="\\\\this-pc.example.lan")]  # an explicit alias
    world.spooler.job_list["\\\\server\\Floor2"] = [PrintJobState(12, "unknown.pdf", USER, 0, 0, 1)]
    own = {j["jobId"]: j["own"] for j in ask(world, "device.jobs", "o1", deviceId=laser["id"])["jobs"]}
    assert own == {7: True, 8: False, 9: False, 10: False, 11: True}  # 7: empty origin on a local, unshared queue
    for request_id, job_id in (("o2", 9), ("o3", 10)):
        assert ask(world, "job.control", request_id, deviceId=laser["id"], jobId=job_id,
                   command="cancel")["code"] == "not_own_job"
    remote = ask(world, "device.jobs", "o4", deviceId=floor2["id"])["jobs"]
    assert remote[0]["own"] is False  # an unknown origin on a network queue is not this computer
    assert ask(world, "job.control", "o5", deviceId=floor2["id"], jobId=12, command="cancel")["code"] == \
        "not_own_job"
    assert ask(world, "job.control", "o6", deviceId=laser["id"], jobId=11, command="pause")["status"] == "done"
    assert [c for c in world.spooler.calls if c[0] == "control"] == [("control", "Office Laser", 11, 1)]


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
    # The person said yes and the CURRENT policy refused: the history says exactly that, never DENIED_BY_USER.
    rows = [(r.event, r.outcome) for r in world.audit.records() if r.target == "printer:Office Laser/job:7"]
    assert AuditEvent.DENIED_BY_USER not in [event for event, _ in rows]
    assert rows[-2:] == [(AuditEvent.APPROVED, ""), (AuditEvent.COMPLETED, "refused")]
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


# ------------------------------------------------------------ 3D printers, proven by their driver

def policy_by(decisions: dict, default: PermissionDecision = PermissionDecision.ALLOW):
    """A fixture policy per category (the Files read and the device change are decided separately)."""
    def evaluate(request, audit=None):
        return policy(decisions.get(request.category, default))(request, audit)
    return evaluate


def handed(world) -> list:
    return [c for c in world.spooler.calls if c[0] == "submit_3d"]


def audit_rows(world) -> list[tuple]:
    return [(r.event, r.category, r.target, r.outcome) for r in world.audit.records()]


def test_3d_capability_comes_from_the_driver_not_from_the_name(world):
    found = devices(world)
    workshop = found["Workshop"]
    assert workshop["kind"] == "3d_printer" and workshop["workflows"] == ["print_3d", "jobs_view", "job_control_own"]
    assert workshop["capabilities"]["buildVolumeMicrons"] == {"width": 250000, "depth": 210000, "height": 220000}
    assert workshop["capabilities"]["threeMfVersion"] == CORE_2015 and workshop["unsupported"] == ""
    assert ("capabilities", "Workshop", "USB003") not in world.spooler.calls  # no 2D document path for it
    assert "print_3d" not in found["Prusa 3D"]["workflows"] and "print_3d" not in found["Office Laser"]["workflows"]
    assert found["Odd Queue"]["kind"] == "printer"  # an unreadable 3D answer is not proof
    assert ask(world, "print.request", "q1", deviceId=workshop["id"], path="C:\\a.pdf")["code"] == "unsupported"
    assert world.prints == []


def test_3d_requests_are_validated_and_only_a_proven_3d_device_accepts_them(world, tmp_path):
    found = devices(world)
    package = write_3mf(tmp_path / "part.3mf")
    (tmp_path / "empty.3mf").write_bytes(b"")
    workshop = found["Workshop"]["id"]
    for request_id, fields, code in (
        ("v1", {"deviceId": workshop, "path": "part.3mf"}, "bad_request"),
        ("v2", {"deviceId": workshop, "path": str(tmp_path / "part.stl")}, "bad_request"),
        ("v3", {"deviceId": workshop, "path": str(tmp_path / "missing.3mf")}, "file_missing"),
        ("v4", {"deviceId": workshop, "path": str(tmp_path / "empty.3mf")}, "file_empty"),
        ("v5", {"deviceId": found["Prusa 3D"]["id"], "path": str(package)}, "unsupported"),
        ("v6", {"deviceId": found["Office Laser"]["id"], "path": str(package)}, "unsupported"),
        ("v7", {"deviceId": "prn-" + "e" * 20, "path": str(package)}, "device_gone"),
    ):
        assert ask(world, "print3d.request", request_id, **fields)["code"] == code, request_id
    world.state["paused"] = True
    assert ask(world, "print3d.request", "v8", deviceId=workshop, path=str(package))["code"] == "paused"
    assert handed(world) == [] and world.hub._ledger.open_tickets() == ()


def test_a_3mf_is_handed_over_only_after_the_typed_secret_and_never_claimed_as_printed(world, tmp_path):
    workshop = devices(world)["Workshop"]
    package = write_3mf(tmp_path / "bracket.3mf", textures={"/3D/Textures/skin.png": b"\x89PNG fixture"})
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    pending = ask(world, "print3d.request", "t1", deviceId=workshop["id"], path=str(package))  # the policy allows
    assert pending["status"] == "approval_required" and pending["needsSecret"] is True  # always, for 3D
    preview = pending["preview"]
    assert (preview["device"], preview["textures"], preview["threeMfVersion"], preview["sha256"]) == (
        "Workshop", 1, CORE_2015, digest)
    assert preview["buildVolumeMicrons"]["height"] == 220000 and handed(world) == []
    assert ask(world, "decide", "t2", approvalId=pending["approvalId"], approved=True,
               secret="wrong horse")["code"] == "secret_rejected"
    assert ask(world, "decide", "t3", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "stale_approval"  # single use, also after a wrong secret
    assert handed(world) == []
    pending = ask(world, "print3d.request", "t4", deviceId=workshop["id"], path=str(package))
    done = ask(world, "decide", "t5", approvalId=pending["approvalId"], approved=True, secret="correct horse")
    assert done["status"] == "handed_to_spooler" and done["jobName"] == pending["preview"]["jobName"]
    assert done["jobId"] == 41 and done["sha256"] == digest and "printed" not in json.dumps(done)
    (printer, job_name, sent), = world.spooler.submitted
    assert printer == "Workshop" and job_name == done["jobName"] and job_name.startswith("Qronos 3D ")
    assert sent.model == model_xml() and sent.textures == (("/3D/Textures/skin.png", b"\x89PNG fixture"),)
    rows = audit_rows(world)
    assert any(r[:3] == (AuditEvent.APPROVED, "read_non_sensitive", str(package)) for r in rows)
    assert (AuditEvent.COMPLETED, "device_control", "printer:Workshop/3d", "succeeded") in rows
    assert "correct horse" not in json.dumps(world.events) and "wrong horse" not in json.dumps(world.events)
    third = ask(world, "print3d.request", "t6", deviceId=workshop["id"], path=str(package))
    assert ask(world, "decline", "t7", approvalId=third["approvalId"])["declined"] is True
    assert any(r[:3] == (AuditEvent.DENIED_BY_USER, "device_control", "printer:Workshop/3d") for r in audit_rows(world))
    assert len(handed(world)) == 1


def test_the_files_gate_decides_before_any_byte_is_read_and_both_policies_decide_again(world, tmp_path, monkeypatch):
    workshop = devices(world)["Workshop"]["id"]
    package = write_3mf(tmp_path / "part.3mf")
    reads: list[Path] = []
    real_read = hub_module.read_3mf
    monkeypatch.setattr(hub_module, "read_3mf", lambda path, caps=None: reads.append(Path(path)) or real_read(path, caps))
    monkeypatch.setattr(gate_module, "evaluate", policy_by({ActionCategory.READ_NON_SENSITIVE: PermissionDecision.DENY}))
    assert ask(world, "print3d.request", "g1", deviceId=workshop, path=str(package))["code"] == "denied"
    assert reads == [] and world.hub._ledger.open_tickets() == ()
    monkeypatch.setattr(gate_module, "evaluate", policy_by({ActionCategory.DEVICE_CONTROL: PermissionDecision.DENY}))
    assert ask(world, "print3d.request", "g2", deviceId=workshop, path=str(package))["code"] == "denied"
    assert world.hub._ledger.open_tickets() == ()
    reads.clear()
    monkeypatch.setattr(gate_module, "evaluate", policy_by(
        {ActionCategory.READ_NON_SENSITIVE: PermissionDecision.REQUIRE_UI_CONFIRMATION}))
    pending = ask(world, "print3d.request", "g3", deviceId=workshop, path=str(package))
    assert pending["preview"]["readAfterApproval"] is True and "sha256" not in pending["preview"] and reads == []
    done = ask(world, "decide", "g4", approvalId=pending["approvalId"], approved=True, secret="correct horse")
    assert done["status"] == "handed_to_spooler" and reads == [package]
    again = ask(world, "print3d.request", "g5", deviceId=workshop, path=str(package))
    monkeypatch.setattr(gate_module, "evaluate", policy_by({ActionCategory.DEVICE_CONTROL: PermissionDecision.DENY}))
    assert ask(world, "decide", "g6", approvalId=again["approvalId"], approved=True,
               secret="correct horse")["code"] == "denied"  # tightened while the approval was open
    rows = audit_rows(world)
    assert not [r for r in rows if r[0] == AuditEvent.DENIED_BY_USER]  # the person did not refuse
    assert rows[-1] == (AuditEvent.COMPLETED, "device_control", "printer:Workshop/3d", "refused")
    assert len(handed(world)) == 1


def test_a_changed_package_or_a_lost_3d_capability_is_refused_at_the_answer(world, tmp_path):
    workshop = devices(world)["Workshop"]["id"]
    package = write_3mf(tmp_path / "part.3mf")
    pending = ask(world, "print3d.request", "c1", deviceId=workshop, path=str(package))
    write_3mf(package, extra={"Metadata/note.txt": b"changed after the approval"})
    assert ask(world, "decide", "c2", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "file_changed"
    pending = ask(world, "print3d.request", "c3", deviceId=workshop, path=str(package))
    world.spooler.proven_3d.clear()  # the driver no longer declares the 3D keywords
    assert ask(world, "decide", "c4", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "capability_gone"
    assert handed(world) == []


def test_the_package_must_match_the_printers_declared_3mf_version_and_extensions(world, tmp_path):
    workshop = devices(world)["Workshop"]["id"]
    beam = {"b": "http://schemas.microsoft.com/3dmanufacturing/beamlattice/2017/02"}
    twice = relationships((REL_MODEL, "/3D/3dmodel.model"), (REL_MODEL, "/3D/3dmodel.model"))
    cases = (
        ("x1", write_3mf(tmp_path / "old.3mf", model=model_xml(CORE_2013)), "incompatible_3mf_version"),
        ("x2", write_3mf(tmp_path / "beam.3mf", model=model_xml(extensions=beam, required="b")),
         "unsupported_3mf_extension"),
        ("x3", write_3mf(tmp_path / "twice.3mf", root_rels=twice), "bad_package"),
    )
    for request_id, package, code in cases:
        assert ask(world, "print3d.request", request_id, deviceId=workshop, path=str(package))["code"] == code
    (tmp_path / "broken.3mf").write_bytes(b"not a zip")
    assert ask(world, "print3d.request", "x4", deviceId=workshop, path=str(tmp_path / "broken.3mf"))["code"] == \
        "bad_package"
    colour = write_3mf(tmp_path / "colour.3mf", model=model_xml(extensions={"m": MATERIAL_EXT}, required="m"))
    assert ask(world, "print3d.request", "x5", deviceId=workshop, path=str(colour))["status"] == "approval_required"
    assert handed(world) == []


def test_a_driver_refusal_is_reported_and_audited_as_failed_and_no_secret_means_no_3d(world, tmp_path):
    from core.print3d_native import Print3DError

    workshop = devices(world)["Workshop"]["id"]
    package = write_3mf(tmp_path / "part.3mf")
    for request_id, error, code in (("f1", Print3DError("not_3d_target", "no 3D package target"), "not_3d_target"),
                                    ("f3", OSError("spooler"), "spooler_refused")):
        world.spooler.submit_error = error
        pending = ask(world, "print3d.request", request_id, deviceId=workshop, path=str(package))
        failed = ask(world, "decide", request_id + "d", approvalId=pending["approvalId"], approved=True,
                     secret="correct horse")
        assert failed["ok"] is False and failed["code"] == code
    assert (AuditEvent.COMPLETED, "device_control", "printer:Workshop/3d", "failed") in audit_rows(world)
    assert world.spooler.submitted == []
    world.hub._verify_secret = None
    assert ask(world, "print3d.request", "f5", deviceId=workshop, path=str(package))["code"] == "secret_unavailable"


# ------------------------------------------------------------ wake THIS PC from sleep (H3), a fake power layer

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
TEHRAN = timezone(timedelta(hours=3, minutes=30))


class FakeArmed:
    def __init__(self) -> None:
        self.cancelled = 0

    @property
    def active(self) -> bool:
        return not self.cancelled

    def cancel(self) -> None:
        self.cancelled += 1


class FakePower:
    """Stands for core/power_wake.WinPowerWake: records capability reads and arm requests; never touches Windows."""

    def __init__(self, capability=None, state="armed") -> None:
        self.cap = capability or WakeCapability(True, True, False, 4, True, "enabled", "disabled", "ac")
        self.state, self.calls, self.armed = state, [], []
        self.on_event = None

    def capability(self):
        self.calls.append(("capability",))
        return self.cap

    def arm(self, due, stay, on_event):
        self.calls.append(("arm", due, stay))
        self.on_event = on_event
        armed = FakeArmed()
        self.armed.append(armed)
        return armed, ArmResult(self.state, "wk-0123456789abcdef", due.astimezone(timezone.utc).isoformat(
            timespec="seconds"))


@pytest.fixture
def wake(world):
    power = FakePower()
    world.hub._power_factory = lambda: power
    world.hub._now = lambda: NOW
    world.power = power
    return world


def at(minutes: int, zone=TEHRAN) -> str:
    return (NOW + timedelta(minutes=minutes)).astimezone(zone).isoformat(timespec="minutes")


def armed_calls(world) -> list:
    return [c for c in world.power.calls if c[0] == "arm"]


def test_wake_status_is_read_only_shows_the_hardware_verdict_and_the_limits_also_while_paused(wake):
    wake.state["paused"] = True
    status = ask(wake, "wake.status", "w1")
    assert status["ok"] and status["capability"]["verdict"] == "supported" and status["armed"] is None
    assert status["capability"]["wakeTimersAc"] == "enabled" and status["capability"]["powerSource"] == "ac"
    assert set(status["limits"]) == set(hub_module.WAKE_LIMITS) and "no_voice_or_brain_while_asleep" in status["limits"]
    assert armed_calls(wake) == []
    assert ask(wake, "wake.schedule", "w2", at=at(60))["code"] == "paused"  # changes are refused while paused


@pytest.mark.parametrize("fields, code", [
    ({"at": "2026-10-13T15:30"}, "bad_request"),                 # no UTC offset: never guessed
    ({"at": "tomorrow at six"}, "bad_request"),
    ({"at": None}, "bad_request"),
    ({"at": "2026-10-13T15:30+03:30", "stayAwakeMinutes": 121}, "bad_request"),
    ({"at": "2026-10-13T15:30+03:30", "stayAwakeMinutes": True}, "bad_request"),
])
def test_wake_requests_are_validated_before_the_power_layer_is_touched(wake, fields, code):
    assert ask(wake, "wake.schedule", "v1", **fields)["code"] == code
    assert wake.power.calls == []


def test_a_wake_time_must_be_two_minutes_to_seven_days_ahead(wake):
    assert ask(wake, "wake.schedule", "t1", at=at(1))["code"] == "bad_time"
    assert ask(wake, "wake.schedule", "t2", at=at(8 * 24 * 60))["code"] == "bad_time"
    assert ask(wake, "wake.schedule", "t3", at=at(3))["status"] == "approval_required"
    assert armed_calls(wake) == []


@pytest.mark.parametrize("capability, code", [
    (WakeCapability(True, True, False, 4, True, "disabled", "disabled", "ac"), "wake_timers_disabled"),
    (WakeCapability(True, True, False, 4, True, "important_only", "enabled", "ac"), "wake_timers_important_only"),
    (WakeCapability(True, True, False, 4, True, "enabled", "disabled", "battery"), "wake_timers_disabled"),
    (WakeCapability(False, True, False, 0, False, "enabled", "enabled", "ac"), "no_rtc_or_always_on_timer_wake"),
])
def test_hardware_or_power_plan_limits_are_said_truthfully_and_nothing_is_asked_or_armed(wake, capability, code):
    wake.power.cap = capability
    answer = ask(wake, "wake.schedule", "c1", at=at(60))
    assert answer["ok"] is False and answer["code"] == code and answer["capability"]["verdict"] != "supported"
    assert wake.hub._ledger.open_tickets() == () and armed_calls(wake) == []


def test_arming_needs_the_typed_secret_every_time_and_claims_only_windows_acceptance(wake):
    pending = ask(wake, "wake.schedule", "a1", at=at(90), stayAwakeMinutes=15)  # the fixture policy ALLOWS
    assert pending["status"] == "approval_required" and pending["needsSecret"] is True
    preview = pending["preview"]
    assert preview["dueUtc"] == "2026-10-13T13:30:00+00:00" and preview["stayAwakeMinutes"] == 15
    assert preview["action"] == "wake_schedule" and "modern_standby_keeps_display_off" in preview["limits"]
    assert ask(wake, "decide", "a2", approvalId=pending["approvalId"], approved=True,
               secret="wrong horse")["code"] == "secret_rejected"
    assert ask(wake, "decide", "a3", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "stale_approval"  # single use
    assert armed_calls(wake) == []
    pending = ask(wake, "wake.schedule", "a4", at=at(90), stayAwakeMinutes=15)
    done = ask(wake, "decide", "a5", approvalId=pending["approvalId"], approved=True, secret="correct horse")
    assert done["status"] == "armed" and done["claim"] == "windows_accepted_wake_timer"
    assert done["dueUtc"] == "2026-10-13T13:30:00+00:00" and "woke" not in json.dumps(done)
    (_, due, stay), = armed_calls(wake)
    assert due == NOW + timedelta(minutes=90) and stay == 15
    assert ask(wake, "wake.schedule", "a6", at=at(200))["code"] == "already_armed"
    status = ask(wake, "wake.status", "a7")["armed"]
    assert status["state"] == "armed" and status["wakeId"] == "wk-0123456789abcdef"
    rows = audit_rows(wake)
    assert (AuditEvent.COMPLETED, "device_control", "power:this-pc/wake-timer", "succeeded") in rows
    assert "correct horse" not in json.dumps(wake.events) and "wrong horse" not in json.dumps(wake.events)
    wake.power.on_event("wake_timer_fired", {"wakeId": "wk-0123456789abcdef", "firedAtUtc": "2026-10-13T13:30:00+00:00",
                                             "stayAwakeMinutes": 15})
    assert ask(wake, "wake.status", "a8")["armed"]["state"] == "fired"
    fired = [p for e, _, p in wake.events if e == hub_module.WAKE_EVENT]
    assert fired and fired[-1]["event"] == "wake_timer_fired"


def test_windows_refusing_to_resume_or_a_changed_power_plan_or_policy_never_arms(wake, monkeypatch):
    wake.power.state = "not_supported"
    pending = ask(wake, "wake.schedule", "n1", at=at(60))
    refused = ask(wake, "decide", "n2", approvalId=pending["approvalId"], approved=True, secret="correct horse")
    assert refused["code"] == "wake_not_supported"
    assert (AuditEvent.COMPLETED, "device_control", "power:this-pc/wake-timer", "failed") in audit_rows(wake)
    assert ask(wake, "wake.status", "n3")["armed"] is None
    wake.power.state = "armed"
    pending = ask(wake, "wake.schedule", "n4", at=at(60))
    wake.power.cap = WakeCapability(True, True, False, 4, True, "disabled", "disabled", "ac")  # changed meanwhile
    assert ask(wake, "decide", "n5", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "wake_timers_disabled"
    wake.power.cap = WakeCapability(True, True, False, 4, True, "enabled", "disabled", "ac")
    pending = ask(wake, "wake.schedule", "n6", at=at(60))
    monkeypatch.setattr(gate_module, "evaluate", policy(PermissionDecision.DENY))  # the policy tightened
    assert ask(wake, "decide", "n7", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "denied"
    assert not [r for r in audit_rows(wake) if r[0] == AuditEvent.DENIED_BY_USER]
    assert ask(wake, "wake.schedule", "n8", at=at(60))["code"] == "denied"  # refused before any approval
    assert len(armed_calls(wake)) == 1  # only the Windows-refused attempt reached arm


def test_cancel_is_gated_and_pause_stop_close_cancel_an_armed_wake(wake, monkeypatch):
    assert ask(wake, "wake.cancel", "x1")["status"] == "none"
    pending = ask(wake, "wake.schedule", "x2", at=at(60))
    ask(wake, "decide", "x3", approvalId=pending["approvalId"], approved=True, secret="correct horse")
    monkeypatch.setattr(gate_module, "evaluate", policy(PermissionDecision.REQUIRE_UI_CONFIRMATION))
    asked = ask(wake, "wake.cancel", "x4")
    assert asked["status"] == "approval_required" and asked["preview"]["action"] == "wake_cancel"
    assert wake.power.armed[0].cancelled == 0  # nothing cancelled before the answer
    assert ask(wake, "decide", "x5", approvalId=asked["approvalId"], approved=True)["status"] == "cancelled"
    assert wake.power.armed[0].cancelled == 1 and ask(wake, "wake.status", "x6")["armed"] is None
    monkeypatch.setattr(gate_module, "evaluate", policy(PermissionDecision.ALLOW))
    pending = ask(wake, "wake.schedule", "x7", at=at(60))
    ask(wake, "decide", "x8", approvalId=pending["approvalId"], approved=True, secret="correct horse")
    assert wake.hub.cancel_wake("pause") is True and wake.power.armed[1].cancelled == 1
    assert wake.hub.cancel_wake("pause") is False and ask(wake, "wake.status", "x9")["armed"] is None


# ------------------------------------------------------------ Media49 B1/B2 regressions (wake lifecycle)


def test_b1_a_pause_landing_while_a_wake_is_being_answered_leaves_nothing_armed(wake):
    original_arm, original_capability = wake.power.arm, wake.hub._wake_capability

    def pause_now():  # what set_runtime_paused does: the flag first, then the bridge's revoke + cancel_wake
        wake.state["paused"] = True
        wake.hub.cancel_wake("pause")

    def arm_then_pause(due, stay, on_event):  # (a) Pause lands while Windows is arming the timer
        armed, result = original_arm(due, stay, on_event)
        pause_now()
        return armed, result

    wake.power.arm = arm_then_pause
    pending = ask(wake, "wake.schedule", "b1a", at=at(60))
    answer = ask(wake, "decide", "b1b", approvalId=pending["approvalId"], approved=True, secret="correct horse")
    assert answer["ok"] is False and answer["code"] == "paused"
    assert wake.power.armed[0].cancelled == 1  # the timer Windows accepted is cancelled at once
    assert ask(wake, "wake.status", "b1c")["armed"] is None  # nothing armed while paused
    assert (AuditEvent.COMPLETED, "device_control", "power:this-pc/wake-timer", "refused") in audit_rows(wake)
    wake.state["paused"] = False
    wake.power.arm = original_arm

    def capability_then_pause():  # (b) Pause lands after the ticket was taken, before arming
        found = original_capability()
        pause_now()
        return found

    pending = ask(wake, "wake.schedule", "b1d", at=at(60))
    wake.hub._wake_capability = capability_then_pause  # only the answer's capability re-read pauses
    answer = ask(wake, "decide", "b1e", approvalId=pending["approvalId"], approved=True, secret="correct horse")
    assert answer["code"] == "paused" and len(armed_calls(wake)) == 1  # arm was never called for this one
    wake.state["paused"] = False
    wake.hub._wake_capability = original_capability
    pending = ask(wake, "wake.schedule", "b1f", at=at(60))  # control: without Pause the same flow arms
    assert ask(wake, "decide", "b1g", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["status"] == "armed"


class ControlledTimerApi:
    """A fake kernel32 timer for a REAL ArmedWake: the timer fires only when the test says so."""

    def __init__(self) -> None:
        self.fire = threading.Event()
        self.states: list[int] = []
        self.closed = threading.Event()

    def create_timer(self):
        return 5

    def set_timer(self, handle, due):
        return True, 0

    def wait(self, handle, timeout_ms):
        return power_wake.WAIT_OBJECT_0 if self.fire.wait(0.01) else power_wake.WAIT_TIMEOUT

    def cancel_timer(self, handle):
        pass

    def close(self, handle):
        self.closed.set()

    def execution_state(self, flags):
        self.states.append(flags)


class RealArmedPower(FakePower):
    def __init__(self) -> None:
        super().__init__()
        self.apis: list[ControlledTimerApi] = []

    def arm(self, due, stay, on_event):
        api = ControlledTimerApi()
        self.apis.append(api)
        armed = power_wake.ArmedWake(api, due, stay, on_event, sleep=lambda seconds: time.sleep(0.01))
        self.armed.append(armed)
        self.calls.append(("arm", due, stay))
        return armed, armed.arm()


def _fire_and_wait(world, api) -> None:
    api.fire.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and ask(world, "wake.status", f"f{time.monotonic_ns()}")["armed"]["state"] != "fired":
        time.sleep(0.01)
    assert api.states == [power_wake.ES_CONTINUOUS | power_wake.ES_SYSTEM_REQUIRED]  # holding the system awake


def test_b2_a_fired_wake_still_holding_the_system_awake_is_live_until_pause_or_cancel_ends_it(wake):
    power = RealArmedPower()
    wake.hub._power_factory = lambda: power
    pending = ask(wake, "wake.schedule", "b2a", at=at(60), stayAwakeMinutes=1)
    assert ask(wake, "decide", "b2b", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["status"] == "armed"
    _fire_and_wait(wake, power.apis[0])
    assert ask(wake, "wake.schedule", "b2c", at=at(90))["code"] == "already_armed"  # no newer wake orphans it
    assert wake.hub.cancel_wake("pause") is True  # Pause ends the hold
    power.armed[0].thread.join(5)
    assert not power.armed[0].thread.is_alive() and power.apis[0].states[-1] == power_wake.ES_CONTINUOUS
    pending = ask(wake, "wake.schedule", "b2d", at=at(90), stayAwakeMinutes=1)  # now a new wake may be set
    ask(wake, "decide", "b2e", approvalId=pending["approvalId"], approved=True, secret="correct horse")
    _fire_and_wait(wake, power.apis[1])
    assert ask(wake, "wake.cancel", "b2f")["status"] == "cancelled"  # wake.cancel ends a fired hold too
    power.armed[1].thread.join(5)
    assert not power.armed[1].thread.is_alive() and power.apis[1].states[-1] == power_wake.ES_CONTINUOUS
    assert all(thread_dead for thread_dead in (not a.thread.is_alive() for a in power.armed))


# ------------------------------------------------------------ scan to ONE exact new image file (WIA 2.0 scanners)

SCANNER = ImagingDevice("{6BDD1FC6-810F-11D0-BEC7-08002BE2092F}\\0000", "Office Scanner", "Acme", "A4", "scanner")
CAMERA = ImagingDevice("{6BDD1FC6-810F-11D0-BEC7-08002BE2092F}\\0001", "Old Camera", "Cam Co", "WIA 1", "camera")
WEBCAM = ImagingDevice("{6BDD1FC6-810F-11D0-BEC7-08002BE2092F}\\0002", "Webcam", "Cam Co", "video", "video")


class FakeImaging:
    """Stands for core/wia_imaging.WinWia: records listings and scans; a scan writes a synthetic page."""

    def __init__(self) -> None:
        self.devices = [SCANNER, CAMERA, WEBCAM]
        self.calls: list[tuple] = []
        self.stagings: list[Path] = []
        self.during_scan = None  # a hook: what happens while Windows' dialog is open
        self.page = "png"

    def list_devices(self):
        self.calls.append(("list",))
        return list(self.devices)

    def acquire(self, wia_id, staging):
        self.calls.append(("acquire", wia_id))
        self.stagings.append(Path(staging))
        if self.page is None:
            return []  # cancelled in the dialog
        target = Path(staging) / "qronos-scan.png"
        if self.page == "garbage":
            target.write_bytes(b"not an image at all")
        else:
            from PIL import Image

            Image.new("RGB", (64, 48), (250, 250, 240)).save(target, format="PNG")
        if self.during_scan:
            self.during_scan()
        return [str(target)]


@pytest.fixture
def scan(world):
    imaging = FakeImaging()
    world.hub._imaging_factory = lambda: imaging
    world.imaging = imaging
    return world


def scanner_id(world) -> str:
    return next(d["id"] for d in ask(world, "imaging.list", f"il{len(world.events)}")["devices"]
                if d["kind"] == "scanner")


def scans(world) -> list:
    return [c for c in world.imaging.calls if c[0] == "acquire"]


def test_only_wia_scanners_are_offered_and_cameras_webcams_are_said_unsupported_also_while_paused(scan):
    scan.state["paused"] = True
    listed = ask(scan, "imaging.list", "s1")
    by_kind = {d["kind"]: d for d in listed["devices"]}
    assert by_kind["scanner"]["workflows"] == ["scan_to_file"] and by_kind["scanner"]["unsupported"] == ""
    assert by_kind["camera"]["workflows"] == [] and by_kind["camera"]["unsupported"] == "wia_camera_not_supported"
    assert by_kind["video"]["workflows"] == [] and by_kind["video"]["unsupported"] == "webcam_not_supported"
    assert "no_camera_webcam_or_phone_capture" in listed["limits"] and scans(scan) == []
    assert ask(scan, "image.capture", "s2", deviceId=by_kind["scanner"]["id"],
               destination="C:\\scans\\a.png")["code"] == "paused"


def test_scan_requests_are_validated_and_only_a_listed_scanner_and_a_new_file_qualify(scan, tmp_path):
    good = scanner_id(scan)
    camera = next(d["id"] for d in ask(scan, "imaging.list", "v0")["devices"] if d["kind"] == "camera")
    (tmp_path / "exists.png").write_bytes(b"x")
    for request_id, fields, code in (
        ("v1", {"deviceId": "../../x", "destination": str(tmp_path / "a.png")}, "bad_request"),
        ("v2", {"deviceId": good, "destination": "relative.png"}, "bad_request"),
        ("v3", {"deviceId": good, "destination": str(tmp_path / "a.gif")}, "bad_destination"),
        ("v4", {"deviceId": good, "destination": str(tmp_path / "exists.png")}, "destination_exists"),
        ("v5", {"deviceId": good, "destination": str(tmp_path / "nope" / "a.png")}, "bad_destination"),
        ("v6", {"deviceId": camera, "destination": str(tmp_path / "a.png")}, "unsupported"),
        ("v7", {"deviceId": "img-" + "0" * 20, "destination": str(tmp_path / "a.png")}, "device_gone"),
    ):
        assert ask(scan, "image.capture", request_id, **fields)["code"] == code, request_id
    assert scans(scan) == [] and scan.hub._ledger.open_tickets() == ()


def test_one_scan_needs_the_typed_secret_and_writes_exactly_one_new_file(scan, tmp_path):
    destination = tmp_path / "page.png"
    pending = ask(scan, "image.capture", "c1", deviceId=scanner_id(scan), destination=str(destination))
    assert pending["status"] == "approval_required" and pending["needsSecret"] is True  # the policy ALLOWS
    assert pending["preview"]["device"] == "Office Scanner" and pending["preview"]["format"] == "PNG"
    assert ask(scan, "decide", "c2", approvalId=pending["approvalId"], approved=True,
               secret="wrong horse")["code"] == "secret_rejected"
    assert scans(scan) == [] and not destination.exists()
    pending = ask(scan, "image.capture", "c3", deviceId=scanner_id(scan), destination=str(destination))
    done = ask(scan, "decide", "c4", approvalId=pending["approvalId"], approved=True, secret="correct horse")
    assert done["status"] == "saved" and done["path"] == str(destination) and (done["width"], done["height"]) == (64, 48)
    assert destination.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and done["bytes"] == destination.stat().st_size
    assert scans(scan) == [("acquire", SCANNER.wia_id)]
    assert all(not staging.exists() for staging in scan.imaging.stagings)  # the private staging folder is gone
    rows = audit_rows(scan)
    assert (AuditEvent.COMPLETED, "device_control", "imaging:Office Scanner/scan", "succeeded") in rows
    assert (AuditEvent.COMPLETED, "create_or_edit_file", str(destination), "succeeded") in rows
    assert "correct horse" not in json.dumps(scan.events) and "wrong horse" not in json.dumps(scan.events)
    assert ask(scan, "image.capture", "c5", deviceId=scanner_id(scan),
               destination=str(destination))["code"] == "destination_exists"  # never overwritten
    third = ask(scan, "image.capture", "c6", deviceId=scanner_id(scan), destination=str(tmp_path / "next.jpg"))
    assert ask(scan, "decline", "c7", approvalId=third["approvalId"])["declined"] is True
    assert any(r[:3] == (AuditEvent.DENIED_BY_USER, "device_control", "imaging:Office Scanner/scan") for r in audit_rows(scan))


def test_both_gates_decide_before_and_at_the_answer(scan, tmp_path, monkeypatch):
    destination = str(tmp_path / "page.png")
    for category in (ActionCategory.CREATE_OR_EDIT_FILE, ActionCategory.DEVICE_CONTROL):
        monkeypatch.setattr(gate_module, "evaluate", policy_by({category: PermissionDecision.DENY}))
        assert ask(scan, "image.capture", f"g-{category.value}", deviceId=scanner_id(scan),
                   destination=destination)["code"] == "denied"
    assert scan.hub._ledger.open_tickets() == ()
    monkeypatch.setattr(gate_module, "evaluate", policy(PermissionDecision.ALLOW))
    pending = ask(scan, "image.capture", "g1", deviceId=scanner_id(scan), destination=destination)
    monkeypatch.setattr(gate_module, "evaluate", policy_by({ActionCategory.CREATE_OR_EDIT_FILE: PermissionDecision.DENY}))
    assert ask(scan, "decide", "g2", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "denied"
    assert scans(scan) == [] and not Path(destination).exists()
    assert not [r for r in audit_rows(scan) if r[0] == AuditEvent.DENIED_BY_USER]


def test_cancel_garbage_pause_and_a_new_file_meanwhile_all_write_nothing(scan, tmp_path):
    def approve(request_id, destination):
        pending = ask(scan, "image.capture", request_id, deviceId=scanner_id(scan), destination=str(destination))
        return ask(scan, "decide", request_id + "d", approvalId=pending["approvalId"], approved=True,
                   secret="correct horse")

    scan.imaging.page = None  # the person cancels in Windows' dialog
    assert approve("x1", tmp_path / "a.png")["code"] == "scan_cancelled" and not (tmp_path / "a.png").exists()
    scan.imaging.page = "garbage"  # the driver wrote something that is not an image
    assert approve("x2", tmp_path / "b.png")["code"] == "not_an_image" and not (tmp_path / "b.png").exists()
    scan.imaging.page = "png"

    def pause_during_dialog():  # what set_runtime_paused does: the flag, then the bridge's lifecycle hook
        scan.state["paused"] = True
        scan.hub.cancel_wake("pause")

    scan.imaging.during_scan = pause_during_dialog
    assert approve("x3", tmp_path / "c.png")["code"] == "paused" and not (tmp_path / "c.png").exists()
    scan.state["paused"], scan.imaging.during_scan = False, None
    pending = ask(scan, "image.capture", "x4", deviceId=scanner_id(scan), destination=str(tmp_path / "d.png"))
    (tmp_path / "d.png").write_bytes(b"made meanwhile")  # the destination appears before the answer
    assert ask(scan, "decide", "x5", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "destination_exists"
    assert (tmp_path / "d.png").read_bytes() == b"made meanwhile"  # untouched
    assert all(not staging.exists() for staging in scan.imaging.stagings)
    assert len(scans(scan)) == 3  # cancel, garbage and the paused one reached the dialog; the last did not


# ------------------------------------------------------------ ONE still frame from a camera (Media Foundation)

USB_CAMERA = CameraDevice(r"\\?\usb#vid_046d&pid_0825&mi_00#7&1a2b3c&0&0000#{e5323777-f976-4f5b-9b55-b94699c46e44}\global",
                          "USB Camera", True, "")
VIRTUAL_CAMERA = CameraDevice(r"\\?\root#image#0000#{e5323777-f976-4f5b-9b55-b94699c46e44}\global", "Virtual Cam", False, "")


class FakeCamera:
    """Stands for core/camera_capture.WinCamera: records listings and captures; a capture returns a synthetic frame."""

    def __init__(self) -> None:
        self.devices = [USB_CAMERA]
        self.calls: list[tuple] = []
        self.during_capture = None  # a hook: what happens while the camera is on
        self.error: CameraError | None = None
        self.closing = False  # an earlier capture's worker has not exited

    def capture_in_flight(self):
        return self.closing

    def list_devices(self):
        self.calls.append(("list",))
        return list(self.devices)

    def capture(self, symbolic_link, should_stop):
        self.calls.append(("capture", symbolic_link))
        if self.during_capture:
            self.during_capture()
        if should_stop():  # what the real worker does when Pause/Stop lands: the camera is shut down, nothing kept
            raise CameraError("cancelled", "stopped")
        if self.error is not None:
            raise self.error
        return CameraFrame(64, 48, bytes([30, 60, 200, 0]) * (64 * 48), 5)


@pytest.fixture
def camera(world):
    fake = FakeCamera()
    world.hub._camera_factory = lambda: fake
    world.camera = fake
    return world


def camera_id(world) -> str:
    return ask(world, "camera.list", f"cl{len(world.events)}")["devices"][0]["id"]


def captures(world) -> list:
    return [c for c in world.camera.calls if c[0] == "capture"]


def test_cameras_are_listed_also_while_paused_but_nothing_is_captured_then(camera):
    camera.state["paused"] = True
    listed = ask(camera, "camera.list", "k1")
    (device,) = listed["devices"]
    assert device["name"] == "USB Camera" and device["workflows"] == ["still_to_file"]
    assert device["id"] == USB_CAMERA.device_id and "symbolic" not in json.dumps(listed).lower()
    assert set(listed["limits"]) == set(hub_module.CAMERA_LIMITS) and set(listed["formats"]) >= {".png", ".jpg"}
    assert ask(camera, "camera.capture", "k2", deviceId=device["id"], destination="C:\\stills\\a.png")["code"] == "paused"
    assert captures(camera) == []


def test_camera_requests_are_validated_and_only_a_listed_camera_and_a_new_file_qualify(camera, tmp_path):
    good = camera_id(camera)
    (tmp_path / "exists.png").write_bytes(b"x")
    for request_id, fields, code in (
        ("q1", {"deviceId": "img-" + "0" * 20, "destination": str(tmp_path / "a.png")}, "bad_request"),
        ("q2", {"deviceId": good, "destination": "relative.png"}, "bad_request"),
        ("q3", {"deviceId": good, "destination": str(tmp_path / "a.gif")}, "bad_destination"),
        ("q4", {"deviceId": good, "destination": str(tmp_path / "exists.png")}, "destination_exists"),
        ("q5", {"deviceId": good, "destination": str(tmp_path / "nope" / "a.png")}, "bad_destination"),
        ("q6", {"deviceId": "cam-" + "0" * 20, "destination": str(tmp_path / "a.png")}, "device_gone"),
    ):
        assert ask(camera, "camera.capture", request_id, **fields)["code"] == code, request_id
    assert captures(camera) == [] and camera.hub._ledger.open_tickets() == ()


def test_one_still_needs_the_typed_secret_and_writes_exactly_one_new_file(camera, tmp_path):
    destination = tmp_path / "still.png"
    pending = ask(camera, "camera.capture", "p1", deviceId=camera_id(camera), destination=str(destination))
    assert pending["status"] == "approval_required" and pending["needsSecret"] is True  # the policy ALLOWS
    assert pending["preview"]["action"] == "camera" and pending["preview"]["device"] == "USB Camera"
    assert ask(camera, "decide", "p2", approvalId=pending["approvalId"], approved=True,
               secret="wrong horse")["code"] == "secret_rejected"
    assert captures(camera) == [] and not destination.exists()
    pending = ask(camera, "camera.capture", "p3", deviceId=camera_id(camera), destination=str(destination))
    done = ask(camera, "decide", "p4", approvalId=pending["approvalId"], approved=True, secret="correct horse")
    assert done["status"] == "saved" and done["path"] == str(destination) and (done["width"], done["height"]) == (64, 48)
    assert done["framesRead"] == 5 and done["dark"] is False and done["bytes"] == destination.stat().st_size
    from PIL import Image

    with Image.open(destination) as written:
        assert written.format == "PNG" and written.getpixel((0, 0)) == (200, 60, 30)
    assert captures(camera) == [("capture", USB_CAMERA.symbolic_link)]
    rows = audit_rows(camera)
    assert (AuditEvent.COMPLETED, "device_control", "camera:USB Camera/capture", "succeeded") in rows
    assert (AuditEvent.COMPLETED, "create_or_edit_file", str(destination), "succeeded") in rows
    assert "correct horse" not in json.dumps(camera.events) and "wrong horse" not in json.dumps(camera.events)
    assert ask(camera, "camera.capture", "p5", deviceId=camera_id(camera),
               destination=str(destination))["code"] == "destination_exists"  # never overwritten
    third = ask(camera, "camera.capture", "p6", deviceId=camera_id(camera), destination=str(tmp_path / "next.jpg"))
    assert ask(camera, "decline", "p7", approvalId=third["approvalId"])["declined"] is True
    assert any(r[:3] == (AuditEvent.DENIED_BY_USER, "device_control", "camera:USB Camera/capture")
               for r in audit_rows(camera))
    assert len(captures(camera)) == 1


def test_both_camera_gates_decide_before_and_at_the_answer(camera, tmp_path, monkeypatch):
    destination = str(tmp_path / "still.png")
    for category in (ActionCategory.CREATE_OR_EDIT_FILE, ActionCategory.DEVICE_CONTROL):
        monkeypatch.setattr(gate_module, "evaluate", policy_by({category: PermissionDecision.DENY}))
        assert ask(camera, "camera.capture", f"h-{category.value}", deviceId=camera_id(camera),
                   destination=destination)["code"] == "denied"
    assert camera.hub._ledger.open_tickets() == ()
    monkeypatch.setattr(gate_module, "evaluate", policy(PermissionDecision.ALLOW))
    pending = ask(camera, "camera.capture", "h1", deviceId=camera_id(camera), destination=destination)
    monkeypatch.setattr(gate_module, "evaluate", policy_by({ActionCategory.DEVICE_CONTROL: PermissionDecision.DENY}))
    assert ask(camera, "decide", "h2", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "denied"
    assert captures(camera) == [] and not Path(destination).exists()
    assert not [r for r in audit_rows(camera) if r[0] == AuditEvent.DENIED_BY_USER]


def test_pause_before_or_during_the_capture_a_changed_camera_and_os_refusals_write_nothing(camera, tmp_path):
    def approve(request_id, destination, before=None):
        pending = ask(camera, "camera.capture", request_id, deviceId=camera_id(camera), destination=str(destination))
        if before:
            before()
        return ask(camera, "decide", request_id + "d", approvalId=pending["approvalId"], approved=True,
                   secret="correct horse")

    def pause():  # what set_runtime_paused does: the flag, then the bridge's lifecycle hook
        camera.state["paused"] = True
        camera.hub.cancel_wake("pause")

    assert approve("z1", tmp_path / "a.png", before=pause)["code"] == "paused"  # paused before the answer
    assert captures(camera) == [] and not (tmp_path / "a.png").exists()  # the camera was never turned on
    camera.state["paused"] = False
    camera.camera.during_capture = pause
    assert approve("z2", tmp_path / "b.png")["code"] == "paused" and not (tmp_path / "b.png").exists()
    camera.state["paused"], camera.camera.during_capture = False, None
    rows = audit_rows(camera)
    assert rows.count((AuditEvent.COMPLETED, "device_control", "camera:USB Camera/capture", "refused")) == 1
    for error in ("camera_privacy_denied", "camera_busy", "camera_format_unsupported", "camera_timeout"):
        camera.camera.error = CameraError(error, "from Windows")
        assert approve("z-" + error, tmp_path / f"{error}.png")["code"] == error
        assert not (tmp_path / f"{error}.png").exists()
    camera.camera.error = None
    pending = ask(camera, "camera.capture", "z3", deviceId=camera_id(camera), destination=str(tmp_path / "c.png"))
    camera.camera.devices = [CameraDevice(USB_CAMERA.symbolic_link.replace("046d", "1234"), "USB Camera", True, "")]
    assert ask(camera, "decide", "z3d", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "device_gone"  # the approved camera is no longer listed
    camera.camera.devices = [USB_CAMERA]
    pending = ask(camera, "camera.capture", "z4", deviceId=camera_id(camera), destination=str(tmp_path / "d.png"))
    (tmp_path / "d.png").write_bytes(b"made meanwhile")  # the destination appears before the answer
    assert ask(camera, "decide", "z4d", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "destination_exists"
    assert (tmp_path / "d.png").read_bytes() == b"made meanwhile"  # untouched
    assert len(captures(camera)) == 1 + 4  # the paused-during one and the four OS refusals reached the camera


def test_a_camera_not_proven_local_hardware_is_listed_unsupported_and_never_offered(camera, tmp_path):
    camera.camera.devices = [USB_CAMERA, VIRTUAL_CAMERA]
    by_name = {d["name"]: d for d in ask(camera, "camera.list", "o1")["devices"]}
    assert by_name["USB Camera"]["workflows"] == ["still_to_file"] and by_name["USB Camera"]["unsupported"] == ""
    assert by_name["Virtual Cam"]["workflows"] == [] and by_name["Virtual Cam"]["unsupported"] == "not_hardware_source"
    assert ask(camera, "camera.capture", "o2", deviceId=by_name["Virtual Cam"]["id"],
               destination=str(tmp_path / "v.png"))["code"] == "unsupported"
    pending = ask(camera, "camera.capture", "o3", deviceId=by_name["USB Camera"]["id"], destination=str(tmp_path / "u.png"))
    camera.camera.devices = [CameraDevice(USB_CAMERA.symbolic_link, "USB Camera", False, "")]  # no longer proven local
    assert ask(camera, "decide", "o3d", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "unsupported"
    assert captures(camera) == [] and not (tmp_path / "u.png").exists()


def test_while_an_earlier_capture_is_still_closing_no_new_capture_is_asked_or_started(camera, tmp_path):
    camera.camera.closing = True
    listed = ask(camera, "camera.list", "w1")
    assert listed["closing"] is True
    assert ask(camera, "camera.capture", "w2", deviceId=listed["devices"][0]["id"],
               destination=str(tmp_path / "a.png"))["code"] == "camera_still_closing"
    assert camera.hub._ledger.open_tickets() == () and captures(camera) == []  # no secret was even asked for
    camera.camera.error = CameraError("camera_stuck", "asked to stop, may still be on")
    camera.camera.closing = False
    pending = ask(camera, "camera.capture", "w3", deviceId=listed["devices"][0]["id"], destination=str(tmp_path / "b.png"))
    assert ask(camera, "decide", "w3d", approvalId=pending["approvalId"], approved=True,
               secret="correct horse")["code"] == "camera_stuck"
    assert not (tmp_path / "b.png").exists()


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


def test_pause_revokes_a_pending_3d_approval_and_nothing_is_handed_over(runtime_with_hub, tmp_path):
    runtime, events, spooler = runtime_with_hub
    workshop = next(d for d in hub_action(runtime, events, "w1", op="devices.list")["devices"]
                    if d["name"] == "Workshop")
    package = write_3mf(tmp_path / "part.3mf")
    pending = hub_action(runtime, events, "w2", op="print3d.request", deviceId=workshop["id"], path=str(package))
    assert pending["status"] == "approval_required" and pending["preview"]["readAfterApproval"] is True
    assert [t.kind for t in runtime._approvals.open_tickets()] == [ApprovalKind.DEVICE_ACTION]
    runtime.set_runtime_paused(True)
    runtime.set_runtime_paused(False)
    assert runtime._approvals.open_tickets() == ()
    late = hub_action(runtime, events, "w3", op="decide", approvalId=pending["approvalId"], approved=True,
                      secret="anything")
    assert late["code"] == "stale_approval" and spooler.submitted == []


@pytest.mark.parametrize("lifecycle", ["pause", "stop", "close"])
def test_the_real_runtime_pause_stop_and_close_cancel_an_armed_wake(runtime_with_hub, lifecycle):
    runtime, events, spooler = runtime_with_hub
    hub = runtime.device_hub()
    armed = FakeArmed()
    with hub._wake_lock:  # an armed wake as decide leaves it (arming itself is covered with the fake power layer)
        hub._armed, hub._wake_info = armed, {"wakeId": "wk-0123456789abcdef", "dueUtc": "2026-10-13T13:30:00+00:00",
                                             "stayAwakeMinutes": 10, "state": "armed"}
    if lifecycle == "pause":
        runtime.set_runtime_paused(True)
        runtime.set_runtime_paused(False)
    elif lifecycle == "stop":
        assert isinstance(runtime.request_stop_response(), dict)
    else:
        runtime.close()
    assert armed.cancelled == 1 and hub._wake_snapshot() is None
