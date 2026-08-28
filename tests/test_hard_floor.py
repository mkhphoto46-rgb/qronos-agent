"""
The limits the override button must not be able to lift.

Every test here is one sentence of the promise made to the user: press "run
anyway" and Qronos will be rude on your behalf, but it will not do the two
things that would break your machine rather than merely inconvenience it.

Pure functions of one reading, so there is no clock and nothing to inject.
"""

from __future__ import annotations

import unittest

from core.hard_floor import (
    DEFAULT_HARD_FLOOR,
    FloorBreach,
    HardFloorConfig,
    check,
    required_vram_mb,
)
from core.load_signal import LoadSample
from core.model_registry import MODELS
from core.resource_policy import ResourceDecision


CARD_MB = 16_303

FAST_MB = required_vram_mb(MODELS["fast"].estimated_vram_gb)     # 3,482
HEAVY_MB = required_vram_mb(MODELS["heavy"].estimated_vram_gb)   # 10,240


def a_reading(
    free_vram_mb: int | None = 12_000,
    gpu_temperature_c: int | None = 45,
    ram_percent: float = 43.0,
) -> LoadSample:
    return LoadSample(
        at=1000.0,
        decision=ResourceDecision.ALLOW,
        cpu_percent=15.0,
        ram_percent=ram_percent,
        vram_free_mb=free_vram_mb,
        gpu_temperature_c=gpu_temperature_c,
    )


class TestTheModelHasToFit(unittest.TestCase):
    def test_the_fast_brain_is_refused_one_megabyte_short(self) -> None:
        verdict = check(
            a_reading(free_vram_mb=FAST_MB + 511),
            required_vram_mb=FAST_MB,
        )

        self.assertFalse(verdict.passed)
        self.assertIs(verdict.breach, FloorBreach.VRAM_EXHAUSTED)

    def test_and_allowed_one_megabyte_over(self) -> None:
        verdict = check(
            a_reading(free_vram_mb=FAST_MB + 512),
            required_vram_mb=FAST_MB,
        )

        self.assertTrue(verdict.passed)

    def test_this_machine_as_measured_refuses_both_brains(self) -> None:
        # 1,906 MiB free of 16,303 while the user's own work held the card.
        # Recorded on 2026-08-28 and kept here because it is the case the
        # feature was actually demonstrated against.
        today = a_reading(free_vram_mb=1_906)

        self.assertFalse(check(today, FAST_MB).passed)
        self.assertFalse(check(today, HEAVY_MB).passed)

    def test_an_empty_card_takes_the_heavy_brain(self) -> None:
        self.assertTrue(
            check(a_reading(free_vram_mb=CARD_MB - 1_500), HEAVY_MB).passed
        )

    def test_the_refusal_carries_both_numbers(self) -> None:
        # A refusal without numbers invites the user to press the button
        # again. One with them does not.
        verdict = check(a_reading(free_vram_mb=1_906), HEAVY_MB)

        self.assertIn("10240", verdict.message().replace(",", ""))
        self.assertIn("1906", verdict.message().replace(",", ""))

    def test_it_says_the_limit_cannot_be_overridden(self) -> None:
        message = check(a_reading(free_vram_mb=100), FAST_MB).message()

        self.assertIn("cannot be overridden", message)


class TestTemperature(unittest.TestCase):
    def test_eighty_four_is_allowed(self) -> None:
        self.assertTrue(check(a_reading(gpu_temperature_c=84), FAST_MB).passed)

    def test_eighty_five_is_not(self) -> None:
        verdict = check(a_reading(gpu_temperature_c=85), FAST_MB)

        self.assertFalse(verdict.passed)
        self.assertIs(verdict.breach, FloorBreach.GPU_TEMPERATURE)

    def test_once_refused_it_stays_refused_at_eighty(self) -> None:
        # A card that just touched 85 sits near 84 for a while. Without the
        # gap this is the one limit that would flap, letting work on and off
        # the card repeatedly at exactly the moment that is worst.
        verdict = check(
            a_reading(gpu_temperature_c=80),
            FAST_MB,
            currently_refused=True,
        )

        self.assertFalse(verdict.passed)

    def test_and_clears_once_it_has_actually_cooled(self) -> None:
        verdict = check(
            a_reading(gpu_temperature_c=77),
            FAST_MB,
            currently_refused=True,
        )

        self.assertTrue(verdict.passed)

    def test_temperature_is_checked_before_memory(self) -> None:
        # A card that is both too hot and too full should say too hot: it is
        # the condition that will not fix itself by closing an application.
        verdict = check(
            a_reading(free_vram_mb=10, gpu_temperature_c=90),
            FAST_MB,
        )

        self.assertIs(verdict.breach, FloorBreach.GPU_TEMPERATURE)


class TestSystemMemory(unittest.TestCase):
    def test_ninety_five_percent_refuses(self) -> None:
        verdict = check(a_reading(ram_percent=95.0), FAST_MB)

        self.assertIs(verdict.breach, FloorBreach.SYSTEM_MEMORY)

    def test_ninety_four_does_not(self) -> None:
        self.assertTrue(check(a_reading(ram_percent=94.0), FAST_MB).passed)


class TestAMissingGraphicsReading(unittest.TestCase):
    """
    The one place this module deliberately disagrees with the rest of the code.

    ``ActivityGuard`` returns CRITICAL when a reading throws, which is right
    for a guard. Here it would be wrong, because ``read_gpu_status()`` returns
    None both for "the read failed" and for "this machine has no NVIDIA card".
    Failing closed would make Qronos refuse everything forever on every AMD and
    Intel machine — a permanent, silent, total failure, guarding against a
    transient one.
    """

    def test_no_reading_does_not_refuse(self) -> None:
        verdict = check(
            a_reading(free_vram_mb=None, gpu_temperature_c=None),
            HEAVY_MB,
        )

        self.assertTrue(verdict.passed)

    def test_but_it_is_recorded_so_a_caller_can_be_stricter(self) -> None:
        verdict = check(
            a_reading(free_vram_mb=None, gpu_temperature_c=None),
            HEAVY_MB,
        )

        self.assertTrue(verdict.gpu_unknown)

    def test_a_readable_card_is_not_flagged_unknown(self) -> None:
        self.assertFalse(check(a_reading(), FAST_MB).gpu_unknown)

    def test_memory_pressure_still_refuses_without_a_card(self) -> None:
        # The other signals do not stop being trustworthy just because the
        # graphics reading is missing.
        verdict = check(
            a_reading(free_vram_mb=None, gpu_temperature_c=None,
                      ram_percent=97.0),
            FAST_MB,
        )

        self.assertIs(verdict.breach, FloorBreach.SYSTEM_MEMORY)


class TestTheDeclaredFootprints(unittest.TestCase):
    def test_they_round_up(self) -> None:
        # Asking for slightly too much costs nothing; asking for slightly too
        # little is how a load fails halfway through.
        self.assertEqual(required_vram_mb(3.4), 3_482)

    def test_they_exceed_what_was_actually_measured(self) -> None:
        # Measured peaks on the development card: 3,442 and 10,220 MiB. If a
        # declared figure ever slipped below its measured peak the floor would
        # wave through a load that does not fit.
        self.assertGreater(FAST_MB, 3_442)
        self.assertGreater(HEAVY_MB, 10_220)


class TestTheLimitsThemselves(unittest.TestCase):
    def test_the_resume_temperature_is_below_the_critical_one(self) -> None:
        self.assertLess(
            DEFAULT_HARD_FLOOR.gpu_resume_temp_c,
            DEFAULT_HARD_FLOOR.gpu_critical_temp_c,
        )

    def test_a_negative_requirement_is_refused_outright(self) -> None:
        with self.assertRaises(ValueError):
            check(a_reading(), required_vram_mb=-1)

    def test_the_limits_can_be_tightened_but_the_shape_is_fixed(self) -> None:
        strict = HardFloorConfig(gpu_critical_temp_c=70)

        self.assertFalse(
            check(a_reading(gpu_temperature_c=72), FAST_MB, config=strict).passed
        )


if __name__ == "__main__":
    unittest.main()
