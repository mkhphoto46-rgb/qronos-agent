"""
Export, back up and restore what belongs to the user — and nothing else.

The architecture lists five engines: History, Export, Backup, Restore and
Migration. They are one module because they are one question asked five ways:
*what does the user own, and how do they get it out, keep it, and put it back?*

**Scope is the whole design.** The rule is that an archive contains user data
and only user data. Not models, not the whistling runtime binaries, not caches,
not logs. Three reasons, in increasing order of how much trouble getting it
wrong causes:

    A ten-gigabyte model in a backup makes the backup useless — too big to
    keep, too slow to make, so nobody makes one.

    Runtime files restored onto a different machine are wrong there. Models are
    downloaded per machine; a restore is not an install.

    Logs and caches contain fragments of everything. An archive the user hands
    to somebody — a support engineer, a new laptop's setup wizard — should
    contain what they think it contains.

**This module handles personal data.** Conversation transcripts and device
pairing records are covered by it. The export format is deliberately plain
JSON, readable without Qronos, because an export that requires the software
that made it is not much of an export. Retention, what a support bundle may
include, and whether backups should be encrypted at rest are product and
compliance decisions that are explicitly *not* settled here; the structure
below is built so any answer can be implemented without changing the format.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from core.json_store import JsonStore, StoreCorrupt, StoreTooNew


ARCHIVE_VERSION = 1

# What the format calls itself, so a file picked out of a downloads folder can
# be identified before it is trusted.
ARCHIVE_KIND = "qronos-user-data"


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


class Section(Enum):
    """The kinds of user-owned data an archive can carry."""

    CONVERSATIONS = "conversations"
    SETTINGS = "settings"

    # Paired devices. Included by name so a restore can rebuild the list, but
    # see EXCLUDED_FROM_ARCHIVE: the pairing secrets are not part of it.
    DEVICES = "devices"


#: Never in an archive, with the reason. A list rather than a convention,
#: because "we would not do that" is not a control.
EXCLUDED_FROM_ARCHIVE: dict[str, str] = {
    "models": "Downloaded per machine. A restore is not an install.",
    "runtime": "Binaries belong to the installation, not to the user.",
    "cache": "Reproducible, and full of fragments of everything.",
    "logs": "Diagnostics, not user data. Kept out so an archive is safe to share.",
    "temp": "Scratch space. Nothing here outlives the session that made it.",
    "device_secrets": (
        "Pairing keys. Restoring them onto another machine would clone a "
        "trusted device rather than pair a new one."
    ),
}


class ArchiveInvalid(Exception):
    """A file is not a Qronos archive, or not one this version can read."""


@dataclass(frozen=True)
class ArchiveSummary:
    """What is in an archive, without loading all of it."""

    version: int
    created_at: float
    sections: tuple[Section, ...]
    counts: Mapping[str, int]

    def describe(self) -> str:
        parts = [
            f"{section.value}: {self.counts.get(section.value, 0)}"
            for section in self.sections
        ]

        return ", ".join(parts) if parts else "empty"


#: Supplies one section's data for an export.
SectionExporter = Callable[[], Any]

#: Takes one section's data back in during a restore. Returns how many items
#: were accepted.
SectionImporter = Callable[[Any], int]


class UserDataArchive:
    """
    Builds and reads the archive that holds a user's own data.

    Sections are registered rather than hard-coded, so the module does not
    import every store in the codebase — and so a test can register a fake
    without a filesystem.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or time.time

        self._exporters: dict[Section, SectionExporter] = {}
        self._importers: dict[Section, SectionImporter] = {}

    def register(
        self,
        section: Section,
        exporter: SectionExporter | None = None,
        importer: SectionImporter | None = None,
    ) -> None:
        if exporter is not None:
            self._exporters[section] = exporter

        if importer is not None:
            self._importers[section] = importer

    def registered_sections(self) -> tuple[Section, ...]:
        return tuple(
            section
            for section in Section
            if section in self._exporters or section in self._importers
        )

    # ------------------------------------------------------------- export

    def build(self) -> dict[str, Any]:
        """Assemble an archive from every registered section."""
        sections: dict[str, Any] = {}
        counts: dict[str, int] = {}

        for section, exporter in self._exporters.items():
            data = exporter()
            sections[section.value] = data
            counts[section.value] = _count(data)

        return {
            "kind": ARCHIVE_KIND,
            "archiveVersion": ARCHIVE_VERSION,
            "createdAt": round(self.clock(), 3),
            "excluded": dict(EXCLUDED_FROM_ARCHIVE),
            "counts": counts,
            "sections": sections,
        }

    def write(self, path: str | Path) -> Path:
        """
        Write an archive to a file.

        Goes through :class:`core.json_store.JsonStore` so the write is atomic:
        an interrupted backup leaves the previous archive intact rather than a
        truncated file that looks like a backup and is not.
        """
        destination = Path(path)
        store = JsonStore(destination, version=ARCHIVE_VERSION)
        store.save(self.build())

        return destination

    # ------------------------------------------------------------ inspect

    @staticmethod
    def summarise(archive: Mapping[str, Any]) -> ArchiveSummary:
        """Read the header without trusting the body."""
        if archive.get("kind") != ARCHIVE_KIND:
            raise ArchiveInvalid("This is not a Qronos data archive.")

        version = archive.get("archiveVersion")

        if not isinstance(version, int):
            raise ArchiveInvalid("The archive does not say which version it is.")

        if version > ARCHIVE_VERSION:
            raise ArchiveInvalid(
                f"The archive is version {version}; this Qronos reads "
                f"{ARCHIVE_VERSION}. Restoring it would drop whatever the "
                "newer version added."
            )

        raw_sections = archive.get("sections", {})

        if not isinstance(raw_sections, Mapping):
            raise ArchiveInvalid("The archive has no sections.")

        sections: list[Section] = []

        for name in raw_sections:
            try:
                sections.append(Section(str(name)))
            except ValueError:
                # A section this version does not know. Ignored rather than
                # refused: an archive containing something extra is still
                # usable for everything else in it.
                continue

        counts = archive.get("counts", {})

        return ArchiveSummary(
            version=version,
            created_at=float(archive.get("createdAt", 0.0)),
            sections=tuple(sections),
            counts=counts if isinstance(counts, Mapping) else {},
        )

    @staticmethod
    def read(path: str | Path) -> dict[str, Any]:
        """Load an archive from a file."""
        source = Path(path)

        try:
            document = JsonStore(source, version=ARCHIVE_VERSION).load()
        except (StoreCorrupt, StoreTooNew) as error:
            raise ArchiveInvalid(str(error)) from error

        if document is None:
            raise ArchiveInvalid(f"{source} does not exist.")

        return document.payload

    # ------------------------------------------------------------ restore

    def restore(self, archive: Mapping[str, Any]) -> dict[str, int]:
        """
        Put an archive back. Returns how many items each section took.

        The summary is checked first, so a file that is not an archive, or is
        from a newer Qronos, is refused before anything is written. A restore
        that half-succeeds is worse than one that does not start.
        """
        self.summarise(archive)

        raw_sections = archive.get("sections", {})
        restored: dict[str, int] = {}

        for name, data in raw_sections.items():
            try:
                section = Section(str(name))
            except ValueError:
                continue

            importer = self._importers.get(section)

            if importer is None:
                continue

            restored[section.value] = importer(data)

        return restored


def _count(data: Any) -> int:
    """How many items a section holds, for the archive header."""
    if isinstance(data, Mapping):
        for key in ("conversations", "items", "records"):
            nested = data.get(key)

            if isinstance(nested, list):
                return len(nested)

        return len(data)

    if isinstance(data, list):
        return len(data)

    return 0


def readable_export(archive: Mapping[str, Any]) -> str:
    """
    An archive as text a person can actually read.

    An export that needs the software that produced it is not much of an
    export. Indented JSON with Persian left unescaped is legible in any editor.
    """
    return json.dumps(archive, ensure_ascii=False, indent=2, sort_keys=True)
