"""
Where Qronos keeps user data, and how that choice is read, checked and changed.

The locator (``%LOCALAPPDATA%\\Qronos\\storage_locations.json``) records seven
roots. Three rules govern it:

    Every root is an absolute local path. A relative value such as
    ``"Library"`` used to resolve against whatever directory the process
    happened to start in — the source checkout, an installer folder — and the
    bootstrap then created directories there. Relative, drive-relative,
    UNC/device and control-character values are now invalid, reported, and
    never created.

    Reading is not writing. ``read_storage_state`` only reads. Directories and
    the locator file are created by explicit bootstrap (``create=True``,
    ``ensure_storage_root``) and only for valid roots.

    Stored evidence is never rewritten behind the user's back. An invalid
    value is reported and the default root for that key is used for now, but
    the stored value stays as it was until the user chooses a new folder. A
    corrupt or unrecognised locator is left in place; a locator written by a
    newer Qronos is never written to. Changing a location never moves or
    deletes existing files.

Links are allowed for roots: moving a data folder to another drive with a
junction is a legitimate setup, and storage roots are written only by Qronos
itself. Where a root points is still judged: a root that resolves into a
system or program folder, or into the Qronos program folder, is refused.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from typing import Iterator
import uuid

from core.config import CONFIG
from core.path_safety import (
    drive_status,
    has_control_characters,
    is_absolute_local,
    is_link_or_reparse_point,
    is_network_or_device_path,
    is_within,
)

_LOCK = threading.RLock()

LOCATOR_SCHEMA_VERSION = 1
MAX_SHOWN_VALUE_CHARS = 260
# Bounded waits for the locator lock and for Windows to let a writer replace
# a locator that a lock-free reader has open for an instant. There is no
# "stale lock" age: an OS lock disappears with its owner's handle.
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.02
REPLACE_TIMEOUT_SECONDS = 2.0
READ_RETRY_SECONDS = 1.0

# errno values msvcrt.locking uses for "range already locked".
_LOCK_HELD_ERRNOS = frozenset({errno.EACCES, getattr(errno, "EDEADLK", 36), getattr(errno, "EDEADLOCK", 36)})
# ERROR_ACCESS_DENIED and ERROR_SHARING_VIOLATION while another process has
# the file open.
_WINDOWS_IN_USE_ERRORS = frozenset({5, 32})

# The source tree (development) or PyInstaller's _internal folder (installed).
PROGRAM_ROOT = Path(__file__).resolve().parent.parent

# Files the installer leaves in the application folder. Their presence next to
# the bundled runtime folder proves that folder is the installation, so the
# whole installation is protected, not only the runtime bundle.
INSTALL_MARKERS: tuple[str, ...] = ("uninstall.exe", "qronos-desktop.exe")


def installed_bundle_roots(executable: Path) -> tuple[Path, ...]:
    """
    Folders of an installed runtime: the bundle folder holding the closed
    ``qronos-runtime.exe`` and, when an installer marker proves it, the
    application folder that contains the bundle. A drive root is never taken
    as an installation folder.
    """
    bundle = executable.resolve(strict=False).parent
    roots = [bundle]
    install = bundle.parent
    if install != Path(install.anchor) and any((install / m).is_file() for m in INSTALL_MARKERS):
        roots.append(install)
    return tuple(roots)


def program_roots() -> tuple[Path, ...]:
    """Every folder that holds Qronos code or resources; no user data goes there."""
    roots: list[Path] = [PROGRAM_ROOT, Path(CONFIG.paths.root)]
    if CONFIG.paths.packaged or getattr(sys, "frozen", False):
        roots.extend(installed_bundle_roots(Path(sys.executable)))
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return tuple(unique)

LOCATION_KEYS: tuple[str, ...] = (
    "dataRoot",
    "historyRoot",
    "memoryRoot",
    "libraryDbRoot",
    "artifactRoot",
    "cacheRoot",
    "backupRoot",
)

LOCATION_LABELS: dict[str, str] = {
    "dataRoot": "Qronos data",
    "historyRoot": "Conversation history",
    "memoryRoot": "Memory",
    "libraryDbRoot": "Library database",
    "artifactRoot": "Library files",
    "cacheRoot": "Cache",
    "backupRoot": "Backups",
}

_FIELDS: dict[str, str] = {
    "dataRoot": "data_root",
    "historyRoot": "history_root",
    "memoryRoot": "memory_root",
    "libraryDbRoot": "library_db_root",
    "artifactRoot": "artifact_root",
    "cacheRoot": "cache_root",
    "backupRoot": "backup_root",
}

_REASONS: dict[str, str] = {
    "not_text": "the stored value is not text",
    "empty": "the stored value is empty",
    "invalid_characters": "the path contains control characters",
    "network_or_device": "network shares and device paths are not supported",
    "relative": "the path is not an absolute local folder",
    "not_directory": "the path exists but is not a folder",
    "protected": "the path is inside a system, program or Qronos program folder",
    "overlaps_state": "the Library files folder must not contain or sit inside Qronos data folders",
    "contains_program": "the folder contains the Qronos program folder",
    "network_drive": "the folder is on a network drive; Qronos keeps personal data on local drives",
    "drive_unknown": "Windows could not confirm that the folder is on a local drive",
}

# Ordered default folders inside Documents. The first is the established
# layout; the second is used only when the first is unusable, most often
# because Documents\Qronos is itself a Qronos program checkout.
DOCUMENTS_FOLDERS: tuple[str, ...] = ("Qronos", "Qronos Files")


class StorageLocationError(ValueError):
    """A storage location is invalid, or the locator cannot be changed safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StorageLocations:
    data_root: str
    history_root: str
    memory_root: str
    library_db_root: str
    artifact_root: str
    cache_root: str
    backup_root: str

    def get(self, key: str) -> str:
        return str(getattr(self, _FIELDS[key]))

    def to_public_dict(self) -> dict[str, str]:
        return {key: self.get(key) for key in LOCATION_KEYS}


@dataclass(frozen=True)
class LocationProblem:
    key: str
    code: str
    message: str
    stored: str = ""
    # "warning": something the user chose or stored is not used.
    # "info": Qronos chose a different default and says which and why.
    severity: str = "warning"

    def public(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": LOCATION_LABELS.get(self.key, "Storage locator"),
            "code": self.code,
            "message": self.message,
            "stored": self.stored,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class StorageState:
    locations: StorageLocations
    locator_path: Path
    # "missing" (defaults, nothing written yet), "ok", "corrupt", "newer".
    locator_status: str
    problems: tuple[LocationProblem, ...] = ()

    @property
    def writable(self) -> bool:
        """Whether an explicit location change may write the locator."""
        return self.locator_status != "newer"

    def public(self) -> dict[str, object]:
        return {
            "locatorStatus": self.locator_status,
            "locatorPath": str(self.locator_path),
            "writable": self.writable,
            "problems": [problem.public() for problem in self.problems],
        }


# ---------------------------------------------------------------- profile


def _profile_error() -> StorageLocationError:
    return StorageLocationError(
        "profile_unavailable",
        "Qronos cannot find an absolute local user profile folder "
        "(LOCALAPPDATA/USERPROFILE), so it will not choose storage locations.",
    )


def _home() -> Path:
    home = Path.home()
    if not is_absolute_local(str(home)) or is_network_or_device_path(str(home)):
        raise _profile_error()
    return home


def _local_app_data() -> Path:
    raw = os.environ.get("LOCALAPPDATA", "").strip()
    if raw:
        if is_absolute_local(raw) and not is_network_or_device_path(raw):
            return Path(os.path.normpath(raw))
        raise _profile_error()
    return _home() / ".qronos"


def _documents() -> Path:
    return _home() / "Documents"


def _state_root() -> Path:
    """
    The folder for Qronos's own state (locator, default data roots).

    Installed builds use the per-user data folder chosen by the packaged
    configuration (``CONFIG.paths.data``), never the bundle or the cwd;
    development uses ``%LOCALAPPDATA%\\Qronos``. Either way the folder must
    pass the same policy as any data root, or nothing is written.
    """
    candidate = CONFIG.paths.data if CONFIG.paths.packaged else _local_app_data() / "Qronos"
    try:
        return validate_location("dataRoot", str(candidate))
    except StorageLocationError as exc:
        where = "installed Qronos data folder" if CONFIG.paths.packaged else "Qronos profile folder"
        raise StorageLocationError(
            "profile_unavailable",
            f"The {where} is not usable: {_REASONS.get(exc.code, exc.code)}. "
            "Qronos will not write state there.",
        ) from None


def locator_path() -> Path:
    return _state_root() / "storage_locations.json"


def _default_candidates() -> dict[str, tuple[Path, ...]]:
    local = _state_root()
    documents = _documents()
    return {
        "dataRoot": (local,),
        "historyRoot": (local / "History",),
        "memoryRoot": (local / "Memory",),
        "libraryDbRoot": (local / "Library",),
        "artifactRoot": tuple(documents / name / "Library" for name in DOCUMENTS_FOLDERS),
        "cacheRoot": (local / "Cache",),
        "backupRoot": tuple(documents / name / "Backups" for name in DOCUMENTS_FOLDERS),
    }


def _default_state() -> tuple[StorageLocations, tuple[LocationProblem, ...]]:
    """
    Defaults pass the same validation as stored values.

    An unusable default is never used: the next candidate is taken and an
    "info" note says which and why, or, if none is usable, the root is left
    empty (unavailable) with a warning. Nothing here writes to disk.
    """
    values: dict[str, str] = {}
    notes: list[LocationProblem] = []
    for key, options in _default_candidates().items():
        label = LOCATION_LABELS[key]
        first_error: StorageLocationError | None = None
        chosen = ""
        for index, option in enumerate(options):
            try:
                chosen = str(validate_location(key, str(option)))
            except StorageLocationError as exc:
                first_error = first_error or exc
                continue
            if index and first_error is not None:
                notes.append(
                    LocationProblem(
                        key=key,
                        code="default_relocated",
                        severity="info",
                        stored=_shown(str(options[0])),
                        message=(
                            f"The usual {label} folder cannot be used: "
                            f"{_REASONS.get(first_error.code, first_error.code)}. "
                            f"Qronos uses {chosen} instead. Nothing was moved; "
                            "you can choose another folder at any time."
                        ),
                    )
                )
            break
        if not chosen and first_error is not None:
            notes.append(
                LocationProblem(
                    key=key,
                    code="no_safe_default",
                    stored=_shown(str(options[0])),
                    message=(
                        f"{label} has no usable default folder: "
                        f"{_REASONS.get(first_error.code, first_error.code)}. "
                        f"Choose a folder for {label}; until then it is unavailable."
                    ),
                )
            )
        values[key] = chosen
    return StorageLocations(**{_FIELDS[k]: values[k] for k in LOCATION_KEYS}), tuple(notes)


def _defaults() -> StorageLocations:
    return _default_state()[0]


# ------------------------------------------------------------- validation


def _shown(value: object) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = "".join("?" if ord(ch) < 32 or ch == "\x7f" else ch for ch in text)
    if len(text) > MAX_SHOWN_VALUE_CHARS:
        text = text[: MAX_SHOWN_VALUE_CHARS - 1] + "…"
    return text


def _protected_roots(key: str) -> tuple[Path, ...]:
    names = ["SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"]
    if key == "artifactRoot":
        # Library files are acted on by Library operations; they must never
        # sit among application state or the Startup folder.
        names += ["APPDATA", "LOCALAPPDATA"]
    roots = list(program_roots())
    for name in names:
        raw = os.environ.get(name, "").strip()
        if raw and is_absolute_local(raw) and not is_network_or_device_path(raw):
            roots.append(Path(os.path.normpath(raw)))
    return tuple(roots)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path


def validate_location(key: str, value: object) -> Path:
    """
    Check one root. Returns the normalised absolute path or raises
    StorageLocationError with a stable ``code``. Never touches the disk except
    to inspect an existing path.
    """
    if key not in _FIELDS:
        raise StorageLocationError("unknown_key", f"Unsupported storage location key: {_shown(key)}")
    if not isinstance(value, str):
        raise StorageLocationError("not_text", _REASONS["not_text"])
    text = value.strip()
    if not text:
        raise StorageLocationError("empty", _REASONS["empty"])
    if has_control_characters(text):
        raise StorageLocationError("invalid_characters", _REASONS["invalid_characters"])
    if is_network_or_device_path(text):
        raise StorageLocationError("network_or_device", _REASONS["network_or_device"])
    if not is_absolute_local(text):
        raise StorageLocationError("relative", _REASONS["relative"])
    drive = drive_status(text)
    if drive == "remote":
        raise StorageLocationError("network_drive", _REASONS["network_drive"])
    if drive == "unknown":
        raise StorageLocationError("drive_unknown", _REASONS["drive_unknown"])

    path = Path(os.path.normpath(text))
    # The folder, or the nearest part of its path that already exists, must be
    # a directory; otherwise creating it later is bound to fail.
    existing = next((p for p in (path, *path.parents) if p.exists()), None)
    if existing is not None and not existing.is_dir():
        raise StorageLocationError("not_directory", _REASONS["not_directory"])

    # Judge both the path as written and, for an existing link, where it goes.
    candidates = {path, _resolved(path)}
    for root in _protected_roots(key):
        for candidate in candidates:
            if is_within(candidate, root) or is_within(candidate, _resolved(root)):
                raise StorageLocationError("protected", _REASONS["protected"])
            # Library files are registered and acted on by the Library, so
            # that root must not contain program or application folders.
            if key == "artifactRoot" and (
                is_within(root, candidate) or is_within(_resolved(root), candidate)
            ):
                raise StorageLocationError("contains_program", _REASONS["contains_program"])
    return path


def _problem(key: str, code: str, stored: object, fallback: str) -> LocationProblem:
    label = LOCATION_LABELS.get(key, "Storage")
    reason = _REASONS.get(code, code)
    now = (
        "Qronos is using the default folder for now"
        if fallback
        else "There is no usable default, so it is unavailable for now"
    )
    return LocationProblem(
        key=key,
        code=code,
        message=(
            f"{label} location is not usable: {reason}. {now} and kept the "
            f"stored setting unchanged. Choose a new folder for {label} to fix this."
        ),
        stored=_shown(stored),
    )


def _locator_problem(code: str) -> LocationProblem:
    detail = {
        "corrupt": "the storage locator file is unreadable or not valid JSON",
        "newer": "the storage locator was written by a newer version of Qronos",
    }[code]
    action = (
        "Choosing a new folder keeps a copy of the unreadable file next to it."
        if code == "corrupt"
        else "Update Qronos to change storage locations."
    )
    return LocationProblem(
        key="locator",
        code=f"locator_{code}",
        message=(
            f"Storage locations could not be read: {detail}. Qronos is using "
            f"the default folders for now and left the file unchanged. {action}"
        ),
    )


def _read_locator_text(path: Path) -> str:
    """
    Read the locator without the lock. On Windows the name is briefly
    inaccessible while a writer replaces it; that instant is waited out
    within READ_RETRY_SECONDS instead of being misreported as a corrupt
    locator (which would send this process to the default roots).
    """
    deadline = time.monotonic() + READ_RETRY_SECONDS
    while True:
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(LOCK_POLL_SECONDS)


def _read_locator(path: Path) -> tuple[str, dict[str, object]]:
    """("missing"|"ok"|"corrupt"|"newer", raw dict). Never writes."""
    if not path.exists():
        return "missing", {}
    try:
        raw = json.loads(_read_locator_text(path))
    except FileNotFoundError:
        return "missing", {}
    except (OSError, UnicodeDecodeError, ValueError):
        return "corrupt", {}
    if not isinstance(raw, dict):
        return "corrupt", {}
    version = raw.get("schemaVersion", LOCATOR_SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return "corrupt", {}
    if version > LOCATOR_SCHEMA_VERSION:
        return "newer", {}
    return "ok", raw


def read_storage_state() -> StorageState:
    """Effective locations plus diagnostics. Read-only."""
    with _LOCK:
        defaults, default_notes = _default_state()
        path = locator_path()
        status, raw = _read_locator(path)

        if status in {"corrupt", "newer"}:
            return StorageState(defaults, path, status, (_locator_problem(status), *default_notes))

        values = defaults.to_public_dict()
        problems: list[LocationProblem] = []
        chosen: set[str] = set()
        for key in LOCATION_KEYS:
            if key not in raw:
                continue
            try:
                values[key] = str(validate_location(key, raw[key]))
                chosen.add(key)
            except StorageLocationError as exc:
                problems.append(_problem(key, exc.code, raw[key], defaults.get(key)))

        locations = StorageLocations(**{_FIELDS[k]: values[k] for k in LOCATION_KEYS})
        if "artifactRoot" in raw and not any(p.key == "artifactRoot" for p in problems):
            try:
                _check_overlap("artifactRoot", Path(locations.artifact_root), locations)
            except StorageLocationError as exc:
                problems.append(
                    _problem("artifactRoot", exc.code, raw.get("artifactRoot", ""), defaults.artifact_root)
                )
                chosen.discard("artifactRoot")
                values["artifactRoot"] = defaults.artifact_root
                locations = StorageLocations(**{_FIELDS[k]: values[k] for k in LOCATION_KEYS})
        # A default note matters only for a root that actually uses the default.
        notes = tuple(n for n in default_notes if n.key not in chosen)
        return StorageState(locations, path, status, (*problems, *notes))


# --------------------------------------------------------------- bootstrap


def _usable(locations: StorageLocations) -> dict[str, str]:
    """Roots worth persisting: an unavailable (empty) root is never written."""
    return {key: value for key, value in locations.to_public_dict().items() if value}


def _mkdir_root(raw: str) -> Path:
    if not raw:
        raise StorageLocationError(
            "unavailable", "This storage location has no usable folder; choose one first."
        )
    if not is_absolute_local(raw) or is_network_or_device_path(raw):
        # Belt and braces: effective locations are validated, but nothing in
        # this module may ever create a directory relative to the cwd.
        raise StorageLocationError("relative", _REASONS["relative"])
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_storage_root(key: str, state: StorageState | None = None) -> Path:
    """Create (explicit bootstrap) and return the one root a caller needs."""
    if key not in _FIELDS:
        raise StorageLocationError("unknown_key", f"Unsupported storage location key: {_shown(key)}")
    state = state or read_storage_state()
    return _mkdir_root(state.locations.get(key))


def ensure_storage_directories(locations: StorageLocations | None = None) -> None:
    locations = locations or read_storage_state().locations
    for key in LOCATION_KEYS:
        # An unavailable root (no usable default) is reported, never created.
        if key != "dataRoot" and locations.get(key):
            _mkdir_root(locations.get(key))


def load_storage_locations(*, create: bool = False) -> StorageLocations:
    """
    Effective locations. ``create=True`` is explicit bootstrap: it writes the
    locator only if none exists and creates the (valid) roots.
    """
    with _LOCK:
        state = read_storage_state()
        if create:
            if state.locator_status == "missing":
                with _locator_lock(state.locator_path):
                    if not state.locator_path.exists():
                        _write_locator(
                            state.locator_path,
                            {"schemaVersion": LOCATOR_SCHEMA_VERSION, **_usable(state.locations)},
                        )
            ensure_storage_directories(state.locations)
        return state.locations


# ------------------------------------------------------------------ change


def locator_lock_path(path: Path) -> Path:
    """
    The persistent lock file next to the locator.

    It is created once and intentionally never deleted: exclusion comes from
    an operating-system lock held on its open handle, not from the file's
    existence. Leaving an empty file behind is harmless; it holds no data.
    """
    return path.with_name(path.name + ".lock")


def _open_lock_file(lock: Path) -> int:
    """
    Open (creating if needed) the lock file, refusing anything that is not an
    ordinary file: a link or junction, a folder, or a device. Errors are
    reported, never retried: they are not contention.
    """
    invalid = StorageLocationError(
        "lock_invalid",
        "The storage lock file is a link, folder or device, so Qronos will not "
        "use it and changed nothing.",
    )
    try:
        before = os.lstat(lock)
    except FileNotFoundError:
        before = None
    except OSError as exc:
        raise StorageLocationError(
            "lock_unavailable", "The storage lock file could not be inspected; nothing was changed."
        ) from exc
    if before is not None and (is_link_or_reparse_point(lock) or not stat.S_ISREG(before.st_mode)):
        raise invalid

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock, flags, 0o600)
    except PermissionError as exc:
        raise StorageLocationError(
            "lock_denied",
            "Qronos is not allowed to open the storage lock file; nothing was changed.",
        ) from exc
    except OSError as exc:
        raise StorageLocationError(
            "lock_unavailable", "The storage lock file could not be opened; nothing was changed."
        ) from exc

    # The name must still be the ordinary file that was opened (not swapped
    # for a link between the check and the open).
    try:
        opened = os.fstat(fd)
        after = os.lstat(lock)
        if (
            not stat.S_ISREG(opened.st_mode)
            or is_link_or_reparse_point(lock)
            or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise invalid
    except BaseException as exc:
        os.close(fd)
        if isinstance(exc, OSError):
            raise StorageLocationError(
                "lock_unavailable", "The storage lock file could not be inspected; nothing was changed."
            ) from exc
        raise
    return fd


def _try_lock(fd: int) -> bool:
    """One non-blocking attempt. False means another owner holds the lock."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            # LockFile reports a held range as EACCES (lock violation).
            if exc.errno in _LOCK_HELD_ERRNOS:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _locator_lock(path: Path) -> Iterator[None]:
    """
    Cross-process exclusion for read-modify-write of the locator.

    The exclusion is an operating-system advisory lock (LockFile through
    msvcrt on Windows, flock on POSIX) on a persistent lock file. The old
    scheme created and deleted the lock file on every acquisition; on Windows
    a concurrent CREATE_NEW then intermittently fails with ACCESS_DENIED
    while the name is mid-create or mid-delete, which escaped as an unhandled
    PermissionError. With an OS lock:

    - an owner that exits or is killed releases the lock with its handle, so
      nothing ever needs to be declared stale and an active owner, however
      slow, is never robbed;
    - a lock belongs to the handle that took it, so an old owner can only
      release its own lock, never a successor's;
    - waiting is bounded by LOCK_TIMEOUT_SECONDS and ends in a truthful
      "busy"; permission, invalid-path and other errors are reported at once.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = _open_lock_file(locator_lock_path(path))
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                acquired = _try_lock(fd)
            except OSError as exc:
                raise StorageLocationError(
                    "lock_unavailable", "The storage lock could not be taken; nothing was changed."
                ) from exc
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise StorageLocationError(
                    "busy",
                    "Storage locations are being changed by another Qronos process; "
                    "nothing was changed. Try again.",
                )
            time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            try:
                _unlock(fd)
            except OSError:
                pass  # closing the handle below releases the lock regardless
    finally:
        os.close(fd)


def _replace_locator(temporary: Path, path: Path) -> None:
    """
    Atomically replace the locator, under the locator lock.

    Writers are serialised by the lock, but readers are lock-free, and Windows
    refuses to replace a file that any process has open (ACCESS_DENIED or
    SHARING_VIOLATION), even for the instant a reader takes to read it. Only
    that Windows condition is waited out, within REPLACE_TIMEOUT_SECONDS;
    anything else, or a denial that outlasts the bound, is reported.
    """
    deadline = time.monotonic() + REPLACE_TIMEOUT_SECONDS
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            transient = os.name == "nt" and getattr(exc, "winerror", None) in _WINDOWS_IN_USE_ERRORS
            if not transient or time.monotonic() >= deadline:
                raise StorageLocationError(
                    "locator_write_failed",
                    "The storage locator could not be replaced (access denied); nothing was changed.",
                ) from exc
            time.sleep(LOCK_POLL_SECONDS)
        except OSError as exc:
            raise StorageLocationError(
                "locator_write_failed", "The storage locator could not be replaced; nothing was changed."
            ) from exc


def _write_locator(path: Path, document: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_locator(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _check_overlap(key: str, path: Path, locations: StorageLocations) -> None:
    """
    Library files must not contain, or sit inside, any other root.

    Library operations act on files under the artifact root, and the Library
    refuses to act inside Qronos state and backup folders; overlapping them
    would either expose state to Library operations or make the Library
    unusable.
    """
    if key == "artifactRoot":
        others = [Path(locations.get(k)) for k in LOCATION_KEYS if k != key]
    else:
        others = [Path(locations.artifact_root)]
    for other in others:
        if not str(other) or not other.is_absolute():
            continue
        if is_within(other, path) or is_within(path, other):
            raise StorageLocationError("overlaps_state", _REASONS["overlaps_state"])


def _prepare_destination(path: Path) -> None:
    """Create only the final folder of an explicit choice, then prove it is writable."""
    if not path.exists():
        parent = path.parent
        if not parent.is_dir():
            raise StorageLocationError(
                "parent_missing", "The parent of the chosen folder does not exist."
            )
        path.mkdir()
    fd, probe = tempfile.mkstemp(prefix=".qronos-write-test-", dir=path)
    os.close(fd)
    os.unlink(probe)


def change_storage_location(key: str, destination: object) -> dict[str, object]:
    """
    Explicit user change of one root. Existing files are not moved or deleted.

    Returns the previous and current folder and the new diagnostics.
    """
    path = validate_location(key, destination)
    with _LOCK:
        state = read_storage_state()
        if not state.writable:
            raise StorageLocationError(
                "locator_newer",
                "Storage locations were written by a newer version of Qronos and "
                "will not be changed by this version.",
            )
        _check_overlap(key, path, state.locations)
        _prepare_destination(path)

        preserved = ""
        with _locator_lock(state.locator_path):
            status, raw = _read_locator(state.locator_path)
            if status == "newer":
                raise StorageLocationError(
                    "locator_newer",
                    "Storage locations were written by a newer version of Qronos.",
                )
            if status == "corrupt":
                # Keep the evidence: move it aside under a unique name, never
                # overwrite it. Then start from the effective (default) roots.
                stamp = time.strftime("%Y%m%dT%H%M%S")
                aside = state.locator_path.with_name(
                    f"{state.locator_path.name}.corrupt-{stamp}-{uuid.uuid4().hex[:6]}"
                )
                os.replace(state.locator_path, aside)
                preserved = aside.name
                raw = {}
            document: dict[str, object] = dict(raw)
            if status != "ok":
                document.update(_usable(state.locations))
            document["schemaVersion"] = LOCATOR_SCHEMA_VERSION
            document[key] = str(path)
            _write_locator(state.locator_path, document)

    _audit_change(key)
    after = read_storage_state()
    return {
        "key": key,
        "previous": state.locations.get(key),
        "current": after.locations.get(key),
        "moved": False,
        "message": (
            f"{LOCATION_LABELS[key]} now uses the chosen folder. Existing files in "
            "the previous folder were not moved or deleted."
        ),
        "preservedLocator": preserved,
        "storage": after.public(),
    }


def set_storage_location(key: str, destination: str) -> StorageLocations:
    change_storage_location(key, destination)
    return read_storage_state().locations


def _audit_change(key: str) -> None:
    """Record that a location changed: which key, never the path."""
    try:
        from core.append_only_log import AppendOnlyLog

        override = os.environ.get("QRONOS_PERMISSION_AUDIT_PATH", "").strip()
        if override:
            target = Path(override)
        else:
            from core.action_audit import DEFAULT_ACTION_AUDIT_PATH

            target = Path(DEFAULT_ACTION_AUDIT_PATH)
        AppendOnlyLog(target).append(
            {
                "schemaVersion": 1,
                "at": round(time.time(), 3),
                "event": "storage_location_changed",
                "actionId": f"storage-{uuid.uuid4().hex}",
                "category": "storage",
                "target": key,
                "summary": f"{LOCATION_LABELS[key]} location was changed by the user.",
                "outcome": "succeeded",
                "decision": "",
            }
        )
    except Exception:
        pass


__all__ = [
    "LOCATION_KEYS",
    "LOCATION_LABELS",
    "LocationProblem",
    "StorageLocationError",
    "StorageLocations",
    "StorageState",
    "change_storage_location",
    "ensure_storage_directories",
    "ensure_storage_root",
    "load_storage_locations",
    "locator_path",
    "read_storage_state",
    "set_storage_location",
    "validate_location",
]
