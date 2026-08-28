from __future__ import annotations

import unittest

from core.activity_guard import ActivityMode, ResourcePressure
from core.resource_governor import (
    DEFAULT_LEASE_SECONDS,
    Refusal,
    ResourceGovernor,
    Weight,
)
from core.resource_guard import GpuStatus, SystemStatus
from core.resource_policy import ResourceDecision


class FakeClock:
    def __init__(self, initial_time: float = 1000.0) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


def idle_system() -> SystemStatus:
    return SystemStatus(
        cpu_usage_percent=10.0,
        ram_usage_percent=30.0,
        ram_used_gb=9.0,
        ram_total_gb=31.9,
    )


def hammered_system() -> SystemStatus:
    return SystemStatus(
        cpu_usage_percent=97.0,
        ram_usage_percent=95.0,
        ram_used_gb=30.0,
        ram_total_gb=31.9,
    )


def cool_gpu() -> GpuStatus:
    return GpuStatus(
        name="RTX 5080",
        temperature_c=40,
        gpu_utilization_percent=5,
        vram_used_mb=1000,
        vram_total_mb=16000,
    )


def governor(
    system=idle_system,
    gpu=cool_gpu,
    clock: FakeClock | None = None,
) -> ResourceGovernor:
    return ResourceGovernor(
        read_system=system,
        read_gpu=gpu,
        clock=clock or FakeClock(),
    )


class TestGranting(unittest.TestCase):
    def test_an_idle_machine_grants_light_work(self) -> None:
        grant = governor().request("t1", Weight.LIGHT)

        self.assertTrue(grant.granted)
        self.assertIs(grant.decision, ResourceDecision.ALLOW)

    def test_an_idle_machine_grants_heavy_work(self) -> None:
        self.assertTrue(governor().request("t1", Weight.HEAVY).granted)

    def test_a_loaded_machine_refuses(self) -> None:
        grant = governor(system=hammered_system).request("t1", Weight.LIGHT)

        self.assertFalse(grant.granted)
        self.assertIs(grant.refusal, Refusal.RESOURCE_PRESSURE)

    def test_a_task_must_be_named(self) -> None:
        with self.assertRaises(ValueError):
            governor().request("  ", Weight.LIGHT)


class TestOneHeavyConsumer(unittest.TestCase):
    """
    The rule the architecture states and no code could previously enforce.

    A function that returns a decision and forgets cannot hold "one heavy task
    at a time"; somebody has to remember that the first one is running. That is
    the reservation.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.governor = governor(clock=self.clock)

    def test_a_second_heavy_task_is_refused(self) -> None:
        self.governor.request("first", Weight.HEAVY)

        grant = self.governor.request("second", Weight.HEAVY)

        self.assertFalse(grant.granted)
        self.assertIs(grant.refusal, Refusal.HEAVY_TASK_IN_PROGRESS)
        self.assertIn("first", grant.detail)

    def test_light_work_still_flows_beside_a_heavy_task(self) -> None:
        # Blocking everything while one heavy task runs would make Qronos stop
        # answering questions during a long job.
        self.governor.request("heavy", Weight.HEAVY)

        self.assertTrue(self.governor.request("light", Weight.LIGHT).granted)

    def test_the_slot_frees_on_release(self) -> None:
        self.governor.request("first", Weight.HEAVY)
        self.governor.release("first")

        self.assertTrue(self.governor.request("second", Weight.HEAVY).granted)

    def test_the_heavy_owner_is_answerable(self) -> None:
        self.governor.request("render", Weight.HEAVY)

        self.assertEqual(self.governor.heavy_owner(), "render")

    def test_nobody_owns_it_when_nothing_heavy_runs(self) -> None:
        self.governor.request("light", Weight.LIGHT)

        self.assertIsNone(self.governor.heavy_owner())

    def test_check_and_reserve_are_one_step(self) -> None:
        # The window this closes: two heavy tasks both told yes on readings
        # taken before either started. There is no point between the check and
        # the reservation at which a second caller can be granted.
        first = self.governor.request("a", Weight.HEAVY)
        second = self.governor.request("b", Weight.HEAVY)

        self.assertTrue(first.granted)
        self.assertFalse(second.granted)


class TestActivityModeBeatsTheReadings(unittest.TestCase):
    def test_heavy_work_is_refused_in_a_performance_mode(self) -> None:
        # The readings describe this instant. A game at a loading screen looks
        # idle, and starting a heavy job into it is exactly what "stay out of
        # the way" is supposed to prevent.
        for mode in (
            ActivityMode.GAMING_PERFORMANCE,
            ActivityMode.CREATOR_PERFORMANCE,
        ):
            with self.subTest(mode=mode):
                grant = governor().request(
                    "render",
                    Weight.HEAVY,
                    activity_mode=mode,
                )

                self.assertFalse(grant.granted)
                self.assertIs(grant.refusal, Refusal.ACTIVITY_MODE)

    def test_light_work_is_allowed_in_a_performance_mode(self) -> None:
        grant = governor().request(
            "answer",
            Weight.LIGHT,
            activity_mode=ActivityMode.GAMING_PERFORMANCE,
        )

        self.assertTrue(grant.granted)

    def test_assist_modes_still_allow_heavy_work(self) -> None:
        grant = governor().request(
            "render",
            Weight.HEAVY,
            activity_mode=ActivityMode.GAMING_ASSIST,
        )

        self.assertTrue(grant.granted)

    def test_critical_pressure_refuses_heavy_work(self) -> None:
        grant = governor().request(
            "render",
            Weight.HEAVY,
            resource_pressure=ResourcePressure.CRITICAL,
        )

        self.assertFalse(grant.granted)
        self.assertIs(grant.refusal, Refusal.RESOURCE_PRESSURE)


class TestLeases(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.governor = governor(clock=self.clock)

    def test_a_lease_expires(self) -> None:
        # A task that dies between reserving and running must not hold the GPU
        # shut forever. Expiry is the recovery path; there is no supervisor.
        self.governor.request("dead", Weight.HEAVY)

        self.clock.advance(DEFAULT_LEASE_SECONDS + 1)

        self.assertIsNone(self.governor.heavy_owner())
        self.assertTrue(self.governor.request("live", Weight.HEAVY).granted)

    def test_a_lease_does_not_expire_early(self) -> None:
        self.governor.request("alive", Weight.HEAVY)

        self.clock.advance(DEFAULT_LEASE_SECONDS - 1)

        self.assertEqual(self.governor.heavy_owner(), "alive")

    def test_renewing_pushes_the_lease_out(self) -> None:
        self.governor.request("long", Weight.HEAVY)

        self.clock.advance(DEFAULT_LEASE_SECONDS - 1)
        self.governor.renew("long")
        self.clock.advance(DEFAULT_LEASE_SECONDS - 1)

        self.assertEqual(self.governor.heavy_owner(), "long")

    def test_renewing_something_that_expired_returns_nothing(self) -> None:
        self.governor.request("gone", Weight.HEAVY)
        self.clock.advance(DEFAULT_LEASE_SECONDS + 1)

        self.assertIsNone(self.governor.renew("gone"))

    def test_releasing_something_unheld_is_not_an_error(self) -> None:
        self.assertFalse(self.governor.release("never-existed"))


class TestWarningsDoNotBlock(unittest.TestCase):
    def test_a_warn_decision_still_grants(self) -> None:
        # WARN means "this is getting tight", not "stop". Refusing on a warning
        # would make Qronos unusable on a busy machine, which is most machines.
        def busy() -> SystemStatus:
            return SystemStatus(
                cpu_usage_percent=82.0,
                ram_usage_percent=40.0,
                ram_used_gb=12.0,
                ram_total_gb=31.9,
            )

        grant = governor(system=busy).request("t1", Weight.LIGHT)

        self.assertTrue(grant.granted)
        self.assertIs(grant.decision, ResourceDecision.WARN)


class TestReadingWithoutReserving(unittest.TestCase):
    def test_evaluate_takes_no_reservation(self) -> None:
        instance = governor()

        instance.evaluate()

        self.assertEqual(instance.active(), ())

    def test_a_machine_with_no_gpu_is_fine(self) -> None:
        grant = governor(gpu=lambda: None).request("t1", Weight.HEAVY)

        self.assertTrue(grant.granted)


if __name__ == "__main__":
    unittest.main()
