"""
One JSON object per line, appended, rotated when it grows too large.

Extracted from the device link's audit log, which had the mechanics right and
was the only thing in Qronos that had them. The action audit trail needs the
same behaviour, and two copies of a log that must not lose records is one copy
too many.

The behaviour worth naming, because it is a choice and not an oversight:

    A log that cannot be written does not raise. A full disk, a read-only
    directory or a file held open by something else must not take down the
    thing being logged. The caller keeps its own in-memory copy of the record,
    so a lost line costs history, not correctness. The alternative — a link
    that drops the user's connection because it could not write a log line —
    is worse in every case.

    Rotation keeps exactly one previous file. Enough to survive the moment the
    current file turns over mid-incident, without an unbounded pile of history
    on a user's disk. Qronos manages its own storage, so a log that grows
    forever is a bug in the same product.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


# Small enough that a runaway logger cannot fill a disk, large enough to hold
# an ordinary session many times over.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024


class AppendOnlyLog:
    """
    A line-per-record file that trims itself.

    ``path=None`` disables the file entirely and every append becomes a no-op,
    which is what the tests and the in-memory demos use.
    """

    def __init__(
        self,
        path: str | Path | None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.path = None if path is None else Path(path)
        self.max_bytes = max_bytes

    @property
    def enabled(self) -> bool:
        return self.path is not None

    @property
    def previous_path(self) -> Path | None:
        """Where the last rotation put the older half, if there is one."""
        if self.path is None:
            return None

        return self.path.with_suffix(self.path.suffix + ".1")

    def append(self, payload: Mapping[str, Any]) -> bool:
        """
        Write one record. True when it reached the file.

        Never raises. The return value exists so a caller that genuinely wants
        to know can ask, without every caller having to handle an exception it
        would only swallow.
        """
        if self.path is None:
            return False

        line = json.dumps(dict(payload), ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed(len(encoded))

            with self.path.open("ab") as handle:
                handle.write(encoded)
        except OSError:
            return False

        return True

    def _rotate_if_needed(self, incoming: int) -> None:
        if self.path is None:
            return

        try:
            current = self.path.stat().st_size
        except OSError:
            # No file yet, or it cannot be measured. Either way there is
            # nothing to rotate and the append below will find out.
            return

        if current + incoming <= self.max_bytes:
            return

        previous = self.previous_path

        if previous is None:
            return

        try:
            os.replace(self.path, previous)
        except OSError:
            # Rotation failed, so the file keeps growing past the cap rather
            # than the record being dropped. History beyond the limit is the
            # cheaper thing to lose.
            return

    def read_records(self) -> tuple[dict[str, Any], ...]:
        """
        Every record currently in the live file, oldest first.

        Rotated records are not included: this reads what is on disk now, which
        is what a diagnostic wants. A line that cannot be parsed is skipped
        rather than raising, because a truncated final line is the expected
        result of a crash mid-write and is not a reason to refuse the rest.
        """
        if self.path is None:
            return ()

        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return ()

        records: list[dict[str, Any]] = []

        for line in text.splitlines():
            if not line.strip():
                continue

            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue

            if isinstance(parsed, dict):
                records.append(parsed)

        return tuple(records)
