"""Offline update lifecycle for the product: discover, verify, request, approve, launch, confirm, roll back.

This is the controller the desktop reaches through ``core.update_cli``. It adds
the product lifecycle around the verifier and store in ``core.local_update``
and never weakens them.

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
    bound to exactly this version, kind and SHA-256. A refusing policy refuses.
    Otherwise the approval always needs the person's approval secret (the
    TYPED_SECRET level, never less, even if the policy was relaxed), so a
    configured secret is required up front. ``request`` returns a random
    single-use token; only its SHA-256 is stored. ``approve(token, secret)``
    verifies the secret (persisted lockout, never stored or logged), re-verifies
    the files, checks they are still exactly what was requested, consumes the
    token, stages the copy, and returns a second single-use token for
    ``launch``. Both tokens expire. A direct call without them changes nothing.

Launch and quiescence
    ``launch(token)`` first proves the runtime is not running by creating the
    named mutexes the runtime bridge, supervisor and sidecars hold while they
    run (``Local\\QronosAgentRuntimeBridge`` and others). If any already
    exists the launch is refused; while the update holds them none of those can
    start. The caller's claim is not evidence. The launch is then claimed in the
    lifecycle file (so a crash is recovered at the next start), the staged copy
    is re-verified, held open so it cannot be changed or replaced, re-hashed
    through that handle, and only that file is started, with fixed arguments,
    through an injected launcher. Package code, scripts or migrations are never
    run by this module. The store lock is not held during snapshot I/O, the
    process start or any wait for a person.

Data
    Forward migrations belong to the new build's own reviewed store code. An
    upgrade that would change the format of a store this updater cannot keep a
    copy of (anything but a single fixed file in the data root; SQLite stores
    are never copied raw) is refused with ``unsupported_migration``. For the
    others, a bounded, hashed snapshot is kept before the installer starts. A
    signed rollback that cannot read the current data is offered only when such
    a snapshot brings every incompatible store back to a format it reads;
    restoring first saves the current files as another snapshot, and a failed or
    interrupted restore puts them back. Otherwise ``rollback_data``.

Start-up
    ``startup_check`` records whether a handed-off update applied (the running
    version is the target, and its data formats are the signed ones) or not
    (cancelled, failed or crashed installer), recovers an interrupted launch,
    and removes interrupted snapshot folders.

All state lives in ``<data root>/Updates`` under the store's cross-process lock,
in strictly typed, bounded JSON, published atomically. Every failure is an
``UpdateError`` with a code. Tokens and secrets never appear in the state,
events or results other than the one response that hands a token out.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from core import local_update, update_stores
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
CLAIM_STALE_SECONDS = 15 * 60
LIFECYCLE_FILE = "update-lifecycle.json"
SNAPSHOT_DIR = "snapshots"
# Held for the whole launch. Every other backend service can refuse to start
# while it exists (update_in_progress(); the runtime entry point calls it).
UPDATE_IN_PROGRESS_MUTEX = "Local\\Qronos.UpdateInProgress.v1"
RUNTIME_MUTEXES = (
    "Local\\QronosAgentRuntimeBridge",
    "Local\\QronosSupervisor",
    "Local\\Qronos.Chatterbox.Sidecar.v1",
    "Local\\Qronos.Memory.Sidecar.v1",
    "Local\\Qronos.Whisper.Sidecar.v1",
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
                     "launched", "applied", "not_applied", "recovered", "discarded"})
_KINDS = ("upgrade", "repair", "rollback")

Launcher = Callable[[Path, tuple[str, ...]], None]


# --------------------------------------------------------------- authorisation


class Authorizer(Protocol):
    def allowed(self, offer: "Offer") -> str | None:
        """None when the gate permits asking for approval, otherwise why it refuses."""

    def secret_problem(self) -> str | None:
        """None when an approval secret is configured and not locked out, otherwise why not."""

    def verify_secret(self, secret: str) -> bool:
        """True only for the person's approval secret. Raises UpdateError when it cannot be checked."""


class GateAuthorizer:
    """The product's authoriser: security.gate for INSTALL_SOFTWARE, then the approval secret.

    ``audit`` receives the gate verdict (the update CLI passes the action audit log).
    """

    def __init__(self, audit: Callable[[Any], None] | None = None) -> None:
        self._audit = audit

    def allowed(self, offer: "Offer") -> str | None:
        from core.actions import ActionRequest
        from security import gate
        from security.permissions import ActionCategory

        request = ActionRequest(
            category=ActionCategory.INSTALL_SOFTWARE,
            target=f"Qronos {offer.kind} {offer.from_version} -> {offer.version}",
            summary="Install a signed Qronos update from a file you chose.",
            parameters={"version": offer.version, "kind": offer.kind, "packageSha256": offer.package_sha256,
                        "keyId": offer.key_id},
        )
        verdict = gate.evaluate(request, audit=self._audit)
        return verdict.reason if verdict.refused else None

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


def hold_runtime_mutexes(names: tuple[str, ...] = (UPDATE_IN_PROGRESS_MUTEX, *RUNTIME_MUTEXES),
                         ) -> Callable[[], None]:
    """Creates each named mutex; any that already exists means that part of Qronos is running.

    Returns a function releasing them. While they are held, none of those
    processes can start (each refuses when its mutex already exists), and the
    update-in-progress mutex tells every other backend service to refuse. If the
    update's own mutex already exists, another launch is under way.
    """
    if os.name != "nt":
        raise UpdateError("unsupported_platform", "Updates are installed on Windows only.")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    held: list[int] = []

    def release() -> None:
        while held:
            kernel.CloseHandle(held.pop())

    for name in names:
        handle = kernel.CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
        if not handle:
            release()
            raise UpdateError("busy", "Windows could not check whether Qronos is still running.")
        held.append(handle)
        if error == 183:  # ERROR_ALREADY_EXISTS
            release()
            if name == UPDATE_IN_PROGRESS_MUTEX:
                raise UpdateError("busy", "An update is already being started.")
            raise UpdateError("runtime_running", "Qronos is still running; it must stop before the update starts.")
    return release


def update_in_progress(name: str = UPDATE_IN_PROGRESS_MUTEX) -> bool:
    """True while an update launch holds its mutex; backend services then refuse to start."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
    if handle:
        kernel.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5  # exists but cannot be opened: still an update in progress


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
    notes: str
    installer: str
    manifest: str
    changed_stores: tuple[str, ...]
    snapshot_stores: tuple[str, ...]       # kept before an upgrade
    restore_snapshot: str | None           # rollback only: the snapshot restored first

    def binding(self) -> dict[str, Any]:
        return {"version": self.version, "kind": self.kind, "packageSha256": self.package_sha256,
                "keyId": self.key_id, "installer": self.installer, "manifest": self.manifest,
                "restoreSnapshot": self.restore_snapshot}

    def to_json(self) -> dict[str, Any]:
        return {"version": self.version, "fromVersion": self.from_version, "kind": self.kind, "keyId": self.key_id,
                "owner": self.owner, "packageSha256": self.package_sha256, "packageBytes": self.package_bytes,
                "packageName": self.package_name, "notes": self.notes, "installer": self.installer,
                "manifest": self.manifest, "changedStores": list(self.changed_stores),
                "snapshotStores": list(self.snapshot_stores), "restoreSnapshot": self.restore_snapshot}


# ------------------------------------------------------------ lifecycle state

_STATE_FIELDS = frozenset({"schema", "pending", "launchGrant", "launching", "snapshots", "handoff", "outcome",
                           "events"})
_BINDING_FIELDS = frozenset({"version", "kind", "packageSha256", "keyId", "installer", "manifest",
                             "restoreSnapshot"})
_GRANT_FIELDS = frozenset({"tokenHash", "binding", "issuedAt", "expiresAt"})
_LAUNCHING_FIELDS = frozenset({"claimedAt", "version", "packageSha256", "step", "safetySnapshot"})
_SNAPSHOT_FIELDS = frozenset({"id", "reason", "fromVersion", "forVersion", "createdAt", "stores"})
_SNAPSHOT_STORE_FIELDS = frozenset({"version", "file", "bytes", "sha256"})
_HANDOFF_FIELDS = frozenset({"version", "fromVersion", "kind", "packageSha256", "launchedAt", "restoredSnapshot",
                             "safetySnapshot", "dataStores"})
_OUTCOME_FIELDS = frozenset({"result", "version", "runningVersion", "at", "dataMismatch"})
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


def _check_binding(binding: Any) -> None:
    binding = _exact(binding, _BINDING_FIELDS)
    if not _is_version(binding["version"]) or binding["kind"] not in _KINDS \
            or not _is_sha(binding["packageSha256"]) or not _is_str(binding["keyId"], 32) \
            or not _is_str(binding["installer"]) or not _is_str(binding["manifest"]) \
            or not (binding["restoreSnapshot"] is None or _is_snapshot_id(binding["restoreSnapshot"])):
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
        if spec is None or spec.snapshot is None or item["file"] != spec.snapshot \
                or type(item["version"]) is not int or not 1 <= item["version"] <= 1_000_000 \
                or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= _SNAPSHOT_FILE_LIMIT \
                or not _is_sha(item["sha256"]):
            raise _state_error()


def _check_lifecycle(state: Any) -> dict[str, Any]:
    state = _exact(state, _STATE_FIELDS)
    if type(state["schema"]) is not int or state["schema"] != 1:
        raise _state_error()
    _check_grant(state["pending"])
    _check_grant(state["launchGrant"])
    launching = state["launching"]
    if launching is not None:
        launching = _exact(launching, _LAUNCHING_FIELDS)
        if not _is_time(launching["claimedAt"]) or not _is_version(launching["version"]) \
                or not _is_sha(launching["packageSha256"]) or launching["step"] not in _STEPS \
                or not (launching["safetySnapshot"] is None or _is_snapshot_id(launching["safetySnapshot"])):
            raise _state_error()
    if not isinstance(state["snapshots"], list) or len(state["snapshots"]) > _SNAPSHOT_KEEP + 1:
        raise _state_error()
    for entry in state["snapshots"]:
        _check_snapshot(entry)
    handoff = state["handoff"]
    if handoff is not None:
        handoff = _exact(handoff, _HANDOFF_FIELDS)
        if not _is_version(handoff["version"]) or not _is_version(handoff["fromVersion"]) \
                or handoff["kind"] not in _KINDS or not _is_sha(handoff["packageSha256"]) \
                or not _is_time(handoff["launchedAt"]) \
                or not all(v is None or _is_snapshot_id(v) for v in (handoff["restoredSnapshot"],
                                                                     handoff["safetySnapshot"])):
            raise _state_error()
        _check_data_stores(handoff["dataStores"])
    outcome = state["outcome"]
    if outcome is not None:
        outcome = _exact(outcome, _OUTCOME_FIELDS)
        if outcome["result"] not in ("applied", "not_applied") or not _is_version(outcome["version"]) \
                or not _is_version(outcome["runningVersion"]) or not _is_time(outcome["at"]) \
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
    return {"schema": 1, "pending": None, "launchGrant": None, "launching": None, "snapshots": [],
            "handoff": None, "outcome": None, "events": []}


def _token_hash(token: Any) -> str | None:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


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


def windows_launcher(path: Path, arguments: tuple[str, ...]) -> None:
    """Start the verified installer as its own process, detached from this short-lived CLI."""
    import subprocess

    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([str(path), *arguments], cwd=str(path.parent), close_fds=True, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        raise UpdateError("launch_failed", "Windows could not start the update installer.") from error


# ------------------------------------------------------------------- service


class UpdateService:
    """The product update lifecycle for one data root."""

    def __init__(self, data_root: Path, running_version: str, *,
                 keys_loader: Callable[[], dict[str, TrustedKey]] = local_update.load_trusted_keys,
                 profile: DataProfile | None = None, authorizer: Authorizer | None = None,
                 quiesce: Callable[[], Callable[[], None]] = hold_runtime_mutexes,
                 launcher: Launcher = windows_launcher,
                 hold: Callable[[Path], tuple[Callable[[], None], str, int]] = hold_and_hash,
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
        self._launcher = launcher
        self._hold = hold
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

    def _claim_active(self, state: dict[str, Any]) -> bool:
        claim = state["launching"]
        return claim is not None and self._clock() - claim["claimedAt"] < CLAIM_STALE_SECONDS

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

        def grant(entry: dict[str, Any] | None) -> dict[str, Any] | None:  # never the token hash
            if entry is None:
                return None
            return {**entry["binding"], "expiresAt": entry["expiresAt"], "expired": entry["expiresAt"] <= now}

        return {
            "runningVersion": self.running_version,
            "trust": trust,
            "phase": store_state["phase"],
            "securityFloor": store_state["securityFloor"],
            "staged": _public_record(store_state["staged"]),
            "previous": _public_record(store_state["previous"]),
            "history": store_state["history"],
            "pending": grant(lifecycle["pending"]),
            "launchReady": grant(lifecycle["launchGrant"]),
            "launching": lifecycle["launching"] is not None,
            "handoff": lifecycle["handoff"],
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
            offer = self._offer(update, keys, installer, manifest, restore)
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
            if update.kind == "rollback":
                return update, snapshot
        if compare_versions(_payload_version(envelope, keys), self.running_version) < 0:
            raise UpdateError("rollback_data", f"{first} No saved copy of the data from before the upgrade can "
                                               "bring it back to a format that version reads, so this rollback "
                                               "is refused.") from first
        raise first

    def _offer(self, update: VerifiedUpdate, keys: dict[str, TrustedKey], installer: Path, manifest: Path,
               restore: dict[str, Any] | None) -> Offer:
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
            unsupported = [s for s in changed if not update_stores.STORE_BY_ID[s].snapshot]
            if unsupported:
                raise UpdateError("unsupported_migration",
                                  f"This upgrade changes the format of {', '.join(unsupported)}, which the updater "
                                  "cannot keep a copy of, so rolling back later would be impossible. It is refused.")
            snapshot_stores = changed
        return Offer(update.version, self.running_version, update.kind, update.key_id, keys[update.key_id].owner,
                     update.package_sha256, update.package_bytes, update.package_name,
                     update.payload["notes"][:2000], str(installer), str(manifest), changed, snapshot_stores,
                     None if restore is None else restore["id"])

    # -- request and approve

    def request(self, installer: Path, manifest: Path) -> dict[str, Any]:
        """Verifies, asks the permission gate, and returns a single-use approval token for this exact package."""
        offer = self.verify(installer, manifest)
        try:
            refusal = self._authorizer.allowed(offer)
            if refusal is not None:
                raise UpdateError("permission_denied", refusal)
            problem = self._authorizer.secret_problem()
            if problem is not None:
                raise UpdateError("approval_secret_missing", problem)
        except UpdateError as error:
            raise self._refuse(error, offer.version)
        token = secrets.token_urlsafe(32)
        now = self._clock()

        def record(state: dict[str, Any]) -> None:
            if self._claim_active(state):
                raise UpdateError("busy", "An update is being started.")
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
            if self._claim_active(state):
                raise UpdateError("busy", "An update is being started.")
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
            if (update.version, update.package_sha256, update.kind) != \
                    (binding["version"], binding["packageSha256"], binding["kind"]):
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
            staged = _public_record(self.store.state()["staged"])
        return {"staged": staged, "launchToken": launch_token, "expiresAt": now + LAUNCH_SECONDS}

    def _profile_for(self, binding: dict[str, Any], state: dict[str, Any]) -> DataProfile:
        if binding["restoreSnapshot"] is None:
            return self.profile
        return _restored_profile(self.profile, self._snapshot(state, binding["restoreSnapshot"]))

    # -- launch

    def launch(self, token: Any) -> dict[str, Any]:
        """Hands the staged installer to Windows, once, after proving the runtime is stopped."""
        token_hash = _token_hash(token)
        release = self._quiesce()  # raises runtime_running; holds the runtime off until released
        try:
            binding = self._claim_launch(token_hash)
            try:
                return self._launch_claimed(binding)
            except BaseException as error:
                self._release_claim(binding, error)
                raise
        finally:
            release()

    def _claim_launch(self, token_hash: str | None) -> dict[str, Any]:
        with self.store._locked():
            state = self._read()
            grant = state["launchGrant"]
            if token_hash is None or grant is None or not hmac.compare_digest(grant["tokenHash"], token_hash):
                raise UpdateError("not_approved", "This launch is unknown, expired or already used. Nothing was "
                                                  "started.")
            binding = grant["binding"]
            state["launchGrant"] = None  # single use
            if grant["expiresAt"] <= self._clock():
                self._event(state, "refused", binding["version"], "approval_expired: launch")
                self._save(state)
                raise UpdateError("approval_expired", "The approval expired; check the update again.")
            if self._claim_active(state):
                raise UpdateError("busy", "An update is already being started.")
            store_state = self.store.state()
            staged = store_state["staged"]
            if store_state["phase"] != "staged" or not staged \
                    or staged["packageSha256"] != binding["packageSha256"]:
                self._save(state)
                raise UpdateError("not_staged", "The approved update is not staged.")
            state["launching"] = {"claimedAt": self._clock(), "version": binding["version"],
                                  "packageSha256": binding["packageSha256"], "step": "claimed",
                                  "safetySnapshot": None}
            self._save(state)
            return binding

    def _launch_claimed(self, binding: dict[str, Any]) -> dict[str, Any]:
        # Outside the store lock from here on, except for short recorded steps.
        with self.store._locked():
            state = self._read()
            payload = self._staged_payload(self.store.state()["staged"])
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
                safety = self._take_snapshot("pre_restore", binding["version"], sorted(snapshot["stores"]))["id"]
                self._change(lambda s: s["launching"].update(step="restoring", safetySnapshot=safety))
                self._restore_or_revert(snapshot, safety)
                restored = snapshot["id"]
                self._change(lambda s: self._event(s, "restored", binding["version"],
                                                   f"{restored} (current saved as {safety})"))
            handoff = {"version": binding["version"], "fromVersion": self.running_version, "kind": binding["kind"],
                       "packageSha256": binding["packageSha256"], "launchedAt": self._clock(),
                       "restoredSnapshot": restored, "safetySnapshot": safety,
                       "dataStores": {k: v["writes"] for k, v in sorted(payload["dataStores"].items())}}
            try:
                self._launcher(installer, ())
            except UpdateError:
                if safety is not None:  # the installer never ran: put the current data back
                    self._restore(self._snapshot_by_id(safety))
                raise

            def record(state: dict[str, Any]) -> None:
                state["launching"] = None
                state["handoff"] = handoff
                self._event(state, "launched", binding["version"], binding["kind"])

            self._change(record)
            return handoff
        finally:
            closer()

    def _release_claim(self, binding: dict[str, Any], error: BaseException) -> None:
        detail = f"{error.code}: {error}" if isinstance(error, UpdateError) else type(error).__name__
        try:
            def release(state: dict[str, Any]) -> None:
                if state["launching"] is not None and state["launching"]["packageSha256"] == binding["packageSha256"]:
                    state["launching"] = None
                self._event(state, "refused", binding["version"], detail)
            self._change(release)
        except UpdateError:
            pass  # a stale claim is recovered at the next start

    def _staged_payload(self, staged: dict[str, Any] | None) -> dict[str, Any]:
        if not staged:
            raise UpdateError("not_staged", "No update is staged.")
        envelope = local_update._read_chosen_file(self.store._owned_existing(staged["manifestFile"]),
                                                  local_update._MAX_ENVELOPE_BYTES, "staged manifest")
        payload_bytes, _ = local_update._authenticate(envelope, self._keys())
        return local_update._payload(payload_bytes)

    # -- startup and discard

    def startup_check(self) -> dict[str, Any]:
        """At start-up: recover an interrupted launch, and record whether a handed-off update applied."""
        if not os.path.lexists(self.store.root):
            return {"result": "nothing", "phase": "idle"}
        self._remove_partial_snapshots()
        recovered = self._recover_claim()
        with self.store._locked():
            state = self._read()
            store_state = self.store.state()
            if store_state["phase"] != "handed_off":
                result = "nothing" if store_state["phase"] == "idle" else "staged"
                return {"result": result, "phase": store_state["phase"], "recovered": recovered}
            target = store_state["staged"]["version"]
            handoff = state["handoff"]
            result = self.store.confirm(self.running_version)
            applied = result not in ("not applied", "nothing staged")
            mismatch: list[str] = []
            if applied and handoff is not None:
                # The build now running must keep exactly the data formats the package was signed with.
                mismatch = sorted(store_id for store_id, version in handoff["dataStores"].items()
                                  if self.profile.stores.get(store_id, version) != version)
            state["outcome"] = {"result": "applied" if applied else "not_applied", "version": target,
                                "runningVersion": self.running_version, "at": self._clock(),
                                "dataMismatch": mismatch}
            if applied:
                state["handoff"] = None
            self._event(state, "applied" if applied else "not_applied", target,
                        result + (f"; data formats differ from the signed ones: {', '.join(mismatch)}"
                                  if mismatch else ""))
            self._save(state)
            return {"result": state["outcome"]["result"], "kind": result, "version": target,
                    "phase": self.store.state()["phase"], "dataMismatch": mismatch, "recovered": recovered}

    def _recover_claim(self) -> bool:
        """A launch interrupted by a crash: put back any data restored before the installer started."""
        with self.store._locked():
            state = self._read()
            claim = state["launching"]
            if claim is None or self._claim_active(state):
                return False
        if claim["step"] == "restoring" and claim["safetySnapshot"] is not None:
            self._restore(self._snapshot_by_id(claim["safetySnapshot"]))

        def clear(state: dict[str, Any]) -> None:
            state["launching"] = None
            self._event(state, "recovered", claim["version"],
                        "an interrupted launch" + (", data put back" if claim["step"] == "restoring" else ""))
        self._change(clear)
        return True

    def discard(self) -> None:
        def drop(state: dict[str, Any]) -> None:
            if self._claim_active(state):
                raise UpdateError("busy", "An update is being started.")
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
        """Copies the named single-file stores into a new snapshot folder, bounded and hashed (no store lock)."""
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
            source = self.data_root / spec.snapshot
            if _plain_single_file(source) is None:
                continue  # never created: nothing to bring back
            data = _read_bounded(source, _SNAPSHOT_FILE_LIMIT)
            total += len(data)
            if total > _SNAPSHOT_TOTAL_LIMIT:
                raise UpdateError("snapshot_too_large", "The data to keep before this update is too large.")
            self.store._publish(partial, spec.snapshot, lambda stream, data=data: stream.write(data))
            stores[store_id] = {"version": self.profile.stores[store_id], "file": spec.snapshot,
                                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        os.replace(partial, self.snapshot_root / snapshot_id)
        entry = {"id": snapshot_id, "reason": reason, "fromVersion": self.running_version,
                 "forVersion": for_version, "createdAt": self._clock(), "stores": stores}

        def record(state: dict[str, Any]) -> None:
            state["snapshots"].append(entry)
            keep = {state["launching"]["safetySnapshot"]} if state["launching"] else set()
            while len([s for s in state["snapshots"] if s["reason"] == "upgrade"]) > _SNAPSHOT_KEEP \
                    or len(state["snapshots"]) > _SNAPSHOT_KEEP + 1:
                oldest = next(s for s in state["snapshots"] if s["id"] not in keep | {entry["id"]})
                state["snapshots"].remove(oldest)
                self._delete_snapshot(oldest)
        self._change(record)
        return entry

    def _snapshot_contents(self, snapshot: dict[str, Any]) -> dict[str, bytes]:
        folder = self.snapshot_root / snapshot["id"]
        if not _is_plain_dir(folder):
            raise UpdateError("rollback_data", "The saved copy of the data is missing or not a plain folder.")
        contents = {}
        for store_id, item in snapshot["stores"].items():
            data = _read_bounded(folder / item["file"], _SNAPSHOT_FILE_LIMIT)
            if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise UpdateError("rollback_data", f"The saved copy of {store_id} is damaged; nothing was restored.")
            contents[item["file"]] = data
        return contents

    def _restore(self, snapshot: dict[str, Any]) -> None:
        contents = self._snapshot_contents(snapshot)  # every file checked before any is replaced
        for name, data in contents.items():
            _plain_single_file(self.data_root / name)
            self.store._publish(self.data_root, name, lambda stream, data=data: stream.write(data))

    def _restore_or_revert(self, snapshot: dict[str, Any], safety: str) -> None:
        try:
            self._restore(snapshot)
        except UpdateError:
            # Disk full or a damaged file part-way: put back what was there before.
            try:
                self._restore(self._snapshot_by_id(safety))
            except UpdateError:
                pass  # left for recovery at the next start, from the recorded claim
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


# ------------------------------------------------------------ runtime hook


def startup_check(context: Any) -> dict[str, Any]:
    """Runtime start-up hook ("update_status"): {appVersion} -> a small, bounded status dict.

    Call once at start-up, before any store is opened. The version must come from
    the product's own version authority (the desktop binary), never a frontend.
    Never raises; a refusal is reported as {"result": "error", "code": ...}.
    """
    try:
        if not isinstance(context, dict) or set(context) != {"appVersion"}:
            raise UpdateError("bad_request", "The start-up hook needs exactly {appVersion}.")
        from core.config import CONFIG

        result = UpdateService(CONFIG.paths.data, context["appVersion"]).startup_check()
    except UpdateError as error:
        return {"result": "error", "code": error.code}
    except (OSError, ValueError):
        return {"result": "error", "code": "store"}
    allowed = {"result": str, "kind": str, "version": str, "phase": str, "recovered": bool}
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
