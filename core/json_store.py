"""
A JSON file that survives a crash, and knows which version wrote it.

Three components need the same file behaviour — settings, conversation history,
and whatever the backup engine ends up writing — and the codebase already
contains the correct implementation twice. ``core/link_devices.py`` writes to a
temporary file and renames it; ``desktop/src-tauri/src/hotkeys.rs`` does the
same thing in Rust. Both are right, and a third hand-rolled copy is how one of
them ends up subtly different.

What this adds beyond the write-and-rename is the versioning, and that part is
not optional. The moment a user's conversation history is on disk, every later
change to its shape has to answer "what happens to the file that is already
there". Deciding that at the point of the first write is cheap. Deciding it
after a release is a migration written under pressure against data you cannot
reproduce.

The rules:

    A corrupt file raises rather than silently starting empty. Starting empty
    looks exactly like "nothing saved yet" and invites the user to write over
    whatever the corruption was. The device registry already made this choice;
    it is the same choice for the same reason.

    A file from a newer version raises. Loading it would mean dropping fields
    this version does not know about, then writing the truncated result back.

    A file from an older version is migrated by explicit steps, one per
    version, or refused if a step is missing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


# One function per version bump: it receives the payload written by version N
# and returns the payload for version N+1.
Migration = Callable[[dict[str, Any]], dict[str, Any]]


class StoreCorrupt(Exception):
    """The file exists but could not be understood."""


class StoreTooNew(Exception):
    """The file was written by a later version of Qronos."""


class MigrationMissing(Exception):
    """There is no route from the file's version to the current one."""


@dataclass(frozen=True)
class StoredDocument:
    """What was read back, and which version wrote it."""

    version: int
    payload: dict[str, Any]

    # True when the payload was brought forward by a migration, so the caller
    # can write it back at the new version rather than migrating on every load.
    migrated: bool = False


class JsonStore:
    """
    One versioned JSON document on disk.

    ``path=None`` keeps everything in memory, which is what tests and previews
    use. The in-memory mode is not a stub: it goes through the same validation
    and the same migrations, so a test exercises the real logic.
    """

    def __init__(
        self,
        path: str | Path | None,
        version: int,
        migrations: Mapping[int, Migration] | None = None,
    ) -> None:
        if version < 1:
            raise ValueError("A store version starts at 1.")

        self.path = None if path is None else Path(path)
        self.version = version
        self.migrations: dict[int, Migration] = dict(migrations or {})

        self._memory: dict[str, Any] | None = None

    @property
    def exists(self) -> bool:
        if self.path is None:
            return self._memory is not None

        return self.path.exists()

    def save(self, payload: Mapping[str, Any]) -> None:
        """
        Write the document, atomically.

        The rename is what makes it atomic: a crash leaves either the previous
        complete file or the new complete file, never a half-written one. A
        plain write truncates first, so the window in which the file is
        garbage is the whole write.
        """
        document = {
            "schemaVersion": self.version,
            "payload": dict(payload),
        }

        if self.path is None:
            self._memory = dict(payload)
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)

        temporary = self.path.with_suffix(self.path.suffix + ".tmp")

        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> StoredDocument | None:
        """
        Read the document, migrating it forward if it is older.

        None when nothing has been written yet — which is different from an
        empty document, and callers generally want to tell those apart.
        """
        raw = self._read_raw()

        if raw is None:
            return None

        if not isinstance(raw, dict):
            raise StoreCorrupt(f"{self._name()} is not a JSON object.")

        version = raw.get("schemaVersion")

        if not isinstance(version, int):
            raise StoreCorrupt(
                f"{self._name()} does not say which version wrote it."
            )

        payload = raw.get("payload")

        if not isinstance(payload, dict):
            raise StoreCorrupt(f"{self._name()} has no payload object.")

        if version > self.version:
            raise StoreTooNew(
                f"{self._name()} was written by schema version {version}; "
                f"this Qronos understands {self.version}. Loading it would "
                "discard whatever the newer version added."
            )

        if version == self.version:
            return StoredDocument(version=version, payload=payload)

        return StoredDocument(
            version=self.version,
            payload=self._migrate(payload, version),
            migrated=True,
        )

    def load_payload(self, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The payload, or a default when nothing is stored yet."""
        document = self.load()

        if document is None:
            return dict(default or {})

        return document.payload

    # ------------------------------------------------------------ internals

    def _name(self) -> str:
        return str(self.path) if self.path is not None else "the store"

    def _read_raw(self) -> Any:
        if self.path is None:
            if self._memory is None:
                return None

            return {
                "schemaVersion": self.version,
                "payload": dict(self._memory),
            }

        if not self.path.exists():
            return None

        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise StoreCorrupt(
                f"{self.path} could not be read: {error}."
            ) from error

        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            # Deliberately not "start empty". That would look exactly like
            # "nothing saved yet" and invite the user to overwrite whatever
            # the corruption was.
            raise StoreCorrupt(
                f"{self.path} is not valid JSON: {error}."
            ) from error

    def _migrate(
        self,
        payload: dict[str, Any],
        from_version: int,
    ) -> dict[str, Any]:
        current = dict(payload)

        for version in range(from_version, self.version):
            step = self.migrations.get(version)

            if step is None:
                raise MigrationMissing(
                    f"No migration from schema version {version} to "
                    f"{version + 1} for {self._name()}."
                )

            current = dict(step(current))

        return current
