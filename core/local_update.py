"""Authenticated local update packages: verify, stage, hand off, confirm, roll back.

Qronos never fetches updates. A person obtains an update through the visible,
approved browser or from removable media, then chooses two local files:

* the installer (``qronos-desktop_<version>_x64-setup.exe``), and
* its signed manifest envelope (``<installer>.qronos-update.json``).

This module checks them entirely offline:

* The envelope's payload must carry a valid Ed25519 signature (RFC 8032, see
  ``core.ed25519``) by a key listed in ``config/update_trusted_keys.json``. That
  file holds only OWNER-provided public keys (one reviewed owner key since
  2026-10-09); with no key configured, every package is refused. Unknown and
  revoked keys are refused.
* The signed payload binds the product, version, architecture and build target,
  and the installer's file size and SHA-256; the chosen installer must match.
* Version policy, against the installed version and the recorded security floor:
  upgrades must respect the package's minimum starting version; reinstalling the
  same version is a repair; going back requires a package signed explicitly as a
  rollback, at or above the security floor, that can read the installed data.
* Data compatibility. Payload schema 2 (the only one the product accepts) names,
  per persistent store, the format version the package writes and the range it
  reads, plus a compatibility epoch; it is checked against the running build's
  ``DataProfile`` (``core.update_stores``). Every installed store must be listed
  and readable, an upgrade never lowers a store's format, and the epoch must
  match. Schema 1 (a single ``dataSchema`` integer) is kept for library callers
  only; it cannot describe Qronos's separately versioned stores.

``LocalUpdateStore`` keeps a small state file and copies of verified packages in
its own folder (``<data root>/Updates``). It deletes only files it created there,
by recorded name, and never touches other user data. Launching the staged
installer is a separate, user-approved step outside this module.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from core import ed25519

PRODUCT = "com.qronos.agent"
ARCHITECTURE = "x64"
TARGET = "x86_64-pc-windows-msvc"
TRUSTED_KEYS_PATH = Path(__file__).resolve().parent.parent / "config" / "update_trusted_keys.json"
MANIFEST_SUFFIX = ".qronos-update.json"

_MAX_ENVELOPE_BYTES = 64 * 1024
_MAX_PAYLOAD_BYTES = 32 * 1024
_MAX_SIGNATURES = 8
_MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
_READ_CHUNK = 4 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEY_ID = re.compile(r"[0-9a-f]{32}")
_PACKAGE_NAME = re.compile(r"qronos-desktop_[0-9A-Za-z.+-]{1,64}_x64-setup\.exe")
_NUMERIC = r"(?:0|[1-9][0-9]*)"
_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER = re.compile(rf"({_NUMERIC})\.({_NUMERIC})\.({_NUMERIC})(?:-({_IDENTIFIER}(?:\.{_IDENTIFIER})*))?"
                     r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", re.ASCII)

_ENVELOPE_FIELDS = frozenset({"schema", "payload", "signatures"})
_SIGNATURE_FIELDS = frozenset({"keyId", "signature"})
_PAYLOAD_FIELDS = frozenset({"schema", "product", "version", "architecture", "target", "package", "dataSchema",
                             "minimumVersion", "securityFloor", "rollback", "notes"})
_PAYLOAD_FIELDS_2 = (_PAYLOAD_FIELDS - {"dataSchema"}) | {"dataStores", "compatibilityEpoch"}
_STORE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,47}")
_MAX_STORES = 64
_PACKAGE_FIELDS = frozenset({"fileName", "bytes", "sha256"})
_DATA_SCHEMA_FIELDS = frozenset({"writes", "reads"})
_KEY_FIELDS = frozenset({"keyId", "publicKey", "owner", "revoked"})


class UpdateError(ValueError):
    """An update package was refused. ``code`` names the rule for the UI and the audit trail."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    public_key: bytes
    owner: str
    revoked: bool


@dataclass(frozen=True)
class DataProfile:
    """The data formats a running build reads and writes: {store id: format version}, and its epoch."""

    stores: dict[str, int]
    epoch: int


@dataclass(frozen=True)
class InstalledState:
    """What the running Qronos knows about itself, plus the recorded security floor.

    ``data_schema`` is a DataProfile for signed payload schema 2 (the product), or a
    single integer for schema 1 (library callers); a mismatch is refused.
    """

    version: str
    data_schema: int | DataProfile
    security_floor: str | None = None


@dataclass(frozen=True)
class VerifiedUpdate:
    kind: str                # "upgrade", "repair" or "rollback"
    version: str
    package_sha256: str
    package_bytes: int
    package_name: str
    key_id: str
    payload: dict[str, Any]
    payload_bytes: bytes


# ------------------------------------------------------------- versions


def parse_version(value: Any) -> tuple[tuple[int, int, int], tuple[Any, ...] | None]:
    match = _SEMVER.fullmatch(value) if isinstance(value, str) else None
    if match is None or len(value) > 128:
        raise UpdateError("invalid_version", f"{value!r} is not a semantic version.")
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if match.group(4) is None:
        return core, None
    parts = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in match.group(4).split("."))
    return core, parts


def compare_versions(left: str, right: str) -> int:
    """Semantic Versioning 2.0 precedence; build metadata is ignored."""
    (core_l, pre_l), (core_r, pre_r) = parse_version(left), parse_version(right)
    if core_l != core_r:
        return -1 if core_l < core_r else 1
    if pre_l == pre_r:
        return 0
    if pre_l is None:
        return 1
    if pre_r is None:
        return -1
    return -1 if pre_l < pre_r else 1


# ---------------------------------------------------------- trusted keys


def key_id(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()[:32]


def _b64(value: Any, length: int | None, what: str) -> bytes:
    if not isinstance(value, str):
        raise UpdateError("malformed", f"{what} must be base64 text.")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise UpdateError("malformed", f"{what} is not valid base64.") from error
    if length is not None and len(data) != length:
        raise UpdateError("malformed", f"{what} must be {length} bytes.")
    return data


def load_trusted_keys(path: Path = TRUSTED_KEYS_PATH) -> dict[str, TrustedKey]:
    """Owner-provided public keys. A missing file means no key, so nothing is accepted."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("keys"), list):
        raise UpdateError("key_file", "The trusted update key file needs schema 1 and a keys list.")
    keys: dict[str, TrustedKey] = {}
    for entry in data["keys"]:
        if not isinstance(entry, dict) or set(entry) != _KEY_FIELDS or not isinstance(entry["revoked"], bool) \
                or not isinstance(entry["owner"], str) or not entry["owner"]:
            raise UpdateError("key_file", f"Each trusted key needs exactly {sorted(_KEY_FIELDS)}.")
        try:
            public = _b64(entry["publicKey"], ed25519.PUBLIC_KEY_BYTES, "A trusted public key")
        except UpdateError as error:
            raise UpdateError("key_file", str(error)) from error
        if entry["keyId"] != key_id(public):
            raise UpdateError("key_file", f"Trusted key {entry['keyId']!r} does not match its public key.")
        if entry["keyId"] in keys:
            raise UpdateError("key_file", f"Trusted key {entry['keyId']!r} is listed twice.")
        try:
            ed25519.check_public_key(public)  # canonical, prime-order subgroup: no weak/identity keys
        except ed25519.SignatureError as error:
            raise UpdateError("key_file", f"Trusted key {entry['keyId']!r} is not an acceptable Ed25519 key: "
                                          f"{error}") from error
        keys[entry["keyId"]] = TrustedKey(entry["keyId"], public, entry["owner"], entry["revoked"])
    return keys


# -------------------------------------------------------------- envelope


def _exact(value: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise UpdateError("malformed", f"{what} must have exactly the fields {sorted(fields)}.")
    return value


def build_envelope(payload: dict[str, Any], secrets: list[bytes]) -> bytes:
    """Owner tooling and test fixtures: sign a payload with one or more Ed25519 private keys."""
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signatures = [{"keyId": key_id(ed25519.public_key(secret)),
                   "signature": base64.b64encode(ed25519.sign(secret, payload_bytes)).decode("ascii")}
                  for secret in secrets]
    return json.dumps({"schema": 1, "payload": base64.b64encode(payload_bytes).decode("ascii"),
                       "signatures": signatures}, indent=1).encode("utf-8")


def _authenticate(envelope: bytes, keys: dict[str, TrustedKey]) -> tuple[bytes, str]:
    if not keys:
        raise UpdateError("no_trusted_key", "No owner update key is configured, so no update package can be "
                                            "accepted.")
    if len(envelope) > _MAX_ENVELOPE_BYTES:
        raise UpdateError("malformed", "The update manifest is too large.")
    try:
        data = json.loads(envelope.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError("malformed", "The update manifest is not JSON.") from error
    data = _exact(data, _ENVELOPE_FIELDS, "The update manifest")
    if data["schema"] != 1 or not isinstance(data["signatures"], list) \
            or not 0 < len(data["signatures"]) <= _MAX_SIGNATURES:
        raise UpdateError("malformed", "The update manifest needs schema 1 and one to eight signatures.")
    payload = _b64(data["payload"], None, "The signed payload")
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise UpdateError("malformed", "The signed payload is too large.")
    revoked = False
    for entry in data["signatures"]:
        entry = _exact(entry, _SIGNATURE_FIELDS, "A signature")
        if not isinstance(entry["keyId"], str) or not _KEY_ID.fullmatch(entry["keyId"]):
            raise UpdateError("malformed", "A signature names an invalid key id.")
        signature = _b64(entry["signature"], ed25519.SIGNATURE_BYTES, "A signature")
        key = keys.get(entry["keyId"])
        if key is None:
            continue
        try:
            valid = ed25519.verify(key.public_key, payload, signature)
        except ed25519.SignatureError as error:  # a weak key that reached this map some other way
            raise UpdateError("weak_key", f"Trusted key {key.key_id!r} is not an acceptable Ed25519 key.") from error
        if not valid:
            continue
        if key.revoked:
            revoked = True
            continue
        return payload, key.key_id
    if revoked:
        raise UpdateError("revoked_key", "The update is signed only by a revoked owner key.")
    raise UpdateError("untrusted_signature", "The update is not signed by a trusted owner key.")


def _payload(payload_bytes: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError("malformed", "The signed payload is not JSON.") from error
    schema_number = payload.get("schema") if isinstance(payload, dict) else None
    if type(schema_number) is not int or schema_number not in (1, 2):
        raise UpdateError("malformed", "The signed payload must be schema 1 or 2.")
    payload = _exact(payload, _PAYLOAD_FIELDS if schema_number == 1 else _PAYLOAD_FIELDS_2, "The signed payload")
    if payload["product"] != PRODUCT:
        raise UpdateError("wrong_product", f"The update is for {payload['product']!r}, not Qronos.")
    if payload["architecture"] != ARCHITECTURE or payload["target"] != TARGET:
        raise UpdateError("wrong_platform", f"The update is for {payload['architecture']!r}/{payload['target']!r}, "
                                            f"not {ARCHITECTURE}/{TARGET}.")
    parse_version(payload["version"])
    parse_version(payload["securityFloor"])
    if payload["minimumVersion"] is not None:
        parse_version(payload["minimumVersion"])
    if not isinstance(payload["rollback"], bool) or not isinstance(payload["notes"], str):
        raise UpdateError("malformed", "rollback must be true or false and notes must be text.")
    package = _exact(payload["package"], _PACKAGE_FIELDS, "The signed package description")
    if not isinstance(package["fileName"], str) or not _PACKAGE_NAME.fullmatch(package["fileName"]) \
            or not isinstance(package["bytes"], int) or isinstance(package["bytes"], bool) \
            or not 0 < package["bytes"] <= _MAX_PACKAGE_BYTES \
            or not isinstance(package["sha256"], str) or not _SHA256.fullmatch(package["sha256"]):
        raise UpdateError("malformed", "The signed package needs a Qronos installer name, a size and a SHA-256.")
    if schema_number == 1:
        _check_format(payload["dataSchema"], "dataSchema")
        return payload
    stores = payload["dataStores"]
    if not isinstance(stores, dict) or not 0 < len(stores) <= _MAX_STORES:
        raise UpdateError("malformed", f"dataStores must name one to {_MAX_STORES} stores.")
    for store_id, entry in stores.items():
        if not _STORE_ID.fullmatch(store_id):
            raise UpdateError("malformed", f"dataStores names an invalid store id {store_id!r}.")
        _check_format(entry, f"dataStores[{store_id!r}]")
    epoch = payload["compatibilityEpoch"]
    if type(epoch) is not int or not 1 <= epoch <= 1_000_000:
        raise UpdateError("malformed", "compatibilityEpoch must be a positive integer.")
    return payload


def _check_format(entry: Any, what: str) -> None:
    schema = _exact(entry, _DATA_SCHEMA_FIELDS, what)
    reads = schema["reads"]
    if type(schema["writes"]) is not int or schema["writes"] < 1 \
            or not isinstance(reads, list) or len(reads) != 2 \
            or not all(type(v) is int for v in reads) \
            or not 1 <= reads[0] <= schema["writes"] <= reads[1]:
        raise UpdateError("malformed", f"{what} needs writes and a reads range [oldest, newest] containing it.")


def _check_data(payload: dict[str, Any], installed: InstalledState, kind: str) -> None:
    """Data compatibility between the package and the running build, for either payload schema."""
    version = payload["version"]
    if payload["schema"] == 1:
        if isinstance(installed.data_schema, DataProfile):
            raise UpdateError("unsupported_schema", "This package describes its data with a single schema number; "
                                                    "Qronos needs a package that lists every data store.")
        if kind == "upgrade" and payload["dataSchema"]["writes"] < installed.data_schema:
            raise UpdateError("data_schema", "This upgrade would write an older data format than the installed one.")
        low, high = payload["dataSchema"]["reads"]
        if not low <= installed.data_schema <= high:
            raise UpdateError("data_schema", f"Version {version} cannot read the data format "
                                             f"{installed.data_schema} that the installed Qronos keeps.")
        return
    profile = installed.data_schema
    if not isinstance(profile, DataProfile):
        raise UpdateError("unsupported_schema", "The running Qronos did not describe its data stores.")
    if payload["compatibilityEpoch"] != profile.epoch:
        raise UpdateError("data_epoch", f"Version {version} belongs to data compatibility epoch "
                                        f"{payload['compatibilityEpoch']}, the installed Qronos to {profile.epoch}. "
                                        "Moving between epochs is not supported.")
    stores = payload["dataStores"]
    for store_id, installed_version in sorted(profile.stores.items()):
        entry = stores.get(store_id)
        if entry is None:
            raise UpdateError("data_schema", f"Version {version} does not know the data store {store_id!r} that the "
                                             "installed Qronos keeps.")
        low, high = entry["reads"]
        if not low <= installed_version <= high:
            raise UpdateError("data_schema", f"Version {version} cannot read format {installed_version} of the data "
                                             f"store {store_id!r}.")
        if kind == "upgrade" and entry["writes"] < installed_version:
            raise UpdateError("data_schema", f"This upgrade would write an older format of the data store "
                                             f"{store_id!r}.")


# --------------------------------------------------------------- package


def _is_link(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _read_chosen_file(path: Path, limit: int, what: str) -> bytes:
    if _is_link(path) or not path.is_file():
        raise UpdateError("not_a_file", f"The chosen {what} is missing, not a regular file or a link.")
    # At most limit + 1 bytes are read, whatever size was reported or however the file grows.
    with open(path, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("malformed", f"The chosen {what} is too large.")
    return data


def _hash_package(path: Path) -> tuple[int, str]:
    if _is_link(path) or not path.is_file():
        raise UpdateError("not_a_file", "The chosen installer is missing, not a regular file or a link.")
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            size += len(chunk)
            if size > _MAX_PACKAGE_BYTES:
                raise UpdateError("package_mismatch", "The chosen installer is larger than any Qronos installer.")
            digest.update(chunk)
    return size, digest.hexdigest()


def verify_update(envelope: bytes, package: Path, keys: dict[str, TrustedKey],
                  installed: InstalledState) -> VerifiedUpdate:
    """Every check, offline. Raises UpdateError naming the first rule the package breaks."""
    payload_bytes, signer = _authenticate(envelope, keys)
    payload = _payload(payload_bytes)
    size, digest = _hash_package(package)
    if size != payload["package"]["bytes"] or digest != payload["package"]["sha256"]:
        raise UpdateError("package_mismatch", "The chosen installer is not the one this signed manifest describes.")

    version = payload["version"]
    # The higher of the package's own floor and the floor recorded by earlier updates.
    floor = payload["securityFloor"]
    if installed.security_floor and compare_versions(installed.security_floor, floor) > 0:
        floor = installed.security_floor
    if compare_versions(version, floor) < 0:
        raise UpdateError("below_security_floor", f"Version {version} is below the security floor {floor}.")
    order = compare_versions(version, installed.version)
    if order > 0:
        kind = "upgrade"
        minimum = payload["minimumVersion"]
        if minimum is not None and compare_versions(installed.version, minimum) < 0:
            raise UpdateError("upgrade_path", f"Version {version} needs at least {minimum} installed first.")
    elif order == 0:
        kind = "repair"
    else:
        if not payload["rollback"]:
            raise UpdateError("unsigned_rollback", f"Version {version} is older than the installed "
                                                   f"{installed.version} and is not signed as a rollback.")
        kind = "rollback"
    _check_data(payload, installed, kind)
    return VerifiedUpdate(kind, version, digest, size, payload["package"]["fileName"], signer, payload,
                          payload_bytes)


# ----------------------------------------------------------------- store

_STATE_FILE_LIMIT = 256 * 1024
_HISTORY_LIMIT = 50
_KINDS = frozenset({"upgrade", "repair", "rollback"})
_PHASES = frozenset({"idle", "staged", "handed_off"})
_STATE_FIELDS = frozenset({"schema", "phase", "securityFloor", "staged", "previous", "history"})
_RECORD_FIELDS = frozenset({"kind", "version", "packageSha256", "packageBytes", "packageName", "keyId",
                            "securityFloor", "fromVersion", "packageFile", "manifestFile"})
_HISTORY_FIELDS = frozenset({"kind", "fromVersion", "version", "packageSha256", "keyId"})


def _unsafe_file(path: Path) -> str | None:
    """Why an existing directory entry is not a plain file this store may replace, if it is not."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    attributes = getattr(info, "st_file_attributes", 0)
    if stat.S_ISLNK(info.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return "is a link"
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    if info.st_nlink != 1:
        return "has another hard link"
    return None


def _state_error() -> UpdateError:
    return UpdateError("store", "The update state file is not understood; nothing was changed.")


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _check_version(value: Any) -> None:
    try:
        parse_version(value)
    except UpdateError:
        raise _state_error() from None


# Every field's type is checked before any membership test, comparison or pattern, so a
# list, dict, number or boolean in the wrong place is an UpdateError, never a TypeError.
def _check_record(record: Any, fields: frozenset[str]) -> None:
    if not isinstance(record, dict) or set(record) != fields:
        raise _state_error()
    if not isinstance(record["kind"], str) or record["kind"] not in _KINDS \
            or not _matches(_SHA256, record["packageSha256"]) or not _matches(_KEY_ID, record["keyId"]):
        raise _state_error()
    for name in ("version", "fromVersion") + (("securityFloor",) if "securityFloor" in record else ()):
        _check_version(record[name])
    if fields == _RECORD_FIELDS:
        digest = record["packageSha256"]
        if not isinstance(record["packageFile"], str) or record["packageFile"] != f"{digest}.exe" \
                or not isinstance(record["manifestFile"], str) or record["manifestFile"] != f"{digest}.json" \
                or not _matches(_PACKAGE_NAME, record["packageName"]) \
                or type(record["packageBytes"]) is not int or not 0 < record["packageBytes"] <= _MAX_PACKAGE_BYTES:
            raise UpdateError("store", "The update state names a file this store did not create.")


def _check_state(state: Any) -> dict[str, Any]:
    """A strictly typed, bounded state document, or UpdateError."""
    if not isinstance(state, dict) or set(state) != _STATE_FIELDS:
        raise _state_error()
    if type(state["schema"]) is not int or state["schema"] != 1 \
            or not isinstance(state["phase"], str) or state["phase"] not in _PHASES \
            or not isinstance(state["history"], list) or len(state["history"]) > _HISTORY_LIMIT:
        raise _state_error()
    if state["securityFloor"] is not None:
        _check_version(state["securityFloor"])
    if (state["phase"] == "idle") != (state["staged"] is None):
        raise _state_error()
    for name in ("staged", "previous"):
        if state[name] is not None:
            _check_record(state[name], _RECORD_FIELDS)
    for entry in state["history"]:
        _check_record(entry, _HISTORY_FIELDS)
    return state


class LocalUpdateStore:
    """State and verified package copies under one folder this store owns.

    Phases: "idle" -> "staged" (verified copy in place) -> "handed_off" (given to the
    approved install step) -> back to "idle" once confirm() sees the new version
    running. A failed or abandoned install keeps the staged copy until discard().

    File ownership: every write goes to an exclusively created temporary file with an
    unpredictable name in the store's own folder and is published with an atomic
    rename. A destination that is a link, a reparse point, not a regular file or has
    another hard link is refused, never written through. The store folder, its staging
    folder and every existing ancestor must not be links or junctions. Changes are
    serialised across processes by an exclusive lock on ``update.lock``.
    """

    lock_timeout = 10.0
    replace_timeout = 2.0

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))
        self.state_path = self.root / "update-state.json"
        self.staged_dir = self.root / "staged"
        self.lock_path = self.root / "update.lock"
        # The file lock is held once per store object; nested use by the same thread
        # (a caller holding the lock across several steps) re-enters without
        # deadlocking, and other threads of this process wait their turn.
        self._thread_lock = threading.RLock()
        self._depth = 0

    # -- folders, files and the lock

    def _check_folders(self) -> None:
        for ancestor in reversed(self.root.parents):
            if os.path.lexists(ancestor) and _is_link(ancestor):
                raise UpdateError("store", f"A folder above the update store ({ancestor.name!r}) is a link.")
        for path in (self.root, self.staged_dir):
            if os.path.lexists(path) and (_is_link(path) or not path.is_dir()):
                raise UpdateError("store", f"The update folder {path.name!r} is not a plain folder.")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        # Both waits are bounded: another thread of this process holding the store
        # gives "busy" after lock_timeout, exactly like another process does.
        if not self._thread_lock.acquire(timeout=self.lock_timeout):
            raise UpdateError("busy", "Another part of Qronos is changing the update store.")
        try:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            with self._file_locked():
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
        finally:
            self._thread_lock.release()

    @contextlib.contextmanager
    def _file_locked(self) -> Iterator[None]:
        self._check_folders()
        self.root.mkdir(parents=True, exist_ok=True)
        self._check_folders()
        problem = _unsafe_file(self.lock_path)
        if problem:
            raise UpdateError("store", f"The update lock file {problem}; nothing was changed.")
        handle = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOINHERIT", 0), 0o600)
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UpdateError("store", "The update lock file is not this store's own file.")
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise UpdateError("busy", "Another Qronos process is changing the update store.") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(handle, 0, os.SEEK_SET)
                    msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)

    def _publish(self, directory: Path, name: str, write: Callable[[Any], None]) -> Path:
        """Write a new file through an exclusive unpredictable temporary, then rename it into place.

        Every failure is an UpdateError and leaves no temporary behind: "busy" when
        Windows refuses the rename because another program has the destination open
        (retried for replace_timeout seconds first), "store" for any other write error.
        """
        final = directory / name
        problem = _unsafe_file(final)
        if problem:
            raise UpdateError("store", f"{name} {problem}; it is not this store's file and was not touched.")
        try:
            handle, temporary = tempfile.mkstemp(prefix=".qronos-", suffix=".tmp", dir=directory)
        except OSError as error:
            raise UpdateError("store", f"{name} could not be written; nothing was changed.") from error
        try:
            with os.fdopen(handle, "wb") as stream:
                write(stream)
                stream.flush()
                os.fsync(stream.fileno())
            deadline = time.monotonic() + self.replace_timeout
            while True:
                problem = _unsafe_file(final)
                if problem:
                    raise UpdateError("store", f"{name} {problem}; it is not this store's file and was not touched.")
                try:
                    os.replace(temporary, final)
                    break
                except PermissionError as error:
                    # Another program (a reader outside the store lock, a scanner, a
                    # running installer) has the destination open without delete sharing.
                    if time.monotonic() >= deadline:
                        raise UpdateError("busy", f"{name} is in use by another program; nothing was changed. "
                                                  "Try again.") from error
                    time.sleep(0.05)
        except UpdateError:
            self._discard_temporary(temporary)
            raise
        except OSError as error:
            self._discard_temporary(temporary)
            raise UpdateError("store", f"{name} could not be written; nothing was changed.") from error
        except BaseException:
            self._discard_temporary(temporary)
            raise
        return final

    @staticmethod
    def _discard_temporary(temporary: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)

    def _owned(self, name: str) -> Path:
        if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}\.(exe|json)", name):
            raise UpdateError("store", "The update state names a file this store did not create.")
        return self.staged_dir / name

    def _owned_existing(self, name: str) -> Path:
        path = self._owned(name)
        problem = _unsafe_file(path)
        if problem or not os.path.lexists(path):
            raise UpdateError("store", f"The staged file {name} {problem or 'is missing'}.")
        return path

    def _remove(self, record: dict[str, Any] | None) -> None:
        if record:
            for name in (record["packageFile"], record["manifestFile"]):
                path = self._owned(name)
                if os.path.lexists(path):
                    try:
                        os.unlink(path)  # removes only this directory entry, never a link target
                    except OSError as error:
                        raise UpdateError("busy", f"The staged file {name} is in use and could not be removed. "
                                                  "Try again.") from error

    # -- state

    def _read_state(self) -> dict[str, Any]:
        if not os.path.lexists(self.state_path):
            return {"schema": 1, "phase": "idle", "securityFloor": None, "staged": None, "previous": None,
                    "history": []}
        data = self._read_state_bytes()
        try:
            state = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _state_error() from error
        return _check_state(state)

    def _read_state_bytes(self) -> bytes:
        """The state file's bytes: this store's own plain file, read with a hard bound.

        The handle that is read must be the very file checked by lstat (same file id,
        a regular file with one link), so a file swapped for a link between the check
        and the open is not read. A swap by an ordinary atomic save is retried.
        """
        for _ in range(3):
            problem = _unsafe_file(self.state_path)
            if problem:
                raise UpdateError("store", f"The update state file {problem}; nothing was changed.")
            try:
                checked = os.lstat(self.state_path)
                handle = os.open(self.state_path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                                 | getattr(os, "O_NOINHERIT", 0))
            except OSError as error:
                raise UpdateError("store", "The update state file could not be read; nothing was changed.") \
                    from error
            with os.fdopen(handle, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
                    continue
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise UpdateError("store", "The update state file is not this store's own file; "
                                               "nothing was changed.")
                data = stream.read(_STATE_FILE_LIMIT + 1)
            if len(data) > _STATE_FILE_LIMIT:
                raise UpdateError("store", "The update state file is too large; nothing was changed.")
            return data
        raise UpdateError("busy", "The update state file kept changing while it was read; try again.")

    def state(self) -> dict[str, Any]:
        """The current state, read under the store lock.

        On Windows a file that is open cannot be replaced, so an unlocked read could make
        another process's save fail. Readers therefore take the same lock as writers. A
        store that does not exist yet is not created by reading it.
        """
        self._check_folders()
        if not os.path.lexists(self.root):
            return self._read_state()
        with self._locked():
            return self._read_state()

    def _save(self, state: dict[str, Any]) -> None:
        state["history"] = state["history"][-_HISTORY_LIMIT:]
        data = (json.dumps(_check_state(state), indent=1, sort_keys=True) + "\n").encode("utf-8")
        self._publish(self.root, self.state_path.name, lambda stream: stream.write(data))

    # -- operations

    def installed(self, version: str, data_schema: int | DataProfile) -> InstalledState:
        return InstalledState(version, data_schema, self.state()["securityFloor"])

    def stage(self, manifest: Path, package: Path, keys: dict[str, TrustedKey], version: str,
              data_schema: int | DataProfile) -> VerifiedUpdate:
        """Verify user-chosen local files and keep a verified copy for the install step."""
        floor = self.state()["securityFloor"]
        envelope = _read_chosen_file(manifest, _MAX_ENVELOPE_BYTES, "update manifest")
        update = verify_update(envelope, package, keys, InstalledState(version, data_schema, floor))
        with self._locked():
            state = self._read_state()
            if state["securityFloor"] != floor:
                raise UpdateError("busy", "The update state changed while the package was checked; try again.")
            self.staged_dir.mkdir(exist_ok=True)
            self._check_folders()

            def copy(stream: Any) -> None:
                digest = hashlib.sha256()
                size = 0
                with open(package, "rb") as source:
                    for chunk in iter(lambda: source.read(_READ_CHUNK), b""):
                        size += len(chunk)
                        if size > update.package_bytes:
                            break
                        digest.update(chunk)
                        stream.write(chunk)
                if size != update.package_bytes or digest.hexdigest() != update.package_sha256:
                    raise UpdateError("package_mismatch", "The chosen installer changed while it was being copied.")

            target = self._publish(self.staged_dir, f"{update.package_sha256}.exe", copy)
            self._publish(self.staged_dir, f"{update.package_sha256}.json", lambda stream: stream.write(envelope))
            if state["staged"] and state["staged"]["packageSha256"] != update.package_sha256:
                self._remove(state["staged"])
            state.update(phase="staged", staged={
                "kind": update.kind, "version": update.version, "packageSha256": update.package_sha256,
                "packageBytes": update.package_bytes, "packageName": update.package_name, "keyId": update.key_id,
                "securityFloor": update.payload["securityFloor"], "fromVersion": version,
                "packageFile": target.name, "manifestFile": f"{update.package_sha256}.json"})
            self._save(state)
        return update

    def handoff(self, keys: dict[str, TrustedKey], version: str, data_schema: int | DataProfile) -> Path:
        """The staged installer, re-verified now, for the user-approved install step."""
        with self._locked():
            state = self._read_state()
            staged = state["staged"]
            if state["phase"] not in ("staged", "handed_off") or not staged:
                raise UpdateError("store", "No verified update is staged.")
            envelope = _read_chosen_file(self._owned_existing(staged["manifestFile"]), _MAX_ENVELOPE_BYTES,
                                         "staged manifest")
            package = self._owned_existing(staged["packageFile"])
            if package.stat().st_size != staged["packageBytes"]:
                raise UpdateError("package_mismatch", "The staged installer no longer matches its record.")
            update = verify_update(envelope, package, keys, InstalledState(version, data_schema,
                                                                           state["securityFloor"]))
            if update.package_sha256 != staged["packageSha256"]:
                raise UpdateError("package_mismatch", "The staged installer no longer matches its record.")
            state["phase"] = "handed_off"
            self._save(state)
        return package

    def confirm(self, version: str, *, replacement_evidence: bool = False) -> str:
        """Called when Qronos starts: records a completed update. Returns what happened.

        A version change proves another build is running. A repair (the same
        version) cannot be told apart from a cancelled one by the version, so it
        is "unconfirmed" and stays staged unless the caller has reviewed evidence
        that the replacement ran (``replacement_evidence``; no such receipt
        exists yet, so the product never passes it).
        """
        with self._locked():
            state = self._read_state()
            staged = state["staged"]
            if state["phase"] == "idle" or not staged:
                return "nothing staged"
            if compare_versions(version, staged["version"]) != 0:
                return "not applied"
            if staged["kind"] == "repair" and not replacement_evidence:
                return "unconfirmed"
            floor = state["securityFloor"]
            if floor is None or compare_versions(staged["securityFloor"], floor) > 0:
                floor = staged["securityFloor"]
            previous = state["previous"]
            if previous and previous["packageSha256"] != staged["packageSha256"]:
                self._remove(previous)
            state.update(phase="idle", securityFloor=floor, previous=staged, staged=None,
                         history=state["history"] + [{key: staged[key] for key in _HISTORY_FIELDS}])
            self._save(state)
        return staged["kind"]

    def discard(self) -> None:
        """Forget a staged update and delete only its own copies."""
        with self._locked():
            state = self._read_state()
            if state["staged"]:
                self._remove(state["staged"])
            state.update(phase="idle", staged=None)
            self._save(state)
