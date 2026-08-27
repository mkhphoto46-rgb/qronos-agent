from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Protocol

from core.config import CONFIG
from core.storage_budget import BudgetComponent


DEFAULT_REGISTRY_PATH = CONFIG.paths.data / "artifact_ownership.json"

REGISTRY_FORMAT_VERSION = 1


class ArtifactOwner(Enum):
    """Who owns an artifact Qronos produced."""

    QRONOS = "qronos"
    USER = "user"


class OwnershipTransferError(RuntimeError):
    """Raised when an ownership transfer cannot be completed safely."""


@dataclass(frozen=True)
class ArtifactRecord:
    """
    One artifact Qronos created, and who owns it now.

    Ownership is the single fact that decides whether Qronos may delete a file.
    Every other property below is derived from it, deliberately, so the two can
    never drift apart:

    ``owner = qronos``  temporary, auto-cleanup allowed, counts toward quota
    ``owner = user``    permanent, never auto-deleted, outside every quota

    Once ownership has moved to the user, Qronos has no further say over the
    file. Age and size caps do not apply to it, and it stops ageing for the
    purposes of cleanup, because it is no longer Qronos's data.
    """

    artifact_id: str
    path: Path
    owner: ArtifactOwner
    component: BudgetComponent
    size_bytes: int
    created_at: float
    transferred_at: float | None = None

    @property
    def is_temporary(self) -> bool:
        return self.owner is ArtifactOwner.QRONOS

    @property
    def auto_cleanup_allowed(self) -> bool:
        return self.owner is ArtifactOwner.QRONOS

    @property
    def counts_toward_quota(self) -> bool:
        return self.owner is ArtifactOwner.QRONOS

    def to_json(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "path": str(self.path),
            "owner": self.owner.value,
            "component": self.component.value,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "transferred_at": self.transferred_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> ArtifactRecord:
        return cls(
            artifact_id=str(data["artifact_id"]),
            path=Path(str(data["path"])),
            owner=ArtifactOwner(str(data["owner"])),
            component=BudgetComponent(str(data["component"])),
            size_bytes=int(data["size_bytes"]),  # type: ignore[arg-type]
            created_at=float(data["created_at"]),  # type: ignore[arg-type]
            transferred_at=(
                None
                if data.get("transferred_at") is None
                else float(data["transferred_at"])  # type: ignore[arg-type]
            ),
        )


class OwnershipStore(Protocol):
    """Persistence for the ownership registry."""

    def load(self) -> tuple[ArtifactRecord, ...]:
        ...

    def save(self, records: tuple[ArtifactRecord, ...]) -> None:
        ...


class InMemoryOwnershipStore:
    """Non-persistent store, used by tests and by ephemeral sessions."""

    def __init__(
        self,
        records: Iterable[ArtifactRecord] = (),
    ) -> None:
        self._records: tuple[ArtifactRecord, ...] = tuple(records)
        self.save_count = 0

    def load(self) -> tuple[ArtifactRecord, ...]:
        return self._records

    def save(self, records: tuple[ArtifactRecord, ...]) -> None:
        self._records = tuple(records)
        self.save_count += 1


class JsonFileOwnershipStore:
    """
    Ownership registry persisted as a single JSON file.

    Writes go to a temporary file in the same directory and are then moved into
    place with :func:`os.replace`, which is atomic on the same filesystem. A
    crash mid-write therefore leaves the previous registry intact rather than a
    half-written file: losing the record of who owns a file would be worse than
    losing the write.

    The registry is essential state, so it is persisted; telemetry-style data is
    not, in line with the rule that Qronos minimises unnecessary SSD writes.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_REGISTRY_PATH,
    ) -> None:
        self.path = Path(path)

    def load(self) -> tuple[ArtifactRecord, ...]:
        if not self.path.is_file():
            return ()

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt registry must not be silently replaced with an empty
            # one: that would turn every user-owned artifact into a deletion
            # candidate. Refuse to load instead.
            raise OwnershipTransferError(
                f"Ownership registry could not be read: {self.path}"
            ) from exc

        if not isinstance(raw, dict):
            raise OwnershipTransferError(
                f"Ownership registry has an unexpected shape: {self.path}"
            )

        entries = raw.get("artifacts", [])

        if not isinstance(entries, list):
            raise OwnershipTransferError(
                f"Ownership registry has an unexpected shape: {self.path}"
            )

        return tuple(
            ArtifactRecord.from_json(entry)
            for entry in entries
        )

    def save(self, records: tuple[ArtifactRecord, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "version": REGISTRY_FORMAT_VERSION,
            "artifacts": [record.to_json() for record in records],
        }

        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.tmp"
        )

        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            os.replace(temporary, self.path)
        finally:
            # If os.replace succeeded the temporary file is gone already.
            temporary.unlink(missing_ok=True)


def _default_id_factory() -> str:
    return uuid.uuid4().hex


class ArtifactOwnershipRegistry:
    """
    Tracks who owns every artifact Qronos creates.

    The registry is the authority the storage janitor consults before deleting
    anything. Its central guarantee is one-directional: ownership moves from
    Qronos to the user and never back. There is deliberately no method to
    reclaim an artifact, because a bug that reclaimed one would delete a user's
    file.
    """

    def __init__(
        self,
        store: OwnershipStore | None = None,
        id_factory: Callable[[], str] = _default_id_factory,
    ) -> None:
        self.store: OwnershipStore = (
            store
            if store is not None
            else InMemoryOwnershipStore()
        )

        self._id_factory = id_factory
        self._records: dict[str, ArtifactRecord] = {
            record.artifact_id: record
            for record in self.store.load()
        }

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _normalise(path: str | Path) -> Path:
        """
        Resolve a path for comparison.

        ``strict=False`` so that a path which does not exist yet still
        normalises, which matters when an artifact is registered before it has
        been written.
        """
        return Path(path).expanduser().resolve()

    def _persist(self) -> None:
        self.store.save(tuple(self._records.values()))

    # ------------------------------------------------------------- inspection

    @property
    def records(self) -> tuple[ArtifactRecord, ...]:
        return tuple(self._records.values())

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        return self._records.get(artifact_id)

    def for_path(self, path: str | Path) -> ArtifactRecord | None:
        """Return the record for ``path``, if the path is registered."""
        target = self._normalise(path)

        for record in self._records.values():
            if record.path == target:
                return record

        return None

    def is_user_owned(self, path: str | Path) -> bool:
        """
        True when the registry says the user owns this path.

        The janitor calls this before every deletion. An unregistered path is
        not user-owned as far as the registry knows, so callers must apply their
        own orphan policy rather than treating False as permission to delete.
        """
        record = self.for_path(path)

        return (
            record is not None
            and record.owner is ArtifactOwner.USER
        )

    def qronos_owned(
        self,
        component: BudgetComponent | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        """Every Qronos-owned record, optionally filtered by component."""
        return tuple(
            record
            for record in self._records.values()
            if record.owner is ArtifactOwner.QRONOS
            and (component is None or record.component is component)
        )

    def user_owned(self) -> tuple[ArtifactRecord, ...]:
        return tuple(
            record
            for record in self._records.values()
            if record.owner is ArtifactOwner.USER
        )

    def quota_bytes(
        self,
        component: BudgetComponent | None = None,
    ) -> int:
        """
        Total size counted against Qronos's quota.

        User-owned artifacts are excluded, which is the whole point of the
        ownership model: confirming a file removes it from Qronos's budget as
        well as from its reach.
        """
        return sum(
            record.size_bytes
            for record in self.qronos_owned(component)
        )

    # ---------------------------------------------------------------- mutation

    def register(
        self,
        path: str | Path,
        component: BudgetComponent,
        size_bytes: int,
        created_at: float,
        artifact_id: str | None = None,
    ) -> ArtifactRecord:
        """
        Record a newly created artifact as Qronos-owned.

        Every generated artifact starts here: a preview, a screenshot, an OCR
        crop, a report, a converted file. Nothing Qronos makes is user-owned
        until the user says so.
        """
        if size_bytes < 0:
            raise ValueError(
                f"size_bytes must not be negative: {size_bytes}"
            )

        target = self._normalise(path)
        existing = self.for_path(target)

        if existing is not None:
            raise OwnershipTransferError(
                f"Path is already registered as {existing.owner.value}: "
                f"{target}"
            )

        record = ArtifactRecord(
            artifact_id=artifact_id or self._id_factory(),
            path=target,
            owner=ArtifactOwner.QRONOS,
            component=component,
            size_bytes=size_bytes,
            created_at=created_at,
        )

        if record.artifact_id in self._records:
            raise OwnershipTransferError(
                f"Duplicate artifact id: {record.artifact_id}"
            )

        self._records[record.artifact_id] = record
        self._persist()

        return record

    def transfer_to_user(
        self,
        artifact_id: str,
        final_path: str | Path,
        transferred_at: float,
        size_bytes: int | None = None,
    ) -> ArtifactRecord:
        """
        Move ownership of an artifact to the user.

        Called only after the surrounding operation has completed and been
        verified: target validated, permission checked, file copied or moved,
        destination confirmed. This method performs the final state change and
        nothing else.

        The destination must exist. Recording a transfer for a file that was
        never written would leave Qronos believing the user owns something that
        is not there, and would remove it from the quota it still occupies.

        Transfer is irreversible by design.
        """
        record = self._records.get(artifact_id)

        if record is None:
            raise OwnershipTransferError(
                f"Unknown artifact id: {artifact_id}"
            )

        if record.owner is ArtifactOwner.USER:
            raise OwnershipTransferError(
                f"Artifact is already user-owned: {artifact_id}"
            )

        destination = self._normalise(final_path)

        if not destination.is_file():
            raise OwnershipTransferError(
                "Ownership transfer requires a verified destination, but no "
                f"file exists at: {destination}"
            )

        clash = self.for_path(destination)

        if clash is not None and clash.artifact_id != artifact_id:
            raise OwnershipTransferError(
                f"Destination is already registered: {destination}"
            )

        try:
            resolved_size = (
                destination.stat().st_size
                if size_bytes is None
                else size_bytes
            )
        except OSError as exc:
            raise OwnershipTransferError(
                f"Destination could not be measured: {destination}"
            ) from exc

        transferred = replace(
            record,
            path=destination,
            owner=ArtifactOwner.USER,
            size_bytes=resolved_size,
            transferred_at=transferred_at,
        )

        self._records[artifact_id] = transferred
        self._persist()

        return transferred

    def forget(self, artifact_id: str) -> None:
        """
        Drop a record.

        Used after a Qronos-owned artifact has been deleted, so the registry
        does not accumulate entries for files that no longer exist.

        Refuses to drop user-owned records. Forgetting one would make the file
        look like an orphan on the next cleanup pass, which is precisely how a
        user's file gets deleted by accident.
        """
        record = self._records.get(artifact_id)

        if record is None:
            return

        if record.owner is ArtifactOwner.USER:
            raise OwnershipTransferError(
                "Refusing to forget a user-owned artifact: "
                f"{artifact_id}"
            )

        del self._records[artifact_id]
        self._persist()

    def prune_missing_qronos_records(self) -> tuple[ArtifactRecord, ...]:
        """
        Drop Qronos-owned records whose files no longer exist.

        Keeps quota accounting honest after an external deletion. User-owned
        records are never pruned, even when the file is missing: the user may
        have moved it, and Qronos forgetting that it is theirs is worse than a
        stale entry.
        """
        removed: list[ArtifactRecord] = []

        for record in tuple(self._records.values()):
            if record.owner is not ArtifactOwner.QRONOS:
                continue

            if not record.path.exists():
                del self._records[record.artifact_id]
                removed.append(record)

        if removed:
            self._persist()

        return tuple(removed)


def main() -> None:
    """Show a summary of the persisted ownership registry."""
    registry = ArtifactOwnershipRegistry(
        store=JsonFileOwnershipStore()
    )

    print("=== Qronos Artifact Ownership ===")
    print(f"Records: {len(registry.records)}")
    print(f"Qronos-owned: {len(registry.qronos_owned())}")
    print(f"User-owned: {len(registry.user_owned())}")

    for component in BudgetComponent:
        total = registry.quota_bytes(component)

        if total:
            print(f"{component.value}: {total} bytes counted")


if __name__ == "__main__":
    main()
