"""
Power, disk and graphics-memory protection through the real monitor, floor,
foreground guard and attribution, with every reading injected. The scheduler's
preemption is in tests/test_protective_preemption.py.

No test reads the machine it runs on: batteries, drives and graphics cards are
fakes, the clock is moved by hand, and the "work" records what it was asked to
do. Nothing here loads a model or touches another application.
"""

from __future__ import annotations


import pytest

from core import resource_context
from core.activity_guard import ActivityGuard, ResourcePressure
from core.environment_signals import DiskReading, EnvironmentReading, PowerReading
from core.hard_floor import FloorBreach, HardFloorConfig, check, check_running
from core.load_signal import LoadLevel, LoadSample, SustainedLoadConfig, SustainedLoadMonitor
from core.resource_context import VramAttribution, snapshot_for_external_pressure
from core.resource_guard import GpuStatus, SystemStatus
from core.resource_ownership import ResourceBudget, ResourceLedger, ResourceOwner, WorkloadPriority
from core.resource_policy import ResourceDecision
from core.telemetry_cache import Snapshot
from tests.fixtures.clock import FakeClock

QUIET = SystemStatus(cpu_usage_percent=10.0, ram_usage_percent=40.0, ram_used_gb=6.0, ram_total_gb=16.0)
CARD = GpuStatus(name="test card", temperature_c=45, gpu_utilization_percent=5, vram_used_mb=1_000, vram_total_mb=8_192)


def power(on_battery=False, percent=None, has_battery=True, saver=None, available=True):
    if not available:
        return PowerReading(available=False)
    return PowerReading(True, has_battery, on_battery, percent, saver)


def environment(on_battery=False, percent=None, free_mb=50_000, disk_ok=True, power_ok=True):
    disks = (DiskReading("data", True, free_mb, 100_000),) if disk_ok else (DiskReading("data", False),)
    return EnvironmentReading(power=power(on_battery, percent, available=power_ok), disks=disks)


class Environment:
    """A power-and-disk source a test sets directly; counts its reads."""

    def __init__(self, reading: EnvironmentReading | None = None):
        self.reading = reading or environment()
        self.reads = 0

    def __call__(self) -> EnvironmentReading:
        self.reads += 1
        return self.reading


def a_monitor(clock: FakeClock, env: Environment) -> SustainedLoadMonitor:
    return SustainedLoadMonitor(
        config=SustainedLoadConfig(),
        clock=clock,
        read_system=lambda: QUIET,
        read_gpu=lambda: CARD,
        read_environment=env,
    )


def run_for(monitor: SustainedLoadMonitor, clock: FakeClock, seconds: float) -> None:
    for _ in range(int(seconds / 2)):
        clock.advance(2.0)
        monitor.observe()


# ------------------------------------------------- sustained monitor: politeness


def test_running_on_battery_holds_queued_work_only_after_the_busy_dwell():
    clock, env = FakeClock(), Environment(environment(on_battery=True, percent=80))
    monitor = a_monitor(clock, env)
    sample = monitor.observe()

    assert sample.decision is ResourceDecision.ALLOW  # CPU/GPU thresholds unchanged
    assert sample.environment_decision is ResourceDecision.WARN and sample.loaded
    assert sample.on_battery is True and sample.battery_percent == 80.0

    run_for(monitor, clock, 10)
    assert monitor.snapshot().level is LoadLevel.UNKNOWN
    run_for(monitor, clock, 14)
    assert monitor.snapshot().level is LoadLevel.BUSY


def test_one_battery_blip_does_not_hold_work_and_the_charger_releases_it_slowly():
    clock, env = FakeClock(), Environment()
    monitor = a_monitor(clock, env)
    run_for(monitor, clock, 40)
    assert monitor.snapshot().level is LoadLevel.FREE

    env.reading = environment(on_battery=True, percent=60)
    run_for(monitor, clock, 2)
    env.reading = environment()
    run_for(monitor, clock, 30)
    assert monitor.snapshot().level is LoadLevel.FREE  # a spike is not a state

    env.reading = environment(on_battery=True, percent=60)
    run_for(monitor, clock, 30)
    assert monitor.snapshot().level is LoadLevel.BUSY

    env.reading = environment()
    run_for(monitor, clock, 20)
    assert monitor.snapshot().level is LoadLevel.BUSY  # quick to yield, slow to take
    run_for(monitor, clock, 14)
    assert monitor.snapshot().level is LoadLevel.FREE


def test_every_sample_reads_power_and_disk_afresh():
    clock, env = FakeClock(), Environment()
    monitor = a_monitor(clock, env)
    run_for(monitor, clock, 10)
    assert env.reads == 5


def test_unreadable_power_and_disk_are_recorded_not_guessed():
    clock = FakeClock()
    env = Environment(environment(power_ok=False, disk_ok=False))
    sample = a_monitor(clock, env).observe()
    assert sample.power_unknown and sample.disk_unknown
    assert sample.on_battery is None and sample.disk_free_mb is None
    assert not sample.loaded


def test_a_failing_environment_reader_never_stops_the_sampler():
    def broken():
        raise RuntimeError("unexpected")

    sample = SustainedLoadMonitor(read_system=lambda: QUIET, read_gpu=lambda: CARD, read_environment=broken).observe()
    assert sample.power_unknown and sample.disk_unknown and not sample.loaded


def test_low_disk_space_makes_the_machine_count_as_loaded():
    clock = FakeClock()
    sample = a_monitor(clock, Environment(environment(free_mb=4_000))).observe()
    assert sample.environment_decision is ResourceDecision.BLOCK and sample.disk_free_mb == 4_000


# ------------------------------------------------------------ hard floor


def reading(**fields) -> LoadSample:
    base = dict(at=0.0, decision=ResourceDecision.ALLOW, cpu_percent=10.0, ram_percent=40.0,
                vram_free_mb=7_000, gpu_temperature_c=45)
    base.update(fields)
    return LoadSample(**base)


def test_a_nearly_empty_battery_refuses_work_and_cannot_be_overridden():
    verdict = check(reading(on_battery=True, battery_percent=9.0), required_vram_mb=0)
    assert not verdict.passed and verdict.breach is FloorBreach.BATTERY_CRITICAL
    assert "9%" in verdict.message() and "cannot be overridden" in verdict.message()


def test_the_same_charge_on_the_charger_is_not_refused():
    assert check(reading(on_battery=False, battery_percent=5.0), required_vram_mb=0).passed


def test_battery_floor_is_hysteretic():
    assert not check(reading(on_battery=True, battery_percent=10.0), 0).passed
    assert check(reading(on_battery=True, battery_percent=11.0), 0).passed
    assert not check(reading(on_battery=True, battery_percent=14.9), 0, currently_refused=True).passed
    assert check(reading(on_battery=True, battery_percent=15.0), 0, currently_refused=True).passed


def test_disk_floor_is_hysteretic_and_reports_the_space():
    verdict = check(reading(disk_free_mb=2_047), 0)
    assert verdict.breach is FloorBreach.DISK_CRITICAL and "2047 MB" in verdict.message()
    assert check(reading(disk_free_mb=2_048), 0).passed
    assert not check(reading(disk_free_mb=3_071), 0, currently_refused=True).passed
    assert check(reading(disk_free_mb=3_072), 0, currently_refused=True).passed


def test_unknown_power_and_disk_pass_and_say_so():
    verdict = check(reading(power_unknown=True, disk_unknown=True), required_vram_mb=0)
    assert verdict.passed and verdict.power_unknown and verdict.disk_unknown


def test_fit_is_judged_on_physically_free_memory_not_on_the_external_view():
    # The card shows 7,000 MB free once Qronos's own 4,000 MB are taken off
    # the used figure; only 3,000 MB are actually free.
    sample = reading(vram_free_mb=7_000, own_vram_mb=4_000)
    assert sample.physical_vram_free_mb == 3_000
    verdict = check(sample, required_vram_mb=3_000)
    assert verdict.breach is FloorBreach.VRAM_EXHAUSTED
    assert check(reading(vram_free_mb=7_000), required_vram_mb=3_000).passed


def test_running_work_yields_when_another_application_is_squeezed():
    squeezed = reading(vram_free_mb=6_400 + 100, own_vram_mb=6_400, external_vram_growth_mb=292)
    verdict = check_running(squeezed)
    assert verdict.breach is FloorBreach.VRAM_SQUEEZED and "gives the memory back" in verdict.message()


def test_a_card_qronos_filled_by_itself_does_not_make_it_yield():
    full_by_itself = reading(vram_free_mb=6_400 + 100, own_vram_mb=6_400, external_vram_growth_mb=0)
    assert check_running(full_by_itself).passed
    noise = reading(vram_free_mb=6_400 + 100, own_vram_mb=6_400, external_vram_growth_mb=63)
    assert check_running(noise).passed


def test_growth_into_memory_nobody_needs_is_not_a_squeeze():
    roomy = reading(vram_free_mb=6_400 + 2_000, own_vram_mb=6_400, external_vram_growth_mb=900)
    assert check_running(roomy).passed


def test_running_work_is_not_refused_for_fitting_but_is_for_safety():
    assert check_running(reading(vram_free_mb=0)).passed
    assert check_running(reading(gpu_temperature_c=86)).breach is FloorBreach.GPU_TEMPERATURE
    assert check_running(reading(on_battery=True, battery_percent=5.0)).breach is FloorBreach.BATTERY_CRITICAL


def test_floor_refuses_inconsistent_new_lines():
    with pytest.raises(ValueError):
        HardFloorConfig(battery_critical_percent=20.0, battery_resume_percent=15.0)
    with pytest.raises(ValueError):
        HardFloorConfig(disk_critical_free_mb=4_000, disk_resume_free_mb=3_000)


# ------------------------------------------------- foreground model loads


def guard_with(env_reading: EnvironmentReading | None) -> ActivityGuard:
    snapshot = Snapshot(system=QUIET, gpu=CARD, taken_at=0.0, environment=env_reading)
    guard = ActivityGuard(game_processes=set(), creator_processes=set(), snapshot_reader=lambda: snapshot)
    guard._get_running_processes = staticmethod(lambda: set())  # type: ignore[method-assign]
    return guard


@pytest.mark.parametrize(
    "env_reading, expected",
    [
        (None, ResourcePressure.NORMAL),
        (environment(on_battery=True, percent=70), ResourcePressure.NORMAL),
        (environment(on_battery=True, percent=18), ResourcePressure.HIGH),
        (environment(on_battery=True, percent=8), ResourcePressure.CRITICAL),
        (environment(free_mb=4_000), ResourcePressure.HIGH),
        (environment(free_mb=1_000), ResourcePressure.CRITICAL),
        (environment(power_ok=False, disk_ok=False), ResourcePressure.NORMAL),
    ],
)
def test_foreground_pressure_includes_power_and_disk(env_reading, expected):
    assert guard_with(env_reading).detect().resource_pressure is expected


def test_foreground_snapshot_gets_a_fresh_environment_reading_each_time(monkeypatch):
    env = Environment(environment(on_battery=True, percent=8))
    monkeypatch.setattr(resource_context, "production_environment_reader", env)
    raw = Snapshot(system=QUIET, gpu=CARD, taken_at=0.0)
    ledger = ResourceLedger()

    first = snapshot_for_external_pressure(raw, ledger)
    second = snapshot_for_external_pressure(raw, ledger)
    assert env.reads == 2 and first.environment is env.reading and second.environment is env.reading
    assert snapshot_for_external_pressure(raw, ledger, environment=None).environment is None


# ---------------------------------------- graphics memory: partial offload


def reserve(ledger: ResourceLedger, mb: int):
    return ledger.reserve(owner=ResourceOwner.QRONOS, workload="heavy", priority=WorkloadPriority.ACTIVE_QRONOS_REQUEST,
                          budget=ResourceBudget(vram_mb=mb))


def card(used: int, total: int = 8_192) -> GpuStatus:
    return GpuStatus(name="test card", temperature_c=50, gpu_utilization_percent=60,
                     vram_used_mb=min(used, total), vram_total_mb=total)


class Card:
    def __init__(self, used: int):
        self.used = used

    def __call__(self) -> GpuStatus:
        return card(self.used)


def test_partially_offloaded_model_no_longer_hides_other_applications():
    clock, ledger, gpu = FakeClock(), ResourceLedger(), Card(1_500)
    attribution = VramAttribution(ledger, read_gpu=gpu, settle_seconds=10.0, clock=clock)
    attribution.read_gpu()  # baseline: 1,500 MB used by others

    reserve(ledger, 10_240)  # declared Heavy footprint, larger than the card
    gpu.used = 7_900  # the runtime fills what was free
    attribution.read_gpu()
    assert attribution.own_vram_mb() == 6_400 and attribution.external_growth_mb() == 0

    clock.advance(11.0)
    gpu.used = 1_500 + 600 + 6_400  # another application grows; the card tops out
    attribution.read_gpu()
    assert attribution.own_vram_mb() == 6_400
    assert attribution.external_growth_mb() == 292  # all that a full card can show

    # The old subtraction of the reservation would have read 0 MB external.
    assert max(0, 8_192 - 10_240) == 0
    assert 8_192 - attribution.own_vram_mb() == 1_792


def test_growth_while_the_model_loads_is_counted_as_its_own():
    clock, ledger, gpu = FakeClock(), ResourceLedger(), Card(1_000)
    attribution = VramAttribution(ledger, read_gpu=gpu, settle_seconds=10.0, clock=clock)
    attribution.read_gpu()
    reserve(ledger, 3_482)
    for used in (2_000, 3_500, 4_400):
        clock.advance(2.0)
        gpu.used = used
        attribution.read_gpu()
    assert attribution.own_vram_mb() == 3_400 and attribution.external_growth_mb() == 0


def test_releasing_the_reservation_resets_the_baseline():
    clock, ledger, gpu = FakeClock(), ResourceLedger(), Card(1_000)
    attribution = VramAttribution(ledger, read_gpu=gpu, clock=clock)
    attribution.read_gpu()
    held = reserve(ledger, 3_000)
    gpu.used = 4_000
    attribution.read_gpu()
    ledger.release(held.reservation_id)
    gpu.used = 2_500
    attribution.read_gpu()
    assert attribution.own_vram_mb() == 0 and attribution.external_growth_mb() is None


def test_without_a_baseline_the_share_is_clamped_and_no_growth_is_claimed():
    clock, ledger, gpu = FakeClock(), ResourceLedger(), Card(7_900)
    reserve(ledger, 10_240)
    attribution = VramAttribution(ledger, read_gpu=gpu, clock=clock)
    attribution.read_gpu()
    assert attribution.own_vram_mb() == 7_900 and attribution.external_growth_mb() is None


def test_gpu_utilisation_is_suppressed_only_while_qronos_holds_the_card():
    ledger, gpu = ResourceLedger(), Card(1_000)
    attribution = VramAttribution(ledger, read_gpu=gpu, clock=FakeClock())
    assert attribution.read_gpu().gpu_utilization_percent == 60
    reserve(ledger, 1_000)
    assert attribution.read_gpu().gpu_utilization_percent is None


def test_production_monitor_wires_attribution_environment_and_growth(monkeypatch):
    ledger, gpu = ResourceLedger(), Card(1_500)
    monkeypatch.setattr(resource_context, "read_gpu_status", gpu)
    env = Environment(environment(free_mb=40_000))
    monitor = resource_context.build_sustained_load_monitor(ledger=ledger, environment=env)
    monitor._read_system = lambda: QUIET  # type: ignore[attr-defined]

    assert monitor.observe().external_vram_growth_mb is None
    reserve(ledger, 10_240)
    gpu.used = 7_900
    sample = monitor.observe()
    assert sample.own_vram_mb == 6_400 and sample.external_vram_growth_mb == 0
    assert sample.disk_free_mb == 40_000 and env.reads == 2
