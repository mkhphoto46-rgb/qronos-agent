"""
The devices allowed to connect, and the keys that let them.

One JSON file, one record per paired phone. The file holds the pairing keys, so
two things matter more than anything else in this module:

    It lives under ``data/``, which is already in ``.gitignore``. The keys are
    outside version control by construction rather than by remembering.

    ``DeviceRecord`` redacts the key in ``repr``. A stray debug print, a log
    line, or an exception traceback showing a record cannot leak it.

Revocation is enforced during the TLS handshake, not after it. ``secret_for``
stops resolving a revoked device, so the handshake itself fails and there is no
window in which a revoked phone holds an open session.

A corrupt file raises rather than starting empty. Starting empty would look
exactly like "nothing is paired yet" and would invite the user to re-pair over
whatever the corruption was, quietly discarding the real device list. Same rule
as the artifact ownership store.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Iterable, Protocol

from core.config import CONFIG
from core.link_capability import Capability


DEFAULT_DEVICE_PATH = CONFIG.paths.data / "link_devices.json"

# 256 bits, which is what makes throttling the handshake a denial-of-service
# measure rather than an authentication measure.
SECRET_BYTES = 32

# Eight random bytes rendered as hex. The identity travels in the TLS
# handshake, so it is deliberately a fixed shape that can be validated before
# it is used as a dictionary key or written to a log.
DEVICE_ID_BYTES = 8
DEVICE_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")

SCHEMA_VERSION = 1

# How long a remote opt-in lasts when the PC does not say. Remote access is a
# decision about one phone for a period, not a setting that outlives the
# reason it was turned on; the user renews it at the PC.
DEFAULT_REMOTE_SECONDS = 30 * 24 * 3600.0

# The longest a single opt-in may run, whatever the caller asks for.
MAX_REMOTE_SECONDS = 365 * 24 * 3600.0


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


class DeviceStoreCorrupt(Exception):
    """The device file exists but could not be understood."""


class UnknownDevice(Exception):
    """An operation named a device that is not in the registry."""


class DeviceStatus(Enum):
    """Where a device is in its life."""

    # Paired, but has never completed a handshake. Promoted on first contact
    # inside the pairing window; discarded if the window closes first.
    PENDING = "pending"

    ACTIVE = "active"

    # Cannot authenticate. The record is kept so the audit log can still name
    # the device, and so the same phone cannot be silently re-paired into
    # looking like a fresh one.
    REVOKED = "revoked"


def is_valid_device_id(device_id: str) -> bool:
    """
    True for an identity of the shape this module issues.

    The identity in a handshake comes from the peer, so it is untrusted input.
    Checking its shape before using it keeps arbitrary strings out of the
    registry keys and the audit log.
    """
    return bool(DEVICE_ID_PATTERN.match(device_id))


class SecretFileNotProtected(Exception):
    """The device file could not be restricted to its owner."""


# Principals that may legitimately appear on the device file's Windows ACL
# besides the owner. Neither is a boundary worth defending: an administrator
# can take ownership of any file, and SYSTEM already runs above the user. What
# the ACL has to exclude is every *other* standard account on the machine.
_PERMITTED_ACL_PRINCIPALS = frozenset(
    {
        "NT AUTHORITY\\SYSTEM",
        "BUILTIN\\ADMINISTRATORS",
        "OWNER RIGHTS",
    }
)

_ICACLS_TIMEOUT_SECONDS = 15

# Only defined on Windows, and zero is the documented "no special flags" value
# everywhere else, so the calls below stay importable on a POSIX machine even
# though nothing there reaches them.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _acl_principals(path: Path) -> frozenset[str]:
    """
    The accounts named on a file's Windows ACL, upper-cased.

    ``icacls`` prints the path, then one ``PRINCIPAL:(rights)`` entry per line.
    Rights are irrelevant here: any entry at all means that account can reach
    the file, and the question being asked is who appears, not what they may
    do.
    """
    result = subprocess.run(
        ["icacls", str(path)],
        capture_output=True,
        text=True,
        timeout=_ICACLS_TIMEOUT_SECONDS,
        creationflags=_NO_WINDOW,
    )

    if result.returncode != 0:
        raise SecretFileNotProtected(
            f"Could not read the permissions of {path}."
        )

    principals: set[str] = set()

    for line in result.stdout.splitlines():
        # Strip the path off the first line; later lines are indented.
        entry = line.replace(str(path), "", 1).strip()

        if not entry or ":" not in entry:
            continue

        principal, _, _rights = entry.partition(":")
        principal = principal.strip()

        if principal:
            principals.add(principal.upper())

    return frozenset(principals)


def _restrict_to_owner(path: Path) -> None:
    """
    Make the file readable by its owner and nobody else.

    POSIX gets mode 0600. Windows ignores the mode entirely — the file keeps
    whatever it inherited from its parent, and ``stat`` keeps reporting 0666 no
    matter what the real permissions are — so the access-control list is
    rewritten instead: inheritance off, one entry for the current user.

    Raises :class:`SecretFileNotProtected` rather than returning quietly. This
    file holds the shared secret every paired device authenticates with. A
    secret sitting in a world-readable file is worse than no pairing at all,
    because the pairing is what the rest of the link trusts, so the write is
    abandoned instead.
    """
    if os.name != "nt":
        os.chmod(path, 0o600)
        return

    owner = getpass.getuser()

    try:
        granted = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{owner}:(F)",
            ],
            capture_output=True,
            text=True,
            timeout=_ICACLS_TIMEOUT_SECONDS,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SecretFileNotProtected(
            f"Could not restrict {path} to {owner}: {error}"
        ) from error

    if granted.returncode != 0:
        raise SecretFileNotProtected(
            f"Could not restrict {path} to {owner}: "
            f"{granted.stderr.strip() or granted.stdout.strip()}"
        )

    # Read the list back rather than trusting the exit code. Group policy on a
    # domain-joined machine can reapply inherited entries, and icacls reports
    # success for the change it made regardless.
    unexpected = {
        principal
        for principal in _acl_principals(path)
        if principal not in _PERMITTED_ACL_PRINCIPALS
        and not principal.endswith("\\" + owner.upper())
        and principal != owner.upper()
    }

    if unexpected:
        raise SecretFileNotProtected(
            f"{path} is still reachable by {sorted(unexpected)}."
        )


def new_device_id() -> str:
    return secrets.token_hex(DEVICE_ID_BYTES)


def new_secret() -> bytes:
    return secrets.token_bytes(SECRET_BYTES)


@dataclass(frozen=True)
class DeviceRecord:
    """One paired phone."""

    device_id: str
    name: str
    secret: bytes
    status: DeviceStatus
    created_at: float
    last_seen_at: float | None = None
    paired_from: str = ""

    # ``None`` means "everything the scope allows". A set narrows it, and can
    # only ever narrow it — see ``resolve_capabilities``.
    grants: frozenset[Capability] | None = None

    # Layer 2 is opt-in for one named device at a time, not a global switch.
    remote_enabled: bool = False

    # When that opt-in lapses. Remote access with no expiry is not a state the
    # registry creates; a record that has one anyway (written before expiry
    # existed, or edited by hand) is treated as lapsed, which is the safe
    # reading of a missing limit.
    remote_expires_at: float | None = None

    @property
    def can_authenticate(self) -> bool:
        """A pending device may connect; that is how pairing completes."""
        return self.status in {DeviceStatus.PENDING, DeviceStatus.ACTIVE}

    def remote_allowed(self, now: float) -> bool:
        """
        Whether this device may be served through the relay at ``now``.

        Active, opted in, and inside the opt-in's period. A pending device
        never qualifies: pairing happens on the local network only.
        """

        return (
            self.status is DeviceStatus.ACTIVE
            and self.remote_enabled
            and self.remote_expires_at is not None
            and now < self.remote_expires_at
        )

    def __repr__(self) -> str:
        """Never render the key."""
        return (
            f"DeviceRecord(device_id={self.device_id!r}, "
            f"name={self.name!r}, status={self.status.value}, "
            f"secret=<redacted {len(self.secret)} bytes>, "
            f"remote_enabled={self.remote_enabled}, "
            f"remote_expires_at={self.remote_expires_at})"
        )

    def describe(self) -> str:
        seen = (
            "never"
            if self.last_seen_at is None
            else time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(self.last_seen_at)
            )
        )

        return (
            f"{self.name} ({self.device_id}) "
            f"{self.status.value}, last seen {seen}"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "secret": base64.b64encode(self.secret).decode("ascii"),
            "status": self.status.value,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "paired_from": self.paired_from,
            "grants": (
                None
                if self.grants is None
                else sorted(item.value for item in self.grants)
            ),
            "remote_enabled": self.remote_enabled,
            "remote_expires_at": self.remote_expires_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> DeviceRecord:
        """
        Rebuild one record, refusing anything malformed.

        Every failure here becomes ``DeviceStoreCorrupt`` at the registry
        level. A half-understood key file is not something to work around.
        """

        try:
            device_id = str(data["device_id"])
            secret = base64.b64decode(str(data["secret"]), validate=True)
            status = DeviceStatus(str(data["status"]))
            created_at = float(data["created_at"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError, base64.binascii.Error) as exc:
            raise DeviceStoreCorrupt(f"unreadable device record: {exc}") from exc

        if not is_valid_device_id(device_id):
            raise DeviceStoreCorrupt(f"bad device id: {device_id!r}")

        if len(secret) != SECRET_BYTES:
            raise DeviceStoreCorrupt(
                f"{device_id} has a {len(secret)} byte key, "
                f"expected {SECRET_BYTES}"
            )

        raw_grants = data.get("grants")

        if raw_grants is None:
            grants: frozenset[Capability] | None = None
        elif isinstance(raw_grants, list):
            try:
                grants = frozenset(
                    Capability(str(item)) for item in raw_grants
                )
            except ValueError as exc:
                raise DeviceStoreCorrupt(
                    f"{device_id} lists an unknown capability: {exc}"
                ) from exc
        else:
            raise DeviceStoreCorrupt(f"{device_id} has malformed grants")

        last_seen = data.get("last_seen_at")
        remote_expires = data.get("remote_expires_at")

        try:
            remote_expires_at = (
                None if remote_expires is None else float(remote_expires)  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise DeviceStoreCorrupt(
                f"{device_id} has a malformed remote expiry"
            ) from exc

        return cls(
            device_id=device_id,
            name=str(data.get("name", "")),
            secret=secret,
            status=status,
            created_at=created_at,
            last_seen_at=None if last_seen is None else float(last_seen),  # type: ignore[arg-type]
            paired_from=str(data.get("paired_from", "")),
            grants=grants,
            remote_enabled=bool(data.get("remote_enabled", False)),
            remote_expires_at=remote_expires_at,
        )


class DeviceStoreBusy(Exception):
    """Another process held the device file's lock for too long."""


# How long a change waits for another process to finish its own. A change is a
# read, one record and a file write, so anything near this is a stuck process.
LOCK_TIMEOUT_SECONDS = 10.0


def _try_lock(handle: object) -> None:
    """Take an exclusive lock on byte 0 of an open file, or raise OSError."""

    fileno = handle.fileno()  # type: ignore[attr-defined]

    if os.name == "nt":
        import msvcrt

        handle.seek(0)  # type: ignore[attr-defined]
        msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - POSIX
        import fcntl

        fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: object) -> None:
    fileno = handle.fileno()  # type: ignore[attr-defined]

    if os.name == "nt":
        import msvcrt

        handle.seek(0)  # type: ignore[attr-defined]
        msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - POSIX
        import fcntl

        fcntl.flock(fileno, fcntl.LOCK_UN)


class _FileLock:
    """
    An exclusive lock between processes, on a file beside the device file.

    The lock file holds nothing; it exists to be locked. Waiting is bounded:
    a process that cannot get the lock in ``LOCK_TIMEOUT_SECONDS`` raises
    ``DeviceStoreBusy`` rather than hanging the desktop or a handshake.
    """

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self.path = path
        self.timeout = timeout
        self._handle: object | None = None

    def __enter__(self) -> _FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout

        while True:
            try:
                _try_lock(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise DeviceStoreBusy(
                        f"{self.path} stayed locked by another process"
                    ) from None

                time.sleep(0.02)

        self._handle = handle

        return self

    def __exit__(self, *exc: object) -> None:
        handle, self._handle = self._handle, None

        if handle is None:
            return

        try:
            _unlock(handle)
        except OSError:
            pass
        finally:
            handle.close()  # type: ignore[attr-defined]


class DeviceRegistry:
    """
    The paired devices, backed by one JSON file.

    Every change is written through immediately. A phone pairing or being
    revoked is rare and consequential, so there is no value in batching it and
    real cost in losing it to a crash.

    More than one process holds a registry for the same file — the link
    service and whatever the user revokes a phone from — so neither may trust
    its own memory:

        Reads check the file first. If it has been replaced since it was last
        read (a different modification time, size or file id), it is read
        again. A ``stat`` per lookup is the cost, and it means a revocation
        written by another process reaches the next handshake and, through the
        server's per-request check, the next request of an open session.

        Changes hold a lock shared between processes, re-read the file under
        it, change one record and write the whole file back. Without the
        re-read two processes would each write back their own stale copy and
        the second would silently undo the first — a revocation lost that way
        is a revoked phone working again.

    A file that disappears reads as no devices, which is the safe direction.
    """

    def __init__(
        self,
        path: str | Path | None = DEFAULT_DEVICE_PATH,
        clock: Clock | None = None,
    ) -> None:
        # ``None`` keeps the registry entirely in memory, which is what the
        # tests use.
        self.path = None if path is None else Path(path)
        self.clock: Clock = clock if clock is not None else time.time

        # Threads within this process: the link server's session threads, the
        # relay dialler and the desktop. Reentrant, because a change can be
        # made of smaller changes.
        self._lock = threading.RLock()
        self._file_lock = (
            None
            if self.path is None
            else _FileLock(self.path.with_suffix(self.path.suffix + ".lock"))
        )
        self._changing_depth = 0
        self._signature: tuple[int, int, int] | None = None
        self._records: dict[str, DeviceRecord] = self._load()

    # ------------------------------------------------------------- persistence

    def _stat_signature(self) -> tuple[int, int, int] | None:
        assert self.path is not None

        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None

        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def _refresh(self) -> None:
        """Re-read the file if another process has replaced it."""

        if self.path is None:
            return

        if self._stat_signature() == self._signature:
            return

        with self._lock:
            if self._stat_signature() != self._signature:
                self._records = self._load()

    def _changing(self) -> _Changing:
        return _Changing(self)

    def _load(self) -> dict[str, DeviceRecord]:
        if self.path is None:
            return {}

        # Stat before reading. If the file is replaced in between, the
        # signature kept is the older one and the next lookup reads again,
        # which costs a read and is never stale.
        #
        # The signature is recorded only once the file has been read
        # successfully. Recording it first meant a file that failed to parse
        # was remembered as read, and every later lookup went on serving the
        # records from before it — a revocation hidden behind a corrupt write
        # would have been ignored. Now every lookup retries, and fails, until
        # the file is readable again.
        signature = self._stat_signature()

        if signature is None:
            self._signature = None
            return {}

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DeviceStoreCorrupt(
                f"cannot read {self.path}: {exc}"
            ) from exc

        if not isinstance(raw, dict):
            raise DeviceStoreCorrupt(f"{self.path} is not an object")

        devices = raw.get("devices")

        if not isinstance(devices, list):
            raise DeviceStoreCorrupt(f"{self.path} has no device list")

        records: dict[str, DeviceRecord] = {}

        for entry in devices:
            if not isinstance(entry, dict):
                raise DeviceStoreCorrupt(f"{self.path} has a malformed entry")

            record = DeviceRecord.from_json(entry)
            records[record.device_id] = record

        self._signature = signature

        return records

    def _save(self) -> None:
        if self.path is None:
            return

        payload = {
            "version": SCHEMA_VERSION,
            "devices": [
                record.to_json()
                for record in sorted(
                    self._records.values(), key=lambda r: r.created_at
                )
            ],
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Best effort on the directory, and it stays best effort: a readable
        # directory reveals that a device file exists, which is not a secret.
        # The keys inside it are, and those are protected on the file itself.
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

        temporary = self.path.with_suffix(self.path.suffix + ".tmp")

        try:
            # Create the file empty, restrict it, and only then write the keys
            # into it. Writing first would leave every pairing secret on disk
            # under inherited permissions for as long as the restriction takes
            # to apply — brief, but a window that does not need to exist.
            temporary.touch()
            _restrict_to_owner(temporary)

            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # Replace, so a crash mid-write leaves the previous file intact
            # rather than a truncated one. The Windows access-control list is
            # carried across by the rename, and the POSIX mode with it.
            os.replace(temporary, self.path)
            self._signature = self._stat_signature()
        finally:
            temporary.unlink(missing_ok=True)

    # ------------------------------------------------------------------ queries

    def __len__(self) -> int:
        self._refresh()
        return len(self._records)

    def get(self, device_id: str) -> DeviceRecord | None:
        self._refresh()
        return self._records.get(device_id)

    def all(self) -> tuple[DeviceRecord, ...]:
        self._refresh()
        return tuple(
            sorted(self._records.values(), key=lambda r: r.created_at)
        )

    def with_status(self, status: DeviceStatus) -> tuple[DeviceRecord, ...]:
        return tuple(
            record for record in self.all() if record.status is status
        )

    def secret_for(self, device_id: str) -> bytes | None:
        """
        The key for a device that is allowed to authenticate, or ``None``.

        This is what the TLS handshake calls. Returning ``None`` for a revoked
        or unknown device is what makes the handshake fail, which is why
        revocation needs no separate enforcement anywhere else.

        The identity comes from the peer, so its shape is checked before it is
        used as a key.
        """

        if not is_valid_device_id(device_id):
            return None

        record = self.get(device_id)

        if record is None or not record.can_authenticate:
            return None

        return record.secret

    # ------------------------------------------------------------------ changes

    def create(
        self,
        name: str,
        paired_from: str = "",
        grants: Iterable[Capability] | None = None,
    ) -> DeviceRecord:
        """Mint a new pending device with a fresh key."""

        with self._changing():
            device_id = new_device_id()

            while device_id in self._records:  # pragma: no cover - 2**64 odds
                device_id = new_device_id()

            record = DeviceRecord(
                device_id=device_id,
                name=name,
                secret=new_secret(),
                status=DeviceStatus.PENDING,
                created_at=self.clock(),
                paired_from=paired_from,
                grants=None if grants is None else frozenset(grants),
            )

            self._records[device_id] = record
            self._save()

            return record

    def _update(self, device_id: str, **changes: object) -> DeviceRecord:
        with self._changing():
            record = self._records.get(device_id)

            if record is None:
                raise UnknownDevice(device_id)

            updated = replace(record, **changes)  # type: ignore[arg-type]
            self._records[device_id] = updated
            self._save()

            return updated

    def activate(self, device_id: str) -> DeviceRecord:
        """
        Promote a pending device after its first successful handshake.

        Refuses to resurrect a revoked device: that must go through pairing
        again, deliberately, at the PC.
        """

        with self._changing():
            record = self._records.get(device_id)

            if record is None:
                raise UnknownDevice(device_id)

            if record.status is DeviceStatus.REVOKED:
                raise ValueError(
                    f"{device_id} is revoked and cannot be activated"
                )

            return self._update(
                device_id,
                status=DeviceStatus.ACTIVE,
                last_seen_at=self.clock(),
            )

    def revoke(self, device_id: str) -> DeviceRecord:
        """
        Stop a device authenticating, and withdraw its remote opt-in with it.

        Withdrawing the opt-in is not what makes this safe — a revoked key does
        not resolve — but it means a revoked record never reads as
        remote-enabled anywhere, a device list included.

        Revoking here takes effect at the next handshake. A running
        ``LinkServer`` also closes sessions that are already open; use its
        ``revoke`` so both happen together.
        """
        return self._update(
            device_id,
            status=DeviceStatus.REVOKED,
            remote_enabled=False,
            remote_expires_at=None,
        )

    def rename(self, device_id: str, name: str) -> DeviceRecord:
        """A device's name is a label. Its identity does not change with it."""
        return self._update(device_id, name=name)

    def touch(self, device_id: str) -> DeviceRecord:
        return self._update(device_id, last_seen_at=self.clock())

    def set_remote_enabled(
        self,
        device_id: str,
        enabled: bool,
        expires_at: float | None = None,
    ) -> DeviceRecord:
        """
        Layer 2 opt-in for one device, always with an end.

        ``expires_at`` defaults to ``DEFAULT_REMOTE_SECONDS`` from now and is
        clamped to ``MAX_REMOTE_SECONDS``. A revoked device cannot be opted in:
        that is a decision about a phone that is no longer trusted at all.
        """

        if not enabled:
            return self._update(
                device_id, remote_enabled=False, remote_expires_at=None
            )

        with self._changing():
            record = self._records.get(device_id)

            if record is None:
                raise UnknownDevice(device_id)

            if record.status is DeviceStatus.REVOKED:
                raise ValueError(
                    f"{device_id} is revoked and cannot be given remote access"
                )

            now = self.clock()
            until = (
                now + DEFAULT_REMOTE_SECONDS if expires_at is None else expires_at
            )

            if until <= now:
                raise ValueError("a remote opt-in must end in the future")

            return self._update(
                device_id,
                remote_enabled=True,
                remote_expires_at=min(until, now + MAX_REMOTE_SECONDS),
            )

    def set_grants(
        self,
        device_id: str,
        grants: Iterable[Capability] | None,
    ) -> DeviceRecord:
        return self._update(
            device_id,
            grants=None if grants is None else frozenset(grants),
        )

    def remove(self, device_id: str) -> None:
        """Forget a device entirely. Pairing again produces a new identity."""

        with self._changing():
            if device_id not in self._records:
                raise UnknownDevice(device_id)

            del self._records[device_id]
            self._save()


class _Changing:
    """
    One change to the registry: thread lock, process lock, fresh read.

    Reentrant within a thread, because ``activate`` is built on ``_update``;
    only the outermost level takes the process lock and re-reads the file.
    """

    def __init__(self, registry: DeviceRegistry) -> None:
        self.registry = registry

    def __enter__(self) -> _Changing:
        registry = self.registry
        registry._lock.acquire()

        try:
            if registry._changing_depth == 0 and registry._file_lock is not None:
                registry._file_lock.__enter__()

                try:
                    registry._records = registry._load()
                except BaseException:
                    registry._file_lock.__exit__(None, None, None)
                    raise

            registry._changing_depth += 1
        except BaseException:
            registry._lock.release()
            raise

        return self

    def __exit__(self, *exc: object) -> None:
        registry = self.registry

        try:
            registry._changing_depth -= 1

            if registry._changing_depth == 0 and registry._file_lock is not None:
                registry._file_lock.__exit__(*exc)
        finally:
            registry._lock.release()


def main() -> None:
    """Pair, connect, revoke — in memory."""

    registry = DeviceRegistry(path=None)

    phone = registry.create("Amin's phone", paired_from="192.168.1.42")
    print("created:", phone)

    print("key resolves before activation:",
          registry.secret_for(phone.device_id) is not None)

    registry.activate(phone.device_id)
    print("after activation:", registry.get(phone.device_id).describe())

    registry.revoke(phone.device_id)
    print("key resolves after revocation:",
          registry.secret_for(phone.device_id) is not None)


if __name__ == "__main__":
    main()
