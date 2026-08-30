"""
The same minute of a real machine, judged both ways.

`tests/test_load_signal.py` proves the rules against readings invented to
exercise them. This proves the same thing against readings nobody invented:
sixty-two seconds recorded off the development machine on 2026-08-28 while its
owner was working — another application holding most of the graphics card, and
the GPU swinging between idle and pegged every couple of seconds.

The point of keeping it as a file rather than a live measurement is that it is
deterministic and runs anywhere, including the Linux CI machine that has no
graphics card at all. The point of it being a *recording* rather than a
fabrication is that no one chose these numbers to make the argument work.

`tests/test_telemetry_cache.py` reached the same conclusion the hard way: a
caching bug slipped through every fake-clock test in that file and was only
caught by a test that touched something real.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from core.load_signal import (
    DEFAULT_LOAD_CONFIG,
    LoadLevel,
    LoadSample,
    SustainedLoadMonitor,
)
from core.resource_guard import GpuStatus, SystemStatus
from core.resource_policy import ResourceDecision, evaluate_resources
from tests.fixtures.clock import FakeClock


TRACE = Path(__file__).parent / "fixtures" / "machine_trace_2026_08_28.json"


def load_trace() -> tuple[dict, ...]:
    with TRACE.open(encoding="utf-8") as handle:
        return tuple(json.load(handle)["samples"])


def as_readings(row: dict) -> tuple[SystemStatus, GpuStatus]:
    """One recorded row, back in the shape the real readers return."""
    return (
        SystemStatus(
            cpu_usage_percent=row["cpu_percent"],
            ram_usage_percent=row["ram_percent"],
            ram_used_gb=0.0,
            ram_total_gb=0.0,
        ),
        GpuStatus(
            name="NVIDIA GeForce RTX 5080",
            temperature_c=row["gpu_temperature_c"],
            gpu_utilization_percent=row["gpu_utilization_percent"],
            vram_used_mb=row["vram_used_mb"],
            vram_total_mb=row["vram_total_mb"],
        ),
    )


def count_changes(verdicts) -> int:
    return sum(1 for a, b in zip(verdicts, verdicts[1:]) if a != b)


class TestTheRecordingIsWhatWeThinkItIs(unittest.TestCase):
    """
    Guard the evidence itself.

    Every argument below rests on this file containing a busy machine with a
    volatile GPU. If somebody re-records it on an idle machine the other tests
    would still pass and would be proving nothing.
    """

    def setUp(self) -> None:
        self.trace = load_trace()

    def test_there_is_a_useful_amount_of_it(self) -> None:
        self.assertGreaterEqual(len(self.trace), 25)

    def test_the_graphics_card_really_was_occupied(self) -> None:
        used = [row["vram_used_mb"] for row in self.trace]
        total = self.trace[0]["vram_total_mb"]

        self.assertGreater(min(used) / total, 0.75)

    def test_the_gpu_signal_really_was_volatile(self) -> None:
        # Both extremes present in one minute is the whole reason this module
        # exists. Without it the recording proves nothing about spikes.
        utilisation = [row["gpu_utilization_percent"] for row in self.trace]

        self.assertLess(min(utilisation), 25)
        self.assertGreater(max(utilisation), 75)


class TestJudgingOneReadingAtATime(unittest.TestCase):
    """
    What the code did before this module existed.

    Worth being precise about what this particular recording does and does not
    show. When it was taken the card was at 93%, past the policy's own block
    line, so every reading came back BLOCK and the per-reading rule happened to
    be stable. The flapping is real and was measured — ten consecutive reads of
    an unchanging machine gave pressure ``high`` eight times and ``critical``
    twice, and the policy ``warn`` seven times and ``block`` three — but that
    needed the card sitting in the band between the warn line at 75% and the
    block line at 90%, where the volatile GPU signal decides the answer.

    So the flapping case is covered by the deliberately synthetic tests in
    ``tests/test_load_signal.py``, which are honest about being invented, and
    this file covers what a real busy machine does. Asserting flapping here
    would be asserting something this recording does not contain.
    """

    def test_every_single_reading_calls_the_machine_loaded(self) -> None:
        verdicts = [
            evaluate_resources(*as_readings(row)) for row in load_trace()
        ]

        self.assertTrue(all(verdict is not ResourceDecision.ALLOW
                            for verdict in verdicts))

    def test_the_card_barely_moved_across_the_whole_minute(self) -> None:
        # The premise of calling this machine's state "unchanging": whatever
        # the GPU utilisation was doing, occupancy was steady.
        used = [row["vram_used_mb"] for row in load_trace()]

        self.assertLess(max(used) - min(used), 500)


class TestJudgingTheWindow(unittest.TestCase):
    """What it does now."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.monitor = SustainedLoadMonitor(clock=self.clock)
        self.verdicts: list[LoadLevel] = []

        for row in load_trace():
            system, gpu = as_readings(row)
            self.clock.advance(DEFAULT_LOAD_CONFIG.sample_interval_seconds)

            self.monitor.offer(
                LoadSample(
                    at=self.clock(),
                    decision=evaluate_resources(system, gpu),
                    cpu_percent=system.cpu_usage_percent,
                    ram_percent=system.ram_usage_percent,
                    vram_used_percent=gpu.vram_used_percent,
                    vram_free_mb=gpu.vram_total_mb - gpu.vram_used_mb,
                    gpu_utilization_percent=gpu.gpu_utilization_percent,
                    gpu_temperature_c=gpu.temperature_c,
                )
            )
            self.verdicts.append(self.monitor.snapshot().level)

    def test_it_settles_on_busy(self) -> None:
        self.assertIs(self.verdicts[-1], LoadLevel.BUSY)

    def test_it_makes_up_its_mind_once(self) -> None:
        # One change is the move out of UNKNOWN. Anything beyond that is the
        # flapping this module was written to remove.
        self.assertLessEqual(count_changes(self.verdicts), 1)

    def test_it_never_claims_the_machine_is_free(self) -> None:
        self.assertNotIn(LoadLevel.FREE, self.verdicts)

    def test_it_is_never_noisier_than_judging_each_reading(self) -> None:
        # On a recording where the per-reading rule is already stable this can
        # only tie, and that is the point: smoothing must never *add* churn.
        per_reading = [
            evaluate_resources(*as_readings(row)) for row in load_trace()
        ]

        self.assertLessEqual(
            count_changes(self.verdicts),
            count_changes(per_reading) + 1,   # +1 for the move out of UNKNOWN
        )


if __name__ == "__main__":
    unittest.main()
