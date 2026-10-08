"""Authenticated local update packages: verify, stage, hand off, confirm, roll back.

Qronos never fetches updates. A person obtains an update through the visible,
approved browser or from removable media, then chooses two local files:

* the installer (``qronos-desktop_<version>_x64-setup.exe``), and
* its signed manifest envelope (``<installer>.qronos-update.json``).

This module checks them entirely offline:

* The envelope's payload must carry a valid Ed25519 signature (RFC 8032, see
  ``core.ed25519``) by a key listed in ``config/update_trusted_keys.json``. That
  file holds only OWNER-provided public keys and ships empty: with no key
  configured, every package is refused. Unknown and revoked keys are refused.
* The signed payload binds the product, version, architecture and build target,
  and the installer's file size and SHA-256; the chosen installer must match.
* Version policy, against the installed version and the recorded security floor:
  upgrades must respect the package's minimum starting version; reinstalling the
  same version is a repair; going back requires a package signed explicitly as a
  rollback, at or above the security floor, that can read the installed data
  schema. No package may lower the data schema an upgrade writes.

``LocalUpdateStore`` keeps a small state file and copies of verified packages in
its own folder (``<data root>/Updates``). It deletes only files it created there,
by recorded name, and never touches other user data. Launching the staged
installer is a separate, user-approved step outside this module.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
class InstalledState:
    """What the running Qronos knows about itself, plus the recorded security floor."""

    version: str
    data_schema: int
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
    payload = _exact(payload, _PAYLOAD_FIELDS, "The signed payload")
    if payload["schema"] != 1:
        raise UpdateError("malformed", "The signed payload must be schema 1.")
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
    schema = _exact(payload["dataSchema"], _DATA_SCHEMA_FIELDS, "The signed data schema")
    reads = schema["reads"]
    if not isinstance(schema["writes"], int) or isinstance(schema["writes"], bool) or schema["writes"] < 1 \
            or not isinstance(reads, list) or len(reads) != 2 \
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in reads) \
            or not 1 <= reads[0] <= schema["writes"] <= reads[1]:
        raise UpdateError("malformed", "dataSchema needs writes and a reads range [oldest, newest] containing it.")
    return payload


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
    if path.stat().st_size > limit:
        raise UpdateError("malformed", f"The chosen {what} is too large.")
    return path.read_bytes()


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
        if payload["dataSchema"]["writes"] < installed.data_schema:
            raise UpdateError("data_schema", "This upgrade would write an older data format than the installed one.")
    elif order == 0:
        kind = "repair"
    else:
        if not payload["rollback"]:
            raise UpdateError("unsigned_rollback", f"Version {version} is older than the installed "
                                                   f"{installed.version} and is not signed as a rollback.")
        kind = "rollback"
    low, high = payload["dataSchema"]["reads"]
    if not low <= installed.data_schema <= high:
        raise UpdateError("data_schema", f"Version {version} cannot read the data format {installed.data_schema} "
                                         "that the installed Qronos keeps.")
    return VerifiedUpdate(kind, version, digest, size, payload["package"]["fileName"], signer, payload,
                          payload_bytes)


# ----------------------------------------------------------------- store


class LocalUpdateStore:
    """State and verified package copies under one folder this store owns.

    Phases: "idle" -> "staged" (verified copy in place) -> "handed_off" (given to the
    approved install step) -> back to "idle" once confirm() sees the new version
    running. A failed or abandoned install keeps the staged copy until discard().
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_path = root / "update-state.json"
        self.staged_dir = root / "staged"

    # -- state

    def _check_root(self) -> None:
        for path in (self.root, self.staged_dir):
            if os.path.lexists(path) and (_is_link(path) or not path.is_dir()):
                raise UpdateError("store", f"The update folder {path.name!r} is not a plain folder.")

    def state(self) -> dict[str, Any]:
        self._check_root()
        if not self.state_path.exists():
            return {"schema": 1, "phase": "idle", "securityFloor": None, "staged": None, "previous": None,
                    "history": []}
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("schema") != 1 or state.get("phase") not in ("idle", "staged", "handed_off"):
            raise UpdateError("store", "The update state file is not understood; nothing was changed.")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._check_root()
        temporary = self.root / "update-state.json.tmp"
        temporary.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _owned(self, name: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}\.(exe|json)", name):
            raise UpdateError("store", "The update state names a file this store did not create.")
        return self.staged_dir / name

    def _remove(self, record: dict[str, Any] | None) -> None:
        if record:
            for name in (record["packageFile"], record["manifestFile"]):
                path = self._owned(name)
                if path.exists() and not _is_link(path):
                    path.unlink()

    # -- operations

    def installed(self, version: str, data_schema: int) -> InstalledState:
        return InstalledState(version, data_schema, self.state()["securityFloor"])

    def stage(self, manifest: Path, package: Path, keys: dict[str, TrustedKey], version: str,
              data_schema: int) -> VerifiedUpdate:
        """Verify user-chosen local files and keep a verified copy for the install step."""
        state = self.state()
        envelope = _read_chosen_file(manifest, _MAX_ENVELOPE_BYTES, "update manifest")
        update = verify_update(envelope, package, keys, InstalledState(version, data_schema,
                                                                       state["securityFloor"]))
        self.staged_dir.mkdir(parents=True, exist_ok=True)
        self._check_root()
        target = self._owned(f"{update.package_sha256}.exe")
        partial = self.staged_dir / f"{update.package_sha256}.part"
        digest = hashlib.sha256()
        with open(package, "rb") as source, open(partial, "wb") as copy:
            for chunk in iter(lambda: source.read(_READ_CHUNK), b""):
                digest.update(chunk)
                copy.write(chunk)
        if digest.hexdigest() != update.package_sha256:
            partial.unlink()
            raise UpdateError("package_mismatch", "The chosen installer changed while it was being copied.")
        os.replace(partial, target)
        self._owned(f"{update.package_sha256}.json").write_bytes(envelope)
        if state["staged"] and state["staged"]["packageSha256"] != update.package_sha256:
            self._remove(state["staged"])
        state.update(phase="staged", staged={
            "kind": update.kind, "version": update.version, "packageSha256": update.package_sha256,
            "packageBytes": update.package_bytes, "packageName": update.package_name, "keyId": update.key_id,
            "securityFloor": update.payload["securityFloor"], "fromVersion": version,
            "packageFile": target.name, "manifestFile": f"{update.package_sha256}.json"})
        self._save(state)
        return update

    def handoff(self, keys: dict[str, TrustedKey], version: str, data_schema: int) -> Path:
        """The staged installer, re-verified now, for the user-approved install step."""
        state = self.state()
        staged = state["staged"]
        if state["phase"] not in ("staged", "handed_off") or not staged:
            raise UpdateError("store", "No verified update is staged.")
        envelope = self._owned(staged["manifestFile"]).read_bytes()
        package = self._owned(staged["packageFile"])
        update = verify_update(envelope, package, keys, InstalledState(version, data_schema,
                                                                       state["securityFloor"]))
        if update.package_sha256 != staged["packageSha256"]:
            raise UpdateError("package_mismatch", "The staged installer no longer matches its record.")
        state["phase"] = "handed_off"
        self._save(state)
        return package

    def confirm(self, version: str) -> str:
        """Called when Qronos starts: records a completed update. Returns what happened."""
        state = self.state()
        staged = state["staged"]
        if state["phase"] == "idle" or not staged:
            return "nothing staged"
        if compare_versions(version, staged["version"]) != 0:
            return "not applied"
        floor = state["securityFloor"]
        if floor is None or compare_versions(staged["securityFloor"], floor) > 0:
            floor = staged["securityFloor"]
        previous = state["previous"]
        if previous and previous["packageSha256"] != staged["packageSha256"]:
            self._remove(previous)
        state.update(phase="idle", securityFloor=floor, previous=staged, staged=None,
                     history=state["history"] + [{key: staged[key] for key in
                                                   ("kind", "fromVersion", "version", "packageSha256", "keyId")}])
        self._save(state)
        return staged["kind"]

    def discard(self) -> None:
        """Forget a staged update and delete only its own copies."""
        state = self.state()
        if state["staged"]:
            self._remove(state["staged"])
        state.update(phase="idle", staged=None)
        self._save(state)
