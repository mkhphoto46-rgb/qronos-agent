"""
Power and disk pressure: the two sentinel signals that were still missing.

``docs/qronos_product_architecture.md`` asks the resource sentinel to read
"disk pressure, battery/power state" before every model load and to keep
sampling while Qronos is active. CPU, RAM and the graphics card were already
read; this module reads the other two, as fresh samples every time it is
asked, and says honestly when it cannot.

Two readings, both cheap and local:

    **Power.** ``psutil.sensors_battery()`` (on Windows it wraps
    ``GetSystemPowerStatus``) for whether the machine runs on battery and how
    much is left. A desktop without a battery is a known state, not an
    unknown one. Battery saver comes from Windows' ``GetSystemPowerStatus``
    through the read-only ``core/windows_power_status.py``; elsewhere, or when
    Windows does not say, it is reported as unknown.

    **Disk activity.** Whether something else is saturating the drives: the
    mean time per completed read or write since the previous reading, from
    ``psutil.disk_io_counters``. A creator workload can saturate a drive with
    plenty of free space, so capacity alone does not see it.

    **Disk.** Free space, through ``psutil.disk_usage``, on the volumes Qronos
    writes to — its data root,
    which holds the conversation history, the queue journal, memory and voice
    temporary files. A full volume makes those writes fail half-way, and
    Windows needs the same space for its page file.

Nothing here changes anything. It reads; ``core/hard_floor.py`` and the
sustained-load monitor decide. Every reader is injectable, so the tests drive
batteries and full disks without owning either.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from core.resource_policy import ResourceDecision

_MIB = 1024 * 1024

#: Battery share at or below which a discharging machine is too low for Qronos
#: to start a model at all. Not overridable: see ``core/hard_floor.py``.
BATTERY_CRITICAL_PERCENT = 10.0

#: A refused battery must recover to this before work starts again, so a
#: battery hovering at the line does not flap.
BATTERY_RESUME_PERCENT = 15.0

#: Free space on a watched volume below which Qronos refuses new work. An
#: engineering default, not a measurement: nobody has measured how much one
#: turn writes. It is meant to stay clear of a volume that is effectively
#: full — where history, queue-journal and voice-file writes start failing
#: and Windows can no longer grow its page file — with a margin. Like the
#: other lines here it is a candidate for project configuration.
DISK_CRITICAL_FREE_MB = 2 * 1024

#: A refused volume must recover to this before work starts again.
DISK_RESUME_FREE_MB = 3 * 1024


@dataclass(frozen=True)
class EnvironmentThresholds:
    """
    The politeness lines. Safety lines live in ``HardFloorConfig``.

    Kept here, with defaults, until the project configuration allocates fields
    for them; nothing outside this module and the floor reads them.
    """

    #: A discharging battery at or below this makes the machine count as
    #: loaded, so queued work waits for the charger. The user can still press
    #: "run anyway"; the critical line above cannot be pressed past.
    battery_low_percent: float = 20.0

    #: Free space below this counts as loaded. Below the warning line the
    #: reading only warns; both are politeness.
    disk_low_free_mb: int = 5 * 1024
    disk_warn_free_mb: int = 10 * 1024

    #: Drive contention: completed reads and writes taking this long on
    #: average, queueing included. Engineering defaults, not measurements of
    #: this machine: an idle SSD answers in well under a millisecond and an
    #: idle hard disk in roughly ten, so fifty means requests are waiting
    #: behind somebody else's. Politeness only — it holds queued work through
    #: the sustained monitor and never refuses a turn the user asked for.
    io_latency_warn_ms: float = 50.0

    #: ...over at least this many operations a second, so a single slow
    #: request on an otherwise idle drive is not mistaken for contention.
    io_min_ops_per_second: float = 10.0

    def __post_init__(self) -> None:
        if not 0.0 < self.battery_low_percent <= 100.0:
            raise ValueError("battery_low_percent must be within (0, 100].")

        if not 0 < self.disk_low_free_mb <= self.disk_warn_free_mb:
            raise ValueError("Disk lines must be positive and low <= warn.")

        if self.io_latency_warn_ms <= 0 or self.io_min_ops_per_second <= 0:
            raise ValueError("Disk I/O lines must be positive.")


DEFAULT_ENVIRONMENT_THRESHOLDS = EnvironmentThresholds()


@dataclass(frozen=True)
class PowerReading:
    """What the machine said about its power, or that it said nothing."""

    #: False when the power state could not be read at all. Nothing else in
    #: the reading is then meaningful.
    available: bool

    #: False for a desktop: no battery is a known answer, not a missing one.
    has_battery: Optional[bool] = None

    #: True when running from the battery. None when the machine has a battery
    #: but did not say whether it is plugged in.
    on_battery: Optional[bool] = None

    battery_percent: Optional[float] = None

    #: Battery saver as Windows reports it. None when not known: not Windows,
    #: the call failed, or Windows gave a value other than on or off.
    battery_saver: Optional[bool] = None

    @property
    def discharging(self) -> bool:
        return self.available and self.has_battery is True and self.on_battery is True


@dataclass(frozen=True)
class DiskReading:
    """Free space on one watched volume."""

    label: str
    available: bool
    free_mb: Optional[int] = None
    total_mb: Optional[int] = None


@dataclass(frozen=True)
class EnvironmentReading:
    """One fresh reading of power and every watched volume."""

    power: PowerReading
    disks: tuple[DiskReading, ...] = ()

    #: Drive activity since the previous reading. None when not measured.
    disk_activity: Optional["DiskActivityReading"] = None

    @property
    def disk_io_unknown(self) -> bool:
        return self.disk_activity is None or not self.disk_activity.available

    @property
    def power_unknown(self) -> bool:
        return not self.power.available

    @property
    def disk_unknown(self) -> bool:
        """No watched volume, or one that could not be read."""
        return not self.disks or any(not disk.available for disk in self.disks)

    @property
    def disk_free_mb(self) -> Optional[int]:
        """The tightest readable volume, which is the one that fills first."""
        free = [disk.free_mb for disk in self.disks if disk.available and disk.free_mb is not None]
        return min(free) if free else None


#: Reads power, as ``psutil.sensors_battery`` does: an object with ``percent``
#: and ``power_plugged``, or None for a machine without a battery.
BatteryReader = Callable[[], object]
SaverReader = Callable[[], Optional[bool]]
DiskUsageReader = Callable[[str], object]


def _psutil_battery() -> object:
    import psutil

    return psutil.sensors_battery()


def _psutil_disk_usage(path: str) -> object:
    import psutil

    return psutil.disk_usage(path)


def no_battery_saver_reading() -> Optional[bool]:
    """A saver reader for callers that do not want battery saver read at all."""
    return None


def windows_battery_saver() -> Optional[bool]:
    """
    Battery saver as Windows reports it; None when not known.

    The Win32 call lives in ``core/windows_power_status.py`` so that this
    module stays free of system-access imports; that module only reads.
    """
    from core.windows_power_status import read_battery_saver

    return read_battery_saver()


def read_power(
    battery: BatteryReader = _psutil_battery,
    saver: SaverReader = windows_battery_saver,
) -> PowerReading:
    """A fresh power reading. Never raises."""
    try:
        state = battery()
    except Exception:
        return PowerReading(available=False)

    try:
        saver_on = saver()
    except Exception:
        saver_on = None

    if state is None:
        return PowerReading(available=True, has_battery=False, on_battery=False, battery_saver=saver_on)

    plugged = getattr(state, "power_plugged", None)
    percent = getattr(state, "percent", None)

    if type(percent) not in (int, float) or not 0 <= percent <= 100:
        percent = None

    return PowerReading(
        available=True,
        has_battery=True,
        on_battery=None if plugged is None else not bool(plugged),
        battery_percent=None if percent is None else float(percent),
        battery_saver=saver_on,
    )


def _existing_ancestor(path: Path) -> Optional[Path]:
    """The path itself or its nearest existing parent. Nothing is created."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate

    return None


def read_disk(
    label: str,
    path: Path,
    disk_usage: DiskUsageReader = _psutil_disk_usage,
) -> DiskReading:
    """Free space on the volume holding ``path``. Never raises."""
    try:
        target = _existing_ancestor(Path(path))

        if target is None:
            return DiskReading(label=label, available=False)

        usage = disk_usage(os.fspath(target))
        free, total = int(usage.free), int(usage.total)
    except Exception:
        return DiskReading(label=label, available=False)

    if free < 0 or total <= 0:
        return DiskReading(label=label, available=False)

    return DiskReading(label=label, available=True, free_mb=free // _MIB, total_mb=total // _MIB)


@dataclass(frozen=True)
class DiskActivityReading:
    """
    How hard the drives have been working since the previous reading.

    Measured, not estimated: deltas of the operating system's own I/O
    counters over a real interval. ``latency_ms`` is the mean time each
    completed read or write took, queueing included, which rises when
    something saturates the drive (a video export, a game installing) even
    while plenty of space is free. There is deliberately no "busy percent":
    Windows does not report one, and the time counters sum overlapping
    requests, so dividing them by wall time would not be a percentage.
    """

    available: bool
    interval_seconds: Optional[float] = None
    ops_per_second: Optional[float] = None
    mb_per_second: Optional[float] = None

    #: None when nothing completed in the interval: an idle drive has no
    #: per-operation time, which is different from a fast one.
    latency_ms: Optional[float] = None


#: ``psutil.disk_io_counters()``: totals across all drives, or None when the
#: operating system keeps no disk counters.
IoCountersReader = Callable[[], object]

_IO_FIELDS = ("read_count", "write_count", "read_bytes", "write_bytes", "read_time", "write_time")


def _psutil_disk_io_counters() -> object:
    import psutil

    return psutil.disk_io_counters()


class DiskActivitySampler:
    """
    Turns cumulative I/O counters into one interval's activity.

    The first call only records a baseline and reports unavailable, because
    a delta needs two readings. A call sooner than ``min_interval_seconds``
    after the last one returns that reading again rather than measuring a
    sliver of time; a gap longer than ``max_interval_seconds`` re-baselines
    and reports unavailable rather than averaging an old burst into now.
    Counters that went backwards (a reset) re-baseline the same way.
    """

    def __init__(
        self,
        io_counters: IoCountersReader = _psutil_disk_io_counters,
        clock: Callable[[], float] = time.monotonic,
        min_interval_seconds: float = 0.5,
        max_interval_seconds: float = 30.0,
    ) -> None:
        if not 0 < min_interval_seconds < max_interval_seconds:
            raise ValueError("Need 0 < min_interval_seconds < max_interval_seconds.")

        self._io_counters = io_counters
        self._clock = clock
        self._min = min_interval_seconds
        self._max = max_interval_seconds
        self._lock = threading.Lock()
        self._previous: Optional[tuple[float, tuple[int, ...]]] = None
        self._last = DiskActivityReading(available=False)

    def __call__(self) -> DiskActivityReading:
        with self._lock:
            now = self._clock()

            if self._previous is not None and now - self._previous[0] < self._min:
                return self._last

            try:
                counters = self._io_counters()
                values = None if counters is None else tuple(int(getattr(counters, name)) for name in _IO_FIELDS)
            except Exception:
                values = None

            if values is None or any(value < 0 for value in values):
                self._previous = None
                self._last = DiskActivityReading(available=False)
                return self._last

            previous, self._previous = self._previous, (now, values)

            if previous is None:
                self._last = DiskActivityReading(available=False)
                return self._last

            interval = now - previous[0]
            delta = tuple(new - old for new, old in zip(values, previous[1]))

            if interval > self._max or any(value < 0 for value in delta):
                self._last = DiskActivityReading(available=False)
                return self._last

            reads, writes, read_bytes, write_bytes, read_ms, write_ms = delta
            ops = reads + writes

            self._last = DiskActivityReading(
                available=True,
                interval_seconds=interval,
                ops_per_second=ops / interval,
                mb_per_second=(read_bytes + write_bytes) / _MIB / interval,
                latency_ms=None if ops == 0 else (read_ms + write_ms) / ops,
            )
            return self._last


def disk_io_contended(
    activity: Optional[DiskActivityReading],
    thresholds: "EnvironmentThresholds",
) -> bool:
    """Slow operations, and enough of them that it is not one stray request."""
    return (
        activity is not None
        and activity.available
        and activity.latency_ms is not None
        and activity.ops_per_second is not None
        and activity.ops_per_second >= thresholds.io_min_ops_per_second
        and activity.latency_ms >= thresholds.io_latency_warn_ms
    )


def default_watch_paths() -> tuple[tuple[str, Path], ...]:
    """The Qronos data root: history, queue journal, memory and voice files."""
    from core.config import CONFIG

    return (("data", CONFIG.paths.data),)


class EnvironmentReader:
    """
    Takes one fresh power-and-disk reading per call.

    Injected readers make this testable; the defaults are the real local OS
    facilities and are only touched when the production sentinel samples.
    """

    def __init__(
        self,
        watch_paths: Callable[[], Iterable[tuple[str, Path]]] = default_watch_paths,
        battery: BatteryReader = _psutil_battery,
        saver: SaverReader = windows_battery_saver,
        disk_usage: DiskUsageReader = _psutil_disk_usage,
        disk_activity: Optional[Callable[[], DiskActivityReading]] = None,
    ) -> None:
        self._watch_paths = watch_paths
        self._battery = battery
        self._saver = saver
        self._disk_usage = disk_usage
        # One sampler per reader: its baseline is the previous call.
        self._disk_activity = disk_activity if disk_activity is not None else DiskActivitySampler()

    def __call__(self) -> EnvironmentReading:
        power = read_power(self._battery, self._saver)

        try:
            watched: Sequence[tuple[str, Path]] = tuple(self._watch_paths())
        except Exception:
            watched = ()

        disks = tuple(read_disk(label, path, self._disk_usage) for label, path in watched)

        try:
            activity = self._disk_activity()
        except Exception:
            activity = DiskActivityReading(available=False)

        return EnvironmentReading(power=power, disks=disks, disk_activity=activity)


def environment_decision(
    reading: EnvironmentReading,
    thresholds: EnvironmentThresholds = DEFAULT_ENVIRONMENT_THRESHOLDS,
) -> ResourceDecision:
    """
    The politeness verdict for power and disk, in the existing vocabulary.

    BLOCK and WARN both make a sample count as loaded for the sustained
    monitor, exactly as a CPU or GPU reading would. An unreadable signal adds
    nothing: it is recorded on the sample rather than guessed at, and the
    safety floor still covers whatever can be read.
    """
    power = reading.power
    free = reading.disk_free_mb
    decision = ResourceDecision.ALLOW

    if power.discharging:
        decision = ResourceDecision.WARN

        if power.battery_percent is not None and power.battery_percent <= thresholds.battery_low_percent:
            return ResourceDecision.BLOCK

    if power.available and power.battery_saver is True:
        decision = ResourceDecision.WARN

    if free is not None:
        if free < thresholds.disk_low_free_mb:
            return ResourceDecision.BLOCK

        if free < thresholds.disk_warn_free_mb:
            decision = ResourceDecision.WARN

    if disk_io_contended(reading.disk_activity, thresholds):
        decision = ResourceDecision.WARN

    return decision
