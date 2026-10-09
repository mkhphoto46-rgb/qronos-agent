"""Offline update lifecycle for the product: discover, verify, request, approve, launch, recover, roll back.

This is the controller the desktop reaches through ``core.update_cli``. It adds
the product lifecycle around the verifier and store in ``core.local_update``
and never weakens them.

Activation status (read first)
    LAUNCHING IS REFUSED BY DEFAULT (``launch_unavailable``). Starting an
    installer is safe only when nothing else can write Qronos's stores for the
    installer's whole lifetime, and that needs work outside this module that
    does not exist yet (see ``LAUNCH_DEPENDENCIES``): the runtime bridge and
    supervisor must hold exclusive instance mutexes, every backend service must
    refuse while an update is in progress, the desktop must block and drain its
    own writers, and the start-up recovery barrier below must run before any
    store opens. Until those are implemented and independently accepted, the
    default ``quiesce`` refuses. Tests inject owned fixtures; nothing a request
    carries (no flag, no frontend value) can switch it on.

Inputs and verification
    Only local paths the person chose: an installer and its signed
    ``.qronos-update.json`` manifest, or one folder scanned (not recursively)
    for such pairs. No network path. Every package is verified offline before
    anything else: an owner Ed25519 signature from
    ``config/update_trusted_keys.json`` (weak keys refused; an empty or missing
    key file refuses everything), the exact size and SHA-256 of the installer,
    version policy, and data compatibility against the running build's store
    registry (``core.update_stores``). Unsigned or self-signed packages never
    become trusted.

Authorisation (INSTALL_SOFTWARE, TYPED_SECRET)
    ``request`` evaluates the permission gate for an INSTALL_SOFTWARE action
    bound to exactly this version, kind, installer SHA-256, manifest SHA-256 and
    signer. A refusing policy refuses. Otherwise the approval always needs the
    person's approval secret (the TYPED_SECRET level, never less, even if the
    policy was relaxed), so a configured secret is required up front.
    ``request`` returns a random single-use token; only its SHA-256 is stored.
    ``approve(token, secret)`` asks the gate again (a policy changed meanwhile
    is honoured), verifies the secret (persisted lockout, never stored or
    logged), re-verifies the files, checks they are still exactly what was
    requested, consumes the token, stages the copy, records the approval in the
    action audit trail, and returns a second single-use token for ``launch``.
    ``launch`` asks the gate a third time and records completion. Both tokens
    expire. A direct call without them changes nothing.

Launch and quiescence
    ``launch(token)`` first obtains a quiescence hold from ``quiesce``. The
    real hold (``hold_runtime_mutexes``) owns every named mutex on a dedicated
    holder thread, acquired by create-then-wait, so a holder that uses either
    "already exists" or "wait to acquire" semantics sees it as taken; an
    existing or un-acquirable mutex means that part of Qronos is running. The
    hold's handles are inherited by the installer process, so the
    update-in-progress mutex keeps existing for the installer's whole lifetime
    after this short-lived process ends (``update_in_progress()`` tests that).
    The launch is claimed in the lifecycle file, the staged copy and manifest
    are re-verified, the installer is held open so it cannot be changed or
    replaced and re-hashed through that handle, and only that file is started,
    with fixed arguments. Package code, scripts or migrations are never run.
    The store lock is not held during snapshot I/O, the process start or any
    wait for a person.

Data
    Forward migrations belong to the new build's own reviewed store code. An
    upgrade that would change the format of a store this updater cannot keep a
    copy of is refused with ``unsupported_migration``: SQLite stores (never
    copied raw), security registries (paired devices, permissions: an old copy
    would undo revocations) and stores moved out of the data root by an
    environment variable. For the others a bounded, hashed snapshot of the
    exact file (or the recorded fact that it did not exist) is kept before the
    installer starts. A signed rollback that cannot read today's data is
    offered only when such a snapshot brings every incompatible store back to a
    format it reads; restoring first saves the current files as a safety
    snapshot. Snapshots that an unresolved launch or handoff refers to are never
    pruned. Otherwise ``rollback_data``.

Start-up: two separate entry points
    ``recover_before_stores`` is the MUTATING barrier. It must run before any
    store module opens a file, exclusively, and be awaited. It refuses
    (``update_in_progress``) while the update mutex exists, i.e. while a launch
    or the installer is still alive. Otherwise it recovers an interrupted
    launch (puts back data restored before the installer started), resolves a
    handed-off update (``applied`` only on a version change; a same-version
    repair stays ``unconfirmed`` and staged; a rollback that did not apply puts
    the safety snapshot back), and removes interrupted snapshot folders. A
    failure there is a typed refusal: the caller must exit without opening
    stores. ``startup_check`` is the READ-ONLY status for after ``runtime_ready``.

All state lives in ``<data root>/Updates`` under the store's cross-process lock,
in strictly typed, bounded JSON, published atomically. Every failure is an
``UpdateError`` with a code. Tokens and secrets never appear in the state,
events, audit or results other than the one response that hands a token out.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from core import local_update, store_admission, update_stores
from core.local_update import (
    MANIFEST_SUFFIX,
    DataProfile,
    InstalledState,
    LocalUpdateStore,
    TrustedKey,
    UpdateError,
    VerifiedUpdate,
    compare_versions,
    parse_version,
)

APPROVAL_SECONDS = 10 * 60
LAUNCH_SECONDS = 15 * 60
LIFECYCLE_FILE = "update-lifecycle.json"
LIFECYCLE_SCHEMA = 2
SNAPSHOT_DIR = "snapshots"
# Held for the whole launch, then kept existing by the installer's inherited
# handle for the installer's lifetime. Every backend service must refuse to
# start while it exists (update_in_progress()); the start-up barrier refuses too.
UPDATE_IN_PROGRESS_MUTEX = store_admission.UPDATE_IN_PROGRESS_MUTEX
RUNTIME_MUTEXES = (
    "Local\\QronosAgentRuntimeBridge",
    "Local\\QronosSupervisor",
    "Local\\Qronos.Chatterbox.Sidecar.v1",
    "Local\\Qronos.Memory.Sidecar.v1",
    "Local\\Qronos.Whisper.Sidecar.v1",
)
#: What must exist, and be independently accepted, before the default launch path opens.
LAUNCH_DEPENDENCIES = (
    "Runtime: the bridge and supervisor own exclusive instance mutexes for their whole life "
    "(core/runtime_instance_guard.py is not used by the bridge today).",
    "Runtime and Control: EVERY process that opens a Qronos store (bridge, settings/permissions/library/memory/"
    "update services, standalone jobs) takes core.store_admission.admit_writer before its first store opens and "
    "keeps it for its life.",
    "Runtime: every backend service entry (runtime_entry, sidecar guards) refuses while update_in_progress() is true.",
    "Runtime: recover_before_stores() runs and is awaited before any store opens, and its refusal stops start-up.",
    "Control: the desktop blocks and drains its own service writers during a launch and derives the stop result itself.",
    "Independent acceptance of these hooks together with this launch path.",
)
_LIFECYCLE_LIMIT = 256 * 1024
_EVENT_LIMIT = 100
_DISCOVER_ENTRY_LIMIT = 4096
_DISCOVER_RESULT_LIMIT = 16
_SNAPSHOT_FILE_LIMIT = 64 * 1024 * 1024
_SNAPSHOT_TOTAL_LIMIT = 256 * 1024 * 1024
_SNAPSHOT_KEEP = 2
_SNAPSHOT_ID = re.compile(r"[0-9]{10}-[0-9a-f]{12}")
_PARTIAL = re.compile(r"\.partial-[0-9]{10}-[0-9a-f]{12}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_EVENTS = frozenset({"verified", "refused", "requested", "approved", "staged", "snapshot", "restored",
                     "launched", "applied", "not_applied", "unconfirmed", "recovered", "recovery_failed",
                     "discarded"})
_KINDS = ("upgrade", "repair", "rollback")
_OUTCOMES = ("applied", "not_applied", "unconfirmed")

Launcher = Callable[[Path, tuple[str, ...], tuple[int, ...]], Any]


# --------------------------------------------------------------- authorisation


class Authorizer(Protocol):
    def allowed(self, binding: dict[str, Any], stage: str) -> str | None:
        """None when the gate permits this stage ("request", "approve", "launch"), else why it refuses."""

    def secret_problem(self) -> str | None:
        """None when an approval secret is configured and not locked out, otherwise why not."""

    def verify_secret(self, secret: str) -> bool:
        """True only for the person's approval secret. Raises UpdateError when it cannot be checked."""

    def record(self, event: str, binding: dict[str, Any]) -> None:
        """Audit "approved" or "launched" (never a token or secret)."""


def _install_request(binding: dict[str, Any], stage: str):
    from core.actions import ActionRequest
    from security.permissions import ActionCategory

    summaries = {
        "request": "Install a signed Qronos update from a file you chose.",
        "approve": "Approve installing this signed Qronos update.",
        "launch": "Start the approved, verified Qronos update installer.",
    }
    return ActionRequest(
        category=ActionCategory.INSTALL_SOFTWARE,
        target=f"Qronos {binding['kind']} {binding['fromVersion']} -> {binding['version']}",
        summary=summaries[stage],
        parameters={"stage": stage, "version": binding["version"], "kind": binding["kind"],
                    "packageSha256": binding["packageSha256"], "manifestSha256": binding["manifestSha256"],
                    "keyId": binding["keyId"]},
    )


class GateAuthorizer:
    """The product's authoriser: security.gate for INSTALL_SOFTWARE at every stage, then the approval secret.

    ``audit_log`` (an ActionAuditLog) receives every gate verdict plus the
    APPROVED and COMPLETED records; none of them carries a token or secret.
    """

    def __init__(self, audit_log: Any = None) -> None:
        self._audit_log = audit_log

    def allowed(self, binding: dict[str, Any], stage: str) -> str | None:
        from security import gate

        sink = self._audit_log.record_verdict if self._audit_log is not None else None
        verdict = gate.evaluate(_install_request(binding, stage), audit=sink)
        return verdict.reason if verdict.refused else None

    def record(self, event: str, binding: dict[str, Any]) -> None:
        if self._audit_log is None:
            return
        from core.action_audit import AuditEvent
        from core.actions import ActionOutcome
        from security.permissions import PermissionDecision

        if event == "approved":
            self._audit_log.record(AuditEvent.APPROVED, _install_request(binding, "approve"),
                                   decision=PermissionDecision.REQUIRE_TYPED_SECRET)
        elif event == "launched":
            self._audit_log.record(AuditEvent.COMPLETED, _install_request(binding, "launch"),
                                   outcome=ActionOutcome.SUCCEEDED)

    def secret_problem(self) -> str | None:
        from core.approval_secret import ApprovalSecretError, ApprovalSecretStore

        try:
            status = ApprovalSecretStore().status()
        except ApprovalSecretError as error:
            return str(error)
        if not status.configured:
            return "Set an approval secret in Settings first; updates need it."
        if status.locked_until:
            return "Too many wrong approval secrets; try again later."
        return None

    def verify_secret(self, secret: str) -> bool:
        from core.approval_secret import ApprovalSecretError, ApprovalSecretStore, InvalidApprovalSecret

        try:
            return ApprovalSecretStore().verify(secret)
        except InvalidApprovalSecret:
            return False
        except ApprovalSecretError as error:
            raise UpdateError("approval_secret_unavailable", str(error)) from None


# ----------------------------------------------------------------- quiescence


class QuiescenceHold(Protocol):
    def handles(self) -> tuple[int, ...]:
        """Handles the installer process inherits, keeping the exclusion for its lifetime."""

    def release(self) -> None:
        """Give up this process's ownership (idempotent)."""


def launch_unavailable() -> QuiescenceHold:
    """The default: launching is refused until its dependencies exist and are accepted."""
    raise UpdateError("launch_unavailable",
                      "Starting an update installer is not enabled yet. Missing: " + " ".join(LAUNCH_DEPENDENCIES))


class MutexHold:
    """Owns named mutexes on a dedicated holder thread until released.

    Each mutex is created and then acquired with a zero wait. Either "it already
    existed" or "it could not be acquired" means another part of Qronos holds
    it, so the hold is refused and nothing is kept. Ownership belongs to the
    holder thread, so no other thread of this process (a mutex is recursive for
    its owning thread) and no other process can acquire it while held.
    """

    def __init__(self, names: tuple[str, ...], timeout: float = 5.0) -> None:
        if os.name != "nt":
            raise UpdateError("unsupported_platform", "Updates are installed on Windows only.")
        self._names = names
        self._release = threading.Event()
        self._result: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._handles: tuple[int, ...] = ()
        self._thread = threading.Thread(target=self._hold, name="qronos-update-mutexes", daemon=True)
        self._thread.start()
        try:
            kind, value = self._result.get(timeout=timeout)
        except queue.Empty:
            self._release.set()
            raise UpdateError("busy", "Windows did not answer whether Qronos is still running.") from None
        if kind == "error":
            self._thread.join(timeout)
            raise value
        self._handles = value

    def _hold(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        opened: list[int] = []
        owned: list[int] = []

        def let_go() -> None:
            for handle in owned:
                kernel.ReleaseMutex(handle)
            for handle in opened:
                kernel.CloseHandle(handle)

        try:
            for name in self._names:
                handle = kernel.CreateMutexW(None, False, name)
                existed = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
                if not handle:
                    raise UpdateError("busy", "Windows could not check whether Qronos is still running.")
                opened.append(handle)
                if existed or kernel.WaitForSingleObject(handle, 0) != 0:  # WAIT_OBJECT_0 only
                    if name == UPDATE_IN_PROGRESS_MUTEX:
                        raise UpdateError("update_in_progress", "An update is already being started or installed.")
                    raise UpdateError("runtime_running",
                                      "Qronos is still running; it must stop before the update starts.")
                owned.append(handle)
        except UpdateError as error:
            let_go()
            self._result.put(("error", error))
            return
        self._result.put(("held", tuple(opened)))
        self._release.wait()
        let_go()

    def handles(self) -> tuple[int, ...]:
        return self._handles

    def release(self) -> None:
        self._release.set()
        self._thread.join(10)

    __call__ = release  # a hold can be used as its own release function


def hold_runtime_mutexes(names: tuple[str, ...] = (UPDATE_IN_PROGRESS_MUTEX, *RUNTIME_MUTEXES)) -> MutexHold:
    return MutexHold(names)


class LaunchHold:
    """The whole exclusion a launch needs: the EXCLUSIVE store admission, then the owned mutexes.

    The admission is atomic against every cooperating writer (none inside a store,
    none can start); the mutexes keep the runtime, supervisor and sidecars from
    starting and mark the update for update_in_progress(). Not the default
    quiescence: see LAUNCH_DEPENDENCIES.
    """

    def __init__(self, data_root: Path, names: tuple[str, ...] = (UPDATE_IN_PROGRESS_MUTEX, *RUNTIME_MUTEXES),
                 admit: Callable[[Path], Any] = store_admission.admit_exclusive) -> None:
        self._admission = admit(data_root)  # raises writers_active / update_in_progress
        try:
            self._mutexes = MutexHold(names)
        except BaseException:
            self._admission.release()
            raise

    def handles(self) -> tuple[int, ...]:
        return self._mutexes.handles()

    def release(self) -> None:
        try:
            self._mutexes.release()
        finally:
            self._admission.release()


def owned_launch_hold(data_root: Path) -> Callable[[], LaunchHold]:
    """A quiescence provider for when the launch dependencies are accepted (never the default)."""
    return lambda: LaunchHold(data_root)


def update_in_progress(name: str = UPDATE_IN_PROGRESS_MUTEX) -> bool:
    """True while the named update mutex exists (see core.store_admission.update_in_progress)."""
    return store_admission.update_in_progress(name)


# ----------------------------------------------------------------------- offer


@dataclass(frozen=True)
class Offer:
    """A verified package, as the approval card shows it."""

    version: str
    from_version: str
    kind: str
    key_id: str
    owner: str
    package_sha256: str
    package_bytes: int
    package_name: str
    manifest_sha256: str
    notes: str
    installer: str
    manifest: str
    changed_stores: tuple[str, ...]
    snapshot_stores: tuple[str, ...]       # kept before an upgrade
    restore_snapshot: str | None           # rollback only: the snapshot restored first

    def binding(self) -> dict[str, Any]:
        return {"version": self.version, "fromVersion": self.from_version, "kind": self.kind,
                "packageSha256": self.package_sha256, "manifestSha256": self.manifest_sha256,
                "keyId": self.key_id, "installer": self.installer, "manifest": self.manifest,
                "restoreSnapshot": self.restore_snapshot}

    def to_json(self) -> dict[str, Any]:
        return {"version": self.version, "fromVersion": self.from_version, "kind": self.kind, "keyId": self.key_id,
                "owner": self.owner, "packageSha256": self.package_sha256, "packageBytes": self.package_bytes,
                "packageName": self.package_name, "manifestSha256": self.manifest_sha256, "notes": self.notes,
                "installer": self.installer, "manifest": self.manifest, "changedStores": list(self.changed_stores),
                "snapshotStores": list(self.snapshot_stores), "restoreSnapshot": self.restore_snapshot}


# ------------------------------------------------------------ lifecycle state

_STATE_FIELDS = frozenset({"schema", "pending", "launchGrant", "launching", "snapshots", "handoff", "outcome",
                           "events"})
_BINDING_FIELDS = frozenset({"version", "fromVersion", "kind", "packageSha256", "manifestSha256", "keyId",
                             "installer", "manifest", "restoreSnapshot"})
_GRANT_FIELDS = frozenset({"tokenHash", "binding", "issuedAt", "expiresAt"})
_LAUNCHING_FIELDS = frozenset({"claimedAt", "pid", "version", "packageSha256", "step", "restoredSnapshot",
                               "safetySnapshot"})
_SNAPSHOT_FIELDS = frozenset({"id", "reason", "fromVersion", "forVersion", "createdAt", "stores"})
_SNAPSHOT_STORE_FIELDS = frozenset({"version", "file", "present", "bytes", "sha256"})
_HANDOFF_FIELDS = frozenset({"version", "fromVersion", "kind", "packageSha256", "manifestSha256", "launchedAt",
                             "restoredSnapshot", "safetySnapshot", "dataStores"})
_OUTCOME_FIELDS = frozenset({"result", "kind", "version", "runningVersion", "at", "dataMismatch", "dataRestored"})
_EVENT_FIELDS = frozenset({"at", "event", "version", "detail"})
_STEPS = ("claimed", "restoring")


def _state_error() -> UpdateError:
    return UpdateError("store", "The update lifecycle file is not understood; nothing was changed.")


def _is_str(value: Any, limit: int = 4096) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _is_time(value: Any) -> bool:
    return type(value) in (int, float) and 0 <= value < 10_000_000_000


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_snapshot_id(value: Any) -> bool:
    return isinstance(value, str) and _SNAPSHOT_ID.fullmatch(value) is not None


def _is_version(value: Any) -> bool:
    try:
        parse_version(value)
    except UpdateError:
        return False
    return True


def _exact(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise _state_error()
    return value


def _optional_snapshot(value: Any) -> bool:
    return value is None or _is_snapshot_id(value)


def _check_binding(binding: Any) -> None:
    binding = _exact(binding, _BINDING_FIELDS)
    if not _is_version(binding["version"]) or not _is_version(binding["fromVersion"]) \
            or binding["kind"] not in _KINDS or not _is_sha(binding["packageSha256"]) \
            or not _is_sha(binding["manifestSha256"]) or not _is_str(binding["keyId"], 32) \
            or not _is_str(binding["installer"]) or not _is_str(binding["manifest"]) \
            or not _optional_snapshot(binding["restoreSnapshot"]):
        raise _state_error()


def _check_grant(grant: Any) -> None:
    if grant is None:
        return
    grant = _exact(grant, _GRANT_FIELDS)
    _check_binding(grant["binding"])
    if not _is_sha(grant["tokenHash"]) or not _is_time(grant["issuedAt"]) or not _is_time(grant["expiresAt"]):
        raise _state_error()


def _check_data_stores(value: Any) -> None:
    if not isinstance(value, dict) or len(value) > 64:
        raise _state_error()
    for store_id, version in value.items():
        if not _is_str(store_id, 48) or type(version) is not int or not 1 <= version <= 1_000_000:
            raise _state_error()


def _check_snapshot(entry: Any) -> None:
    entry = _exact(entry, _SNAPSHOT_FIELDS)
    if not _is_snapshot_id(entry["id"]) or entry["reason"] not in ("upgrade", "pre_restore") \
            or not _is_version(entry["fromVersion"]) or not _is_version(entry["forVersion"]) \
            or not _is_time(entry["createdAt"]) or not isinstance(entry["stores"], dict) \
            or len(entry["stores"]) > len(update_stores.STORES):
        raise _state_error()
    for store_id, item in entry["stores"].items():
        item = _exact(item, _SNAPSHOT_STORE_FIELDS)
        spec = update_stores.STORE_BY_ID.get(store_id)
        if spec is None or spec.snapshot is None or spec.sensitive or item["file"] != spec.snapshot \
                or type(item["present"]) is not bool \
                or type(item["version"]) is not int or not 1 <= item["version"] <= 1_000_000 \
                or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= _SNAPSHOT_FILE_LIMIT \
                or not (_is_sha(item["sha256"]) if item["present"] else item["sha256"] == ""):
            raise _state_error()


def _check_lifecycle(state: Any) -> dict[str, Any]:
    state = _exact(state, _STATE_FIELDS)
    if type(state["schema"]) is not int or state["schema"] != LIFECYCLE_SCHEMA:
        raise _state_error()
    _check_grant(state["pending"])
    _check_grant(state["launchGrant"])
    launching = state["launching"]
    if launching is not None:
        launching = _exact(launching, _LAUNCHING_FIELDS)
        if not _is_time(launching["claimedAt"]) or type(launching["pid"]) is not int \
                or not _is_version(launching["version"]) or not _is_sha(launching["packageSha256"]) \
                or launching["step"] not in _STEPS or not _optional_snapshot(launching["restoredSnapshot"]) \
                or not _optional_snapshot(launching["safetySnapshot"]):
            raise _state_error()
    if not isinstance(state["snapshots"], list) or len(state["snapshots"]) > _SNAPSHOT_KEEP + 3:
        raise _state_error()
    for entry in state["snapshots"]:
        _check_snapshot(entry)
    handoff = state["handoff"]
    if handoff is not None:
        handoff = _exact(handoff, _HANDOFF_FIELDS)
        if not _is_version(handoff["version"]) or not _is_version(handoff["fromVersion"]) \
                or handoff["kind"] not in _KINDS or not _is_sha(handoff["packageSha256"]) \
                or not _is_sha(handoff["manifestSha256"]) or not _is_time(handoff["launchedAt"]) \
                or not _optional_snapshot(handoff["restoredSnapshot"]) \
                or not _optional_snapshot(handoff["safetySnapshot"]):
            raise _state_error()
        _check_data_stores(handoff["dataStores"])
    outcome = state["outcome"]
    if outcome is not None:
        outcome = _exact(outcome, _OUTCOME_FIELDS)
        if outcome["result"] not in _OUTCOMES or outcome["kind"] not in _KINDS \
                or not _is_version(outcome["version"]) or not _is_version(outcome["runningVersion"]) \
                or not _is_time(outcome["at"]) or type(outcome["dataRestored"]) is not bool \
                or not isinstance(outcome["dataMismatch"], list) or len(outcome["dataMismatch"]) > 64 \
                or not all(_is_str(v, 48) for v in outcome["dataMismatch"]):
            raise _state_error()
    if not isinstance(state["events"], list) or len(state["events"]) > _EVENT_LIMIT:
        raise _state_error()
    for event in state["events"]:
        event = _exact(event, _EVENT_FIELDS)
        if not _is_time(event["at"]) or event["event"] not in _EVENTS \
                or not (event["version"] is None or _is_str(event["version"], 128)) \
                or not _is_str(event["detail"], 512):
            raise _state_error()
    return state


def _empty_lifecycle() -> dict[str, Any]:
    return {"schema": LIFECYCLE_SCHEMA, "pending": None, "launchGrant": None, "launching": None, "snapshots": [],
            "handoff": None, "outcome": None, "events": []}


def _token_hash(token: Any) -> str | None:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _pinned(state: dict[str, Any]) -> set[str]:
    """Snapshots an unresolved launch, handoff, request or grant still refers to."""
    pinned: set[str] = set()
    for holder in (state["launching"], state["handoff"]):
        if holder is not None:
            pinned.update(v for v in (holder["restoredSnapshot"], holder["safetySnapshot"]) if v)
    for grant in (state["pending"], state["launchGrant"]):
        if grant is not None and grant["binding"]["restoreSnapshot"]:
            pinned.add(grant["binding"]["restoreSnapshot"])
    return pinned


# -------------------------------------------------------- safe file helpers


def _plain_single_file(path: Path) -> os.stat_result | None:
    """lstat of a plain, single-link, non-reparse file; None if absent; UpdateError otherwise."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    problem = local_update._unsafe_file(path)
    if problem:
        raise UpdateError("store", f"{path.name} {problem}; it is not touched.")
    return info


def _read_bounded(path: Path, limit: int) -> bytes:
    """The bytes of a plain single-link file, read through one handle with a hard bound."""
    checked = _plain_single_file(path)
    if checked is None:
        raise UpdateError("store", f"{path.name} is missing.")
    try:
        handle = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))
    except OSError as error:
        raise UpdateError("busy", f"{path.name} could not be opened; try again.") from error
    with os.fdopen(handle, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino) \
                or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise UpdateError("busy", f"{path.name} changed while it was opened; try again.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("store", f"{path.name} is too large.")
    return data


def _is_plain_dir(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and not local_update._is_link(path)


def snapshot_file(spec: update_stores.StoreSpec, data_root: Path,
                  environ: Any = None) -> Path | None:
    """The exact file a store uses, when it may be snapshotted; None when it may not.

    Not snapshotted: stores without a single fixed file, security registries, and
    stores an environment variable moves out of the data root.
    """
    if spec.snapshot is None or spec.sensitive:
        return None
    environment = os.environ if environ is None else environ
    if any(environment.get(name, "").strip() for name in spec.relocation_env):
        return None
    return data_root / spec.snapshot


# ------------------------------------------------------------------ launcher


def hold_and_hash(path: Path) -> tuple[Callable[[], None], str, int]:
    """Open ``path`` so nobody can write, rename or delete it, and hash it through that handle.

    Returns (closer, sha256, size). Windows only: CreateFileW with FILE_SHARE_READ,
    without following a link at the final component.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x00200000, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise UpdateError("busy", "The staged installer could not be locked for launch; try again.")
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    stream = os.fdopen(descriptor, "rb")
    try:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UpdateError("store", "The staged installer is not this store's own file.")
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    except BaseException:
        stream.close()
        raise
    return stream.close, digest.hexdigest(), size


def spawn_inheriting(argv: list[str], handles: tuple[int, ...], cwd: str | None = None) -> Any:
    """Start a detached process that inherits exactly ``handles`` (plus null standard handles).

    The handles are made inheritable only for this call and restricted with the
    process-thread attribute handle list, so nothing else of this process leaks.
    """
    import subprocess

    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    startup = None
    if handles:
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": list(handles)}
        for handle in handles:
            os.set_handle_inheritable(handle, True)
    try:
        return subprocess.Popen(argv, cwd=cwd, close_fds=True, creationflags=flags, startupinfo=startup,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        raise UpdateError("launch_failed", "Windows could not start the update installer.") from error
    finally:
        for handle in handles:
            try:
                os.set_handle_inheritable(handle, False)
            except OSError:
                pass


def windows_launcher(path: Path, arguments: tuple[str, ...], handles: tuple[int, ...]) -> Any:
    """Start the verified installer as its own process; it inherits the update's exclusion handles.

    Returns the process, so the launching process can guard the installer's whole lifetime.
    """
    return spawn_inheriting([str(path), *arguments], handles, cwd=str(path.parent))


# ------------------------------------------------------------------- service


class UpdateService:
    """The product update lifecycle for one data root."""

    def __init__(self, data_root: Path, running_version: str, *,
                 keys_loader: Callable[[], dict[str, TrustedKey]] = local_update.load_trusted_keys,
                 profile: DataProfile | None = None, authorizer: Authorizer | None = None,
                 quiesce: Callable[[], QuiescenceHold] = launch_unavailable,
                 liveness: Callable[[], bool] = update_in_progress,
                 launcher: Launcher = windows_launcher,
                 hold: Callable[[Path], tuple[Callable[[], None], str, int]] = hold_and_hash,
                 admit: Callable[[Path], Any] = store_admission.admit_exclusive,
                 environ: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        parse_version(running_version)
        self.data_root = Path(os.path.abspath(data_root))
        self.running_version = running_version
        self.store = LocalUpdateStore(self.data_root / "Updates")
        self.lifecycle_path = self.store.root / LIFECYCLE_FILE
        self.snapshot_root = self.store.root / SNAPSHOT_DIR
        self._keys_loader = keys_loader
        self.profile = profile or update_stores.current_profile()
        self._authorizer = authorizer or GateAuthorizer()
        self._quiesce = quiesce
        self._liveness = liveness
        self._launcher = launcher
        self._hold = hold
        self._admit = admit
        self._environ = os.environ if environ is None else environ
        self._clock = clock

    # -- lifecycle file (always under the store lock)

    def _read(self) -> dict[str, Any]:
        if not os.path.lexists(self.lifecycle_path):
            return _empty_lifecycle()
        data = _read_bounded(self.lifecycle_path, _LIFECYCLE_LIMIT)
        try:
            return _check_lifecycle(json.loads(data.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _state_error() from error

    def _save(self, state: dict[str, Any]) -> None:
        state["events"] = state["events"][-_EVENT_LIMIT:]
        data = (json.dumps(_check_lifecycle(state), indent=1, sort_keys=True) + "\n").encode("utf-8")
        self.store._publish(self.store.root, LIFECYCLE_FILE, lambda stream: stream.write(data))

    def _event(self, state: dict[str, Any], event: str, version: str | None, detail: str = "") -> None:
        state["events"].append({"at": self._clock(), "event": event, "version": version, "detail": detail[:512]})

    def _change(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        """Read, change and save the lifecycle file in one short locked step."""
        with self.store._locked():
            state = self._read()
            result = change(state)
            self._save(state)
            return result

    def _refuse(self, error: UpdateError, version: str | None = None) -> UpdateError:
        if error.code not in ("busy",) and os.path.isdir(self.data_root):
            try:
                self._change(lambda state: self._event(state, "refused", version, f"{error.code}: {error}"))
            except UpdateError:
                pass  # the refusal itself is what the caller needs; recording it is best effort
        return error

    def _unresolved(self, state: dict[str, Any]) -> None:
        """Refuse new work while a launch is alive or an interrupted one is not yet recovered."""
        if self._liveness():
            raise UpdateError("update_in_progress", "An update is being started or installed.")
        if state["launching"] is not None:
            raise UpdateError("recovery_pending", "An interrupted update must be recovered first; restart Qronos.")

    # -- keys

    def _keys(self) -> dict[str, TrustedKey]:
        try:
            return self._keys_loader()
        except UpdateError:
            raise
        except (OSError, ValueError) as error:
            raise UpdateError("key_file", "The trusted update key file cannot be read.") from error

    # -- status

    def status(self) -> dict[str, Any]:
        """Read-only."""
        try:
            keys = self._keys()
            trust = {"configured": bool(keys), "activeKeys": sum(1 for k in keys.values() if not k.revoked),
                     "error": None}
        except UpdateError as error:
            trust = {"configured": False, "activeKeys": 0, "error": error.code}
        if not os.path.lexists(self.store.root):
            store_state, lifecycle = self.store.state(), _empty_lifecycle()
        else:
            with self.store._locked():
                store_state, lifecycle = self.store.state(), self._read()
        now = self._clock()
        live = bool(self._liveness())

        def grant(entry: dict[str, Any] | None) -> dict[str, Any] | None:  # never the token hash
            if entry is None:
                return None
            return {**entry["binding"], "expiresAt": entry["expiresAt"], "expired": entry["expiresAt"] <= now}

        handoff = lifecycle["handoff"]
        return {
            "runningVersion": self.running_version,
            "trust": trust,
            "launchAvailable": self._quiesce is not launch_unavailable,
            "phase": store_state["phase"],
            "securityFloor": store_state["securityFloor"],
            "staged": _public_record(store_state["staged"]),
            "previous": _public_record(store_state["previous"]),
            "history": store_state["history"],
            "pending": grant(lifecycle["pending"]),
            "launchReady": grant(lifecycle["launchGrant"]),
            "updateInProgress": live,
            "launching": lifecycle["launching"] is not None,
            "recoveryPending": (lifecycle["launching"] is not None or handoff is not None) and not live,
            "handoff": handoff,
            "outcome": lifecycle["outcome"],
            "snapshots": [{"id": s["id"], "reason": s["reason"], "fromVersion": s["fromVersion"],
                           "forVersion": s["forVersion"], "stores": sorted(s["stores"])}
                          for s in lifecycle["snapshots"]],
            "events": lifecycle["events"],
            "dataStores": dict(sorted(self.profile.stores.items())),
            "compatibilityEpoch": self.profile.epoch,
        }

    # -- discover

    def discover(self, folder: Path) -> list[dict[str, str]]:
        """Installer + manifest pairs directly in a folder the person chose; nothing is opened or verified."""
        folder = Path(os.path.abspath(folder))
        if not _is_plain_dir(folder):
            raise UpdateError("not_a_folder", "The chosen folder is missing, not a folder or a link.")
        names: set[str] = set()
        with os.scandir(folder) as entries:
            for count, entry in enumerate(entries):
                if count >= _DISCOVER_ENTRY_LIMIT:
                    raise UpdateError("too_many_files", "The chosen folder has too many files; choose the folder "
                                                        "that holds only the update.")
                names.add(entry.name)
        found = []
        for name in sorted(names):
            if not local_update._PACKAGE_NAME.fullmatch(name) or name + MANIFEST_SUFFIX not in names:
                continue
            installer, manifest = folder / name, folder / (name + MANIFEST_SUFFIX)
            if local_update._is_link(installer) or local_update._is_link(manifest) \
                    or not installer.is_file() or not manifest.is_file():
                continue
            found.append({"installer": str(installer), "manifest": str(manifest)})
            if len(found) >= _DISCOVER_RESULT_LIMIT:
                break
        return found

    # -- verify

    def verify(self, installer: Path, manifest: Path) -> Offer:
        """Every check, offline, before anything is copied. Hashing happens outside the store lock."""
        installer, manifest = Path(os.path.abspath(installer)), Path(os.path.abspath(manifest))
        try:
            keys = self._keys()
            if os.path.lexists(self.store.root):
                with self.store._locked():
                    floor = self.store.state()["securityFloor"]
                    snapshots = self._read()["snapshots"]
            else:
                floor, snapshots = None, []
            envelope = local_update._read_chosen_file(manifest, local_update._MAX_ENVELOPE_BYTES,
                                                      "update manifest")
            update, restore = self._verify_with_data(envelope, installer, keys, floor, snapshots)
            offer = self._offer(update, keys, installer, manifest, hashlib.sha256(envelope).hexdigest(), restore)
        except UpdateError as error:
            raise self._refuse(error)
        if os.path.isdir(self.data_root):
            self._change(lambda state: self._event(state, "verified", offer.version,
                                                   f"{offer.kind} {offer.package_sha256[:16]}"))
        return offer

    def _verify_with_data(self, envelope: bytes, installer: Path, keys: dict[str, TrustedKey], floor: str | None,
                          snapshots: list[dict[str, Any]]) -> tuple[VerifiedUpdate, dict[str, Any] | None]:
        installed = InstalledState(self.running_version, self.profile, floor)
        try:
            return local_update.verify_update(envelope, installer, keys, installed), None
        except UpdateError as error:
            if error.code != "data_schema":
                raise
            first = error
        # A rollback that cannot read today's data: is there a snapshot that brings
        # every store back to formats it reads? Newest first.
        for snapshot in reversed(snapshots):
            if snapshot["reason"] != "upgrade":
                continue
            try:
                update = local_update.verify_update(envelope, installer, keys,
                                                    InstalledState(self.running_version,
                                                                   _restored_profile(self.profile, snapshot), floor))
            except UpdateError:
                continue
            if update.kind == "rollback" and self._restorable(snapshot) is None:
                return update, snapshot
        if compare_versions(_payload_version(envelope, keys), self.running_version) < 0:
            raise UpdateError("rollback_data", f"{first} No saved copy of the data from before the upgrade can "
                                               "bring it back to a format that version reads, so this rollback "
                                               "is refused.") from first
        raise first

    def _restorable(self, snapshot: dict[str, Any]) -> str | None:
        """Why a snapshot cannot be restored over today's files, or None."""
        for store_id, item in snapshot["stores"].items():
            path = snapshot_file(update_stores.STORE_BY_ID[store_id], self.data_root, self._environ)
            if path is None:
                return f"{store_id} can no longer be restored safely"
            if not item["present"] and os.path.lexists(path):
                return f"{store_id} did not exist before the upgrade; restoring would have to delete it"
        return None

    def _offer(self, update: VerifiedUpdate, keys: dict[str, TrustedKey], installer: Path, manifest: Path,
               manifest_sha256: str, restore: dict[str, Any] | None) -> Offer:
        data_stores = update.payload["dataStores"]
        changed = tuple(update_stores.changed_stores(self.profile, data_stores))
        if update.kind == "repair":
            # The same version must describe exactly the data this build has.
            if set(data_stores) != set(self.profile.stores) or changed:
                raise UpdateError("data_schema", "This repair package describes different data formats than the "
                                                 "installed version of the same number.")
        if update.kind == "rollback" and set(data_stores) - set(self.profile.stores):
            raise UpdateError("data_schema", "This rollback names data stores the installed Qronos does not have.")
        snapshot_stores: tuple[str, ...] = ()
        if update.kind == "upgrade":
            unsupported = [s for s in changed
                           if snapshot_file(update_stores.STORE_BY_ID[s], self.data_root, self._environ) is None]
            if unsupported:
                raise UpdateError("unsupported_migration",
                                  f"This upgrade changes the format of {', '.join(unsupported)}, which the updater "
                                  "cannot keep a safe copy of (a database, a security registry such as paired "
                                  "devices or permissions, or a store moved out of the data folder), so rolling "
                                  "back later would be impossible or unsafe. It is refused.")
            snapshot_stores = changed
        return Offer(update.version, self.running_version, update.kind, update.key_id, keys[update.key_id].owner,
                     update.package_sha256, update.package_bytes, update.package_name, manifest_sha256,
                     update.payload["notes"][:2000], str(installer), str(manifest), changed, snapshot_stores,
                     None if restore is None else restore["id"])

    def _gate(self, binding: dict[str, Any], stage: str) -> None:
        refusal = self._authorizer.allowed(binding, stage)
        if refusal is not None:
            raise UpdateError("permission_denied", refusal)

    # -- request and approve

    def request(self, installer: Path, manifest: Path) -> dict[str, Any]:
        """Verifies, asks the permission gate, and returns a single-use approval token for this exact package."""
        offer = self.verify(installer, manifest)
        try:
            self._gate(offer.binding(), "request")
            problem = self._authorizer.secret_problem()
            if problem is not None:
                raise UpdateError("approval_secret_missing", problem)
        except UpdateError as error:
            raise self._refuse(error, offer.version)
        token = secrets.token_urlsafe(32)
        now = self._clock()

        def record(state: dict[str, Any]) -> None:
            self._unresolved(state)
            state["pending"] = {"tokenHash": _token_hash(token), "binding": offer.binding(), "issuedAt": now,
                                "expiresAt": now + APPROVAL_SECONDS}
            self._event(state, "requested", offer.version, offer.kind)

        self._change(record)
        return {"offer": offer.to_json(), "approvalToken": token, "expiresAt": now + APPROVAL_SECONDS}

    def approve(self, token: Any, secret: Any) -> dict[str, Any]:
        """The person's approval: their secret for the requested package. Stages it; returns the launch token."""
        token_hash = _token_hash(token)
        with self.store._locked():
            pending = self._read()["pending"]
        if token_hash is None or pending is None or not hmac.compare_digest(pending["tokenHash"], token_hash):
            raise self._refuse(UpdateError("not_approved", "This approval is unknown, expired or already used. "
                                                           "Nothing was changed."))
        binding = pending["binding"]
        if pending["expiresAt"] <= self._clock():
            self._change(lambda state: state.update(pending=None))
            raise self._refuse(UpdateError("approval_expired", "The approval expired. Nothing was changed."),
                               binding["version"])
        try:
            self._gate(binding, "approve")  # a policy changed while the question was open is honoured
        except UpdateError as error:
            self._change(lambda state: state.update(pending=None))
            raise self._refuse(error, binding["version"])
        # A wrong secret leaves the request usable until it expires; every wrong
        # guess counts toward the approval secret's persisted lockout.
        if not isinstance(secret, str) or not secret or not self._authorizer.verify_secret(secret):
            raise self._refuse(UpdateError("approval_secret_rejected", "The approval secret is wrong. Nothing was "
                                                                        "changed."), binding["version"])
        try:
            offer = self.verify(Path(binding["installer"]), Path(binding["manifest"]))
        except UpdateError:
            self._change(lambda state: state.update(pending=None))
            raise
        if offer.binding() != binding:
            self._change(lambda state: state.update(pending=None))
            raise self._refuse(UpdateError("approval_mismatch", "The files changed since the approval was asked "
                                                                "for. Nothing was changed."), binding["version"])
        launch_token = secrets.token_urlsafe(32)
        with self.store._locked():
            state = self._read()
            if state["pending"] is None or state["pending"]["tokenHash"] != token_hash:
                raise UpdateError("not_approved", "This approval was already used. Nothing was changed.")
            self._unresolved(state)
            state["pending"] = None  # single use, consumed before anything is copied
            self._event(state, "approved", binding["version"], binding["kind"])
            self._save(state)
            try:
                update = self.store.stage(Path(binding["manifest"]), Path(binding["installer"]), self._keys(),
                                          self.running_version, self._profile_for(binding, state))
            except UpdateError as error:
                self._event(state, "refused", binding["version"], f"{error.code}: {error}")
                self._save(state)
                raise
            staged_record = self.store.state()["staged"]
            if (update.version, update.package_sha256, update.kind, update.key_id) != \
                    (binding["version"], binding["packageSha256"], binding["kind"], binding["keyId"]) \
                    or self._staged_manifest_sha(staged_record) != binding["manifestSha256"]:
                self.store.discard()
                self._event(state, "refused", update.version, "approval_mismatch: the staged package differs")
                self._save(state)
                raise UpdateError("approval_mismatch", "The files changed while they were copied; the copy was "
                                                       "discarded.")
            now = self._clock()
            state["launchGrant"] = {"tokenHash": _token_hash(launch_token), "binding": binding, "issuedAt": now,
                                    "expiresAt": now + LAUNCH_SECONDS}
            state["outcome"] = None
            self._event(state, "staged", update.version, update.package_sha256[:16])
            self._save(state)
            staged = _public_record(staged_record)
        self._authorizer.record("approved", binding)
        return {"staged": staged, "launchToken": launch_token, "expiresAt": now + LAUNCH_SECONDS}

    def _staged_manifest_sha(self, staged: dict[str, Any] | None) -> str:
        if not staged:
            return ""
        envelope = local_update._read_chosen_file(self.store._owned_existing(staged["manifestFile"]),
                                                  local_update._MAX_ENVELOPE_BYTES, "staged manifest")
        return hashlib.sha256(envelope).hexdigest()

    def _profile_for(self, binding: dict[str, Any], state: dict[str, Any]) -> DataProfile:
        if binding["restoreSnapshot"] is None:
            return self.profile
        return _restored_profile(self.profile, self._snapshot(state, binding["restoreSnapshot"]))

    # -- launch

    def launch(self, token: Any, on_launched: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Hands the staged installer to Windows, once, and guards it until it ends.

        Refused by default (``launch_unavailable``) until the launch dependencies
        exist and are accepted; see the module documentation. Once the installer
        has started, ``on_launched(handoff)`` is called (the CLI answers the
        desktop there), and this process keeps the whole exclusion until the
        installer process exits: it is the installer's lifetime guard.
        """
        token_hash = _token_hash(token)
        hold = self._quiesce()  # raises launch_unavailable / runtime_running / writers_active / update_in_progress
        try:
            binding = self._claim_launch(token_hash)
            try:
                handoff, process = self._launch_claimed(binding, hold)
            except BaseException as error:
                self._release_claim(binding, error)
                raise
            if on_launched is not None:
                try:
                    on_launched(handoff)
                except Exception:
                    pass  # the caller's channel may be gone; the guard still holds until the installer ends
            _wait_for_exit(process)
            return handoff
        finally:
            hold.release()

    def _claim_launch(self, token_hash: str | None) -> dict[str, Any]:
        with self.store._locked():
            state = self._read()
            grant = state["launchGrant"]
            if token_hash is None or grant is None or not hmac.compare_digest(grant["tokenHash"], token_hash):
                raise UpdateError("not_approved", "This launch is unknown, expired or already used. Nothing was "
                                                  "started.")
            binding = grant["binding"]
            if state["launching"] is not None:
                raise UpdateError("recovery_pending", "An interrupted update must be recovered first; restart "
                                                      "Qronos.")
            state["launchGrant"] = None  # single use
            if grant["expiresAt"] <= self._clock():
                self._event(state, "refused", binding["version"], "approval_expired: launch")
                self._save(state)
                raise UpdateError("approval_expired", "The approval expired; check the update again.")
            refusal = self._authorizer.allowed(binding, "launch")
            if refusal is not None:
                self._event(state, "refused", binding["version"], "permission_denied: launch")
                self._save(state)
                raise UpdateError("permission_denied", refusal)
            store_state = self.store.state()
            staged = store_state["staged"]
            if store_state["phase"] != "staged" or not staged \
                    or staged["packageSha256"] != binding["packageSha256"]:
                self._save(state)
                raise UpdateError("not_staged", "The approved update is not staged.")
            state["launching"] = {"claimedAt": self._clock(), "pid": os.getpid(), "version": binding["version"],
                                  "packageSha256": binding["packageSha256"], "step": "claimed",
                                  "restoredSnapshot": None, "safetySnapshot": None}
            self._save(state)
            return binding

    def _launch_claimed(self, binding: dict[str, Any], hold: QuiescenceHold) -> dict[str, Any]:
        # Outside the store lock from here on, except for short recorded steps.
        with self.store._locked():
            staged = self.store.state()["staged"]
            if self._staged_manifest_sha(staged) != binding["manifestSha256"]:
                raise UpdateError("package_mismatch", "The staged manifest is not the approved one.")
            payload = self._staged_payload(staged)
        if binding["kind"] == "upgrade":
            stores = update_stores.changed_stores(self.profile, payload["dataStores"])
            if stores:
                snapshot = self._take_snapshot("upgrade", binding["version"], stores)
                self._change(lambda s: self._event(s, "snapshot", binding["version"], snapshot["id"]))
        with self.store._locked():
            state = self._read()
            installer = self.store.handoff(self._keys(), self.running_version, self._profile_for(binding, state))
        closer, digest, size = self._hold(installer)
        try:
            if digest != binding["packageSha256"] or size != payload["package"]["bytes"]:
                raise UpdateError("package_mismatch", "The staged installer changed; it was not started.")
            safety, restored = None, None
            if binding["restoreSnapshot"] is not None:
                with self.store._locked():
                    snapshot = self._snapshot(self._read(), binding["restoreSnapshot"])
                problem = self._restorable(snapshot)
                if problem is not None:
                    raise UpdateError("rollback_data", f"The saved data cannot be put back safely: {problem}.")
                safety = self._take_snapshot("pre_restore", binding["version"], sorted(snapshot["stores"]))["id"]
                self._change(lambda s: s["launching"].update(step="restoring", safetySnapshot=safety,
                                                             restoredSnapshot=snapshot["id"]))
                self._restore_or_revert(snapshot, safety)
                restored = snapshot["id"]
                self._change(lambda s: self._event(s, "restored", binding["version"],
                                                   f"{restored} (current saved as {safety})"))
            handoff = {"version": binding["version"], "fromVersion": self.running_version, "kind": binding["kind"],
                       "packageSha256": binding["packageSha256"], "manifestSha256": binding["manifestSha256"],
                       "launchedAt": self._clock(), "restoredSnapshot": restored, "safetySnapshot": safety,
                       "dataStores": {k: v["writes"] for k, v in sorted(payload["dataStores"].items())}}
            try:
                process = self._launcher(installer, (), hold.handles())
            except UpdateError:
                if safety is not None:  # the installer never ran: put the current data back
                    self._restore(self._snapshot_by_id(safety))
                    self._change(lambda s: s["launching"].update(step="claimed"))  # resolved: the claim may go
                raise

            def record(state: dict[str, Any]) -> None:
                state["launching"] = None
                state["handoff"] = handoff
                self._event(state, "launched", binding["version"], binding["kind"])

            self._change(record)
            self._authorizer.record("launched", binding)
            return handoff, process
        finally:
            closer()

    def _release_claim(self, binding: dict[str, Any], error: BaseException) -> None:
        detail = f"{error.code}: {error}" if isinstance(error, UpdateError) else type(error).__name__
        try:
            def release(state: dict[str, Any]) -> None:
                claim = state["launching"]
                # A claim whose data was restored stays: only the start-up barrier may put it back.
                if claim is not None and claim["packageSha256"] == binding["packageSha256"] \
                        and claim["step"] != "restoring":
                    state["launching"] = None
                self._event(state, "refused", binding["version"], detail)
            self._change(release)
        except UpdateError:
            pass  # the claim stays and is recovered by the start-up barrier

    def _staged_payload(self, staged: dict[str, Any] | None) -> dict[str, Any]:
        if not staged:
            raise UpdateError("not_staged", "No update is staged.")
        envelope = local_update._read_chosen_file(self.store._owned_existing(staged["manifestFile"]),
                                                  local_update._MAX_ENVELOPE_BYTES, "staged manifest")
        payload_bytes, _ = local_update._authenticate(envelope, self._keys())
        return local_update._payload(payload_bytes)

    # -- start-up barrier (mutating) and discard

    def recover_before_stores(self) -> dict[str, Any]:
        """The exclusive start-up barrier. Call before any store opens and wait for it.

        Raises ``update_in_progress`` while a launch or installer is alive, and
        ``recovery_failed`` when data could not be put back; in both cases the
        caller must stop without opening stores. Otherwise returns what happened.
        """
        if not os.path.lexists(self.store.root):
            return {"result": "nothing", "phase": "idle", "recovered": False}
        if self._liveness():
            raise UpdateError("update_in_progress", "An update is being installed; Qronos starts when it ends.")
        with self.store._locked():
            self._remove_partial_snapshots()
            state = self._read()
            recovered = False
            claim = state["launching"]
            if claim is not None:  # its process is gone (the update mutex no longer exists)
                if claim["step"] == "restoring" and claim["safetySnapshot"]:
                    self._restore_exclusively(state, claim["safetySnapshot"], claim["version"])
                state["launching"] = None
                self._event(state, "recovered", claim["version"],
                            "an interrupted launch" + (", data put back" if claim["step"] == "restoring" else ""))
                recovered = True
            store_state = self.store.state()
            if store_state["phase"] != "handed_off":
                self._save(state)
                return {"result": "nothing" if store_state["phase"] == "idle" else "staged",
                        "phase": store_state["phase"], "recovered": recovered}
            staged = store_state["staged"]
            target, kind = staged["version"], staged["kind"]
            handoff = state["handoff"]
            result = self.store.confirm(self.running_version)
            mismatch: list[str] = []
            restored = False
            if result in ("upgrade", "rollback"):
                outcome = "applied"
                if handoff is not None:
                    mismatch = sorted(store_id for store_id, version in handoff["dataStores"].items()
                                      if self.profile.stores.get(store_id, version) != version)
                state["handoff"] = None
            elif result == "unconfirmed":
                outcome = "unconfirmed"  # a repair: kept staged and the handoff kept until there is evidence
            else:
                outcome = "not_applied"
                if handoff is not None and handoff["restoredSnapshot"] and handoff["safetySnapshot"]:
                    # A rollback that did not apply: the build that runs is the newer one,
                    # so its own data (saved before the restore) is put back first.
                    self._restore_exclusively(state, handoff["safetySnapshot"], target)
                    restored = True
                state["handoff"] = None
            state["outcome"] = {"result": outcome, "kind": kind, "version": target,
                                "runningVersion": self.running_version, "at": self._clock(),
                                "dataMismatch": mismatch, "dataRestored": restored}
            self._event(state, outcome, target,
                        result + (f"; data formats differ from the signed ones: {', '.join(mismatch)}"
                                  if mismatch else "") + ("; current data put back" if restored else ""))
            self._save(state)
            return {"result": outcome, "kind": kind, "version": target, "phase": self.store.state()["phase"],
                    "dataMismatch": mismatch, "dataRestored": restored, "recovered": recovered}

    #: The same barrier under its earlier name, kept so existing callers and
    #: review fixtures exercise the repaired behaviour. The module-level
    #: ``startup_check(context)`` is the separate READ-ONLY status.
    def startup_check(self) -> dict[str, Any]:
        return self.recover_before_stores()

    def _restore_exclusively(self, state: dict[str, Any], snapshot_id: str, version: str) -> None:
        """Inside the barrier, under the store lock and the EXCLUSIVE store admission: put a safety
        snapshot back, or refuse start-up. A writer still inside a store refuses it (writers_active)."""
        try:
            admission = self._admit(self.data_root)
        except UpdateError as error:
            raise UpdateError("recovery_pending", "Saved data must be put back while nothing else has Qronos's "
                                                  f"data open ({error.code}); close Qronos and start it again.") \
                from error
        try:
            self._restore(self._snapshot(state, snapshot_id))
        except UpdateError as error:
            self._event(state, "recovery_failed", version, f"{error.code}: {error}")
            try:
                self._save(state)
            except UpdateError:
                pass
            raise UpdateError("recovery_failed", "Saved data could not be put back; Qronos must not open its "
                                                 f"stores until this is resolved ({error.code}).") from error
        finally:
            admission.release()
        self._event(state, "restored", version, f"{snapshot_id} put back at start-up")

    def discard(self) -> None:
        def drop(state: dict[str, Any]) -> None:
            self._unresolved(state)
            if state["handoff"] is not None and state["handoff"]["safetySnapshot"]:
                raise UpdateError("recovery_pending", "Start Qronos again so the update's data can be resolved "
                                                      "before it is discarded.")
            self.store.discard()
            state["pending"] = None
            state["launchGrant"] = None
            state["handoff"] = None
            self._event(state, "discarded", None)
        self._change(drop)

    # -- snapshots

    def _snapshot(self, state: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
        for entry in state["snapshots"]:
            if entry["id"] == snapshot_id:
                return entry
        raise UpdateError("rollback_data", "The saved copy of the data this rollback needs is gone.")

    def _snapshot_by_id(self, snapshot_id: str) -> dict[str, Any]:
        with self.store._locked():
            return self._snapshot(self._read(), snapshot_id)

    def _take_snapshot(self, reason: str, for_version: str, store_ids: list[str]) -> dict[str, Any]:
        """Copies the named single-file stores into a new snapshot folder, bounded and hashed (no store lock).

        A store whose file does not exist is recorded as absent, never silently skipped.
        """
        snapshot_id = f"{int(self._clock()):010d}-{secrets.token_hex(6)}"
        self.store._check_folders()
        if os.path.lexists(self.snapshot_root) and not _is_plain_dir(self.snapshot_root):
            raise UpdateError("store", "The snapshot folder is not a plain folder.")
        self.snapshot_root.mkdir(exist_ok=True)
        partial = self.snapshot_root / f".partial-{snapshot_id}"
        partial.mkdir()
        stores: dict[str, Any] = {}
        total = 0
        for store_id in store_ids:
            spec = update_stores.STORE_BY_ID[store_id]
            source = snapshot_file(spec, self.data_root, self._environ)
            if source is None:
                raise UpdateError("unsupported_migration", f"{store_id} cannot be kept as a safe copy.")
            item = {"version": self.profile.stores[store_id], "file": spec.snapshot, "present": False,
                    "bytes": 0, "sha256": ""}
            if _plain_single_file(source) is not None:
                data = _read_bounded(source, _SNAPSHOT_FILE_LIMIT)
                total += len(data)
                if total > _SNAPSHOT_TOTAL_LIMIT:
                    raise UpdateError("snapshot_too_large", "The data to keep before this update is too large.")
                self.store._publish(partial, spec.snapshot, lambda stream, data=data: stream.write(data))
                item.update(present=True, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            stores[store_id] = item
        os.replace(partial, self.snapshot_root / snapshot_id)
        entry = {"id": snapshot_id, "reason": reason, "fromVersion": self.running_version,
                 "forVersion": for_version, "createdAt": self._clock(), "stores": stores}

        def record(state: dict[str, Any]) -> None:
            state["snapshots"].append(entry)
            keep = _pinned(state) | {entry["id"]}
            while len([s for s in state["snapshots"] if s["reason"] == "upgrade"]) > _SNAPSHOT_KEEP \
                    or len(state["snapshots"]) > _SNAPSHOT_KEEP + 1:
                oldest = next((s for s in state["snapshots"] if s["id"] not in keep), None)
                if oldest is None:
                    break  # everything left is pinned by an unresolved launch or handoff
                state["snapshots"].remove(oldest)
                self._delete_snapshot(oldest)
            if len(state["snapshots"]) > _SNAPSHOT_KEEP + 3:
                raise UpdateError("busy", "Too many unresolved data snapshots; resolve the last update first.")
        self._change(record)
        return entry

    def _snapshot_contents(self, snapshot: dict[str, Any]) -> dict[str, bytes | None]:
        folder = self.snapshot_root / snapshot["id"]
        if not _is_plain_dir(folder):
            raise UpdateError("rollback_data", "The saved copy of the data is missing or not a plain folder.")
        contents: dict[str, bytes | None] = {}
        for store_id, item in snapshot["stores"].items():
            if not item["present"]:
                contents[item["file"]] = None
                continue
            data = _read_bounded(folder / item["file"], _SNAPSHOT_FILE_LIMIT)
            if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise UpdateError("rollback_data", f"The saved copy of {store_id} is damaged; nothing was restored.")
            contents[item["file"]] = data
        return contents

    def _restore(self, snapshot: dict[str, Any]) -> None:
        contents = self._snapshot_contents(snapshot)  # every file checked before any is replaced
        for store_id in snapshot["stores"]:
            if snapshot_file(update_stores.STORE_BY_ID[store_id], self.data_root, self._environ) is None:
                raise UpdateError("rollback_data", f"{store_id} can no longer be restored safely.")
        for name, data in contents.items():
            target = self.data_root / name
            _plain_single_file(target)
            if data is None:
                # Absent when saved: remove what exists now (its bytes are in the safety snapshot).
                if _plain_single_file(target) is not None:
                    os.unlink(target)
                continue
            self.store._publish(self.data_root, name, lambda stream, data=data: stream.write(data))

    def _restore_or_revert(self, snapshot: dict[str, Any], safety: str) -> None:
        try:
            self._restore(snapshot)
        except UpdateError:
            # Disk full or a damaged file part-way: put back what was there before.
            try:
                self._restore(self._snapshot_by_id(safety))
            except UpdateError:
                pass  # the claim stays; the start-up barrier puts it back or refuses
            raise

    def _delete_snapshot(self, entry: dict[str, Any]) -> None:
        folder = self.snapshot_root / entry["id"]
        if not _is_plain_dir(folder):
            return
        for item in entry["stores"].values():
            path = folder / item["file"]
            if _plain_single_file(path) is not None:
                os.unlink(path)
        try:
            os.rmdir(folder)
        except OSError:
            pass  # something else is in it: left for the person, never removed recursively

    def _remove_partial_snapshots(self) -> None:
        if not _is_plain_dir(self.snapshot_root):
            return
        with os.scandir(self.snapshot_root) as entries:
            candidates = [Path(e.path) for _, e in zip(range(_DISCOVER_ENTRY_LIMIT), entries)
                          if _PARTIAL.fullmatch(e.name)]
        for folder in candidates:
            if not _is_plain_dir(folder):
                continue
            for spec in update_stores.STORES:
                if spec.snapshot and _plain_single_file(folder / spec.snapshot) is not None:
                    os.unlink(folder / spec.snapshot)
            with os.scandir(folder) as children:
                leftovers = [Path(c.path) for c in children
                             if c.name.startswith(".qronos-") and c.name.endswith(".tmp")]
            for child in leftovers:
                if _plain_single_file(child) is not None:
                    os.unlink(child)
            try:
                os.rmdir(folder)
            except OSError:
                pass


def _wait_for_exit(process: Any) -> None:
    """The lifetime guard: wait for the installer process to end (no time limit: it is the installer's life)."""
    wait = getattr(process, "wait", None)
    if wait is None:
        return
    while True:
        try:
            wait(timeout=5)
            return
        except Exception as error:  # subprocess.TimeoutExpired: still running
            if type(error).__name__ != "TimeoutExpired":
                return


# ------------------------------------------------------------ runtime entry points


def recover_before_stores(context: Any) -> dict[str, Any]:
    """Start-up barrier for the runtime ({appVersion}): MUTATING, exclusive, awaited, before any store opens.

    Returns {"ok": True, ...} to continue, or {"ok": False, "code": ...}: the
    caller must then exit without opening stores. Never raises.
    """
    try:
        if not isinstance(context, dict) or set(context) != {"appVersion"}:
            raise UpdateError("bad_request", "The start-up barrier needs exactly {appVersion}.")
        from core.config import CONFIG

        result = UpdateService(CONFIG.paths.data, context["appVersion"]).recover_before_stores()
    except UpdateError as error:
        return {"ok": False, "code": error.code}
    except (OSError, ValueError):
        return {"ok": False, "code": "store"}
    return {"ok": True, **_bounded_status(result)}


def startup_check(context: Any) -> dict[str, Any]:
    """READ-ONLY status after runtime_ready ({appVersion}) -> a small, bounded dict. Never mutates or raises."""
    try:
        if not isinstance(context, dict) or set(context) != {"appVersion"}:
            raise UpdateError("bad_request", "The status check needs exactly {appVersion}.")
        from core.config import CONFIG

        status = UpdateService(CONFIG.paths.data, context["appVersion"]).status()
    except UpdateError as error:
        return {"result": "error", "code": error.code}
    except (OSError, ValueError):
        return {"result": "error", "code": "store"}
    outcome = status["outcome"] or {}
    return _bounded_status({"result": outcome.get("result", "nothing"), "kind": outcome.get("kind"),
                            "version": outcome.get("version"), "phase": status["phase"],
                            "updateInProgress": status["updateInProgress"],
                            "recoveryPending": status["recoveryPending"],
                            "dataMismatch": outcome.get("dataMismatch", [])})


def _bounded_status(result: dict[str, Any]) -> dict[str, Any]:
    allowed = {"result": str, "kind": str, "version": str, "phase": str, "recovered": bool,
               "dataRestored": bool, "updateInProgress": bool, "recoveryPending": bool}
    status: dict[str, Any] = {key: result[key] for key, kind in allowed.items()
                              if isinstance(result.get(key), kind)}
    if isinstance(result.get("dataMismatch"), list):
        status["dataMismatch"] = [str(v)[:48] for v in result["dataMismatch"][:64]]
    return status


def _restored_profile(profile: DataProfile, snapshot: dict[str, Any]) -> DataProfile:
    stores = dict(profile.stores)
    stores.update({store_id: item["version"] for store_id, item in snapshot["stores"].items()})
    return DataProfile(stores, profile.epoch)


def _payload_version(envelope: bytes, keys: dict[str, TrustedKey]) -> str:
    return local_update._payload(local_update._authenticate(envelope, keys)[0])["version"]


def _public_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {key: record[key] for key in ("kind", "version", "fromVersion", "packageSha256", "packageBytes",
                                         "packageName", "keyId", "securityFloor")}
