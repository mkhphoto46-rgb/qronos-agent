"""
Doing a command once, however many times it arrives.

TLS already makes the bytes on the wire impossible to replay: every session has
its own keys, so a recorded request fed into a new connection fails to decrypt,
and a record repeated inside one session fails its sequence check. What TLS
cannot stop is the phone itself sending a command twice. Over a relay that is
the normal case rather than the odd one: a request goes out, the mobile
network drops, and the phone cannot know whether the PC received it, ran it,
or answered into a connection that no longer existed. The only safe retry is
one the PC can recognise as the same command.

So a request may carry a command id, ``cid``: 128 random bits chosen by the
phone once per command and reused on every retry of it. The ledger here
remembers, per device, which ids it has seen and what came of them:

    new           run it
    in progress   do not run it again; tell the phone to wait
    done          do not run it again; send the answer it already earned

The id is bound to what it was first sent with. The same id arriving with a
different operation or different parameters is a conflict and is refused,
because otherwise a retry could be turned into a different command that skips
execution and collects someone else's answer.

Over the relay a command id is required for everything except ``ping``. On the
local network it stays optional, so a Layer 1 client that has never heard of it
keeps working unchanged.

Everything is bounded. Entries expire, each device has a cap, and the answers
kept for replay have a byte budget; an answer that does not fit is forgotten
while its id is still remembered, so a late retry learns the command was done
rather than running it again. The ledger is memory only. It does not survive
the PC restarting, which is stated rather than hidden: the remote profile
holds only ``ask``, ``search`` and ``status``, so the worst a retry across a
restart can do is spend the computation twice.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from core.link_protocol import COMMAND_ID_PATTERN

# Long enough to cover a phone reconnecting after a dropped mobile link and
# retrying, short enough that the memory is not a history of the user's day.
COMMAND_TTL_SECONDS = 600.0

MAX_COMMANDS_PER_DEVICE = 64

# Answers kept so a retry can collect them. One long cited answer is tens of
# kilobytes; this keeps a handful per device without letting a phone that
# retries everything grow the PC's memory.
MAX_RETAINED_RESULT_BYTES = 256 * 1024
MAX_TOTAL_RETAINED_BYTES = 2 * 1024 * 1024


def is_valid_command_id(value: object) -> bool:
    return isinstance(value, str) and bool(COMMAND_ID_PATTERN.match(value))


def command_fingerprint(op: str, params: dict[str, Any]) -> str:
    """
    What a command id is bound to.

    A digest rather than the parameters themselves, so the ledger never holds a
    question the user asked after the command has finished.
    """

    canonical = json.dumps(
        {"op": op, "params": params},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CommandState(Enum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CommandDecision:
    """What to do with one arriving command."""

    state: CommandState

    # For DONE, the answer as it was first sent, minus its request id, or
    # ``None`` when it was too large to keep.
    outcome: dict[str, Any] | None = None


@dataclass
class _Entry:
    fingerprint: str
    started_at: float
    finished: bool = False
    outcome: dict[str, Any] | None = None
    retained_bytes: int = 0


class CommandLedger:
    """Per-device memory of command ids. Safe to share between sessions."""

    def __init__(
        self,
        ttl: float = COMMAND_TTL_SECONDS,
        max_per_device: int = MAX_COMMANDS_PER_DEVICE,
        max_result_bytes: int = MAX_RETAINED_RESULT_BYTES,
        max_total_bytes: int = MAX_TOTAL_RETAINED_BYTES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl = ttl
        self.max_per_device = max_per_device
        self.max_result_bytes = max_result_bytes
        self.max_total_bytes = max_total_bytes
        self.clock = clock if clock is not None else time.monotonic

        self._lock = threading.Lock()
        self._devices: dict[str, OrderedDict[str, _Entry]] = {}
        self._retained = 0

    # ---------------------------------------------------------------- queries

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained

    def size(self, device_id: str) -> int:
        with self._lock:
            return len(self._devices.get(device_id, ()))

    # ---------------------------------------------------------------- changes

    def begin(
        self,
        device_id: str,
        command_id: str,
        fingerprint: str,
    ) -> CommandDecision:
        """
        Claim a command id, or report what became of it.

        Claiming and checking are one step under the lock, so two copies of a
        command arriving on two connections at once cannot both be told NEW.
        """

        with self._lock:
            entries = self._devices.setdefault(device_id, OrderedDict())
            self._expire(entries)

            entry = entries.get(command_id)

            if entry is not None:
                if entry.fingerprint != fingerprint:
                    return CommandDecision(CommandState.CONFLICT)

                if not entry.finished:
                    return CommandDecision(CommandState.IN_PROGRESS)

                return CommandDecision(CommandState.DONE, entry.outcome)

            while len(entries) >= self.max_per_device:
                if not self._evict_oldest_finished(entries):
                    # Every remembered command is still running. Refusing the
                    # new one is the bounded answer; the phone can retry.
                    return CommandDecision(CommandState.IN_PROGRESS)

            entries[command_id] = _Entry(
                fingerprint=fingerprint, started_at=self.clock()
            )

            return CommandDecision(CommandState.NEW)

    def finish(
        self,
        device_id: str,
        command_id: str,
        outcome: dict[str, Any],
    ) -> None:
        """Record the answer a command produced, if there is room to keep it."""

        size = _encoded_size(outcome)

        with self._lock:
            entry = self._devices.get(device_id, {}).get(command_id)

            if entry is None or entry.finished:
                return

            entry.finished = True

            if size > self.max_result_bytes:
                return

            while (
                self._retained + size > self.max_total_bytes
                and self._drop_oldest_outcome()
            ):
                pass

            if self._retained + size > self.max_total_bytes:
                return

            entry.outcome = outcome
            entry.retained_bytes = size
            self._retained += size

    def forget_device(self, device_id: str) -> None:
        """Drop everything about one device, as revocation does."""

        with self._lock:
            entries = self._devices.pop(device_id, None)

            if entries:
                for entry in entries.values():
                    self._retained -= entry.retained_bytes

    # ---------------------------------------------------------------- helpers

    def _expire(self, entries: OrderedDict[str, _Entry]) -> None:
        cutoff = self.clock() - self.ttl

        for command_id in [
            key
            for key, entry in entries.items()
            if entry.finished and entry.started_at < cutoff
        ]:
            self._retained -= entries.pop(command_id).retained_bytes

    def _evict_oldest_finished(self, entries: OrderedDict[str, _Entry]) -> bool:
        for command_id, entry in entries.items():
            if entry.finished:
                self._retained -= entry.retained_bytes
                del entries[command_id]

                return True

        return False

    def _drop_oldest_outcome(self) -> bool:
        """Free the oldest retained answer anywhere. False if there is none."""

        oldest: _Entry | None = None

        for entries in self._devices.values():
            for entry in entries.values():
                if entry.outcome is not None and (
                    oldest is None or entry.started_at < oldest.started_at
                ):
                    oldest = entry

        if oldest is None:
            return False

        self._retained -= oldest.retained_bytes
        oldest.outcome = None
        oldest.retained_bytes = 0

        return True


def _encoded_size(outcome: dict[str, Any]) -> int:
    try:
        return len(
            json.dumps(outcome, ensure_ascii=False, separators=(",", ":"))
            .encode("utf-8")
        )
    except (TypeError, ValueError):
        # Not serialisable means it could not have been sent either. Count it
        # as too large to keep, which is the safe answer.
        return MAX_TOTAL_RETAINED_BYTES + 1


class RequestBudget:
    """
    A token bucket for one remote session.

    Not an authentication control: the session is already authenticated. It
    bounds how much work a phone over the internet can queue on the PC — a lost
    phone in someone else's hand included — before the owner revokes it.
    """

    def __init__(
        self,
        capacity: float,
        refill_per_second: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.clock = clock if clock is not None else time.monotonic
        self._tokens = capacity
        self._at = self.clock()

    def take(self) -> bool:
        now = self.clock()
        elapsed = max(0.0, now - self._at)
        self._at = now
        self._tokens = min(
            self.capacity, self._tokens + elapsed * self.refill_per_second
        )

        if self._tokens < 1.0:
            return False

        self._tokens -= 1.0

        return True
