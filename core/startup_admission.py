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
installed stores, so it runs the full order on that root; an override that is
an existing file with more than one name (a hard link) is refused, because its
other names cannot be placed without listing them. ``installed``,
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
    "recovery_pending": "An interrupted update has not been resolved yet; Qronos does not open its data. It is "
                        "resolved at a start when no other part of Qronos is running.",
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


#: Environment overrides that move one persistent store to a chosen file (the
#: media index: its folder, ``qronos_media.service.default_data_dir``).
STORE_PATH_OVERRIDES = (
    "QRONOS_SETTINGS_PATH", "QRONOS_CONVERSATION_HISTORY_PATH", "QRONOS_QUEUE_STATE_PATH",
    "QRONOS_PERMISSION_POLICY_PATH", "QRONOS_PERMISSION_AUDIT_PATH", "QRONOS_APPROVAL_SECRET_PATH",
    "QRONOS_MEDIA_DATA_DIR",
)


def _known_local_appdata() -> Path:
    """
    This user's Local AppData folder from Windows' known-folder API for this
    process's own token (``SHGetKnownFolderPath``), the folder the desktop
    keeps the installed data in (``<Local AppData>\\Qronos``). Tests replace
    this function with a private folder; nothing else does.
    """
    if os.name != "nt":
        raise OSError("Local AppData is a Windows known folder.")
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    folder = GUID.from_buffer_copy(uuid.UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091").bytes_le)  # FOLDERID_LocalAppData
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_wchar_p)]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    path = ctypes.c_wchar_p()
    if shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(path)) != 0 or not path.value:
        raise OSError("Windows did not report this user's Local AppData folder.")
    try:
        return Path(path.value)
    finally:
        ole32.CoTaskMemFree(path)


def _plain_spelling(text: str) -> str:
    """Drop Windows' extended-length / device prefixes: \\\\?\\C:\\x and \\\\.\\C:\\x are C:\\x."""
    for prefix in ("\\\\?\\UNC\\", "\\\\.\\UNC\\"):
        if text[:len(prefix)].upper() == prefix:
            return "\\\\" + text[len(prefix):]
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _resolved(value: Any) -> tuple[str, Path]:
    """
    (comparison key, resolved path) of a folder or file that may not exist yet.

    The nearest EXISTING ancestor is resolved by Windows itself (its final
    path: junctions, symbolic links, 8.3 short names and drive substitutions),
    then the missing tail is appended. Nothing is created or opened for
    reading. A path that cannot be resolved (for example access denied on
    the way) is refused rather than compared by its spelling.
    """
    try:
        text = _plain_spelling(os.path.expanduser(os.fspath(value)))
        if not text or "\0" in text:
            raise ValueError("empty or invalid path")
        current, tail = os.path.abspath(text), []
        while True:
            try:
                os.lstat(current)
                break
            except FileNotFoundError:
                parent = os.path.dirname(current)
                if parent == current:
                    raise
                tail.append(os.path.basename(current))
                current = parent
        base = _plain_spelling(os.path.realpath(current, strict=True))
        resolved = os.path.join(base, *reversed(tail)) if tail else base
    except (OSError, ValueError) as error:
        raise StartupRefused("store", "Qronos cannot tell which data folder a store path leads to; "
                                      "nothing was opened.") from error
    return os.path.normcase(os.path.normpath(resolved)), Path(os.path.normpath(resolved))


def _require_one_name(resolved: Path) -> None:
    """
    Refuse a store override that is a file with more than one name (an NTFS hard link; Media20 H20-1).

    A hard link keeps its own full name, so resolving the path cannot show that
    the same file also lives in installed data, and an appending or in-place
    store would then write installed data without its admission. A store file
    a source process keeps has one name; telling where another name lives would
    mean listing the file's names across volumes and mount points, so a file
    with several names is refused as undecidable, wherever its names are.
    Bounded: one metadata read of the override itself (Windows' own link
    count), nothing opened, no folder listed. A missing leaf (nothing to link
    yet) and a folder are not affected; metadata that cannot be read refuses.
    """
    import stat

    try:
        info = os.stat(resolved)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as error:
        raise StartupRefused("store", "Qronos cannot read whether a store file has other names; nothing was "
                                      "opened.") from error
    if not stat.S_ISDIR(info.st_mode) and info.st_nlink != 1:
        raise StartupRefused("store", "A store path is a file with more than one name (a hard link), so Qronos "
                                      "cannot tell whether it is installed data; nothing was opened.")


def installed_store_target(environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """
    The installed data root a NON-installed process would write into through a store override, if any.

    Overrides and roots are compared as RESOLVED paths, so a junction, a
    symbolic link, a \\\\?\\ spelling or an 8.3 alias of the installed data
    still counts as the installed data. The roots are the known-folder
    ``<Local AppData>\\Qronos`` (what the desktop uses), LOCALAPPDATA\\Qronos
    and an absolute QRONOS_DATA_ROOT. Overrides leading into two different
    installed roots, a path that cannot be resolved, or an existing store file
    with more than one name (a hard link, ``_require_one_name``) refuse start-up
    (StartupRefused) instead of choosing one.
    """
    environment = os.environ if environ is None else environ
    targets = [environment.get(name, "").strip() for name in STORE_PATH_OVERRIDES]
    targets = [value for value in targets if value]
    if not targets:
        return None  # nothing is redirected: no query, no resolution
    try:
        candidates = [_known_local_appdata() / "Qronos"]
    except OSError as error:
        message = ("Windows did not report this user's Local AppData folder, so Qronos cannot tell whether a store "
                   "path leads into installed data; nothing was opened.")
        raise StartupRefused("store", message) from error
    local = environment.get("LOCALAPPDATA", "").strip()
    candidates.append((Path(local) if local else Path.home() / ".local" / "share") / "Qronos")
    configured = environment.get("QRONOS_DATA_ROOT", "").strip()
    if configured and Path(configured).is_absolute():
        candidates.append(Path(configured))
    roots: dict[str, Path] = {}
    for candidate in candidates:
        key, path = _resolved(candidate)
        roots.setdefault(key, path)
    matched: dict[str, Path] = {}
    for value in targets:
        key, path = _resolved(value)
        _require_one_name(path)
        for root_key, root_path in roots.items():
            if key == root_key or key.startswith(root_key.rstrip(os.sep) + os.sep):
                matched[root_key] = root_path
    if len(matched) > 1:
        raise StartupRefused("store", "Store paths lead into more than one installed Qronos data folder; "
                                      "nothing was opened.")
    return next(iter(matched.values()), None)


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


def pending_on(data_root: Path, version: str) -> dict:
    """
    Re-check, UNDER the shared admission just taken, that no update became pending on this root after the
    barrier ran (Runtime17 O1): {"ok": True} or {"ok": False, "code": ...}; never raises, never restores.

    After a successful barrier there is no launching claim, and a handoff remains only as a repair the barrier
    resolved as unconfirmed (its outcome names that handoff's version and kind). So any claim, or any other
    handoff, appeared in between: an update ran (and ended) before this admission. Refused as
    ``recovery_pending``: the next start runs the barrier again. The update fence itself was already checked by
    ``admit_writer`` under the same shared lock, so it is not asked again here.
    """
    from core.local_update import UpdateError
    from core.update_service import UpdateService

    try:
        status = UpdateService(Path(data_root), version, liveness=lambda: False).status()
    except UpdateError as error:
        return {"ok": False, "code": error.code}
    except (OSError, ValueError, KeyError, TypeError):
        return {"ok": False, "code": "recovery_failed"}
    if status.get("launching"):
        return {"ok": False, "code": "recovery_pending"}
    handoff, outcome = status.get("handoff"), status.get("outcome")
    if handoff is not None:
        resolved = (isinstance(outcome, dict) and isinstance(handoff, dict) and outcome.get("result") == "unconfirmed"
                    and outcome.get("version") == handoff.get("version") and outcome.get("kind") == handoff.get("kind"))
        if not resolved:
            return {"ok": False, "code": "recovery_pending"}
    return {"ok": True}


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
    confirm: Optional[Callable[[str], dict]] = None,
    running_version: Optional[Callable[[], str]] = None,
) -> Any:
    """Run steps 1-4 once for this process; return the held writer admission (or raise StartupRefused)."""
    with _lock:
        # What THIS call asks for (applicability and the RESOLVED data root), decided before anything held is
        # handed back: an earlier admission authorises only the same root and mode (Runtime17 F2).
        applicable, key, root = _binding(environ, data_root, windows, installed)
        if "admission" in _held:
            if _held.get("binding") != (applicable, key):
                raise StartupRefused("store", "This process was already admitted for another Qronos data folder or "
                                              "start-up mode; it must not continue.")
            if instance and "instance" not in _held and not _held.get("notApplicable"):
                raise StartupRefused("ownership_unavailable", "This process was admitted without its instance "
                                                              "ownership; it must not continue.")
            return _held["admission"]
        if not applicable:
            _held["admission"] = None
            _held["notApplicable"] = True
            _held["service"] = service
            _held["binding"] = (False, None)
            return None

        guard = None
        admission = None
        try:
            if instance:
                guard = _acquire(acquire_instance, retry_seconds)
            running = version or _version_of(running_version, environ)
            outcome = _recover(recover or (lambda v: recover_on(root, v)), running)
            if outcome.get("ok") is not True:
                raise StartupRefused(str(outcome.get("code") or "recovery_failed"))
            admission = _admit(admit, root)
            # Under the shared admission no update can start any more; one may have run between the barrier
            # and this admission (Runtime17 O1). Re-read this root's lifecycle and refuse a new pending one.
            confirmed = _recover(confirm or (lambda v: pending_on(root, v)), running)
            if confirmed.get("ok") is not True:
                raise StartupRefused(str(confirmed.get("code") or "recovery_pending"))
        except BaseException:
            if admission is not None:
                _release(admission)
            if guard is not None:
                _close(guard)
            raise
        if guard is not None:
            _held["instance"] = guard
        _held["admission"] = admission
        _held["service"] = service
        _held["binding"] = (True, key)
        return admission


def _binding(environ: Optional[Mapping[str, str]], data_root: Optional[Path], windows: Optional[bool],
             installed: Optional[bool]) -> tuple[bool, Optional[str], Optional[Path]]:
    """(applicable, resolved-root key, root to admit) for one ``enter`` call; StartupRefused when undecidable."""
    on_windows = os.name == "nt" if windows is None else windows
    is_installed = _installed() if installed is None else installed
    if on_windows and not is_installed and data_root is None:
        target = installed_store_target(environ)
        if target is not None:  # a source process opening INSTALLED stores: no bypass
            is_installed, data_root = True, target
    if not on_windows or not is_installed:
        return False, None, None
    root = Path(data_root) if data_root is not None else _default_data_root()
    key, _ = _resolved(root)
    return True, key, root


def _version_of(running_version: Optional[Callable[[], str]], environ: Optional[Mapping[str, str]]) -> str:
    """The product version; a caller that already has the authority function (core.update_cli) passes it."""
    if running_version is None:
        return product_version(environ)
    from core.local_update import UpdateError

    try:
        return running_version()
    except (UpdateError, OSError, ValueError) as error:
        raise StartupRefused("version_unknown") from error


def _release(admission: Any) -> None:
    try:
        admission.release()
    except Exception:  # noqa: BLE001 - the process is refusing anyway; Windows releases the lock at exit
        pass


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


def enter_for_store(service: str, override: str, store_path: Any, *,
                    environ: Optional[Mapping[str, str]] = None, **options: Any) -> Any:
    """
    ``enter`` for the ACTUAL resolved location of one store, right before it is opened.

    The exact path is put in its own override (one of ``STORE_PATH_OVERRIDES``),
    so the whole existing order applies to it: a source process whose store
    leads into installed data is admitted on that root (or refused if it was
    already admitted for something else, Runtime17 F2), a file with more than
    one name is refused (H20-1), and an unresolvable path is refused. For an
    installed binding the store must also lie inside the admitted data root:
    a store elsewhere would be data the update barrier does not cover.
    Returns what ``enter`` returns; raises StartupRefused with nothing opened.
    """
    if override not in STORE_PATH_OVERRIDES:
        raise ValueError(f"{override} is not a store path override")
    environment = dict(os.environ if environ is None else environ)
    environment[override] = os.fspath(store_path)
    admission = enter(service, environ=environment, **options)
    with _lock:
        binding = _held.get("binding")
    if binding and binding[0]:
        key, path = _resolved(store_path)
        _require_one_name(path)
        root_key = str(binding[1])
        if not (key == root_key or key.startswith(root_key.rstrip(os.sep) + os.sep)):
            raise StartupRefused("store", "A store is outside the admitted Qronos data folder; nothing was opened.")
    return admission


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
