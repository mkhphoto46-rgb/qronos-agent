"""
The start-up order of every Qronos process that opens persistent stores.

Before the FIRST store opens (settings, history, journals, library, memory,
permissions, approval secret, audit logs, media, update records), a process
runs, in this order, once:

1. Instance ownership, when the service is a singleton (the runtime bridge):
   its mutex is OWNED by a holder thread (``core.instance_ownership``), never
   merely created. Then the update fence is checked AFTER that acquisition;
   while it exists, what was acquired is released and start-up is refused.
2. The product version: QRONOS_APP_VERSION, which the desktop sets from its own
   package information (tauri.conf.json); from source only, tauri.conf.json
   itself (``core.update_cli.running_version``). A packaged process without it
   is refused (``version_unknown``). There is no second version constant.
3. ``core.update_service.recover_before_stores`` for that version: awaited
   and bounded (the update store lock waits at most 10 s; the exclusive restore
   never waits). It may put data back after an interrupted update, so it runs
   BEFORE any store opens and is never a background or status hook. Anything
   but ``{"ok": true}`` refuses start-up.
4. ``core.store_admission.admit_writer`` on the data root: a SHARED admission,
   kept for the whole life of the process (Windows releases it at exit). While
   it is held, the updater's EXCLUSIVE admission is refused, so no update can
   restore data or replace the program under this process. Refused while an
   update holds the exclusive admission or its fence exists.

Any refusal is TERMINAL: ``StartupRefused`` carries a typed code, nothing
after it runs, and no store has been opened. ``enter`` is idempotent per
process: the frozen entry point (``build_support/runtime_entry.py``) calls it
before importing the service, so module imports cannot open a store first,
and each service's ``main`` calls it again (a no-op then), which covers
starting a module from source.

The supervisor opens no store, so it takes no writer admission; it owns its
own singleton and checks the fence (``core.supervisor``). The barrier's
liveness test is the update fence only, never the supervisor's or bridge's
own mutex, so holding those first cannot deadlock it, and no caller flag can
waive another live owner.

Where the steps apply: the INSTALLED runtime (``CONFIG.paths.packaged``, which
is always true for the frozen executable: no environment value, request or
argument can make it false there). A source checkout is never updated by the
installer and keeps its data in the checkout (``core.config`` ignores
QRONOS_DATA_ROOT and LocalAppData outside the installed runtime), and off
Windows there is no update facility, so there ``enter`` admits without these
steps (and takes no bridge singleton, as before). One exception closes the
remaining path: a source process whose store-path overrides
(``STORE_PATH_OVERRIDES``) point INTO an installed data root is opening
installed stores, so it runs the full order on that root. ``installed``,
``windows`` and the other keyword seams are function parameters for tests only;
no product entry point passes them from a request, argument or environment.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

#: Exit code of a process that refused to start (the supervisor does not restart it).
STARTUP_REFUSED_EXIT = 78
from core.instance_ownership import ENTRY_RETRY_SECONDS  # noqa: E402,F401  (one bound for every entry point)

_lock = threading.Lock()
_held: dict[str, Any] = {}

#: Codes a refusal may carry; anything else becomes "store".
CODES = frozenset({
    "update_in_progress", "recovery_failed", "recovery_pending", "version_unknown", "writers_active",
    "store", "busy", "already_running", "ownership_unavailable", "bad_request", "unsupported_platform",
})
MESSAGES = {
    "update_in_progress": "An update is being installed; Qronos starts when it ends.",
    "recovery_failed": "An interrupted update could not be resolved; Qronos does not open its data.",
    "version_unknown": "The running Qronos version is unknown; Qronos does not open its data.",
    "writers_active": "Another part of Qronos holds its data while an update needs it; nothing was opened.",
    "already_running": "Another Qronos runtime is already running in this Windows session.",
    "ownership_unavailable": "Windows could not establish the runtime's ownership; nothing was opened.",
}


class StartupRefused(RuntimeError):
    """Start-up was refused before any store opened. ``code`` is typed; the process must exit."""

    def __init__(self, code: str, message: str = "") -> None:
        code = code if code in CODES else "store"
        super().__init__(message or MESSAGES.get(code, "Qronos could not open its data safely; nothing was opened."))
        self.code = code


def product_version(environ: Optional[Mapping[str, str]] = None) -> str:
    """The running product version from the desktop's package information (see the module doc)."""
    from core.local_update import UpdateError
    from core.update_cli import running_version

    try:
        return running_version(dict(os.environ if environ is None else environ))
    except (UpdateError, OSError, ValueError) as error:
        raise StartupRefused("version_unknown") from error


def admitted() -> bool:
    with _lock:
        return "admission" in _held


#: Environment overrides that move one persistent store to a chosen file.
STORE_PATH_OVERRIDES = (
    "QRONOS_SETTINGS_PATH", "QRONOS_CONVERSATION_HISTORY_PATH", "QRONOS_QUEUE_STATE_PATH",
    "QRONOS_PERMISSION_POLICY_PATH", "QRONOS_PERMISSION_AUDIT_PATH", "QRONOS_APPROVAL_SECRET_PATH",
)


def installed_store_target(environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """The installed data root a NON-installed process would write into through a store override, if any."""
    environment = os.environ if environ is None else environ
    local = environment.get("LOCALAPPDATA", "").strip()
    roots = [(Path(local) if local else Path.home() / ".local" / "share") / "Qronos"]
    configured = environment.get("QRONOS_DATA_ROOT", "").strip()
    if configured and Path(configured).is_absolute():
        roots.append(Path(configured))
    for name in STORE_PATH_OVERRIDES:
        value = environment.get(name, "").strip()
        if not value:
            continue
        target = Path(os.path.abspath(os.path.expanduser(value)))
        for root in roots:
            root = Path(os.path.abspath(root))
            if target == root or root in target.parents:
                return root
    return None


def _installed() -> bool:
    from core.config import CONFIG

    return bool(CONFIG.paths.packaged)


def _default_data_root() -> Path:
    from core.config import CONFIG

    return Path(CONFIG.paths.data)


def recover_on(data_root: Path, version: str) -> dict:
    """The barrier (core.update_service) on EXACTLY the data root this process will be admitted to.

    The same contract as ``update_service.recover_before_stores``: {"ok": True, ...}
    to continue, {"ok": False, "code": ...} to refuse; never raises.
    """
    from core.local_update import UpdateError
    from core.update_service import UpdateService

    try:
        result = UpdateService(Path(data_root), version).recover_before_stores()
    except UpdateError as error:
        return {"ok": False, "code": error.code}
    except (OSError, ValueError):
        return {"ok": False, "code": "store"}
    return {"ok": True, "result": str(result.get("result", ""))[:32]}


def _default_admit(data_root: Path) -> Any:
    from core import store_admission

    return store_admission.admit_writer(data_root)


def _default_instance(retry_seconds: float) -> Any:
    from core.runtime_instance_guard import RuntimeInstanceGuard

    return RuntimeInstanceGuard.acquire(retry_seconds=retry_seconds)


def enter(
    service: str,
    *,
    instance: bool = False,
    environ: Optional[Mapping[str, str]] = None,
    data_root: Optional[Path] = None,
    version: Optional[str] = None,
    recover: Optional[Callable[[str], dict]] = None,
    admit: Callable[[Path], Any] = _default_admit,
    acquire_instance: Callable[[float], Any] = _default_instance,
    retry_seconds: float = ENTRY_RETRY_SECONDS,
    windows: Optional[bool] = None,
    installed: Optional[bool] = None,
    barrier: bool = True,
) -> Any:
    """Run steps 1-4 once for this process; return the held writer admission (or raise StartupRefused).

    ``barrier=False`` skips steps 2-3 for the elevated action broker only, and
    only as it is called there: AFTER ``core.action_broker.serve_once`` has
    authenticated its parent (same user, the installed image, the pipe served by
    exactly that process, and the session key received from it). Only a live
    bridge's broker session releases that key, and a bridge exists only after it
    passed this whole order and while it keeps its shared admission, so no update
    can be between steps then. Nothing earlier in the broker opens a store.
    """
    with _lock:
        if "admission" in _held:
            if instance and "instance" not in _held and not _held.get("notApplicable"):
                raise StartupRefused("ownership_unavailable", "This process was admitted without its instance "
                                                              "ownership; it must not continue.")
            return _held["admission"]
        on_windows = os.name == "nt" if windows is None else windows
        is_installed = _installed() if installed is None else installed
        if on_windows and not is_installed and data_root is None:
            target = installed_store_target(environ)
            if target is not None:  # a source process opening INSTALLED stores: no bypass
                is_installed, data_root = True, target
        if not on_windows or not is_installed:
            _held["admission"] = None
            _held["notApplicable"] = True
            _held["service"] = service
            return None

        guard = None
        try:
            if instance:
                guard = _acquire(acquire_instance, retry_seconds)
            root = Path(data_root) if data_root is not None else _default_data_root()
            if barrier:
                running = version or product_version(environ)
                outcome = _recover(recover or (lambda v: recover_on(root, v)), running)
                if outcome.get("ok") is not True:
                    raise StartupRefused(str(outcome.get("code") or "recovery_failed"))
            admission = _admit(admit, root)
        except BaseException:
            if guard is not None:
                _close(guard)
            raise
        if guard is not None:
            _held["instance"] = guard
        _held["admission"] = admission
        _held["service"] = service
        return admission


def _acquire(acquire_instance: Callable[[float], Any], retry_seconds: float) -> Any:
    from core.instance_ownership import AlreadyOwned, OwnershipError, UpdateInProgress
    from core.runtime_instance_guard import RuntimeAlreadyRunning

    try:
        return acquire_instance(retry_seconds)
    except UpdateInProgress as error:
        raise StartupRefused("update_in_progress") from error
    except (RuntimeAlreadyRunning, AlreadyOwned) as error:
        raise StartupRefused("already_running") from error
    except (OwnershipError, OSError) as error:
        raise StartupRefused("ownership_unavailable") from error


def _recover(recover: Callable[[str], dict], version: str) -> dict:
    try:
        outcome = recover(version)
    except StartupRefused:
        raise
    except Exception as error:  # noqa: BLE001 - an unknown recovery result never lets stores open
        raise StartupRefused("recovery_failed") from error
    if not isinstance(outcome, dict):
        raise StartupRefused("recovery_failed")
    return outcome


def _admit(admit: Callable[[Path], Any], data_root: Path) -> Any:
    from core.local_update import UpdateError

    try:
        return admit(Path(data_root))
    except UpdateError as error:
        raise StartupRefused(error.code) from error
    except OSError as error:
        raise StartupRefused("store") from error


def _close(guard: Any) -> None:
    for name in ("close", "release"):
        method = getattr(guard, name, None)
        if callable(method):
            try:
                method()
            except Exception:  # noqa: BLE001
                pass
            return


def refusal(error: StartupRefused) -> dict:
    """The JSON a CLI service prints for a refused start-up."""
    return {"ok": False, "code": error.code, "error": str(error), "errorType": "StartupRefused"}


def enter_cli(service: str, **options: Any) -> Optional[int]:
    """``enter`` for a command-line service: None to continue, or the exit code after printing the refusal."""
    import json
    import sys

    try:
        enter(service, **options)
    except StartupRefused as refused:
        sys.stdout.write(json.dumps(refusal(refused), ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return STARTUP_REFUSED_EXIT
    return None


def _reset_for_tests() -> None:
    """Test fixtures only: forget this process's admission, releasing what it holds."""
    with _lock:
        guard = _held.pop("instance", None)
        admission = _held.pop("admission", None)
        _held.clear()
    if guard is not None:
        _close(guard)
    if admission is not None:
        try:
            admission.release()
        except Exception:  # noqa: BLE001
            pass
