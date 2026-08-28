from __future__ import annotations

import unittest

from core.resource_guard import GpuStatus, SystemStatus
from core.telemetry_cache import (
    DEFAULT_MAX_AGE_SECONDS,
    TelemetryCache,
)


class FakeClock:
    def __init__(self, initial_time: float = 1000.0) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


class CountingReader:
    """A reader that records how often the machine was actually touched."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error
        self.cpu = 10.0

    def system(self) -> SystemStatus:
        self.calls += 1

        if self.error is not None:
            raise self.error

        return SystemStatus(
            cpu_usage_percent=self.cpu,
            ram_usage_percent=30.0,
            ram_used_gb=9.0,
            ram_total_gb=31.9,
        )

    def gpu(self) -> GpuStatus:
        return GpuStatus(
            name="RTX 5080",
            temperature_c=40,
            gpu_utilization_percent=5,
            vram_used_mb=1000,
            vram_total_mb=16000,
        )


def cache(
    reader: CountingReader,
    clock: FakeClock,
    max_age: float = DEFAULT_MAX_AGE_SECONDS,
) -> TelemetryCache:
    return TelemetryCache(
        read_system=reader.system,
        read_gpu=reader.gpu,
        max_age_seconds=max_age,
        clock=clock,
    )


class TestCaching(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.reader = CountingReader()
        self.cache = cache(self.reader, self.clock)

    def test_the_first_read_touches_the_machine(self) -> None:
        self.cache.current()

        self.assertEqual(self.reader.calls, 1)

    def test_a_burst_of_callers_costs_one_read(self) -> None:
        # The saving this exists for. The orchestrator reads twice per step,
        # the governor on every reservation, the panel on a timer — all inside
        # one voice turn, all for a number that has not changed.
        for _ in range(20):
            self.cache.current()

        self.assertEqual(self.reader.calls, 1)

    def test_the_reading_refreshes_once_it_ages_out(self) -> None:
        self.cache.current()
        self.clock.advance(DEFAULT_MAX_AGE_SECONDS + 0.01)
        self.cache.current()

        self.assertEqual(self.reader.calls, 2)

    def test_a_reading_inside_the_window_is_the_same_object(self) -> None:
        first = self.cache.current()
        self.clock.advance(DEFAULT_MAX_AGE_SECONDS / 2)

        self.assertIs(self.cache.current(), first)

    def test_new_values_are_seen_after_the_window(self) -> None:
        self.assertEqual(self.cache.current().system.cpu_usage_percent, 10.0)

        self.reader.cpu = 90.0
        self.clock.advance(DEFAULT_MAX_AGE_SECONDS + 0.01)

        self.assertEqual(self.cache.current().system.cpu_usage_percent, 90.0)

    def test_the_read_count_is_observable(self) -> None:
        # A cache nobody can measure is a cache nobody knows is working.
        self.cache.current()
        self.cache.current()

        self.assertEqual(self.cache.reads, 1)


class TestIdleCostsNothing(unittest.TestCase):
    def test_nothing_is_read_until_somebody_asks(self) -> None:
        # Pull-based on purpose. A sampler thread would keep launching
        # nvidia-smi while Qronos sits idle, which is the exact behaviour a
        # resource-aware assistant is supposed to avoid.
        reader = CountingReader()
        cache(reader, FakeClock())

        self.assertEqual(reader.calls, 0)


class TestForcingAndDiscarding(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.reader = CountingReader()
        self.cache = cache(self.reader, self.clock)

    def test_refresh_ignores_the_window(self) -> None:
        self.cache.current()
        self.cache.refresh()

        self.assertEqual(self.reader.calls, 2)

    def test_invalidating_forces_the_next_read(self) -> None:
        # For the moment after something large changed — a model finished
        # loading — where waiting out the window would answer with a picture of
        # the world that no longer holds.
        self.cache.current()
        self.cache.invalidate()
        self.cache.current()

        self.assertEqual(self.reader.calls, 2)


class TestFailureIsSurvivable(unittest.TestCase):
    def test_a_failed_read_serves_the_previous_value(self) -> None:
        # A voice turn should not fail because nvidia-smi was busy for a
        # moment.
        clock = FakeClock()
        reader = CountingReader()
        instance = cache(reader, clock)

        good = instance.current()
        reader.error = OSError("nvidia-smi is busy")
        clock.advance(DEFAULT_MAX_AGE_SECONDS + 0.01)

        served = instance.current()

        self.assertEqual(
            served.system.cpu_usage_percent,
            good.system.cpu_usage_percent,
        )

    def test_a_served_stale_value_says_so(self) -> None:
        clock = FakeClock()
        reader = CountingReader()
        instance = cache(reader, clock)

        instance.current()
        reader.error = OSError("nvidia-smi is busy")
        clock.advance(DEFAULT_MAX_AGE_SECONDS + 0.01)

        self.assertTrue(instance.current().stale)

    def test_a_failure_with_nothing_cached_propagates(self) -> None:
        # Nothing to serve, so silence would mean inventing a reading. The
        # caller has to know it does not have one.
        reader = CountingReader(error=OSError("no telemetry"))

        with self.assertRaises(OSError):
            cache(reader, FakeClock()).current()

    def test_recovery_resumes_fresh_readings(self) -> None:
        clock = FakeClock()
        reader = CountingReader()
        instance = cache(reader, clock)

        instance.current()
        reader.error = OSError("busy")
        clock.advance(DEFAULT_MAX_AGE_SECONDS + 0.01)
        instance.current()

        reader.error = None
        reader.cpu = 55.0
        clock.advance(DEFAULT_MAX_AGE_SECONDS + 0.01)
        recovered = instance.current()

        self.assertFalse(recovered.stale)
        self.assertEqual(recovered.system.cpu_usage_percent, 55.0)


class TestSnapshotAge(unittest.TestCase):
    def test_age_is_measured_from_when_it_was_taken(self) -> None:
        clock = FakeClock()
        instance = cache(CountingReader(), clock)

        snapshot = instance.current()
        clock.advance(2.0)

        self.assertAlmostEqual(snapshot.age(clock()), 2.0)

    def test_age_is_never_negative(self) -> None:
        clock = FakeClock()
        snapshot = cache(CountingReader(), clock).current()

        self.assertEqual(snapshot.age(clock() - 100), 0.0)


if __name__ == "__main__":
    unittest.main()
