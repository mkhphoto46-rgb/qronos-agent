from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import threading

_LOCK = threading.RLock()


def _local_app_data() -> Path:
    raw = os.environ.get("LOCALAPPDATA", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".qronos"


def _documents() -> Path:
    return Path.home() / "Documents"


def locator_path() -> Path:
    return _local_app_data() / "Qronos" / "storage_locations.json"


@dataclass(frozen=True)
class StorageLocations:
    data_root: str
    history_root: str
    memory_root: str
    library_db_root: str
    artifact_root: str
    cache_root: str
    backup_root: str

    def to_public_dict(self) -> dict[str, str]:
        return {
            "dataRoot": self.data_root,
            "historyRoot": self.history_root,
            "memoryRoot": self.memory_root,
            "libraryDbRoot": self.library_db_root,
            "artifactRoot": self.artifact_root,
            "cacheRoot": self.cache_root,
            "backupRoot": self.backup_root,
        }


def _defaults() -> StorageLocations:
    local = _local_app_data() / "Qronos"
    docs = _documents() / "Qronos"
    return StorageLocations(
        data_root=str(local),
        history_root=str(local / "History"),
        memory_root=str(local / "Memory"),
        library_db_root=str(local / "Library"),
        artifact_root=str(docs / "Library"),
        cache_root=str(local / "Cache"),
        backup_root=str(docs / "Backups"),
    )


def _from_raw(raw: dict[str, object]) -> StorageLocations:
    default = _defaults()
    def value(key: str, fallback: str) -> str:
        found = raw.get(key)
        if isinstance(found, str) and found.strip():
            return found.strip()
        return fallback
    return StorageLocations(
        data_root=value("dataRoot", default.data_root),
        history_root=value("historyRoot", default.history_root),
        memory_root=value("memoryRoot", default.memory_root),
        library_db_root=value("libraryDbRoot", default.library_db_root),
        artifact_root=value("artifactRoot", default.artifact_root),
        cache_root=value("cacheRoot", default.cache_root),
        backup_root=value("backupRoot", default.backup_root),
    )


def load_storage_locations(*, create: bool = True) -> StorageLocations:
    with _LOCK:
        path = locator_path()
        locations = _defaults()
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    locations = _from_raw(raw)
            except (OSError, ValueError, TypeError):
                locations = _defaults()
        elif create:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(locations.to_public_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        if create:
            ensure_storage_directories(locations)
        return locations


def ensure_storage_directories(locations: StorageLocations | None = None) -> None:
    locations = locations or load_storage_locations(create=False)
    for raw in (
        locations.history_root,
        locations.memory_root,
        locations.library_db_root,
        locations.artifact_root,
        locations.cache_root,
        locations.backup_root,
    ):
        Path(raw).mkdir(parents=True, exist_ok=True)


def set_storage_location(key: str, destination: str) -> StorageLocations:
    allowed = {
        "dataRoot", "historyRoot", "memoryRoot", "libraryDbRoot",
        "artifactRoot", "cacheRoot", "backupRoot",
    }
    if key not in allowed:
        raise ValueError(f"Unsupported storage location key: {key}")
    cleaned = destination.strip()
    if not cleaned:
        raise ValueError("Storage destination must not be empty.")
    dest = Path(cleaned).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    probe = dest / ".qronos-write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)
    with _LOCK:
        current = load_storage_locations(create=True).to_public_dict()
        current[key] = str(dest)
        path = locator_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    return load_storage_locations(create=True)
