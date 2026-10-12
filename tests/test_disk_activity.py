"""
Drive contention from I/O counter deltas, with the counters and the clock
injected. No test reads the machine's real disk counters.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.environment_signals import (
    DiskActivityReading,
    DiskActivitySampler,
    EnvironmentReader,
    EnvironmentReading,
    EnvironmentThresholds,
    PowerReading,
    disk_io_contended,
    environment_decision,
)
from core.load_signal import LoadLevel, SustainedLoadMonitor
from core.resource_guard import GpuStatus, SystemStatus
from core.resource_policy import ResourceDecision
from tests.fixtures.clock import FakeClock

MIB = 1024 * 1024


class Counters:
    """Cumulative counters a test advances, like the operating system's."""

    def __init__(self):
        self.values = dict(read_count=0, write_count=0, read_bytes=0, write_bytes=0, read_time=0, write_time=0)
        self.calls = 0
        self.missing = False

    def work(self, ops: int, ms_each: float, mb: float = 0.0):
        self.values["read_count"] += ops
        self.values["read_time"] += int(ops * ms_each)
        self.values["read_bytes"] += int(mb * MIB)

    def __call__(self):
        self.calls += 1
        return None if self.missing else SimpleNamespace(**self.values)


def sampler(counters, clock, **kwargs):
    return DiskActivitySampler(io_counters=counters, clock=clock, **kwargs)


def test_the_first_reading_only_sets_a_baseline():
    counters, clock = Counters(), FakeClock()
    assert sampler(counters, clock)() == DiskActivityReading(available=False)


def test_an_interval_is_measured_from_real_deltas():
    counters, clock = Counters(), FakeClock()
    sample = sampler(counters, clock)
    sample()
    clock.advance(2.0)
    counters.work(ops=400, ms_each=60.0, mb=100.0)
    reading = sample()
    assert reading.available and reading.interval_seconds == 2.0
    assert reading.ops_per_second == 200.0 and reading.mb_per_second == 50.0 and reading.latency_ms == 60.0


def test_an_idle_drive_has_no_latency_rather_than_zero():
    counters, clock = Counters(), FakeClock()
    sample = sampler(counters, clock)
    sample()
    clock.advance(2.0)
    reading = sample()
    assert reading.available and reading.ops_per_second == 0.0 and reading.latency_ms is None


def test_a_call_inside_the_minimum_interval_reuses_the_last_reading():
    counters, clock = Counters(), FakeClock()
    sample = sampler(counters, clock)
    sample()
    clock.advance(2.0)
    counters.work(ops=100, ms_each=5.0)
    first = sample()
    clock.advance(0.1)
    counters.work(ops=1, ms_each=900.0)
    assert sample() is first and counters.calls == 2


def test_a_long_gap_rebaselines_instead_of_averaging_an_old_burst():
    counters, clock = Counters(), FakeClock()
    sample = sampler(counters, clock)
    sample()
    clock.advance(31.0)
    counters.work(ops=1_000, ms_each=80.0)
    assert not sample().available
    clock.advance(2.0)
    counters.work(ops=40, ms_each=2.0)
    assert sample().latency_ms == 2.0


def test_counters_going_backwards_rebaseline():
    counters, clock = Counters(), FakeClock()
    sample = sampler(counters, clock)
    counters.work(ops=500, ms_each=10.0)
    sample()
    clock.advance(2.0)
    counters.values = {key: 0 for key in counters.values}
    assert not sample().available


@pytest.mark.parametrize("problem", ["none", "raises", "missing_field", "negative"])
def test_unreadable_counters_are_unavailable(problem):
    clock = FakeClock()

    def io_counters():
        if problem == "none":
            return None
        if problem == "raises":
            raise OSError("diskperf disabled")
        if problem == "missing_field":
            return SimpleNamespace(read_count=1)
        return SimpleNamespace(read_count=-1, write_count=0, read_bytes=0, write_bytes=0, read_time=0, write_time=0)

    sample = DiskActivitySampler(io_counters=io_counters, clock=clock)
    sample()
    clock.advance(2.0)
    assert sample() == DiskActivityReading(available=False)


def test_sampler_rejects_inconsistent_intervals():
    with pytest.raises(ValueError):
        DiskActivitySampler(io_counters=Counters(), min_interval_seconds=5.0, max_interval_seconds=1.0)


# ---------------------------------------------------------------- verdict


QUIET_POWER = PowerReading(available=True, has_battery=False, on_battery=False)


def with_activity(ops_per_second, latency_ms):
    return EnvironmentReading(
        power=QUIET_POWER,
        disk_activity=DiskActivityReading(True, 2.0, ops_per_second, 10.0, latency_ms),
    )


@pytest.mark.parametrize(
    "ops, latency, expected",
    [
        (200.0, 60.0, ResourceDecision.WARN),
        (200.0, 49.9, ResourceDecision.ALLOW),
        (9.9, 900.0, ResourceDecision.ALLOW),  # one stray slow request
        (0.0, None, ResourceDecision.ALLOW),  # idle
    ],
)
def test_contention_is_slow_operations_in_numbers(ops, latency, expected):
    assert environment_decision(with_activity(ops, latency)) is expected


def test_unavailable_activity_adds_nothing_and_is_flagged():
    reading = EnvironmentReading(power=QUIET_POWER, disk_activity=DiskActivityReading(available=False))
    assert environment_decision(reading) is ResourceDecision.ALLOW and reading.disk_io_unknown
    assert EnvironmentReading(power=QUIET_POWER).disk_io_unknown
    assert not disk_io_contended(None, EnvironmentThresholds())


def test_contention_never_raises_foreground_pressure():
    from core.activity_guard import ActivityGuard

    critical, high = ActivityGuard._environment_pressure(with_activity(500.0, 400.0))
    assert not critical and not high


def test_thresholds_reject_non_positive_io_lines():
    with pytest.raises(ValueError):
        EnvironmentThresholds(io_latency_warn_ms=0)


def test_reader_attaches_the_sampler_reading(tmp_path):
    activity = DiskActivityReading(True, 2.0, 50.0, 5.0, 3.0)
    reader = EnvironmentReader(
        watch_paths=lambda: (("data", tmp_path),),
        battery=lambda: None,
        saver=lambda: None,
        disk_usage=lambda path: SimpleNamespace(free=50_000 * MIB, total=100_000 * MIB),
        disk_activity=lambda: activity,
    )
    assert reader().disk_activity is activity


def test_reader_survives_a_raising_sampler(tmp_path):
    def broken():
        raise RuntimeError("unexpected")

    reader = EnvironmentReader(watch_paths=lambda: (), battery=lambda: None, saver=lambda: None, disk_activity=broken)
    assert reader().disk_io_unknown


# ------------------------------------------------------ sustained monitor


QUIET = SystemStatus(cpu_usage_percent=10.0, ram_usage_percent=40.0, ram_used_gb=6.0, ram_total_gb=16.0)
CARD = GpuStatus(name="card", temperature_c=45, gpu_utilization_percent=5, vram_used_mb=1_000, vram_total_mb=8_192)


def monitor_with_drive(clock, counters):
    sample = sampler(counters, clock)
    reader = EnvironmentReader(
        watch_paths=lambda: (), battery=lambda: None, saver=lambda: None, disk_activity=sample,
    )
    return SustainedLoadMonitor(clock=clock, read_system=lambda: QUIET, read_gpu=lambda: CARD, read_environment=reader)


def run(monitor, clock, counters, seconds, ops_per_tick, ms_each):
    for _ in range(int(seconds / 2)):
        clock.advance(2.0)
        counters.work(ops=ops_per_tick, ms_each=ms_each)
        monitor.observe()


def test_a_sustained_export_holds_queued_work_and_a_short_burst_does_not():
    clock, counters = FakeClock(), Counters()
    monitor = monitor_with_drive(clock, counters)
    monitor.observe()
    run(monitor, clock, counters, 40, ops_per_tick=50, ms_each=1.0)
    assert monitor.snapshot().level is LoadLevel.FREE

    run(monitor, clock, counters, 4, ops_per_tick=400, ms_each=120.0)  # a model load's burst
    run(monitor, clock, counters, 30, ops_per_tick=50, ms_each=1.0)
    assert monitor.snapshot().level is LoadLevel.FREE

    run(monitor, clock, counters, 24, ops_per_tick=400, ms_each=120.0)  # an export
    assert monitor.snapshot().level is LoadLevel.BUSY
    latest = monitor.snapshot().latest
    assert latest.disk_io_latency_ms == 120.0 and not latest.disk_io_unknown
    assert latest.decision is ResourceDecision.ALLOW  # CPU/GPU verdict untouched

    run(monitor, clock, counters, 20, ops_per_tick=50, ms_each=1.0)
    assert monitor.snapshot().level is LoadLevel.BUSY  # slow to take back
    run(monitor, clock, counters, 14, ops_per_tick=50, ms_each=1.0)
    assert monitor.snapshot().level is LoadLevel.FREE
