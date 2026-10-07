"""
Admission of a brain whose configured VRAM estimate understates its real need.

Measured on the development machine (RTX 5080, 16,303 MiB): the specified
Heavy brain at its specified 16,384-token context was resident at
12,740,009,655 bytes (12,149 MiB), while its configured estimate is 6.4 GB
(6,554 MiB). With 5,783 MiB already used by other applications, admission
compared 10,520 MiB free against 6,554 + 512 MiB and let the load start. The
model runtime then placed all 12.1 GB on the card, and other applications'
VRAM shrank while it did.

These tests drive the real chain - Orchestrator.execute_plan ->
_prepare_resources -> ModelManager.select_model -> evaluate_resources and
ActivityGuard pressure through resource_context - with those readings as
synthetic telemetry and a fake model runtime that records whether a request
was sent. Nothing is loaded and no daemon is contacted.

The configured estimate is part of the immutable model specification and is
never lowered; local evidence can only show that a brain needs more.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.activity_guard import ActivityGuard
from core.brain_runtime import BrainRuntimeModelStatus
from core.model_manager import InstalledModelSizes, ModelManager
from core.model_registry import get_model
from core.orchestrator import Orchestrator
from core.resource_context import snapshot_for_external_pressure
from core.resource_guard import GpuStatus, SystemStatus
from core.task_plan import TaskPlan
from core.task_router import TaskType
from core.telemetry_cache import Snapshot


HEAVY = get_model("heavy")
FAST = get_model("fast")

CARD_MIB = 16_303
OTHER_APPS_AT_START_MIB = 5_783
OTHER_APPS_AT_END_MIB = 2_759

#: Measured: the daemon's resident size at the configured context.
HEAVY_RESIDENT_BYTES = 12_740_009_655
#: Measured: sizes in the local model store.
HEAVY_INSTALLED_BYTES = 13_793_440_771
FAST_INSTALLED_BYTES = 5_023_496_320
FAST_RESIDENT_BYTES = 3_435_743_149


class Runtime:
    """A model runtime that records what admission let through."""

    base_url = "http://127.0.0.1:11434"

    def __init__(self, running=()) -> None:
        self.running = list(running)
        self.chats: list[str] = []
        self.stopped: list[str] = []

    def health_check(self) -> bool:
        return True

    def list_running_models(self):
        return list(self.running)

    def chat(self, model_name, **kwargs) -> str:
        self.chats.append(model_name)
        return "an answer"

    def stop_model(self, model_name) -> None:
        self.stopped.append(model_name)
        self.running = [m for m in self.running if m.name != model_name]

    def unload_all(self) -> None:
        raise AssertionError("admission must never unload every model")


def resident(name: str, size: int, context: int) -> BrainRuntimeModelStatus:
    return BrainRuntimeModelStatus(name=name, size=str(size), context=context)


def snapshot(used_mib: int, total_mib: int = CARD_MIB) -> Snapshot:
    return Snapshot(
        system=SystemStatus(
            cpu_usage_percent=6.0,
            ram_usage_percent=30.0,
            ram_used_gb=19.0,
            ram_total_gb=63.2,
        ),
        gpu=GpuStatus(
            name="test card",
            temperature_c=38,
            gpu_utilization_percent=1,
            vram_used_mb=used_mib,
            vram_total_mb=total_mib,
        ),
        taken_at=0.0,
    )


def an_orchestrator(runtime: Runtime, reading: Snapshot, installed=None):
    guard = ActivityGuard(
        snapshot_reader=lambda: snapshot_for_external_pressure(reading),
    )
    guard._get_running_processes = lambda: set()  # no real process listing

    manager = ModelManager(
        runtime=runtime,
        installed_size_bytes=(
            None if installed is None else installed.get
        ),
    )

    return Orchestrator(
        runtime=runtime,  # type: ignore[arg-type]
        model_manager=manager,
        activity_guard=guard,
        telemetry=SimpleNamespace(  # type: ignore[arg-type]
            current=lambda: reading,
            invalidate=lambda: None,
        ),
    )


def run(orchestrator: Orchestrator, task: TaskType):
    plan = TaskPlan(goal="synthetic")
    plan.add_step(task_type=task, description="synthetic request")
    (result,) = orchestrator.execute_plan(plan)
    return result


# ----------------------------------------------- the measured condition


def test_heavy_is_deferred_where_its_real_size_would_displace_other_apps() -> None:
    users_own_model = resident("someone-elses-model:latest", 2_000_000_000, 4096)
    runtime = Runtime(running=[users_own_model])
    orchestrator = an_orchestrator(
        runtime,
        snapshot(OTHER_APPS_AT_START_MIB),
        installed={HEAVY.name: HEAVY_INSTALLED_BYTES},
    )

    result = run(orchestrator, TaskType.HEAVY)

    assert runtime.chats == []
    assert result.success is False
    assert result.resource_blocked is True
    # The reason is the model's size, said plainly.
    assert "free video memory" in result.error
    # Deferring touched nothing: the user's own model stays loaded.
    assert runtime.stopped == []


def test_without_any_evidence_admission_is_unchanged_and_says_so() -> None:
    # Bounded fallback: no observation and no store listing - for example
    # the store could not be read - leaves only the configured estimate, as
    # before this change. This is the remaining unsafe case, kept visible.
    runtime = Runtime()
    manager = ModelManager(runtime=runtime)
    reading = snapshot(OTHER_APPS_AT_START_MIB)

    selection = manager.select_model(
        task_class=_heavy(),
        system=reading.system,
        gpu=reading.gpu,
    )

    assert selection.decision.value == "allow"
    assert selection.capacity_evidence == "configured"


def test_an_observed_footprint_is_learned_and_used_on_the_next_load() -> None:
    # Heavy was resident earlier at its configured context; the daemon said
    # how large it was. It has since been unloaded.
    runtime = Runtime(
        running=[resident(HEAVY.name, HEAVY_RESIDENT_BYTES, HEAVY.context_tokens)]
    )
    orchestrator = an_orchestrator(runtime, snapshot(OTHER_APPS_AT_START_MIB))
    orchestrator.model_manager.is_model_resident(HEAVY.name)
    runtime.running = []

    # Other apps hold 5.8 GB: 12,149 + 512 MiB does not fit 10,520 free.
    assert run(orchestrator, TaskType.HEAVY).resource_blocked is True
    assert runtime.chats == []

    # They release memory: now it fits without displacing anyone.
    released = an_orchestrator(runtime, snapshot(OTHER_APPS_AT_END_MIB))
    released.model_manager = orchestrator.model_manager
    released.model_manager.runtime = runtime

    assert run(released, TaskType.HEAVY).success is True
    assert runtime.chats == [HEAVY.name]


def test_an_observation_at_a_different_context_is_not_reused() -> None:
    manager = ModelManager(runtime=Runtime(
        running=[resident(HEAVY.name, 2_000_000_000, 2048)]
    ))
    manager.is_model_resident(HEAVY.name)

    needed, evidence = manager.footprint_mb(HEAVY)

    # A smaller context's size says nothing about the configured context's.
    assert evidence == "configured"
    assert needed == 6_554


def test_evidence_never_lowers_the_configured_estimate() -> None:
    manager = ModelManager(
        runtime=Runtime(
            running=[resident(FAST.name, FAST_RESIDENT_BYTES, FAST.context_tokens)]
        )
    )
    manager.is_model_resident(FAST.name)

    needed, evidence = manager.footprint_mb(FAST)

    # Fast was observed at 3,277 MiB, below its 3.6 GB estimate.
    assert evidence == "observed"
    assert needed == 3_687


# -------------------------------------------------- what must stay usable


def test_fast_is_still_admitted_under_the_measured_condition() -> None:
    runtime = Runtime()
    orchestrator = an_orchestrator(
        runtime,
        snapshot(OTHER_APPS_AT_START_MIB),
        installed={FAST.name: FAST_INSTALLED_BYTES},
    )

    assert run(orchestrator, TaskType.FAST).success is True
    assert runtime.chats == [FAST.name]


def test_a_brain_that_could_never_fit_this_card_keeps_working_with_offload() -> None:
    # 8 GB card: Heavy's real size exceeds the whole card, so waiting would
    # never help. Admission keeps the configured partial-offload behaviour,
    # rather than making Heavy unusable on this hardware.
    runtime = Runtime()
    orchestrator = an_orchestrator(
        runtime,
        snapshot(600, total_mib=8_192),
        installed={HEAVY.name: HEAVY_INSTALLED_BYTES},
    )

    selection = orchestrator.model_manager.select_model(
        task_class=_heavy(),
        system=snapshot(600, 8_192).system,
        gpu=snapshot(600, 8_192).gpu,
    )

    assert selection.decision.value == "allow"
    assert selection.capacity_evidence == "installed-exceeds-card"
    assert run(orchestrator, TaskType.HEAVY).success is True


def test_an_already_resident_brain_is_reused_without_a_capacity_check() -> None:
    # Heavy is loaded and the card looks full because of it. Reuse needs no
    # second copy, so the projected-fit check must not refuse it.
    runtime = Runtime(
        running=[resident(HEAVY.name, HEAVY_RESIDENT_BYTES, HEAVY.context_tokens)]
    )
    orchestrator = an_orchestrator(
        runtime,
        snapshot(OTHER_APPS_AT_END_MIB + 12_149),
        installed={HEAVY.name: HEAVY_INSTALLED_BYTES},
    )

    assert run(orchestrator, TaskType.HEAVY).success is True
    assert runtime.chats == [HEAVY.name]


# ------------------------------------------ installed sizes, read honestly


def test_installed_sizes_are_cached_briefly_and_failures_read_as_unknown() -> None:
    calls = []
    now = [0.0]
    listing = [SimpleNamespace(name=HEAVY.name, size_bytes=HEAVY_INSTALLED_BYTES)]

    def list_installed():
        calls.append(1)
        if listing is None:
            raise RuntimeError("store unavailable")
        return tuple(listing)

    sizes = InstalledModelSizes(list_installed, ttl_seconds=30, clock=lambda: now[0])

    assert sizes(HEAVY.name) == HEAVY_INSTALLED_BYTES
    assert sizes(HEAVY.name) == HEAVY_INSTALLED_BYTES
    assert len(calls) == 1
    assert sizes("not-installed") is None

    now[0] = 31.0
    listing = None
    assert sizes(HEAVY.name) is None  # a failed read is unknown, not zero

    listing = [SimpleNamespace(name=HEAVY.name, size_bytes=HEAVY_INSTALLED_BYTES)]
    assert sizes(HEAVY.name) == HEAVY_INSTALLED_BYTES  # and not cached


def test_the_production_orchestrator_reads_installed_sizes_locally(
    monkeypatch,
) -> None:
    from core import ollama_models

    seen = []

    def listing(catalog):
        seen.append(catalog.base_url)
        return (SimpleNamespace(name=HEAVY.name, size_bytes=HEAVY_INSTALLED_BYTES),)

    monkeypatch.setattr(
        ollama_models.OllamaModelCatalog,
        "list_installed_models",
        listing,
    )
    runtime = Runtime()

    orchestrator = Orchestrator(runtime=runtime)  # type: ignore[arg-type]

    needed, evidence = orchestrator.model_manager.footprint_mb(HEAVY)
    assert evidence == "installed"
    assert needed == 13_155
    assert seen == ["http://127.0.0.1:11434"]


def _heavy():
    from core.model_manager import TaskClass

    return TaskClass.HEAVY
