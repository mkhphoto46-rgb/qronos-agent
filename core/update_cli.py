"""Desktop entry point for the offline update lifecycle (``core.update_service``).

Same contract as the other backend CLIs: the operation is argv[1], a JSON object
arrives on stdin, one JSON object is printed. Paths are the ones the person
chose in a native file dialog.

The running version is never taken from the caller (a request naming
``runningVersion`` is refused). It comes from the product's version authority:
``QRONOS_APP_VERSION``, which the desktop binary sets from its own compiled
package information when it starts this service, or, in a source checkout,
``desktop/src-tauri/tauri.conf.json``. A packaged backend without it refuses.

Operations: status, discover, verify, request, approve, launch, discard,
startup_check.

* ``request`` {installer, manifest} verifies and returns the offer and a
  single-use ``approvalToken``.
* ``approve`` {approvalToken, secret} checks the approval secret, re-verifies,
  stages, and returns a single-use ``launchToken``. The secret arrives only in
  this stdin JSON (never argv or the environment) and is never echoed.
* ``launch`` {launchToken} is REFUSED (``launch_unavailable``) until the
  launch dependencies in ``core.update_service.LAUNCH_DEPENDENCIES`` exist and
  are accepted; nothing in a request can enable it.
* ``startup_check`` is read-only status; the mutating start-up barrier runs
  inside the runtime, before any store opens, not through this command.

Refusals are ``{"ok": false, "code": ..., "error": ...}`` with the
UpdateError code; nothing here downloads, and no path other than the staged,
re-verified installer is ever started.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.local_update import UpdateError  # noqa: E402
from core.update_service import UpdateService  # noqa: E402

_PAYLOAD_LIMIT = 64 * 1024
OPERATIONS = ("status", "discover", "verify", "request", "approve", "launch", "discard", "startup_check")


def _text(payload: dict[str, Any], name: str, limit: int = 4096) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > limit or "\0" in value:
        raise UpdateError("bad_request", f"{name} must be non-empty text.")
    return value


def _path(payload: dict[str, Any], name: str) -> Path:
    path = Path(_text(payload, name))
    if not path.is_absolute():
        raise UpdateError("bad_request", f"{name} must be an absolute path.")
    return path


def running_version(environ: dict[str, str] | None = None, root: Path = ROOT) -> str:
    """The product version authority, never the request."""
    import os

    from core.local_update import parse_version

    environment = os.environ if environ is None else environ
    version = environment.get("QRONOS_APP_VERSION", "").strip()
    if not version:
        packaged = getattr(sys, "frozen", False) or environment.get("QRONOS_PACKAGED_RUNTIME") == "1"
        config = root / "desktop" / "src-tauri" / "tauri.conf.json"
        if packaged or not config.is_file():
            raise UpdateError("version_unknown", "The desktop did not say which Qronos version is running.")
        version = json.loads(config.read_text(encoding="utf-8")).get("version", "")
    parse_version(version)
    return version


def run(operation: str, payload: dict[str, Any],
        service_factory: Callable[[str], UpdateService] | None = None,
        version: str | None = None,
        emit: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    if operation not in OPERATIONS:
        raise UpdateError("bad_request", f"Unsupported update operation: {operation}")
    if "runningVersion" in payload:
        raise UpdateError("bad_request", "The running version is not taken from the request.")
    version = version or running_version()
    if service_factory is None:
        import os

        from core.action_audit import DEFAULT_ACTION_AUDIT_PATH, ActionAuditLog
        from core.config import CONFIG
        from core.update_service import GateAuthorizer

        audit_path = os.environ.get("QRONOS_PERMISSION_AUDIT_PATH", "").strip() or DEFAULT_ACTION_AUDIT_PATH
        audit = ActionAuditLog(Path(audit_path).expanduser())
        service = UpdateService(CONFIG.paths.data, version, authorizer=GateAuthorizer(audit_log=audit))
    else:
        service = service_factory(version)
    if operation == "status":
        return {"ok": True, "status": service.status()}
    if operation == "discover":
        return {"ok": True, "packages": service.discover(_path(payload, "folder"))}
    if operation == "verify":
        return {"ok": True, "offer": service.verify(_path(payload, "installer"),
                                                    _path(payload, "manifest")).to_json()}
    if operation == "request":
        return {"ok": True, **service.request(_path(payload, "installer"), _path(payload, "manifest"))}
    if operation == "approve":
        return {"ok": True, **service.approve(payload.get("approvalToken"), payload.get("secret"))}
    if operation == "launch":
        # The answer goes out once the installer has taken the exclusion over (handedOver
        # true; this process then exits) or has not (this process keeps the exclusion until
        # the installer ends). Either way, one JSON line, at most HANDOVER_SECONDS after start.
        answered: list[bool] = []

        def launched(handoff: dict[str, Any]) -> None:
            if emit is not None:
                emit({"ok": True, "handoff": handoff})
                answered.append(True)

        handoff = service.launch(payload.get("launchToken"), on_launched=launched)
        return {"ok": True, "handoff": handoff, "answered": bool(answered)}
    if operation == "discard":
        service.discard()
        return {"ok": True}
    # Read-only status. The MUTATING start-up barrier (UpdateService.recover_before_stores)
    # belongs inside the runtime's own start, before any store opens, never here.
    status = service.status()
    return {"ok": True, "startup": {"phase": status["phase"], "outcome": status["outcome"],
                                    "updateInProgress": status["updateInProgress"],
                                    "recoveryPending": status["recoveryPending"]}}


def _emit(result: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    sys.stdout.flush()


#: The one operation that must NOT take the shared writer admission: launching takes the EXCLUSIVE
#: admission itself (core.update_service.LaunchHold). The same normalisation (strip, lower) as
#: build_support/runtime_entry.UPDATE_EXCLUSIVE_OPERATIONS, and only that exact word.
EXCLUSIVE_OPERATION = "launch"


def _admit_before_stores() -> int | None:
    """
    The start-up order of core/startup_admission.py for every operation but ``launch``, before the request is
    read and before any store opens (Installer N9): the same as the frozen dispatch does before importing this
    module, so ``python -m core.update_cli`` from source cannot open installed stores without it. Idempotent
    after the frozen dispatch. Returns the exit code after printing the one refusal JSON, or None to continue.
    """
    from core import startup_admission

    return startup_admission.enter_cli("update", running_version=running_version)


def main() -> int:
    operation = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "status"
    if operation != EXCLUSIVE_OPERATION:
        refused = _admit_before_stores()
        if refused is not None:
            return refused
    try:
        raw = sys.stdin.read(_PAYLOAD_LIMIT + 1)
        if len(raw) > _PAYLOAD_LIMIT:
            raise UpdateError("bad_request", "The request is too large.")
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise UpdateError("bad_request", "The request must be a JSON object.")
        result = run(operation, payload, emit=_emit)
        if result.pop("answered", False):
            return 0  # already answered when the installer started
    except UpdateError as error:
        result = {"ok": False, "code": error.code, "error": str(error)}
    except json.JSONDecodeError:
        result = {"ok": False, "code": "bad_request", "error": "The request is not JSON."}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # the same last-resort contract as the other backend CLIs
        print(json.dumps({"ok": False, "code": "internal", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
