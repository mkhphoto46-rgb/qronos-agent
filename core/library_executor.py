"""
The one place a Library file is changed.

The Library view is a direct, user-driven control plane: a person selects an
artifact and asks for it to be renamed, copied, moved or recycled. That makes
the request explicit, but it does not make the webview trustworthy. Whatever
the frontend sends — an id, a destination, a "yes" — arrives here as data, and
this module decides what that data is allowed to do.

Five rules hold for every operation:

    The permission gate decides first. Each target becomes a typed
    ActionRequest and goes through ``security.gate.evaluate``, so a user policy
    of DENY in the Permissions view is honoured here as everywhere else, and
    the hard floor is never negotiable. Permanent deletion is
    IRREVERSIBLE_DESTRUCTION: refused by the gate and, independently, never
    implemented below — there is no code path that unlinks a user's artifact.

    Approval is bound, not boolean. ``prepare`` validates the exact operation,
    targets, file identities and destination, stores them under a single-use,
    short-lived token and tells the UI which confirmation the verdict needs.
    ``commit`` accepts only that token: the frontend cannot widen the scope by
    sending different ids or a different destination at commit time.

    Only provable artifacts are touched. A target must be a registered artifact
    whose file is still the file Qronos registered (same file index, or same
    content), is not a symlink or reparse point, and does not resolve somewhere
    else through a junction. A stale UI id or a substituted path is refused.

    Nothing is overwritten and nothing is lost. Renames and moves never
    replace an existing file, copies pick a free name atomically, a
    cross-volume move removes the source only after the copy is verified, and
    deletion goes to the Recycle Bin or does not happen. If the metadata update
    after a successful file operation fails, the file operation is rolled back.

    Every decision is audited, including refusals and failures. Audit records
    carry artifact ids and categories, never file names or paths.

Who may ask. Only the Library UI reaches this module (``library_cli`` behind
the Tauri ``library_command``); every request is tagged ``initiator=library_ui``.
Model-proposed file actions do not come here: they go through
``core.file_executor`` and the runtime approval flow, where a spoken or typed
model turn can satisfy at most what that flow allows. The UI path gets no
standing authority from being "trusted": the same gate verdict applies, the
UI only supplies the confirmation the verdict asks for, bound to one token.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import errno
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from typing import Any, Callable, Iterator, Mapping
import uuid

from core.action_audit import (
    DEFAULT_ACTION_AUDIT_PATH,
    ActionAuditLog,
    AuditEvent,
)
from core.actions import ActionOutcome, ActionRequest
from core.config import CONFIG
from core.permission_control import permission_store_path
from core.approval_secret import (
    ApprovalSecretError,
    ApprovalSecretLocked,
    ApprovalSecretStore,
    InvalidApprovalSecret,
    SETUP_HINT,
    approval_secret_path,
)
from core.artifact_registry import (
    Artifact,
    ArtifactRegistry,
    identity_matches,
    is_executable_name,
    is_link_or_reparse_point,
    same_path,
)
from core.link_policy import UnsafeLink, validate_web_url
from core.path_safety import drive_status
from core.storage_locations import load_storage_locations, program_roots
from security.gate import ActionGrantUnavailable, Verdict, evaluate, issue_grant
from security.permissions import ActionCategory, PermissionDecision


ROOT = Path(__file__).resolve().parent.parent

APPROVAL_TTL_SECONDS = 120.0
MAX_BATCH = 500
MAX_COPY_NAME_ATTEMPTS = 1000

INITIATOR = "library_ui"

_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_INVALID_NAME_CHARS = frozenset('<>:"/\\|?*')
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")


class LibraryOperation(Enum):
    RENAME = "rename"
    COPY = "copy"
    MOVE = "move"
    DELETE = "delete"
    DELETE_FOREVER = "delete_forever"
    OPEN_LINK = "open_link"


_CATEGORY: dict[LibraryOperation, ActionCategory] = {
    LibraryOperation.RENAME: ActionCategory.CREATE_OR_EDIT_FILE,
    LibraryOperation.COPY: ActionCategory.CREATE_OR_EDIT_FILE,
    LibraryOperation.MOVE: ActionCategory.CREATE_OR_EDIT_FILE,
    LibraryOperation.DELETE: ActionCategory.DELETE_RECOVERABLE,
    LibraryOperation.DELETE_FOREVER: ActionCategory.IRREVERSIBLE_DESTRUCTION,
    LibraryOperation.OPEN_LINK: ActionCategory.BROWSER_NAVIGATION,
}

_SUMMARY: dict[LibraryOperation, str] = {
    LibraryOperation.RENAME: "Rename one Library artifact in place.",
    LibraryOperation.COPY: "Copy one Library artifact to a user-selected folder.",
    LibraryOperation.MOVE: "Move one Library artifact to a user-selected folder.",
    LibraryOperation.DELETE: "Move one Library artifact to the Recycle Bin.",
    LibraryOperation.DELETE_FOREVER: "Permanently delete one Library artifact.",
    LibraryOperation.OPEN_LINK: "Open one saved web link in a visible browser.",
}

# How the Library UI satisfies each decision. The UI dialog shows the exact
# operation, targets and destination, which is at least what a spoken "yes"
# gives, so it also satisfies voice confirmation. TYPED_SECRET is satisfied only
# by the user's approval secret, verified locally by core.approval_secret at
# commit. A phrase shown in the dialog is not a secret and is not accepted.
_CONFIRMATION: dict[PermissionDecision, str] = {
    PermissionDecision.ALLOW: "none",
    PermissionDecision.REQUIRE_VOICE_CONFIRMATION: "click",
    PermissionDecision.REQUIRE_UI_CONFIRMATION: "click",
    PermissionDecision.REQUIRE_TYPED_SECRET: "secret",
}

BROWSER_UNAVAILABLE = (
    "Opening web links is not available in this build. Qronos opens websites "
    "only through its reviewed, permission-gated browser workflow, which is "
    "not installed here. Use COPY LINK to open it in your own browser."
)

PERMANENT_DELETE_REFUSAL = (
    "Permanent deletion is irreversible destruction, which Qronos hard-denies. "
    "Use DELETE to move the artifact to the Recycle Bin instead."
)


class LibraryActionRefused(Exception):
    """A Library operation was not allowed to start, or to continue."""

    def __init__(
        self,
        message: str,
        *,
        results: tuple[dict[str, object], ...] = (),
    ) -> None:
        super().__init__(message)
        self.results = results


class _ItemRefused(Exception):
    """One target failed validation; nothing was changed for it."""


Recycler = Callable[[Path], None]
# Hands one validated public http(s) URL to a visible browser. Supplied by the
# reviewed browser workflow; there is no default, so without one link opening
# fails closed.
BrowserLauncher = Callable[[str], None]
EvaluateFn = Callable[..., Verdict]
Clock = Callable[[], float]


@dataclass(frozen=True)
class _Target:
    artifact: Artifact
    request: ActionRequest
    path: Path | None


def library_audit_path() -> Path:
    override = os.environ.get("QRONOS_PERMISSION_AUDIT_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(DEFAULT_ACTION_AUDIT_PATH)


def default_protected_roots(
    registry: ArtifactRegistry | None = None,
) -> tuple[Path, ...]:
    """
    Folders a Library operation may never write into or act on.

    System and program folders, every AppData tree (Startup lives there, and
    writing into it is unauthorized persistence), Qronos's own code and
    internal state, and the backup root. The artifact root itself is allowed.
    """
    raw: list[str] = [
        os.environ.get(name, "")
        for name in (
            "SystemRoot",
            "windir",
            "ProgramFiles",
            "ProgramFiles(x86)",
            "ProgramW6432",
            "ProgramData",
            "APPDATA",
            "LOCALAPPDATA",
        )
    ]
    # The source tree and, when installed, the whole application folder.
    raw.append(str(ROOT))
    raw.extend(str(root) for root in program_roots())

    try:
        locations = load_storage_locations(create=False)
        raw.extend(
            [
                locations.data_root,
                locations.history_root,
                locations.memory_root,
                locations.library_db_root,
                locations.cache_root,
                locations.backup_root,
            ]
        )
    except (OSError, ValueError):
        pass

    if registry is not None:
        raw.append(str(Path(registry.path).parent))

    # Qronos's own data and log folders (undo journal, ownership registry,
    # action audit) wherever the installed configuration put them.
    raw.extend([str(CONFIG.paths.data), str(CONFIG.paths.logs)])

    roots: list[Path] = []
    for value in raw:
        cleaned = str(value or "").strip()
        if cleaned:
            roots.append(Path(cleaned).expanduser().resolve(strict=False))
    return tuple(roots)


_SQLITE_SIDECARS = ("", "-journal", "-wal", "-shm", ".lock")


def qronos_state_files(registry: ArtifactRegistry | None = None) -> tuple[Path, ...]:
    """
    Individual Qronos state files the Library must never act on.

    These can be relocated by environment overrides to arbitrary folders, so
    they are matched as files (with their SQLite/lock sidecars) in addition to
    the protected folders above. The approval secret store is the important
    one: copying it out would allow offline guessing, and moving it away
    would make TYPED_SECRET re-provisionable.
    """
    files: list[Path] = []
    candidates: list[Path] = [approval_secret_path(), permission_store_path(), library_audit_path()]
    for name in ("QRONOS_CONVERSATION_HISTORY_PATH", "QRONOS_SETTINGS_PATH"):
        value = os.environ.get(name, "").strip()
        if value:
            candidates.append(Path(value))
    try:
        from core.storage_locations import locator_path

        candidates.append(locator_path())
    except ValueError:
        pass
    if registry is not None:
        candidates.append(Path(registry.path))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve(strict=False)
        files.extend(resolved.with_name(resolved.name + suffix) for suffix in _SQLITE_SIDECARS)
    return tuple(files)


def send_to_recycle_bin(path: Path) -> None:
    """
    Move one file to the Windows Recycle Bin, or fail without deleting it.

    SHFileOperation silently destroys a file when the Recycle Bin cannot take
    it. Two guards keep that from happening: only fixed local drives (which
    have a Recycle Bin) are accepted, and FOF_WANTNUKEWARNING makes the shell
    ask instead of destroying if the file still cannot be recycled.
    """
    if os.name != "nt":
        raise RuntimeError("Recycle Bin is supported on Windows only.")

    import ctypes
    from ctypes import wintypes

    drive_fixed = 3
    anchor = Path(path).anchor
    if not anchor or ctypes.windll.kernel32.GetDriveTypeW(anchor) != drive_fixed:
        raise RuntimeError(
            "This drive has no Recycle Bin; Qronos does not delete permanently."
        )

    fo_delete = 3
    fof_silent = 0x0004
    fof_noconfirmation = 0x0010
    fof_allowundo = 0x0040
    fof_noerrorui = 0x0400
    fof_wantnukewarning = 0x4000

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.wFunc = fo_delete
    op.pFrom = str(path) + "\0\0"
    op.fFlags = (
        fof_silent
        | fof_noconfirmation
        | fof_allowundo
        | fof_noerrorui
        | fof_wantnukewarning
    )
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted:
        raise OSError(f"Recycle Bin operation failed: {result}")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _audit_target(artifact_id: str) -> str:
    return artifact_id if _ARTIFACT_ID.fullmatch(artifact_id) else "invalid-artifact-id"


def _operation(value: object) -> LibraryOperation:
    try:
        return LibraryOperation(str(value or "").strip().lower())
    except ValueError:
        raise LibraryActionRefused(
            f"Unsupported Library operation: {str(value)[:40]!r}."
        ) from None


def _clean_ids(raw: object) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        raise LibraryActionRefused("Library operation needs a list of artifact ids.")
    seen: dict[str, None] = {}
    for value in raw:
        cleaned = str(value).strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    ids = list(seen)
    if not ids:
        raise LibraryActionRefused("Select at least one artifact.")
    if len(ids) > MAX_BATCH:
        raise LibraryActionRefused(f"At most {MAX_BATCH} artifacts per operation.")
    return ids


def validated_file_name(value: object) -> str:
    """A safe, non-executable basename for a file in the same folder."""
    name = str(value or "").strip()
    if not name or name in {".", ".."}:
        raise LibraryActionRefused("The new name must not be empty.")
    if len(name) > 255:
        raise LibraryActionRefused("The new name is too long.")
    if any(ch in _INVALID_NAME_CHARS or ord(ch) < 32 for ch in name):
        raise LibraryActionRefused(
            "The new name contains a path separator or a character Windows does not allow."
        )
    if name.endswith((".", " ")):
        raise LibraryActionRefused("The new name must not end with a dot or a space.")
    if name.split(".", 1)[0].strip().upper() in _WINDOWS_RESERVED_NAMES:
        raise LibraryActionRefused("The new name is reserved by Windows.")
    if is_executable_name(name):
        raise LibraryActionRefused(
            "Renaming an artifact into an executable or script type is not allowed."
        )
    return name


def _validated_link_name(value: object) -> str:
    name = str(value or "").strip()
    if not name:
        raise LibraryActionRefused("The new name must not be empty.")
    if len(name) > 255 or any(ord(ch) < 32 for ch in name):
        raise LibraryActionRefused("The new name is not a valid display name.")
    return name


def _is_cross_device(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17


def _rename_no_replace(source: Path, target: Path) -> None:
    """Rename that fails, rather than overwrites, when ``target`` exists."""
    if os.name == "nt":
        # MoveFileEx without MOVEFILE_REPLACE_EXISTING: never overwrites.
        os.rename(source, target)
        return
    os.link(source, target)
    os.unlink(source)


def _copy_names(directory: Path, name: str) -> Iterator[Path]:
    yield directory / name
    original = Path(name)
    stem, suffix = original.stem, original.suffix
    for index in range(2, MAX_COPY_NAME_ATTEMPTS + 2):
        yield directory / f"{stem} ({index}){suffix}"


class LibraryExecutor:
    """Gate, validate, execute and audit every Library file mutation."""

    def __init__(
        self,
        registry: ArtifactRegistry,
        *,
        audit: ActionAuditLog | None = None,
        evaluate_fn: EvaluateFn = evaluate,
        recycler: Recycler | None = None,
        clock: Clock = time.time,
        approval_ttl: float = APPROVAL_TTL_SECONDS,
        protected_roots: tuple[Path, ...] | None = None,
        approval_secrets: ApprovalSecretStore | None = None,
        browser: BrowserLauncher | None = None,
    ) -> None:
        self.registry = registry
        self.audit = audit
        self._evaluate = evaluate_fn
        self._recycle: Recycler = recycler if recycler is not None else send_to_recycle_bin
        self._clock = clock
        self._ttl = float(approval_ttl)
        self._protected_roots = protected_roots
        self._secrets = approval_secrets
        self._browser = browser

    @property
    def approval_secrets(self) -> ApprovalSecretStore:
        if self._secrets is None:
            self._secrets = ApprovalSecretStore()
        return self._secrets

    # ------------------------------------------------------------ public API

    def prepare(
        self,
        operation: object,
        ids: object,
        *,
        destination: object = "",
        new_name: object = "",
    ) -> dict[str, object]:
        """
        Validate one exact operation and issue a single-use approval for it.

        Nothing on disk changes here. The returned descriptor is what the UI
        shows the person; its token is the only thing ``commit`` accepts.
        """
        op = _operation(operation)
        artifact_ids = _clean_ids(ids)

        if op is LibraryOperation.RENAME and len(artifact_ids) != 1:
            raise LibraryActionRefused("Rename applies to exactly one artifact.")
        if op is LibraryOperation.OPEN_LINK and len(artifact_ids) != 1:
            raise LibraryActionRefused("Open one link at a time.")

        requests = [self._request(op, artifact_id) for artifact_id in artifact_ids]
        verdicts = [self._decide(request) for request in requests]

        refused = [v for v in verdicts if v.refused]
        if refused:
            raise LibraryActionRefused(
                refused[0].reason,
                results=tuple(
                    self._refusal(v.request, v.reason, record=False) for v in refused
                ),
            )

        if op is LibraryOperation.DELETE_FOREVER:
            # Never reached through the real gate, which denies the category.
            # Refused again here so no policy, override or test double can
            # turn permanent deletion into an executable operation.
            raise LibraryActionRefused(
                PERMANENT_DELETE_REFUSAL,
                results=tuple(
                    self._refusal(r, PERMANENT_DELETE_REFUSAL) for r in requests
                ),
            )

        decisions = {v.decision for v in verdicts}
        if len(decisions) != 1:
            raise LibraryActionRefused(
                "Targets require different approvals; select them separately.",
                results=tuple(
                    self._refusal(r, "mixed approval requirements") for r in requests
                ),
            )
        decision = verdicts[0].decision
        confirmation = _CONFIRMATION.get(decision)
        if confirmation is None:
            raise LibraryActionRefused(
                f"{decision.value} cannot be satisfied from the Library.",
                results=tuple(self._refusal(r, decision.value) for r in requests),
            )

        if op is LibraryOperation.OPEN_LINK and self._browser is None:
            # Decided and audited above; refused here because the only
            # sanctioned route to a website is not available in this build.
            raise LibraryActionRefused(
                BROWSER_UNAVAILABLE,
                results=tuple(self._refusal(r, BROWSER_UNAVAILABLE) for r in requests),
            )

        if decision is PermissionDecision.REQUIRE_TYPED_SECRET:
            # Fail closed before showing a dialog that could never succeed.
            self._require_secret_ready(requests)

        dest: Path | None = None
        if op in {LibraryOperation.COPY, LibraryOperation.MOVE}:
            try:
                dest = self._validated_destination(destination)
            except _ItemRefused as exc:
                raise LibraryActionRefused(
                    str(exc),
                    results=tuple(self._refusal(r, str(exc)) for r in requests),
                ) from None

        targets: list[_Target] = []
        problems: list[dict[str, object]] = []
        clean_name = ""

        for request in requests:
            artifact_id = str(request.parameters["artifactId"])
            try:
                target = self._prepare_target(op, artifact_id, request, dest)
                if op is LibraryOperation.RENAME:
                    clean_name = self._validated_rename(target, new_name)
                targets.append(target)
            except LibraryActionRefused as exc:
                problems.append(self._refusal(request, str(exc)))
            except (_ItemRefused, KeyError, OSError) as exc:
                problems.append(self._refusal(request, _reason(exc)))

        if op is LibraryOperation.MOVE and not problems:
            names: set[str] = set()
            for target in targets:
                assert target.path is not None
                key = os.path.normcase(target.path.name)
                if key in names:
                    problems.append(
                        self._refusal(
                            target.request,
                            "Two selected artifacts share a name; move them separately.",
                        )
                    )
                names.add(key)

        if problems:
            raise LibraryActionRefused(
                "; ".join(str(p["error"]) for p in problems[:3]),
                results=tuple(problems),
            )

        token = secrets.token_urlsafe(32)
        now = float(self._clock())
        expires_at = now + self._ttl

        self.registry.put_pending_action(
            _token_hash(token),
            op.value,
            {
                "decision": decision.value,
                "destination": "" if dest is None else str(dest),
                "newName": clean_name,
                "items": [
                    {
                        "id": t.artifact.id,
                        "actionId": t.request.action_id,
                        "path": t.artifact.path_or_url,
                        "identity": t.artifact.file_identity,
                    }
                    for t in targets
                ],
            },
            now=now,
            expires_at=expires_at,
        )

        return {
            "token": token,
            "operation": op.value,
            "decision": decision.value,
            "confirmation": confirmation,
            "expiresAt": expires_at,
            "destination": "" if dest is None else str(dest),
            "newName": clean_name,
            "targets": [
                {
                    "id": t.artifact.id,
                    "name": t.artifact.name,
                    "pathOrUrl": t.artifact.path_or_url,
                }
                for t in targets
            ],
        }

    def commit(
        self,
        token: object,
        *,
        operation: object,
        secret: object = "",
    ) -> dict[str, object]:
        """
        Run exactly what ``prepare`` approved, once.

        Policy is re-evaluated for the same action ids, so a permission change
        made while the dialog was open is honoured. Each target is re-verified
        against what was approved; a target that changed is refused, not
        retargeted. ``secret`` is used only to verify a TYPED_SECRET approval
        and is never stored, returned or logged.
        """
        op = _operation(operation)
        token_hash = _token_hash(str(token or ""))
        pending = self.registry.get_pending_action(token_hash)

        if pending is None:
            raise LibraryActionRefused(
                "This approval is unknown, expired or already used. Nothing was changed."
            )

        stored_op, payload, expires_at = pending
        items = [i for i in payload.get("items", []) if isinstance(i, dict)]
        requests = [self._stored_request(LibraryOperation(stored_op), i) for i in items]

        if stored_op != op.value:
            self.registry.consume_pending_action(token_hash)
            raise LibraryActionRefused(
                "The approval was issued for a different operation. Nothing was changed.",
                results=tuple(self._refusal(r, "operation mismatch") for r in requests),
            )

        if float(self._clock()) >= expires_at:
            self.registry.consume_pending_action(token_hash)
            raise LibraryActionRefused(
                "The approval expired. Nothing was changed.",
                results=tuple(self._refusal(r, "approval expired") for r in requests),
            )

        approved_decision = str(payload.get("decision", ""))
        secret_verified = False
        if approved_decision == PermissionDecision.REQUIRE_TYPED_SECRET.value:
            # Checked before the token is consumed: a wrong secret leaves the
            # approval usable until it expires, and every wrong guess counts
            # toward the persisted lockout.
            self._verify_secret(secret, requests)
            secret_verified = True

        if not self.registry.consume_pending_action(token_hash):
            raise LibraryActionRefused(
                "This approval was already used. Nothing was changed."
            )

        if op is LibraryOperation.DELETE_FOREVER:
            raise LibraryActionRefused(
                PERMANENT_DELETE_REFUSAL,
                results=tuple(
                    self._refusal(r, PERMANENT_DELETE_REFUSAL) for r in requests
                ),
            )

        results: list[dict[str, object]] = []

        for item, request in zip(items, requests):
            results.append(
                self._commit_item(
                    op, item, request, approved_decision, payload, secret_verified
                )
            )

        counts = {
            outcome.value: sum(1 for r in results if r["outcome"] == outcome.value)
            for outcome in (
                ActionOutcome.SUCCEEDED,
                ActionOutcome.FAILED,
                ActionOutcome.REFUSED,
            )
        }
        return {
            "operation": op.value,
            "results": results,
            "succeeded": counts["succeeded"],
            "failed": counts["failed"],
            "refused": counts["refused"],
            "complete": counts["succeeded"] == len(results),
        }

    def cancel(self, token: object) -> dict[str, object]:
        """Record an explicit human decline and discard the approval."""
        token_hash = _token_hash(str(token or ""))
        pending = self.registry.get_pending_action(token_hash)
        if pending is None or not self.registry.consume_pending_action(token_hash):
            return {"cancelled": False}

        stored_op, payload, _ = pending
        try:
            decision = PermissionDecision(str(payload.get("decision", "")))
        except ValueError:
            decision = None

        for item in payload.get("items", []):
            if isinstance(item, dict) and self.audit is not None:
                self.audit.record(
                    AuditEvent.DENIED_BY_USER,
                    self._stored_request(LibraryOperation(stored_op), item),
                    outcome=ActionOutcome.REFUSED,
                    decision=decision,
                )
        return {"cancelled": True}

    # ------------------------------------------------------------- decisions

    def _request(self, op: LibraryOperation, artifact_id: str) -> ActionRequest:
        return ActionRequest(
            category=_CATEGORY[op],
            target=_audit_target(artifact_id),
            summary=_SUMMARY[op],
            parameters={
                "operation": op.value,
                "artifactId": artifact_id,
                "initiator": INITIATOR,
            },
        )

    def _stored_request(
        self,
        op: LibraryOperation,
        item: Mapping[str, Any],
    ) -> ActionRequest:
        artifact_id = str(item.get("id", ""))
        return ActionRequest(
            category=_CATEGORY[op],
            target=_audit_target(artifact_id),
            summary=_SUMMARY[op],
            parameters={
                "operation": op.value,
                "artifactId": artifact_id,
                "initiator": INITIATOR,
            },
            action_id=str(item.get("actionId", "")) or uuid.uuid4().hex,
        )

    def _decide(self, request: ActionRequest) -> Verdict:
        return self._evaluate(
            request,
            audit=self.audit.record_verdict if self.audit is not None else None,
        )

    def _refusal(
        self,
        request: ActionRequest,
        reason: str,
        *,
        record: bool = True,
    ) -> dict[str, object]:
        if record and self.audit is not None:
            self.audit.record(
                AuditEvent.COMPLETED,
                request,
                outcome=ActionOutcome.REFUSED,
            )
        return {
            "id": str(request.parameters.get("artifactId", "")),
            "ok": False,
            "outcome": ActionOutcome.REFUSED.value,
            "error": reason,
        }

    # -------------------------------------------------------- approval secret

    def _require_secret_ready(self, requests: list[ActionRequest]) -> None:
        try:
            status = self.approval_secrets.status()
        except ApprovalSecretError as exc:
            message = str(exc)
        else:
            if not status.configured:
                message = SETUP_HINT
            elif status.locked_until:
                message = str(ApprovalSecretLocked(status.locked_until))
            else:
                return
        raise LibraryActionRefused(
            message,
            results=tuple(self._refusal(r, message) for r in requests),
        )

    def _verify_secret(self, secret: object, requests: list[ActionRequest]) -> None:
        if not isinstance(secret, str) or not secret:
            raise LibraryActionRefused(
                "Enter your approval secret to confirm. Nothing was changed."
            )
        try:
            verified = self.approval_secrets.verify(secret)
        except InvalidApprovalSecret:
            verified = False
        except ApprovalSecretError as exc:
            message = f"{exc} Nothing was changed."
            raise LibraryActionRefused(
                message,
                results=tuple(self._refusal(r, str(exc)) for r in requests),
            ) from None
        if not verified:
            raise LibraryActionRefused(
                "The approval secret is wrong. Nothing was changed.",
                results=tuple(
                    self._refusal(r, "approval secret rejected") for r in requests
                ),
            )

    # ------------------------------------------------------------ validation

    def _protected(self) -> tuple[Path, ...]:
        if self._protected_roots is not None:
            return self._protected_roots
        return default_protected_roots(self.registry)

    def _validated_destination(self, value: object) -> Path:
        raw = str(value or "").strip()
        if not raw:
            raise _ItemRefused("Choose a destination folder.")
        if raw.startswith(("\\\\", "//")):
            raise _ItemRefused(
                "Network and device paths are not Library destinations."
            )
        path = Path(raw)
        if not path.is_absolute():
            raise _ItemRefused("The destination must be an absolute folder path.")
        if is_link_or_reparse_point(path):
            raise _ItemRefused("The destination is a link or junction.")
        if not path.is_dir():
            raise _ItemRefused("The destination folder does not exist.")
        resolved = path.resolve(strict=True)
        if not same_path(resolved, path):
            raise _ItemRefused(
                "The destination resolves through a link or junction."
            )
        if os.name == "nt" and not re.match(r"^[A-Za-z]:\\", str(resolved)):
            raise _ItemRefused("The destination must be on a local drive.")
        # Copying or moving Library files to a mapped network drive sends them
        # off this PC; a drive Windows cannot classify is not assumed local.
        drive = drive_status(str(resolved))
        if drive == "remote":
            raise _ItemRefused("The destination is on a network drive.")
        if drive == "unknown":
            raise _ItemRefused("Windows could not confirm the destination is on a local drive.")
        for root in self._protected():
            if resolved == root or resolved.is_relative_to(root):
                raise _ItemRefused(
                    "The destination is a protected system, application or "
                    "Qronos folder."
                )
        return resolved

    def _refuse_protected_source(self, path: Path) -> None:
        """
        A registered artifact is still refused if it lives among system files
        or Qronos state. Registration is not proof of ownership when the
        artifact root, or the file, ended up somewhere it should not be.
        """
        resolved = path.resolve(strict=False)
        if any(same_path(resolved, state) for state in qronos_state_files(self.registry)):
            raise _ItemRefused("Qronos state files cannot be changed from the Library.")
        for root in self._protected():
            if resolved == root or resolved.is_relative_to(root):
                raise _ItemRefused(
                    "The artifact is inside a protected system, application or "
                    "Qronos folder; the Library will not change it."
                )

    def _verified_file(self, artifact: Artifact) -> Path:
        path = Path(artifact.path_or_url)
        if not path.is_absolute():
            raise _ItemRefused("The artifact path is not absolute.")
        if is_link_or_reparse_point(path):
            raise _ItemRefused("The artifact path is now a link or reparse point.")
        if not path.is_file():
            raise _ItemRefused("The artifact file is missing.")
        if not same_path(path.resolve(strict=True), path):
            raise _ItemRefused(
                "The artifact path now resolves through a link or junction."
            )
        if not identity_matches(
            artifact.identity(),
            path,
            content_hash=artifact.content_hash,
        ):
            raise _ItemRefused(
                "The file at this path is no longer the artifact Qronos registered."
            )
        return path

    def _prepare_target(
        self,
        op: LibraryOperation,
        artifact_id: str,
        request: ActionRequest,
        dest: Path | None,
    ) -> _Target:
        try:
            artifact = self.registry.get(artifact_id)
        except KeyError:
            raise _ItemRefused("Unknown artifact; refresh the Library.") from None

        if op is LibraryOperation.OPEN_LINK:
            if artifact.category != "link":
                raise _ItemRefused("Only saved web links open in the browser.")
            try:
                validate_web_url(artifact.path_or_url)
            except UnsafeLink as exc:
                raise _ItemRefused(str(exc)) from None
            return _Target(artifact=artifact, request=request, path=None)

        if artifact.category == "link":
            if op in {LibraryOperation.COPY, LibraryOperation.MOVE}:
                raise _ItemRefused("Links cannot be copied or moved as files.")
            return _Target(artifact=artifact, request=request, path=None)

        path = self._verified_file(artifact)
        self._refuse_protected_source(path)

        if op is LibraryOperation.MOVE:
            assert dest is not None
            if same_path(path.parent, dest):
                raise _ItemRefused("The artifact is already in that folder.")
            target = dest / path.name
            if target.exists() or is_link_or_reparse_point(target):
                raise _ItemRefused(
                    "A file with the same name already exists in the destination."
                )

        return _Target(artifact=artifact, request=request, path=path)

    def _validated_rename(self, target: _Target, new_name: object) -> str:
        if target.path is None:
            name = _validated_link_name(new_name)
            if name == target.artifact.name:
                raise _ItemRefused("The name is unchanged.")
            return name

        name = validated_file_name(new_name)
        if name == target.path.name:
            raise _ItemRefused("The name is unchanged.")
        destination = target.path.with_name(name)
        if (destination.exists() or is_link_or_reparse_point(destination)) and not (
            destination.exists() and os.path.samefile(destination, target.path)
        ):
            raise _ItemRefused("A file with that name already exists.")
        return name

    # -------------------------------------------------------------- execution

    def _commit_item(
        self,
        op: LibraryOperation,
        item: Mapping[str, Any],
        request: ActionRequest,
        approved_decision: str,
        payload: Mapping[str, Any],
        secret_verified: bool,
    ) -> dict[str, object]:
        verdict = self._decide(request)

        if verdict.refused:
            return self._refusal(request, verdict.reason, record=False)

        if verdict.decision.value != approved_decision:
            return self._refusal(
                request,
                "The permission policy changed after approval; prepare the action again.",
            )

        if (
            verdict.decision is PermissionDecision.REQUIRE_TYPED_SECRET
            and not secret_verified
        ):
            return self._refusal(request, "The approval secret was not verified.")

        try:
            issue_grant(
                request,
                verdict,
                approval=verdict.decision if verdict.needs_approval else None,
            )
        except ActionGrantUnavailable as exc:
            return self._refusal(request, str(exc))

        if verdict.needs_approval and self.audit is not None:
            self.audit.record(
                AuditEvent.APPROVED,
                request,
                decision=verdict.decision,
            )

        try:
            artifact, path = self._current_target(item)
        except (_ItemRefused, KeyError, OSError) as exc:
            return self._refusal(request, _reason(exc))

        extra: dict[str, object] = {}
        try:
            if op is LibraryOperation.RENAME:
                self._rename(artifact, path, str(payload.get("newName", "")))
            elif op is LibraryOperation.MOVE:
                self._move(artifact, path, self._current_destination(payload))
            elif op is LibraryOperation.COPY:
                extra["newArtifactId"] = self._copy(
                    artifact, path, self._current_destination(payload)
                )
            elif op is LibraryOperation.DELETE:
                self._delete(artifact, path)
            elif op is LibraryOperation.OPEN_LINK:
                self._open_link(artifact)
            else:
                raise _ItemRefused(PERMANENT_DELETE_REFUSAL)
        except (_ItemRefused, LibraryActionRefused) as exc:
            return self._refusal(request, str(exc))
        except Exception as exc:  # reported, audited, never silent
            if self.audit is not None:
                self.audit.record(
                    AuditEvent.COMPLETED,
                    request,
                    outcome=ActionOutcome.FAILED,
                )
            return {
                "id": artifact.id,
                "ok": False,
                "outcome": ActionOutcome.FAILED.value,
                "error": _reason(exc),
            }

        if self.audit is not None:
            self.audit.record(
                AuditEvent.COMPLETED,
                request,
                outcome=ActionOutcome.SUCCEEDED,
            )
        return {
            "id": artifact.id,
            "ok": True,
            "outcome": ActionOutcome.SUCCEEDED.value,
            **extra,
        }

    def _current_target(
        self,
        item: Mapping[str, Any],
    ) -> tuple[Artifact, Path | None]:
        try:
            artifact = self.registry.get(str(item.get("id", "")))
        except KeyError:
            raise _ItemRefused(
                "The artifact was removed after approval; nothing was changed."
            ) from None

        if (
            artifact.path_or_url != str(item.get("path", ""))
            or artifact.file_identity != str(item.get("identity", ""))
        ):
            raise _ItemRefused(
                "The artifact changed after approval; nothing was changed."
            )

        if artifact.category == "link":
            return artifact, None
        path = self._verified_file(artifact)
        self._refuse_protected_source(path)
        return artifact, path

    def _current_destination(self, payload: Mapping[str, Any]) -> Path:
        approved = str(payload.get("destination", ""))
        current = self._validated_destination(approved)
        if not same_path(current, approved):
            raise _ItemRefused(
                "The destination changed after approval; nothing was changed."
            )
        return current

    def _rename(self, artifact: Artifact, path: Path | None, new_name: str) -> None:
        if path is None:
            self.registry.rename_link(
                artifact.id,
                _validated_link_name(new_name),
                expected_path=artifact.path_or_url,
            )
            return

        name = validated_file_name(new_name)
        target = path.with_name(name)
        if (target.exists() or is_link_or_reparse_point(target)) and not (
            target.exists() and os.path.samefile(target, path)
        ):
            raise _ItemRefused("A file with that name already exists.")

        _rename_no_replace(path, target)
        self._relocate_or_roll_back(artifact, path, target, cross_volume=False)

    def _move(self, artifact: Artifact, path: Path | None, dest: Path) -> None:
        if path is None:
            raise _ItemRefused("Links cannot be moved as files.")
        if same_path(path.parent, dest):
            raise _ItemRefused("The artifact is already in that folder.")
        target = dest / path.name
        if target.exists() or is_link_or_reparse_point(target):
            raise _ItemRefused(
                "A file with the same name already exists in the destination."
            )

        cross_volume = self._move_file(artifact, path, target)
        self._relocate_or_roll_back(artifact, path, target, cross_volume=cross_volume)

    def _copy(self, artifact: Artifact, path: Path | None, dest: Path) -> str:
        if path is None:
            raise _ItemRefused("Links cannot be copied as files.")

        created = self._copy_verified(artifact, path, _copy_names(dest, path.name))
        try:
            registered = self.registry.register_file(
                created,
                origin="Copied by Qronos",
                source_conversation_id=artifact.source_conversation_id,
                source_message_id=artifact.source_message_id,
                source_request_id=artifact.source_request_id,
            )
        except Exception:
            # The copy is Qronos's own fresh file; removing it restores the
            # state before the operation. The original was never touched.
            if created.is_file() and not is_link_or_reparse_point(created):
                created.unlink()
            raise
        return registered.id

    def _open_link(self, artifact: Artifact) -> None:
        if artifact.category != "link":
            raise _ItemRefused("Only saved web links open in the browser.")
        try:
            url = validate_web_url(artifact.path_or_url)
        except UnsafeLink as exc:
            raise _ItemRefused(str(exc)) from None
        if self._browser is None:
            raise _ItemRefused(BROWSER_UNAVAILABLE)
        self._browser(url)

    def _delete(self, artifact: Artifact, path: Path | None) -> None:
        if path is None:
            # A saved link is a Library record, not a file on disk.
            self.registry.remove(artifact.id, expected_path=artifact.path_or_url)
            return

        self._recycle(path)

        if path.exists() or is_link_or_reparse_point(path):
            raise RuntimeError(
                "The Recycle Bin did not take the file; the Library entry was kept."
            )

        try:
            self.registry.remove(artifact.id, expected_path=artifact.path_or_url)
        except Exception as exc:
            raise RuntimeError(
                "The file is in the Recycle Bin, but the Library entry could not "
                "be removed; it will show as missing."
            ) from exc

    # ----------------------------------------------------- filesystem helpers

    def _move_file(self, artifact: Artifact, source: Path, target: Path) -> bool:
        """Move without overwriting. Returns True for a cross-volume copy+remove."""
        try:
            _rename_no_replace(source, target)
            return False
        except OSError as exc:
            if not _is_cross_device(exc):
                raise

        created = self._copy_verified(artifact, source, iter((target,)))
        try:
            source.unlink()
        except Exception:
            created.unlink()
            raise
        return True

    def _copy_verified(
        self,
        artifact: Artifact,
        source: Path,
        candidates: Iterator[Path],
    ) -> Path:
        first = next(candidates)
        partial = first.parent / f".qronos-partial-{uuid.uuid4().hex}"
        expected = str(artifact.identity().get("sha256") or artifact.content_hash or "")
        try:
            shutil.copy2(source, partial)
            if partial.stat().st_size != source.stat().st_size or (
                expected and not identity_matches({"sha256": expected}, partial)
            ):
                raise RuntimeError("Copy verification failed; the copy was discarded.")

            for candidate in (first, *candidates):
                if is_link_or_reparse_point(candidate):
                    continue
                try:
                    _rename_no_replace(partial, candidate)
                    return candidate
                except FileExistsError:
                    continue
            raise FileExistsError("No free file name was available in the destination.")
        finally:
            if partial.exists():
                partial.unlink()

    def _relocate_or_roll_back(
        self,
        artifact: Artifact,
        original: Path,
        moved_to: Path,
        *,
        cross_volume: bool,
    ) -> None:
        try:
            self.registry.relocate(
                artifact.id,
                moved_to,
                expected_path=artifact.path_or_url,
            )
        except Exception as exc:
            try:
                if cross_volume:
                    self._move_file(artifact, moved_to, original)
                else:
                    _rename_no_replace(moved_to, original)
            except Exception as rollback_exc:
                raise RuntimeError(
                    "The Library entry could not be updated and the file could "
                    "not be put back; it remains at the new location."
                ) from rollback_exc
            raise RuntimeError(
                "The Library entry could not be updated; the file was put back."
            ) from exc


def _reason(exc: BaseException) -> str:
    if isinstance(exc, (_ItemRefused, LibraryActionRefused)):
        return str(exc)
    if isinstance(exc, KeyError):
        return "Unknown artifact; refresh the Library."
    if isinstance(exc, FileExistsError):
        return "A file with that name already exists."
    if isinstance(exc, FileNotFoundError):
        return "The file was not found."
    if isinstance(exc, PermissionError):
        return "Windows denied access to the file."
    message = str(exc).strip()
    return message or type(exc).__name__
