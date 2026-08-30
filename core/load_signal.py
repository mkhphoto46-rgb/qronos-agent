"""
Whether the machine is *actually* busy, as opposed to busy this instant.

``core/resource_policy.py`` already weighs CPU, RAM, VRAM, GPU utilisation and
temperature and returns ALLOW, WARN or BLOCK. It is correct and tested, and
this module does not replace it. The one thing wrong with it is that it judges
a single sample, and a single sample of this machine is close to meaningless.

Measured on the development card on 2026-08-28, sampling at 2 Hz for 45
seconds while the user worked normally: GPU utilisation crossed its own
threshold **29 times**, with p50 = 1 and p75 = 99 and a standard deviation of
45.6. Ten consecutive evaluations of an unchanging machine returned pressure
``high`` eight times and ``critical`` twice, and the policy returned ``warn``
seven times and ``block`` three times. Three different answers in one second.

So the fix is not to judge each signal more cleverly. It is to stop judging one
sample: keep the existing verdict and smooth it over time. A machine is busy
when it has been non-ALLOW for most of a window, not when it was non-ALLOW
once.

Two properties fall out of that, both of which matter:

    A spike cannot make Qronos hold work, because one loaded sample out of ten
    is nowhere near the fraction required.

    A gap cannot make Qronos take work either, because the same rule applies in
    the other direction. That symmetry is the part people leave out, and it is
    why this counts a fraction rather than a consecutive run: a run counter is
    reset by a single sample, which is exactly the fragility being fixed.

Replaying the real 60-second trace through both rules: the raw per-reading rule
changes its mind 34 times, and this one does not change it at all.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Deque, Optional, Protocol

from core.resource_guard import (
    GpuStatus,
    SystemStatus,
    read_gpu_status,
    read_system_status,
    read_system_status_since_last_call,
)
from core.resource_policy import (
    DEFAULT_THRESHOLDS,
    ResourceDecision,
    ResourceThresholds,
    evaluate_resources,
)


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


SystemReader = Callable[[], SystemStatus]
GpuReader = Callable[[], Optional[GpuStatus]]

#: How much VRAM Qronos itself is holding, in MiB. See ``own_vram_mb`` below.
OwnUsageReader = Callable[[], int]


class LoadLevel(Enum):
    """What the machine has been doing lately."""

    #: Not enough samples yet to say. Treated as busy by every caller, because
    #: guessing "free" and being wrong is the failure this module exists to
    #: prevent.
    UNKNOWN = "unknown"

    FREE = "free"
    BUSY = "busy"


@dataclass(frozen=True)
class LoadSample:
    """One reading of the machine, already judged by the existing policy."""

    at: float
    decision: ResourceDecision
    cpu_percent: float
    ram_percent: float
    vram_used_percent: Optional[float] = None
    vram_free_mb: Optional[int] = None
    gpu_utilization_percent: Optional[int] = None
    gpu_temperature_c: Optional[int] = None

    #: What Qronos itself was holding when this was taken, already subtracted
    #: from the figures above. Kept for the audit trail and the UI.
    own_vram_mb: int = 0

    @property
    def loaded(self) -> bool:
        """
        Whether the machine looked busy at this instant.

        Anything the resource policy would not wave through counts. WARN is
        deliberately included: the policy grants on WARN because one warm
        reading is not a reason to refuse, but a window that is *mostly* WARN
        is precisely the sustained pressure this module is looking for.
        """
        return self.decision is not ResourceDecision.ALLOW


@dataclass(frozen=True)
class SustainedLoadConfig:
    """
    How much evidence is needed, and of what.

    Every number here was chosen against the measurements in the module
    docstring rather than picked for feeling about right.
    """

    #: How often the monitor is asked for a reading. One ``nvidia-smi`` costs
    #: about 42 ms, so two seconds is roughly 2% of one core.
    sample_interval_seconds: float = 2.0

    #: How much history to keep. Longer than either dwell, so a dwell always
    #: has a full complement of samples to look at.
    window_seconds: float = 60.0

    #: What share of the samples in a dwell must be loaded. 0.75 over the
    #: 20-second busy dwell means eight readings out of ten. The measured duty
    #: cycle of this machine's bursty GPU work is 0.33, comfortably below, so
    #: compositing and video decode never hold Qronos back. A game or a render
    #: sits near 1.0.
    loaded_fraction: float = 0.75

    #: Evidence required before work is held.
    busy_dwell_seconds: float = 20.0

    #: Evidence required before work resumes. Deliberately longer than the busy
    #: dwell: quick to yield, slow to take. A gap between two bursts is not an
    #: invitation.
    free_dwell_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.sample_interval_seconds <= 0:
            raise ValueError("The sample interval must be positive.")

        if not 0.0 < self.loaded_fraction <= 1.0:
            raise ValueError("loaded_fraction must be above 0 and at most 1.")

        if self.busy_dwell_seconds <= 0 or self.free_dwell_seconds <= 0:
            raise ValueError("Both dwell times must be positive.")

        longest_dwell = max(self.busy_dwell_seconds, self.free_dwell_seconds)

        if self.window_seconds < longest_dwell:
            raise ValueError(
                "The window must be at least as long as the longest dwell, "
                "or a dwell could never see enough samples."
            )


DEFAULT_LOAD_CONFIG = SustainedLoadConfig()


@dataclass(frozen=True)
class LoadSnapshot:
    """The current verdict, and enough context to explain it."""

    level: LoadLevel
    since: Optional[float]
    latest: Optional[LoadSample]
    sample_count: int
    loaded_fraction: float

    @property
    def reason(self) -> str:
        """
        Why, in a sentence that does not change while the answer has not.

        Deliberately carries no elapsed time and no percentage. Both of those
        move every couple of seconds, and anything that reads this to decide
        whether something has changed would conclude that everything changes
        constantly — which is exactly what happened: the queue emitted an
        update on every single tick, because the number of seconds in its own
        explanation had gone up by two.

        How long it has been this way is on the snapshot as ``since``, and in
        the queue as ``holdingSince``, where a display can read it without
        anything mistaking it for news.
        """
        if self.level is LoadLevel.UNKNOWN:
            return "Qronos is still working out how busy the machine is."

        if self.level is LoadLevel.BUSY:
            return (
                "Your machine has been busy, so this is waiting for it to "
                "free up."
            )

        return "The machine is free."

    def describe(self) -> str:
        """The same thing with the numbers in, for logs and harnesses."""
        if self.level is LoadLevel.UNKNOWN:
            return "Not enough readings yet to say whether the machine is busy."

        held_for = "" if self.since is None else f" for {self.since:.0f}s"

        if self.level is LoadLevel.BUSY:
            return (
                f"The machine has been busy{held_for} "
                f"({self.loaded_fraction * 100:.0f}% of recent readings)."
            )

        return f"The machine has been free{held_for}."


class SustainedLoadMonitor:
    """
    Turns a stream of readings into one stable answer.

    Not thread-safe by itself in the sense of being lock-free: it takes a lock
    around its own window, because the scheduler samples from its pump thread
    while the bridge may ask for a snapshot from the reader thread.
    """

    def __init__(
        self,
        config: SustainedLoadConfig = DEFAULT_LOAD_CONFIG,
        clock: Clock | None = None,
        read_system: SystemReader | None = None,
        read_gpu: GpuReader | None = None,
        own_vram_mb: OwnUsageReader | None = None,
        thresholds: ResourceThresholds = DEFAULT_THRESHOLDS,
    ) -> None:
        self.config = config
        self.clock: Clock = clock or time.time
        self.thresholds = thresholds

        # The cheap reader, not the blocking one. read_system_status() sleeps
        # for 500 ms inside psutil on every call, which is unacceptable for
        # something sampled every two seconds. Its cheap sibling exists for
        # exactly this and is documented to need priming — see prime().
        self._read_system: SystemReader = (
            read_system
            if read_system is not None
            else read_system_status_since_last_call
        )
        self._read_gpu: GpuReader = (
            read_gpu if read_gpu is not None else read_gpu_status
        )
        self._own_vram_mb: OwnUsageReader = own_vram_mb or (lambda: 0)

        self._samples: Deque[LoadSample] = deque()
        self._level = LoadLevel.UNKNOWN
        self._changed_at: Optional[float] = None
        self._primed = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------- reading

    def observe(self) -> LoadSample:
        """Take one reading of the real machine and fold it in."""
        sample = self._read()
        self.offer(sample)

        return sample

    def offer(self, sample: LoadSample) -> None:
        """
        Fold in a reading from somewhere else.

        The seam every test uses, and the one the desktop demonstration uses to
        show the machine freeing up without touching a single process of the
        user's.
        """
        with self._lock:
            self._samples.append(sample)
            self._trim(sample.at)
            self._judge(sample.at)

    def reset(self) -> None:
        """
        Forget every reading and go back to knowing nothing.

        The verdict returns to UNKNOWN, which every caller treats as busy, so
        forgetting is safe in the direction that matters.

        This exists because appending readings can only ever add to what the
        monitor has already seen. Something that wants the monitor to hold a
        *different* view — a demonstration, or a test walking through a day —
        has to be able to clear the window first, or its readings merely join
        the argument rather than settling it.
        """
        with self._lock:
            self._samples.clear()
            self._level = LoadLevel.UNKNOWN
            self._changed_at = None

    def prime(self) -> None:
        """
        Take one blocking reading, so the first cheap one is not a lie.

        ``read_system_status_since_last_call`` measures CPU use since the
        previous call, so its first call in a process has nothing to compare
        against and returns 0.0. Left alone, that single fabricated idle
        reading would sit in the window arguing that the machine is free.

        Priming also seeds the verdict. If the machine is quiet right now there
        is no reason to make the first task wait out the full busy dwell, so a
        clear first reading starts the monitor at FREE. Being wrong in that
        direction restores today's behaviour, which is the harmless one; being
        wrong the other way is the failure this module exists to prevent.
        """
        if self._primed:
            return

        self._primed = True

        gpu = self._safe_gpu()
        system = read_system_status()
        sample = self._build(system=system, gpu=gpu)

        with self._lock:
            self._samples.append(sample)

            if not sample.loaded:
                self._level = LoadLevel.FREE
                self._changed_at = sample.at

    def snapshot(self) -> LoadSnapshot:
        """The current verdict. Touches nothing and costs nothing."""
        with self._lock:
            latest = self._samples[-1] if self._samples else None
            now = latest.at if latest is not None else self.clock()

            return LoadSnapshot(
                level=self._level,
                since=(
                    None
                    if self._changed_at is None
                    else max(0.0, now - self._changed_at)
                ),
                latest=latest,
                sample_count=len(self._samples),
                loaded_fraction=self._fraction_over(
                    self._samples, len(self._samples)
                ),
            )

    # -------------------------------------------------------------- judging

    def _judge(self, now: float) -> None:
        """Apply the dwell rules. Called with the lock held."""
        if self._level is LoadLevel.UNKNOWN:
            # No verdict yet, so either dwell may settle it. Busy is asked
            # first: when both would fire, holding is the safe answer.
            if self._crossed(now, self.config.busy_dwell_seconds, True):
                self._settle(LoadLevel.BUSY, now)
            elif self._crossed(now, self.config.free_dwell_seconds, False):
                self._settle(LoadLevel.FREE, now)

            return

        if self._level is LoadLevel.BUSY:
            if self._crossed(now, self.config.free_dwell_seconds, False):
                self._settle(LoadLevel.FREE, now)

            return

        if self._crossed(now, self.config.busy_dwell_seconds, True):
            self._settle(LoadLevel.BUSY, now)

    def _crossed(
        self,
        now: float,
        dwell_seconds: float,
        wants_loaded: bool,
    ) -> bool:
        """Has the last ``dwell_seconds`` been mostly loaded, or mostly not?"""
        window = [
            sample
            for sample in self._samples
            if sample.at > now - dwell_seconds
        ]

        if len(window) < self._expected_samples(dwell_seconds):
            # A dwell judged on two readings would be a spike detector wearing
            # a disguise, so an unfilled dwell decides nothing.
            return False

        loaded = self._fraction_over(window, len(window))
        share = loaded if wants_loaded else 1.0 - loaded

        return share >= self.config.loaded_fraction

    def _settle(self, level: LoadLevel, now: float) -> None:
        if self._level is level:
            return

        self._level = level
        self._changed_at = now

    def _expected_samples(self, dwell_seconds: float) -> int:
        """How many readings a full dwell should contain."""
        return max(
            2,
            int(dwell_seconds / self.config.sample_interval_seconds),
        )

    @staticmethod
    def _fraction_over(samples, count: int) -> float:
        if not count:
            return 0.0

        return sum(1 for sample in samples if sample.loaded) / count

    def _trim(self, now: float) -> None:
        cutoff = now - self.config.window_seconds

        while self._samples and self._samples[0].at <= cutoff:
            self._samples.popleft()

    # -------------------------------------------------------------- reading

    def _read(self) -> LoadSample:
        return self._build(
            system=self._read_system(),
            gpu=self._safe_gpu(),
        )

    def _safe_gpu(self) -> Optional[GpuStatus]:
        try:
            return self._read_gpu()
        except Exception:
            # read_gpu_status already swallows the expected failures and
            # returns None. Anything reaching here is unexpected, and a
            # sampler that dies takes the queue with it.
            return None

    def _build(
        self,
        system: SystemStatus,
        gpu: Optional[GpuStatus],
    ) -> LoadSample:
        own_mb = max(0, self._own_vram_mb())
        adjusted = self._without_own_usage(gpu, own_mb)

        return LoadSample(
            at=self.clock(),
            decision=evaluate_resources(
                system=system,
                gpu=adjusted,
                thresholds=self.thresholds,
            ),
            cpu_percent=system.cpu_usage_percent,
            ram_percent=system.ram_usage_percent,
            vram_used_percent=(
                None if adjusted is None else adjusted.vram_used_percent
            ),
            vram_free_mb=self._free_mb(adjusted),
            gpu_utilization_percent=(
                None if adjusted is None else adjusted.gpu_utilization_percent
            ),
            gpu_temperature_c=(
                None if adjusted is None else adjusted.temperature_c
            ),
            own_vram_mb=own_mb,
        )

    @staticmethod
    def _without_own_usage(
        gpu: Optional[GpuStatus],
        own_mb: int,
    ) -> Optional[GpuStatus]:
        """
        Take Qronos's own models off the reading before judging it.

        Windows does not report VRAM per process — every entry comes back as
        ``[N/A]`` — so Qronos cannot measure whose memory is whose and can only
        subtract what it knows it loaded.

        This is not a refinement. Loading the heavy brain moves the card by 63
        percentage points against a measurement noise of 0.15, so no amount of
        smoothing or hysteresis could ever absorb it: without subtraction
        Qronos reads its own model as user pressure, unloads to be polite,
        reloads to do the work, and does that forever. Because Qronos now
        unloads after every turn this is normally zero, which makes it a guard
        rather than a running account.
        """
        if gpu is None or own_mb <= 0 or gpu.vram_used_mb is None:
            return gpu

        return GpuStatus(
            name=gpu.name,
            temperature_c=gpu.temperature_c,
            gpu_utilization_percent=gpu.gpu_utilization_percent,
            vram_used_mb=max(0, gpu.vram_used_mb - own_mb),
            vram_total_mb=gpu.vram_total_mb,
        )

    @staticmethod
    def _free_mb(gpu: Optional[GpuStatus]) -> Optional[int]:
        if (
            gpu is None
            or gpu.vram_used_mb is None
            or gpu.vram_total_mb is None
        ):
            return None

        return max(0, gpu.vram_total_mb - gpu.vram_used_mb)
