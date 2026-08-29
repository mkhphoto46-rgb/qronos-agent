"""
Telling a busy machine from a machine that was busy for a moment.

The rules being defended here all failed on the real hardware before this
module existed. Ten consecutive evaluations of an unchanging machine produced
``warn`` seven times and ``block`` three times, and the GPU signal crossed its
own threshold twenty-nine times in forty-five seconds. Everything below exists
so that a reading like that produces one answer instead of thirty.

Every test drives a hand-moved clock and hand-built samples. Nothing here
sleeps, reads the machine, or cares how loaded the machine running the suite
happens to be.
"""

from __future__ import annotations

import unittest

from core.load_signal import (
    DEFAULT_LOAD_CONFIG,
    LoadLevel,
    LoadSample,
    SustainedLoadConfig,
    SustainedLoadMonitor,
)
from core.resource_guard import GpuStatus, SystemStatus
from core.resource_policy import ResourceDecision
from tests.fixtures.clock import FakeClock


def a_sample(at: float, loaded: bool) -> LoadSample:
    """One reading, with only the thing under test varied."""
    return LoadSample(
        at=at,
        decision=(
            ResourceDecision.BLOCK if loaded else ResourceDecision.ALLOW
        ),
        cpu_percent=90.0 if loaded else 10.0,
        ram_percent=50.0,
    )


def feed(
    monitor: SustainedLoadMonitor,
    clock: FakeClock,
    pattern: str,
    interval: float = 2.0,
) -> None:
    """
    Feed a run of readings written as a string.

    ``"....X...."`` is nine quiet readings with one loaded one in the middle,
    which is far easier to read in a test than a list of booleans.
    """
    for mark in pattern:
        clock.advance(interval)
        monitor.offer(a_sample(clock(), loaded=mark == "X"))


def monitor_at(
    clock: FakeClock,
    config: SustainedLoadConfig = DEFAULT_LOAD_CONFIG,
) -> SustainedLoadMonitor:
    return SustainedLoadMonitor(config=config, clock=clock)


class TestASpikeIsNotBusy(unittest.TestCase):
    """
    The defect this module was written for.

    On the development machine a single instantaneous reading called the
    machine busy 33% of the time and free the rest, with nothing actually
    changing. One reading must never be able to hold the user's work.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.monitor = monitor_at(self.clock)

    def test_one_loaded_reading_among_many_quiet_ones_is_not_busy(self) -> None:
        feed(self.monitor, self.clock, "....X" + "." * 20)

        self.assertIsNot(self.monitor.snapshot().level, LoadLevel.BUSY)

    def test_the_measured_duty_cycle_of_this_machine_is_not_busy(self) -> None:
        # A third of readings loaded, which is what bursty compositing and
        # video decode actually look like. Qronos must not hold work for that.
        feed(self.monitor, self.clock, "X.." * 15)

        self.assertIsNot(self.monitor.snapshot().level, LoadLevel.BUSY)

    def test_alternating_readings_never_settle_on_busy(self) -> None:
        feed(self.monitor, self.clock, "X." * 25)

        self.assertIsNot(self.monitor.snapshot().level, LoadLevel.BUSY)


class TestAGapIsNotFree(unittest.TestCase):
    """
    The half people leave out.

    A run counter would be reset by one quiet reading, which is the same
    fragility in the other direction: a momentary lull in a render is not an
    invitation to take the graphics card.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.monitor = monitor_at(self.clock)
        feed(self.monitor, self.clock, "X" * 20)

    def test_the_setup_really_is_busy(self) -> None:
        self.assertIs(self.monitor.snapshot().level, LoadLevel.BUSY)

    def test_one_quiet_reading_does_not_release_it(self) -> None:
        feed(self.monitor, self.clock, ".")

        self.assertIs(self.monitor.snapshot().level, LoadLevel.BUSY)

    def test_a_short_lull_does_not_release_it(self) -> None:
        # Four quiet readings is eight seconds. The free dwell is thirty.
        feed(self.monitor, self.clock, "....")

        self.assertIs(self.monitor.snapshot().level, LoadLevel.BUSY)

    def test_a_sustained_quiet_period_does_release_it(self) -> None:
        feed(self.monitor, self.clock, "." * 20)

        self.assertIs(self.monitor.snapshot().level, LoadLevel.FREE)


class TestTheDwellIsRespected(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.monitor = monitor_at(self.clock)

    def test_nine_loaded_readings_are_not_yet_enough(self) -> None:
        # Nine readings at two seconds is eighteen seconds; the busy dwell is
        # twenty. Just short, deliberately.
        feed(self.monitor, self.clock, "X" * 9)

        self.assertIsNot(self.monitor.snapshot().level, LoadLevel.BUSY)

    def test_ten_loaded_readings_are(self) -> None:
        feed(self.monitor, self.clock, "X" * 10)

        self.assertIs(self.monitor.snapshot().level, LoadLevel.BUSY)

    def test_yielding_is_quicker_than_taking(self) -> None:
        # The asymmetry is the politeness, so it is worth asserting rather
        # than leaving as a comment on two constants.
        self.assertLess(
            DEFAULT_LOAD_CONFIG.busy_dwell_seconds,
            DEFAULT_LOAD_CONFIG.free_dwell_seconds,
        )


class TestNotKnowingCountsAsBusy(unittest.TestCase):
    def test_a_fresh_monitor_does_not_claim_the_machine_is_free(self) -> None:
        monitor = monitor_at(FakeClock())

        self.assertIs(monitor.snapshot().level, LoadLevel.UNKNOWN)

    def test_a_handful_of_quiet_readings_is_still_unknown(self) -> None:
        clock = FakeClock()
        monitor = monitor_at(clock)

        feed(monitor, clock, "...")

        self.assertIs(monitor.snapshot().level, LoadLevel.UNKNOWN)

    def test_an_unknown_verdict_settles_once_there_is_evidence(self) -> None:
        clock = FakeClock()
        monitor = monitor_at(clock)

        feed(monitor, clock, "." * 16)

        self.assertIs(monitor.snapshot().level, LoadLevel.FREE)

    def test_a_failing_gpu_reader_does_not_kill_the_sampler(self) -> None:
        # A sampler that dies takes the queue with it, so an unexpected
        # exception from nvidia-smi must degrade rather than propagate.
        def explode():
            raise OSError("nvidia-smi went away")

        monitor = SustainedLoadMonitor(
            clock=FakeClock(),
            read_system=lambda: SystemStatus(
                cpu_usage_percent=10.0,
                ram_usage_percent=40.0,
                ram_used_gb=6.0,
                ram_total_gb=16.0,
            ),
            read_gpu=explode,
        )

        sample = monitor.observe()

        self.assertIsNone(sample.gpu_utilization_percent)


class TestTheWindowStaysBounded(unittest.TestCase):
    def test_ten_simulated_minutes_do_not_grow_the_window(self) -> None:
        # Without trimming this is a memory leak that only shows up after the
        # application has been open for a day.
        clock = FakeClock()
        monitor = monitor_at(clock)

        feed(monitor, clock, "X." * 150)

        expected = (
            DEFAULT_LOAD_CONFIG.window_seconds
            / DEFAULT_LOAD_CONFIG.sample_interval_seconds
        )

        self.assertLessEqual(monitor.snapshot().sample_count, expected + 1)


class TestOwnUsageIsSubtracted(unittest.TestCase):
    """
    Qronos must not read its own model as the user being busy.

    Loading the heavy brain moves the card by 63 percentage points against a
    measurement noise of 0.15, so no amount of smoothing could absorb it.
    Without subtraction Qronos unloads to be polite, reloads to work, and
    repeats that forever.
    """

    def _monitor(self, own_mb: int) -> SustainedLoadMonitor:
        return SustainedLoadMonitor(
            clock=FakeClock(),
            read_system=lambda: SystemStatus(
                cpu_usage_percent=10.0,
                ram_usage_percent=40.0,
                ram_used_gb=6.0,
                ram_total_gb=16.0,
            ),
            read_gpu=lambda: GpuStatus(
                name="RTX 5080",
                temperature_c=45,
                gpu_utilization_percent=5,
                # 79.7% of the card: above the 75% warn line, below the 90%
                # block line. Subtracting the heavy brain takes it to 17%.
                vram_used_mb=13_000,
                vram_total_mb=16_303,
            ),
            own_vram_mb=lambda: own_mb,
        )

    def test_a_card_filled_by_our_own_model_reads_as_quiet(self) -> None:
        sample = self._monitor(own_mb=10_220).observe()

        self.assertFalse(sample.loaded)

    def test_the_same_card_filled_by_somebody_else_reads_as_loaded(self) -> None:
        sample = self._monitor(own_mb=0).observe()

        self.assertTrue(sample.loaded)

    def test_what_was_subtracted_is_recorded(self) -> None:
        # The number has to be visible, or a future argument about whether the
        # subtraction happened is unresolvable.
        self.assertEqual(self._monitor(own_mb=3_442).observe().own_vram_mb, 3_442)


class TestTheConfigurationRefusesNonsense(unittest.TestCase):
    def test_a_window_shorter_than_a_dwell_is_rejected(self) -> None:
        # Otherwise the dwell could never see enough samples and the monitor
        # would silently never change its mind.
        with self.assertRaises(ValueError):
            SustainedLoadConfig(window_seconds=10.0, free_dwell_seconds=30.0)

    def test_a_fraction_above_one_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SustainedLoadConfig(loaded_fraction=1.5)

    def test_a_zero_interval_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SustainedLoadConfig(sample_interval_seconds=0.0)


class TestTheSnapshotExplainsItself(unittest.TestCase):
    def test_a_busy_snapshot_says_how_long(self) -> None:
        clock = FakeClock()
        monitor = monitor_at(clock)

        feed(monitor, clock, "X" * 15)

        self.assertIn("busy", monitor.snapshot().describe())

    def test_an_unknown_snapshot_does_not_pretend_to_know(self) -> None:
        self.assertIn(
            "Not enough",
            monitor_at(FakeClock()).snapshot().describe(),
        )


if __name__ == "__main__":
    unittest.main()
