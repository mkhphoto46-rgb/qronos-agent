"""
One recent reading of the machine, shared, instead of many fresh ones.

Reading resources is not free. ``nvidia-smi`` is a process launch, and
``psutil`` CPU sampling needs a real interval to mean anything. The orchestrator
reads twice per step; the governor reads on every reservation; the telemetry
panel reads on a timer. Each of those is a separate trip to the operating
system for a number that has not meaningfully changed in the last fraction of a
second.

The architecture asks for a 250–500 ms snapshot behind all of them. This is
that, with two deliberate properties:

    It is pull-based, not a background thread. A sampler thread would keep
    launching ``nvidia-smi`` while Qronos sits idle, which is precisely the
    behaviour a resource-aware assistant is supposed to avoid. The cache
    refreshes when somebody asks and the value is stale, so an idle Qronos
    reads nothing at all.

    A failed read serves the previous value rather than propagating. A missing
    GPU or a momentarily unavailable ``nvidia-smi`` should not turn into an
    exception three layers up in a voice turn. The reading carries its own age,
    so a caller that cares can ask how old it is.

Not unified with the desktop yet. The Rust side reads CPU and memory through
the sysinfo crate and never crosses into Python. Closing that needs the desktop
to consume the bridge's readings, which is a change on the other side of the
process boundary and is not attempted here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from core.resource_guard import (
    GpuStatus,
    SystemStatus,
    read_gpu_status,
    read_system_status,
    read_system_status_since_last_call,
)


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


SystemReader = Callable[[], SystemStatus]
GpuReader = Callable[[], Optional[GpuStatus]]


# The middle of the range the architecture specifies. Short enough that a
# reading still describes now, long enough that a burst of callers within one
# voice turn costs one trip to the operating system rather than a dozen.
#
# This window is only achievable because the default system reader does not
# block. ``read_system_status`` samples the CPU over half a second, so a cache
# built on it would hand back readings that were already 500 ms old and
# refresh on every single call — a cache that never hits, which is what the
# first version of this module did. Measured on this machine: 501 ms for the
# blocking reader against 0.2 ms for the one used here.
DEFAULT_MAX_AGE_SECONDS = 0.375


@dataclass(frozen=True)
class Snapshot:
    """One reading of the machine, and when it was taken."""

    system: SystemStatus
    gpu: Optional[GpuStatus]
    taken_at: float

    # True when the underlying read failed and this is the previous value
    # being served again. A caller showing numbers to a person may want to say
    # so; one making a decision generally should not care.
    stale: bool = False

    def age(self, now: float) -> float:
        return max(0.0, now - self.taken_at)


class TelemetryCache:
    """
    A recent reading, refreshed on demand.

    Thread-safe: the bridge runs a voice turn on one thread while the reader
    loop handles commands on another, and both may want the machine's state.
    """

    def __init__(
        self,
        read_system: SystemReader | None = None,
        read_gpu: GpuReader | None = None,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        self.read_system: SystemReader = (
            read_system or read_system_status_since_last_call
        )
        self.read_gpu: GpuReader = read_gpu or read_gpu_status
        self.max_age_seconds = max_age_seconds
        self.clock: Clock = clock or time.time

        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._reads = 0

        # Distinct from ``_snapshot is None``, which invalidate() also
        # produces. Only the genuinely first read pays for accuracy; an
        # invalidated cache has already primed the counter, so making it
        # block again would put half a second on every invalidate.
        self._ever_read = False

        # The very first reading is taken with the accurate reader, and every
        # one after it with the cheap one.
        #
        # The cheap reader reports CPU use since the previous call, so its
        # first answer has nothing to measure against and comes back as 0.0.
        # That is not merely imprecise, it is wrong in the dangerous
        # direction: 0% reads as an idle machine, and the governor grants
        # heavy work on it. One blocking half-second at startup buys a first
        # answer that is true, and it also primes the counter so every later
        # reading is both cheap and correct.
        #
        # Only applies to the default reader. An injected one belongs to the
        # caller, who knows what it does.
        self._first_read: SystemReader = (
            read_system_status if read_system is None else self.read_system
        )

    @property
    def reads(self) -> int:
        """
        How many times the machine was actually read.

        Exposed so the saving can be asserted rather than assumed. A cache
        whose hit rate nobody measures is a cache nobody knows is working.
        """
        with self._lock:
            return self._reads

    def current(self) -> Snapshot:
        """The reading, refreshing it first if it has aged out."""
        with self._lock:
            now = self.clock()
            existing = self._snapshot

            if (
                existing is not None
                and existing.age(now) < self.max_age_seconds
            ):
                return existing

            return self._refresh(now)

    def refresh(self) -> Snapshot:
        """Force a read, however recent the last one was."""
        with self._lock:
            return self._refresh(self.clock())

    def invalidate(self) -> None:
        """
        Throw the reading away.

        For the moment after something large is known to have changed — a model
        finished loading, a heavy task ended — where waiting out the window
        would answer with a picture of the world that no longer holds.
        """
        with self._lock:
            self._snapshot = None

    def _refresh(self, now: float) -> Snapshot:
        """
        Read the machine. Called with the lock held.

        ``now`` is ignored in favour of the time after the read. Stamping
        before means a reading is born as old as the read took, so a slow
        reader produces snapshots that are already stale on arrival and the
        cache refreshes on every call. That is not hypothetical; it is what
        this module did until a real run showed 51 reads for 51 calls.
        """
        reader = (
            self.read_system
            if self._ever_read
            else self._first_read
        )

        try:
            system = reader()
            gpu = self.read_gpu()
        except Exception:
            # Serve the last good reading rather than propagating. A voice turn
            # should not fail because nvidia-smi was busy for a moment.
            if self._snapshot is not None:
                stale = Snapshot(
                    system=self._snapshot.system,
                    gpu=self._snapshot.gpu,
                    taken_at=self._snapshot.taken_at,
                    stale=True,
                )
                self._snapshot = stale

                return stale

            raise

        self._reads += 1
        self._ever_read = True
        snapshot = Snapshot(
            system=system,
            gpu=gpu,
            taken_at=self.clock(),
        )
        self._snapshot = snapshot

        return snapshot
