"""Media contracts with the rest of Qronos: the permission gate, the resource
governor, the offline network policy and the command-line surface."""
from __future__ import annotations

import ast
import io
import json
from pathlib import Path
import sys

import pytest

from core.actions import ActionRequest
from core.activity_guard import ActivityMode, ResourcePressure
from core.permission_control import set_custom_rule, set_permission_policy
from core.resource_governor import ResourceGovernor
from core.resource_guard import SystemStatus
from qronos_media import cli
from core.approval_ledger import ApprovalLedger
from core.approval_secret import ApprovalSecretStore
from qronos_media.permissions import Approval, GatePermissions, MediaPermissionRefused, Pending
from qronos_media.service import MediaRequestError
from qronos_media.resources import GovernorAuthority, ResourceRefused, StageNeeds
from security.gate import evaluate
from security.permissions import ActionCategory, PermissionDecision, PermissionLevel, PermissionPolicy
from tests.media.media_support import FakePermissions, make_service, quiet_activity
from tests.test_offline_network_policy import network_findings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def policy(tmp_path: Path, monkeypatch):
    """The real permission store and audit log, redirected to this test's directory."""
    monkeypatch.setenv("QRONOS_PERMISSION_POLICY_PATH", str(tmp_path / "permissions_v2.json"))
    monkeypatch.setenv("QRONOS_PERMISSION_AUDIT_PATH", str(tmp_path / "permission_audit.jsonl"))
    return tmp_path


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "selected"
    folder.mkdir(exist_ok=True)
    return folder


# ------------------------------------------------------------- permission

def test_files_policy_allow_admits_the_selected_root(policy: Path) -> None:
    set_permission_policy("files", "ALLOW")
    verdicts = []
    approval = GatePermissions(audit=verdicts.append).request_root(_folder(policy), "folder", 3)
    assert isinstance(approval, Approval) and approval.decision == "allow"
    request = verdicts[0].request
    assert request.category is ActionCategory.READ_NON_SENSITIVE and request.target == str(_folder(policy))
    assert request.parameters == {"operation": "media.add_root", "kind": "folder", "maxDepth": 3}


def test_files_policy_deny_refuses_and_revokes(policy: Path) -> None:
    set_permission_policy("files", "ALLOW")
    gate = GatePermissions()
    approval = gate.request_root(_folder(policy), "folder", 3)
    set_permission_policy("files", "DENY")
    with pytest.raises(MediaPermissionRefused) as refused:
        gate.request_root(_folder(policy), "folder", 3)
    assert refused.value.reason == "denied"
    with pytest.raises(MediaPermissionRefused) as revoked:
        gate.recheck_root(_folder(policy), approval, "folder", 3)
    assert revoked.value.reason == "revoked"


def test_custom_read_rule_is_respected(policy: Path) -> None:
    set_custom_rule("files", "read_non_sensitive", "DENY")
    with pytest.raises(MediaPermissionRefused):
        GatePermissions().request_root(_folder(policy), "folder", 3)


def test_ask_policy_without_a_ledger_fails_closed(policy: Path) -> None:
    set_permission_policy("files", "ALWAYS_ASK")
    with pytest.raises(MediaPermissionRefused) as refused:
        GatePermissions().request_root(_folder(policy), "folder", 3)
    assert refused.value.reason == "approval_required"


def test_ask_policy_approval_is_single_use_and_bound_to_the_prepared_root(policy: Path) -> None:
    set_permission_policy("files", "ALWAYS_ASK")
    gate = GatePermissions(ledger=ApprovalLedger())
    pending = gate.request_root(_folder(policy), "folder", 3)
    assert isinstance(pending, Pending) and pending.preview == {"path": str(_folder(policy)), "kind": "folder",
                                                                "maxDepth": 3}
    decision = PermissionDecision(pending.decision)
    with pytest.raises(MediaPermissionRefused) as wrong:
        gate.approve_root(pending.approval_id, PermissionDecision.REQUIRE_TYPED_SECRET)
    assert wrong.value.reason == "approval_rejected"
    with pytest.raises(MediaPermissionRefused) as spent:
        gate.approve_root(pending.approval_id, decision)  # the wrong answer consumed the identity
    assert spent.value.reason == "stale_approval"
    pending = gate.request_root(_folder(policy), "folder", 3)
    payload, approval = gate.approve_root(pending.approval_id, decision)
    assert (payload["path"], payload["kind"], payload["maxDepth"]) == (str(_folder(policy)), "folder", 3)
    assert approval.decision == f"grant:{decision.value}"
    with pytest.raises(MediaPermissionRefused) as replay:
        gate.approve_root(pending.approval_id, decision)
    assert replay.value.reason == "stale_approval"
    gate.recheck_root(_folder(policy), approval, "folder", 3)  # the live in-process grant carries this root
    for kind, depth in (("folder", 64), ("file", 3)):  # a changed root row cannot widen the grant
        with pytest.raises(MediaPermissionRefused) as widened:
            gate.recheck_root(_folder(policy), approval, kind, depth)
        assert widened.value.reason == "approval_required"


def test_an_approval_cannot_be_spent_on_another_operation(policy: Path) -> None:
    set_permission_policy("files", "ALWAYS_ASK")
    gate = GatePermissions(ledger=ApprovalLedger())
    root = gate.request_root(_folder(policy), "folder", 3)
    export = gate.request_export(policy / "r.json", b"{}", "0" * 64, {"assetId": "a", "format": "json", "kind": "full"})
    decision = PermissionDecision(root.decision)
    with pytest.raises(MediaPermissionRefused):
        gate.approve_export(root.approval_id, decision)
    with pytest.raises(MediaPermissionRefused):
        gate.approve_root(export.approval_id, decision)


def test_export_approval_writes_only_the_prepared_bytes(policy: Path) -> None:
    set_permission_policy("files", "ALWAYS_ASK")
    gate = GatePermissions(ledger=ApprovalLedger())
    details = {"assetId": "a", "format": "json", "kind": "full"}
    first = gate.request_export(policy / "r.json", b"first", "1" * 64, details)
    second = gate.request_export(policy / "r.json", b"second", "2" * 64, details)
    assert first.approval_id != second.approval_id
    payload, _ = gate.approve_export(first.approval_id, PermissionDecision(first.decision))
    assert (payload["content"], payload["sha256"]) == (b"first", "1" * 64)
    assert first.preview["sha256"] == "1" * 64 and first.preview["head"] == "first"


def test_a_stored_approval_row_never_continues_work_on_its_own(policy: Path) -> None:
    set_permission_policy("files", "ALWAYS_ASK")
    gate = GatePermissions(ledger=ApprovalLedger())
    decision = evaluate(ActionRequest(category=ActionCategory.READ_NON_SENSITIVE, target="x", summary="s")).decision
    for forged in (Approval("forged", f"grant:{decision.value}", 4_102_444_800.0),  # approved "in 2100"
                   Approval("forged", "allow", 0.0)):
        with pytest.raises(MediaPermissionRefused) as refused:
            gate.recheck_root(_folder(policy), forged, "folder", 3)
        assert refused.value.reason == "approval_required"


def test_policy_changed_to_deny_while_approval_was_open_refuses(policy: Path) -> None:
    set_permission_policy("files", "ALWAYS_ASK")
    gate = GatePermissions(ledger=ApprovalLedger())
    pending = gate.request_root(_folder(policy), "folder", 3)
    set_permission_policy("files", "DENY")
    with pytest.raises(MediaPermissionRefused) as refused:
        gate.approve_root(pending.approval_id, PermissionDecision(pending.decision))
    assert refused.value.reason == "denied"


def test_service_runs_the_single_use_flow_end_to_end(policy: Path) -> None:
    set_permission_policy("files", "ALWAYS_ASK")
    service = make_service(policy / "data", tools=None, permissions=GatePermissions(ledger=ApprovalLedger()))
    pending = service.add_root(str(_folder(policy)))
    assert pending["status"] == "approval_required" and service.list_roots() == []
    added = service.approve_root(pending["approvalId"], pending["decision"])
    assert added["status"] == "added" and added["approval"]["decision"].startswith("grant:")
    with pytest.raises(MediaPermissionRefused):
        service.approve_root(pending["approvalId"], pending["decision"])
    with pytest.raises(MediaRequestError):
        service.approve_root("x" * 30, "not-a-decision")
    service.close()


def test_service_refuses_root_when_the_gate_refuses(policy: Path) -> None:
    set_permission_policy("files", "DENY")
    service = make_service(policy / "data", tools=None, permissions=GatePermissions())
    with pytest.raises(MediaPermissionRefused):
        service.add_root(str(_folder(policy)))
    assert service.list_roots() == []
    service.close()


def test_export_uses_the_file_write_category(policy: Path) -> None:
    set_custom_rule("files", "create_or_edit_file", "ALLOW")
    verdicts = []
    GatePermissions(audit=verdicts.append).request_export(policy / "r.json", b"x", "ab" * 32,
                                                          {"assetId": "a", "format": "json", "kind": "full"})
    assert verdicts[0].request.category is ActionCategory.CREATE_OR_EDIT_FILE
    assert verdicts[0].request.parameters["sha256"] == "ab" * 32


@pytest.fixture
def typed_secret_policy(policy: Path, monkeypatch):
    """Make the real gate return REQUIRE_TYPED_SECRET for both media categories."""
    from security import permissions as policy_table

    for category in (ActionCategory.READ_NON_SENSITIVE, ActionCategory.CREATE_OR_EDIT_FILE):
        stronger = PermissionPolicy(category=category, level=PermissionLevel.TYPED_SECRET, reversible=True,
                                    description="test: typed secret")
        monkeypatch.setitem(policy_table.ACTION_POLICIES, category, stronger)
    store = ApprovalSecretStore(policy / "secret.sqlite3", audit_path=policy / "secret_audit.jsonl")
    store.set_secret("correct horse battery")
    return store


def test_typed_secret_policy_needs_the_configured_secret(typed_secret_policy) -> None:
    folder = _folder(Path(typed_secret_policy.path).parent)
    gate = GatePermissions(ledger=ApprovalLedger(), secrets=typed_secret_policy)
    pending = gate.request_root(folder, "folder", 3)
    assert pending.decision == PermissionDecision.REQUIRE_TYPED_SECRET.value
    assert "correct horse battery" not in json.dumps(pending.preview)
    decision = PermissionDecision(pending.decision)
    for wrong in (None, "", "guess guess guess"):
        with pytest.raises(MediaPermissionRefused) as refused:
            gate.approve_root(pending.approval_id, decision, wrong)
        assert refused.value.reason in {"secret_required", "secret_rejected", "stale_approval"}
    with pytest.raises(MediaPermissionRefused) as spent:  # the identity was consumed by the first attempt
        gate.approve_root(pending.approval_id, decision, "correct horse battery")
    assert spent.value.reason == "stale_approval"
    pending = gate.request_root(folder, "folder", 3)
    payload, approval = gate.approve_root(pending.approval_id, decision, "correct horse battery")
    assert payload["path"] == str(folder) and approval.decision == "grant:require_typed_secret"
    gate.recheck_root(folder, approval, "folder", 3)


def test_typed_secret_without_a_verifier_fails_closed(typed_secret_policy) -> None:
    folder = _folder(Path(typed_secret_policy.path).parent)
    gate = GatePermissions(ledger=ApprovalLedger())  # no secret store wired in
    pending = gate.request_root(folder, "folder", 3)
    with pytest.raises(MediaPermissionRefused) as refused:
        gate.approve_root(pending.approval_id, PermissionDecision(pending.decision), "correct horse battery")
    assert refused.value.reason == "typed_secret_unavailable"


def test_typed_secret_export_writes_nothing_without_the_secret(typed_secret_policy) -> None:
    base = Path(typed_secret_policy.path).parent
    gate = GatePermissions(ledger=ApprovalLedger(), secrets=typed_secret_policy)
    details = {"assetId": "a", "format": "json", "kind": "full"}
    pending = gate.request_export(base / "r.json", b"{}", "0" * 64, details)
    with pytest.raises(MediaPermissionRefused):
        gate.approve_export(pending.approval_id, PermissionDecision(pending.decision), "wrong secret!")
    pending = gate.request_export(base / "r.json", b"{}", "0" * 64, details)
    payload, _ = gate.approve_export(pending.approval_id, PermissionDecision(pending.decision), "correct horse battery")
    assert payload["content"] == b"{}"


def test_denial_after_a_secret_approval_still_revokes(typed_secret_policy) -> None:
    folder = _folder(Path(typed_secret_policy.path).parent)
    gate = GatePermissions(ledger=ApprovalLedger(), secrets=typed_secret_policy)
    pending = gate.request_root(folder, "folder", 3)
    _, approval = gate.approve_root(pending.approval_id, PermissionDecision(pending.decision), "correct horse battery")
    set_custom_rule("files", "read_non_sensitive", "DENY")
    with pytest.raises(MediaPermissionRefused) as revoked:
        gate.recheck_root(folder, approval, "folder", 3)
    assert revoked.value.reason == "revoked"


# --------------------------------------------------------------- governor

QUIET = SystemStatus(cpu_usage_percent=5.0, ram_usage_percent=20.0, ram_used_gb=8.0, ram_total_gb=64.0)


def test_governor_authority_reserves_light_and_releases(tmp_path: Path) -> None:
    governor = ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None)
    authority = GovernorAuthority(governor, tmp_path, activity=quiet_activity, reserve_bytes=0)
    lease = authority.reserve("media:scenes:a", StageNeeds("scenes"))
    assert [r.task_id for r in governor.active()] == ["media:scenes:a"]
    authority.renew(lease)
    authority.release(lease)
    assert governor.active() == ()
    with pytest.raises(ResourceRefused) as refused:
        authority.reserve("media:asr:a", StageNeeds("asr", gpu=True))
    assert refused.value.reason == "gpu_lease_contract_pending"
    assert governor.active() == ()


def test_governor_authority_yields_to_creator_work(tmp_path: Path) -> None:
    governor = ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None)
    creator = lambda: (ActivityMode.CREATOR_ASSIST, ResourcePressure.NORMAL)  # noqa: E731
    authority = GovernorAuthority(governor, tmp_path, activity=creator, reserve_bytes=0)
    with pytest.raises(ResourceRefused) as refused:
        authority.reserve("media:scenes:a", StageNeeds("scenes"))
    assert refused.value.reason == "activity_mode" and governor.active() == ()


def test_disk_reserve_refuses_before_work(tmp_path: Path) -> None:
    governor = ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None)
    authority = GovernorAuthority(governor, tmp_path, activity=quiet_activity, reserve_bytes=10**18)
    with pytest.raises(ResourceRefused) as refused:
        authority.reserve("media:keyframes:a", StageNeeds("keyframes"))
    assert refused.value.reason == "disk_reserve"


# ---------------------------------------------------------------- network

MEDIA_MODULES = sorted((ROOT / "qronos_media").glob("*.py"))


@pytest.mark.parametrize("module", MEDIA_MODULES, ids=lambda path: path.name)
def test_media_modules_have_no_network_path(module: Path) -> None:
    assert network_findings(module.read_text(encoding="utf-8")) == {}


#: Modules that may use the process API at all: the tool runner and its job containment.
PROCESS_MODULES = {"containment.py", "tools.py"}
#: Native (ctypes) use that is NOT process creation, each with what it is for.
NATIVE_METADATA_MODULES = {
    "containment.py": "Job objects: limits, adoption of the suspended child, teardown, accounting, exit proof.",
    "decoder_copy.py": "File handles that pin and identify the verified private decoder copy (reviewed boundary).",
}
#: Native process creation is never reached through ctypes anywhere in the package.
NATIVE_SPAWN_NAMES = {"CreateProcessW", "CreateProcessA", "CreateProcessAsUserW", "ShellExecuteW", "ShellExecuteExW",
                      "WinExec"}


def test_only_the_tool_runner_starts_processes_and_never_through_a_shell() -> None:
    process_users, native_users, popen_sites = set(), set(), set()
    for module in MEDIA_MODULES:
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names] + [getattr(node, "module", None) or ""]
                if any(name.split(".")[0] in {"subprocess", "multiprocessing"} for name in names):
                    process_users.add(module.name)
                if any(name.split(".")[0] == "ctypes" for name in names):
                    native_users.add(module.name)
            if isinstance(node, ast.keyword) and node.arg == "shell":
                assert isinstance(node.value, ast.Constant) and node.value.value is False
            if isinstance(node, ast.Attribute) and node.attr in {"system", "startfile", "popen"}:
                assert not (isinstance(node.value, ast.Name) and node.value.id == "os"), module.name
            if isinstance(node, ast.Attribute) and node.attr == "Popen":
                popen_sites.add(module.name)
            if isinstance(node, (ast.Attribute, ast.Name)) and getattr(node, "attr", getattr(node, "id", "")) \
                    in NATIVE_SPAWN_NAMES:
                raise AssertionError(f"{module.name} reaches native process creation")
    assert process_users == PROCESS_MODULES
    assert popen_sites <= {"tools.py", "containment.py"} and "tools.py" in popen_sites
    assert native_users == set(NATIVE_METADATA_MODULES)


# -------------------------------------------------------------------- cli

def _run_cli(service, argv: list[str]) -> tuple[int, object]:
    buffer = io.BytesIO()

    class Out:
        def __init__(self) -> None:
            self.buffer = buffer

        def write(self, text: str) -> None:
            buffer.write(text.encode("utf-8"))

        def flush(self) -> None:
            pass

    original = sys.stdout
    sys.stdout = Out()
    try:
        code = cli.main(argv, service=service)
    finally:
        sys.stdout = original
    return code, json.loads(buffer.getvalue().decode("utf-8"))


def test_cli_drives_the_service_and_writes_utf8_json(tmp_path: Path) -> None:
    service = make_service(tmp_path / "data", tools=None)
    folder = _folder(tmp_path)
    code, root = _run_cli(service, ["add-root", str(folder)])
    assert code == 0 and root["path"] == str(folder) and root["approval"]["decision"] == "allow"
    code, status = _run_cli(service, ["status"])
    assert code == 0 and status["roots"] == 1 and not status["toolsAvailable"]
    code, error = _run_cli(service, ["search", "نخواستم"])
    assert code == 0 and error["results"] == [] and error["normalisedQuery"] == "نخواستم"
    code, refused = _run_cli(service, ["add-root", "\\\\server\\share"])
    assert code == 2 and "error" in refused
    service.close()


def test_cli_has_no_way_to_assert_approval() -> None:
    commands = cli.build_parser()._subparsers._group_actions[0].choices
    for name in ("add-root", "export-report"):
        options = {option for action in commands[name]._actions for option in action.option_strings}
        assert not any("approv" in option or "grant" in option for option in options), name


def test_cli_permission_refusal_is_a_clean_exit(tmp_path: Path) -> None:
    permissions = FakePermissions()
    folder = _folder(tmp_path)
    permissions.refuse_roots[str(folder)] = "approval_required"
    service = make_service(tmp_path / "data", tools=None, permissions=permissions)
    code, result = _run_cli(service, ["add-root", str(folder)])
    assert code == 2 and "approval_required" in result["error"]
    service.close()
