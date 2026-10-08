"""
The Elevated Action Broker: one exact, approved, typed action per launch.

``docs/qronos_product_architecture.md``: "The Elevated Action Broker must
receive an exact approved action rather than an open-ended shell command.
Qronos must never bypass UAC." And among the always-forbidden capabilities:
"Execute open-ended elevated shell commands proposed by a model."

This is the process that would run elevated. It is launched by the user
runtime through the normal UAC prompt (``core/uac_launch.py``), only when an
approved action needs it, and it exits after that one action. It:

    1. connects back to the runtime's fresh named pipe and authenticates with
       the per-session key (mutual HMAC handshake, ``core/supervisor_ipc.py``),
       then checks that the pipe is served by the exact runtime process that
       launched it, as the same Windows user;
    2. receives one request: a typed action ``{kind, params}`` and an approval
       envelope the runtime built from a live ``security.gate.ActionGrant``;
    3. verifies, in this order, refusing on the first failure:
         - the envelope's shape and its HMAC under the session key;
         - freshness: issued no later than now (with a small clock skew),
           not yet expired, and a lifetime of at most ``MAX_APPROVAL_SECONDS``;
         - the action digest: the envelope approves exactly this kind and
           these parameters, byte for byte in canonical form;
         - the action identity: the kind is in this build's closed registry,
           the parameters have exactly its fields and types and pass its
           validation, and nothing in them names a script, code or shell;
         - the category and target the approval names are the ones the
           registry derives from the parameters;
         - the permission gate, re-asked here: a category the policy forbids
           (script execution, registry, boot, security settings, …) is
           refused even with an approval;
         - single use: the approval's nonce is claimed with an atomic
           exclusive create, so a replay — to this broker or a later one —
           is refused;
    4. performs the action, reports the result, and exits.

There is no shell, interpreter or command-line action, and the registry
refuses to hold one. **The production registry is empty**: no current Qronos
feature needs elevation, so every request is refused until a reviewed action
is added to ``PRODUCTION_ACTIONS``. The mechanics above are real and tested end
to end with a fixture action in a disposable, non-elevated child process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from core.actions import ActionRequest, InvalidAction
import security.gate as gate
from security.permissions import ActionCategory
from core.supervisor_ipc import (
    PROTOCOL_VERSION,
    Channel,
    ChannelError,
    MessageSchema,
    canonical_json,
    connect,
    hmac_sha256,
)

MAX_APPROVAL_SECONDS = 180.0
CLOCK_SKEW_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 30.0

FROM_RUNTIME = MessageSchema({"request": {"action": "object", "approval": "object"}})
TO_RUNTIME = MessageSchema({
    "hello": {"pid": "int", "protocol": "int"},
    "result": {"ok": "bool", "outcome": "str", "detail": "str"},
})

_APPROVAL_FIELDS = {"actionId": str, "category": str, "target": str, "digest": str,
                    "issuedAt": float, "expiresAt": float, "nonce": str, "mac": str}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_KIND = re.compile(r"^[a-z][a-z0-9_.]{2,63}$")

#: Categories the broker never holds an action for, whatever the policy says.
#: The gate forbids them too; this keeps them out of the registry itself.
FORBIDDEN_CATEGORIES = frozenset({
    ActionCategory.SCRIPT_EXECUTION,
    ActionCategory.CODE_GENERATION,
    ActionCategory.CODE_ANALYSIS,
    ActionCategory.CODE_MODIFICATION,
    ActionCategory.REGISTRY_MODIFICATION,
    ActionCategory.BOOT_CONFIGURATION,
    ActionCategory.RAW_DISK_ACCESS,
    ActionCategory.SECURITY_CONFIGURATION,
    ActionCategory.BACKUP_DESTRUCTION,
    ActionCategory.PRIVILEGE_ESCALATION,
    ActionCategory.CYBER_ATTACK,
})

#: Words a kind may not contain: an action is a fixed operation, never a way
#: to run something.
_COMMAND_WORDS = re.compile(r"(shell|cmd|powershell|pwsh|script|exec|run|eval|python|command|macro|registry)")

#: Extensions of code, scripts and executables. A parameter naming one is
#: refused: the broker does not read, create, edit or start code.
CODE_EXTENSIONS = frozenset({
    ".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
    ".py", ".pyw", ".pyc", ".sh", ".bash", ".exe", ".com", ".scr", ".msi", ".msp", ".dll",
    ".sys", ".reg", ".lnk", ".hta", ".jar", ".cpl", ".inf", ".application", ".appref-ms",
})
_SHELL_FRAGMENTS = ("&&", "||", "|", ";", "`", "$(", "%comspec%", "cmd.exe", "cmd /", "powershell",
                    "pwsh", "rundll32", "mshta", "wscript", "cscript", "regsvr32", "certutil", "bitsadmin")


class BrokerRefusal(Exception):
    """The request is not performed. ``outcome`` is a fixed code for the UI."""

    def __init__(self, outcome: str, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


@dataclass(frozen=True)
class ActionSpec:
    """One exact operation the broker can perform, and nothing near it."""

    kind: str
    category: ActionCategory
    #: Exact parameter names and their types (str, int, bool, float).
    fields: Mapping[str, type]
    #: The user-facing target derived from the parameters. Must equal the
    #: target the person approved.
    target: Callable[[Mapping[str, Any]], str]
    perform: Callable[[Mapping[str, Any]], str]
    summary: str
    validate: Callable[[Mapping[str, Any]], None] = field(default=lambda params: None)


class ActionRegistry:
    """A closed table of actions. Construction refuses anything command-like."""

    def __init__(self, specs: Iterable[ActionSpec]) -> None:
        table: dict[str, ActionSpec] = {}
        for spec in specs:
            if not _KIND.fullmatch(spec.kind) or _COMMAND_WORDS.search(spec.kind):
                raise ValueError(f"Action kind {spec.kind!r} is not allowed in the broker.")
            if spec.category in FORBIDDEN_CATEGORIES:
                raise ValueError(f"Category {spec.category.value} is never brokered.")
            if spec.kind in table:
                raise ValueError(f"Duplicate action kind {spec.kind!r}.")
            for name, kind in spec.fields.items():
                if kind not in (str, int, bool, float) or not re.fullmatch(r"[a-z][A-Za-z0-9]{0,31}", name):
                    raise ValueError(f"Action {spec.kind} has an invalid field {name!r}.")
            table[spec.kind] = spec
        self._table = table

    def get(self, kind: str) -> Optional[ActionSpec]:
        return self._table.get(kind)

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._table))


#: What this build can perform elevated. Empty on purpose; see the module doc.
PRODUCTION_ACTIONS = ActionRegistry(())


def action_digest(kind: str, params: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({"kind": kind, "params": dict(params)})).hexdigest()


def approval_mac(key: bytes, approval: Mapping[str, Any]) -> str:
    return hmac_sha256(key, canonical_json({k: v for k, v in approval.items() if k != "mac"}))


def refuse_code_targets(value: Any) -> None:
    """Refuse a parameter value that names code, a script, an executable or a shell."""
    if isinstance(value, str):
        lowered = value.strip().lower().rstrip(". ")
        for extension in CODE_EXTENSIONS:
            if lowered.endswith(extension) or (extension + ":") in lowered or (extension + " ") in lowered:
                raise BrokerRefusal("code_target", "A parameter names code, a script or a program.")
        for fragment in _SHELL_FRAGMENTS:
            if fragment in lowered:
                raise BrokerRefusal("shell_text", "A parameter contains shell or script syntax.")


# ----------------------------------------------------------------- single use


class ConsumedApprovals:
    """
    Remembers which approvals were used, across broker launches.

    One marker file per approval nonce, created with ``O_CREAT | O_EXCL``:
    the create is atomic, so two brokers racing the same approval cannot both
    win. Markers older than the longest approval lifetime are pruned; an
    approval that old is refused as expired before it gets here.
    """

    def __init__(self, directory: Path, clock: Callable[[], float] = time.time) -> None:
        self._directory = Path(directory)
        self._clock = clock

    def claim(self, nonce: str) -> bool:
        if not _NONCE.fullmatch(nonce):
            return False
        self._directory.mkdir(parents=True, exist_ok=True)
        self._prune()
        marker = self._directory / (hashlib.sha256(nonce.encode("ascii")).hexdigest()[:40] + ".used")
        try:
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        os.close(descriptor)
        return True

    def _prune(self) -> None:
        horizon = self._clock() - 2 * (MAX_APPROVAL_SECONDS + CLOCK_SKEW_SECONDS)
        for marker in self._directory.glob("*.used"):
            try:
                if marker.is_file() and not marker.is_symlink() and marker.stat().st_mtime < horizon:
                    marker.unlink()
            except OSError:
                pass


def default_consumed_directory() -> Path:
    from core.config import CONFIG

    return CONFIG.paths.data / "ActionBroker" / "consumed"


# ------------------------------------------------------------------ verifying


@dataclass(frozen=True)
class VerifiedAction:
    spec: ActionSpec
    params: Mapping[str, Any]
    action_id: str


GateCheck = Callable[[ActionRequest], "gate.Verdict"]


def verify_request(
    action: object,
    approval: object,
    key: bytes,
    registry: ActionRegistry,
    consumed: ConsumedApprovals,
    clock: Callable[[], float] = time.time,
    gate_check: GateCheck = gate.evaluate,
) -> VerifiedAction:
    """Every check from the module doc, in order. Raises BrokerRefusal."""
    if not isinstance(approval, dict) or set(approval) != set(_APPROVAL_FIELDS):
        raise BrokerRefusal("malformed", "The approval has missing or unknown fields.")
    for name, kind in _APPROVAL_FIELDS.items():
        value = approval[name]
        if kind is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BrokerRefusal("malformed", f"Approval field {name} has the wrong type.")
        elif not isinstance(value, kind):
            raise BrokerRefusal("malformed", f"Approval field {name} has the wrong type.")
    if not (_HEX64.fullmatch(approval["digest"]) and _HEX64.fullmatch(approval["mac"]) and _NONCE.fullmatch(approval["nonce"])):
        raise BrokerRefusal("malformed", "The approval's digest, MAC or nonce is malformed.")

    if not hmac.compare_digest(approval["mac"], approval_mac(key, approval)):
        raise BrokerRefusal("bad_signature", "The approval was not issued in this session.")

    now = clock()
    issued, expires = float(approval["issuedAt"]), float(approval["expiresAt"])
    if issued > now + CLOCK_SKEW_SECONDS:
        raise BrokerRefusal("not_yet_valid", "The approval is dated in the future.")
    if now > expires:
        raise BrokerRefusal("expired", "The approval has expired.")
    if not 0 < expires - issued <= MAX_APPROVAL_SECONDS:
        raise BrokerRefusal("too_long", "The approval's lifetime exceeds the broker limit.")

    if not isinstance(action, dict) or set(action) != {"kind", "params"} \
            or not isinstance(action["kind"], str) or not isinstance(action["params"], dict):
        raise BrokerRefusal("malformed", "The action must be exactly a kind and parameters.")
    kind, params = action["kind"], action["params"]

    if not hmac.compare_digest(approval["digest"], action_digest(kind, params)):
        raise BrokerRefusal("not_approved", "This exact action was not the one approved.")

    spec = registry.get(kind)
    if spec is None:
        raise BrokerRefusal("unknown_action", "This build cannot perform that action elevated.")

    if set(params) != set(spec.fields):
        raise BrokerRefusal("bad_parameters", "The parameters do not match the action.")
    for name, expected in spec.fields.items():
        value = params[name]
        if isinstance(value, bool) and expected is not bool:
            raise BrokerRefusal("bad_parameters", f"Parameter {name} has the wrong type.")
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            continue
        if not isinstance(value, expected):
            raise BrokerRefusal("bad_parameters", f"Parameter {name} has the wrong type.")
        refuse_code_targets(value)
    spec.validate(params)

    target = spec.target(params)
    if approval["category"] != spec.category.value or approval["target"] != target:
        raise BrokerRefusal("not_approved", "The approval names a different category or target.")

    try:
        request = ActionRequest(
            category=spec.category,
            target=target,
            summary=spec.summary,
            action_id=approval["actionId"],
        )
    except InvalidAction as error:
        raise BrokerRefusal("malformed", "The approved action is not a valid action.") from error

    verdict = gate_check(request)
    if verdict.refused:
        raise BrokerRefusal("forbidden", verdict.reason)

    if not consumed.claim(approval["nonce"]):
        raise BrokerRefusal("already_used", "This approval has already been used.")

    return VerifiedAction(spec=spec, params=params, action_id=approval["actionId"])


def handle_request(message: dict, key: bytes, registry: ActionRegistry, consumed: ConsumedApprovals,
                   clock: Callable[[], float] = time.time, gate_check: GateCheck = gate.evaluate,
                   audit: Optional[Callable[[dict], None]] = None) -> dict:
    """Verify and perform one request; the result fields to send back."""
    try:
        verified = verify_request(message["action"], message["approval"], key, registry, consumed, clock, gate_check)
    except BrokerRefusal as refusal:
        _audit(audit, message, False, refusal.outcome)
        return {"ok": False, "outcome": refusal.outcome, "detail": refusal.detail}

    try:
        detail = verified.spec.perform(verified.params)
    except Exception as error:
        _audit(audit, message, False, "failed")
        return {"ok": False, "outcome": "failed", "detail": f"The action failed: {type(error).__name__}."}

    _audit(audit, message, True, "performed")
    return {"ok": True, "outcome": "performed", "detail": str(detail)[:500]}


def _audit(audit: Optional[Callable[[dict], None]], message: dict, ok: bool, outcome: str) -> None:
    if audit is None:
        return
    action = message.get("action") if isinstance(message, dict) else None
    approval = message.get("approval") if isinstance(message, dict) else None
    try:
        audit({
            "at": time.time(),
            "kind": action.get("kind") if isinstance(action, dict) and isinstance(action.get("kind"), str) else None,
            "actionId": approval.get("actionId") if isinstance(approval, dict) and isinstance(approval.get("actionId"), str) else None,
            "ok": ok,
            "outcome": outcome,
        })
    except Exception:
        pass


def jsonl_audit(path: Path) -> Callable[[dict], None]:
    """Append one JSON line per decision. No parameters, keys or MACs."""
    def write(record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    return write


# --------------------------------------------------------------------- serve


def same_user(pid: int) -> bool:
    """Whether ``pid`` runs as the same Windows user as this process."""
    try:
        import psutil

        return psutil.Process(pid).username() == psutil.Process(os.getpid()).username()
    except Exception:
        return False


def serve_once(
    address: str,
    key: bytes,
    runtime_pid: int,
    registry: ActionRegistry = PRODUCTION_ACTIONS,
    consumed: Optional[ConsumedApprovals] = None,
    audit: Optional[Callable[[dict], None]] = None,
    peer_pid=None,
    owner_check: Callable[[int], bool] = same_user,
) -> dict:
    """Connect back to the runtime, handle exactly one request, return its result."""
    if not owner_check(runtime_pid):
        raise ChannelError("The launching runtime is not the same user.")

    channel: Channel = connect(address, key, receive=FROM_RUNTIME, send=TO_RUNTIME,
                               expected_server_pid=runtime_pid, peer_pid=peer_pid)
    try:
        channel.send("hello", pid=os.getpid(), protocol=PROTOCOL_VERSION)
        message = channel.receive(REQUEST_TIMEOUT_SECONDS)
        result = handle_request(message, key, registry, consumed or ConsumedApprovals(default_consumed_directory()),
                                audit=audit)
        channel.send("result", **result)
        return result
    finally:
        channel.close()


def parse_arguments(arguments: Sequence[str]) -> tuple[str, bytes, int]:
    """``--pipe ADDRESS --key HEX64 --runtime-pid PID``, exactly."""
    if len(arguments) != 6 or arguments[0::2] != ["--pipe", "--key", "--runtime-pid"]:
        raise ValueError("Usage: action-broker --pipe ADDRESS --key HEX --runtime-pid PID")
    address, key_hex, pid_text = arguments[1], arguments[3], arguments[5]
    if not _HEX64.fullmatch(key_hex) or not pid_text.isdigit():
        raise ValueError("Invalid broker arguments.")
    return address, bytes.fromhex(key_hex), int(pid_text)


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        address, key, runtime_pid = parse_arguments(arguments)
    except ValueError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2

    from core.config import CONFIG

    try:
        result = serve_once(address, key, runtime_pid,
                            audit=jsonl_audit(CONFIG.paths.data / "ActionBroker" / "audit.jsonl"))
    except ChannelError as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}))
        return 1

    return 0 if result["ok"] else 1
