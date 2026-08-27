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
import json
import os
import re
import secrets
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

    @property
    def can_authenticate(self) -> bool:
        """A pending device may connect; that is how pairing completes."""
        return self.status in {DeviceStatus.PENDING, DeviceStatus.ACTIVE}

    def __repr__(self) -> str:
        """Never render the key."""
        return (
            f"DeviceRecord(device_id={self.device_id!r}, "
            f"name={self.name!r}, status={self.status.value}, "
            f"secret=<redacted {len(self.secret)} bytes>, "
            f"remote_enabled={self.remote_enabled})"
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
        )


class DeviceRegistry:
    """
    The paired devices, backed by one JSON file.

    Reads are served from memory; every change is written through immediately.
    A phone pairing or being revoked is rare and consequential, so there is no
    value in batching it and real cost in losing it to a crash.
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
        self._records: dict[str, DeviceRecord] = self._load()

    # ------------------------------------------------------------- persistence

    def _load(self) -> dict[str, DeviceRecord]:
        if self.path is None or not self.path.exists():
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

        # Best effort on the directory. Windows ignores the mode, which is why
        # this is not the only protection the key file has.
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

        temporary = self.path.with_suffix(self.path.suffix + ".tmp")

        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass

            # Replace, so a crash mid-write leaves the previous file intact
            # rather than a truncated one.
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    # ------------------------------------------------------------------ queries

    def __len__(self) -> int:
        return len(self._records)

    def get(self, device_id: str) -> DeviceRecord | None:
        return self._records.get(device_id)

    def all(self) -> tuple[DeviceRecord, ...]:
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

        record = self._records.get(device_id)

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

        record = self._records.get(device_id)

        if record is None:
            raise UnknownDevice(device_id)

        if record.status is DeviceStatus.REVOKED:
            raise ValueError(f"{device_id} is revoked and cannot be activated")

        return self._update(
            device_id,
            status=DeviceStatus.ACTIVE,
            last_seen_at=self.clock(),
        )

    def revoke(self, device_id: str) -> DeviceRecord:
        return self._update(device_id, status=DeviceStatus.REVOKED)

    def rename(self, device_id: str, name: str) -> DeviceRecord:
        """A device's name is a label. Its identity does not change with it."""
        return self._update(device_id, name=name)

    def touch(self, device_id: str) -> DeviceRecord:
        return self._update(device_id, last_seen_at=self.clock())

    def set_remote_enabled(
        self, device_id: str, enabled: bool
    ) -> DeviceRecord:
        """Layer 2 opt-in for one device."""
        return self._update(device_id, remote_enabled=enabled)

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

        if device_id not in self._records:
            raise UnknownDevice(device_id)

        del self._records[device_id]
        self._save()


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
