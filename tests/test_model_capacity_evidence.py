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
never lowered; local evidence can only show that a brain needs more. Where
production supplies a store reader and no trustworthy size is available, a
new GPU load is deferred: the estimate is already known to understate.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from core.activity_guard import ActivityGuard
from core.brain_runtime import BrainRuntimeModelStatus
from core.model_manager import (
    InstalledModelSizes,
    ModelManager,
    TaskClass,
    parse_installed_listing,
)
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
OTHER_APPS_LIGHT_MIB = 2_000

#: Measured: the daemon's resident size at the configured context.
HEAVY_RESIDENT_BYTES = 12_740_009_655
FAST_RESIDENT_BYTES = 3_435_743_149
#: Measured: sizes in the local model store.
HEAVY_INSTALLED_BYTES = 13_793_440_771
FAST_INSTALLED_BYTES = 5_023_496_320


# --------------------------------------------------------------- fixtures


class Runtime:
    """A model runtime that records what admission let through."""

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


class LocalRuntime(Runtime):
    """The same, with a local model store - what production has."""

    base_url = "http://127.0.0.1:11434"


class Store:
    """A local store listing that a test can break and repair."""

    def __init__(self, models: list | None = None) -> None:
        self.document: object = {"models": models or []}
        self.fail = False
        self.reads = 0

    def __call__(self):
        self.reads += 1

        if self.fail:
            raise RuntimeError("store unavailable")

        return self.document


def listed(name: str, size) -> dict:
    return {"name": name, "size": size}


def resident(name: str, size: int, context: int) -> BrainRuntimeModelStatus:
    return BrainRuntimeModelStatus(name=name, size=str(size), context=context)


def snapshot(used_mib: int, total_mib: int = CARD_MIB, gpu: bool = True) -> Snapshot:
    return Snapshot(
        system=SystemStatus(
            cpu_usage_percent=6.0,
            ram_usage_percent=30.0,
            ram_used_gb=19.0,
            ram_total_gb=63.2,
        ),
        gpu=(
            GpuStatus(
                name="test card",
                temperature_c=38,
                gpu_utilization_percent=1,
                vram_used_mb=used_mib,
                vram_total_mb=total_mib,
            )
            if gpu
            else None
        ),
        taken_at=0.0,
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def a_manager(runtime: Runtime, store: Store | None, clock: Clock | None = None):
    """A ModelManager wired as production wires it (or without a reader)."""
    return ModelManager(
        runtime=runtime,
        installed_size_bytes=(
            None
            if store is None
            else InstalledModelSizes(store, clock=clock or Clock())
        ),
    )


def an_orchestrator(runtime: Runtime, reading: Snapshot, manager: ModelManager):
    guard = ActivityGuard(
        snapshot_reader=lambda: snapshot_for_external_pressure(reading),
    )
    guard._get_running_processes = lambda: set()  # no real process listing

    return Orchestrator(
        runtime=runtime,  # type: ignore[arg-type]
        model_manager=manager,
        activity_guard=guard,
        telemetry=SimpleNamespace(  # type: ignore[arg-type]
            current=lambda: reading,
            invalidate=lambda: None,
        ),
    )


def run(runtime, reading, manager, task: TaskType):
    plan = TaskPlan(goal="synthetic")
    plan.add_step(task_type=task, description="synthetic request")
    (result,) = an_orchestrator(runtime, reading, manager).execute_plan(plan)
    return result


def a_healthy_store() -> Store:
    return Store(
        [
            listed(HEAVY.name, HEAVY_INSTALLED_BYTES),
            listed(FAST.name, FAST_INSTALLED_BYTES),
            listed("someone-elses-model:latest", 2_000_000_000),
        ]
    )


# ----------------------------------------------- the measured condition


def test_heavy_is_deferred_where_its_real_size_would_displace_other_apps() -> None:
    users_own_model = resident("someone-elses-model:latest", 2_000_000_000, 4096)
    runtime = LocalRuntime(running=[users_own_model])
    manager = a_manager(runtime, a_healthy_store())

    result = run(runtime, snapshot(OTHER_APPS_AT_START_MIB), manager, TaskType.HEAVY)

    assert runtime.chats == []
    assert result.success is False
    assert result.resource_blocked is True
    assert "free video memory" in result.error
    # Deferring touched nothing: the user's own model stays loaded.
    assert runtime.stopped == []


def test_fast_is_still_admitted_under_the_measured_condition() -> None:
    runtime = LocalRuntime()
    manager = a_manager(runtime, a_healthy_store())

    result = run(runtime, snapshot(OTHER_APPS_AT_START_MIB), manager, TaskType.FAST)

    assert result.success is True
    assert runtime.chats == [FAST.name]


# ------------------------------- unknown or malformed capacity: no request


UNTRUSTWORTHY = {
    "store-unreachable": None,
    "heavy-not-listed": {"models": [listed(FAST.name, FAST_INSTALLED_BYTES)]},
    "size-is-bool": {"models": [listed(HEAVY.name, True)]},
    "size-negative": {"models": [listed(HEAVY.name, -13_793_440_771)]},
    "size-zero": {"models": [listed(HEAVY.name, 0)]},
    "size-nan": {"models": [listed(HEAVY.name, math.nan)]},
    "size-infinite": {"models": [listed(HEAVY.name, math.inf)]},
    "size-float": {"models": [listed(HEAVY.name, 13_793_440_771.0)]},
    "size-string": {"models": [listed(HEAVY.name, "13793440771")]},
    "size-absurd": {"models": [listed(HEAVY.name, 1 << 60)]},
    "one-good-one-bad-record": {
        "models": [
            listed(HEAVY.name, 1_000),
            listed(HEAVY.name, "large"),
        ]
    },
    "record-not-an-object": {"models": ["gpt-oss:20b"]},
    "name-with-control-characters": {
        "models": [listed(HEAVY.name + "\n", HEAVY_INSTALLED_BYTES)]
    },
    "models-not-a-list": {"models": {HEAVY.name: HEAVY_INSTALLED_BYTES}},
    "listing-not-an-object": [listed(HEAVY.name, HEAVY_INSTALLED_BYTES)],
}


@pytest.mark.parametrize("case", sorted(UNTRUSTWORTHY))
def test_no_request_is_sent_when_capacity_is_unknown_or_malformed(case: str) -> None:
    # Plenty of free memory by the old estimate, and even for the real size:
    # what is refused is loading without trustworthy evidence of the size.
    store = Store()

    if UNTRUSTWORTHY[case] is None:
        store.fail = True
    else:
        store.document = UNTRUSTWORTHY[case]

    runtime = LocalRuntime()
    manager = a_manager(runtime, store)

    result = run(runtime, snapshot(OTHER_APPS_LIGHT_MIB), manager, TaskType.HEAVY)

    assert runtime.chats == []
    assert result.resource_blocked is True
    assert "could not confirm how much video memory" in result.error
    assert runtime.stopped == []


def test_malformed_values_read_as_unknown_never_as_a_smaller_number() -> None:
    sizes = parse_installed_listing(UNTRUSTWORTHY["one-good-one-bad-record"])

    # A model with any malformed record is unknown, not its smaller value.
    assert sizes == {}
    assert parse_installed_listing({"models": [listed(HEAVY.name, True)]}) == {}
    assert parse_installed_listing("not json") is None


# ------------------------------------------------------------- recovery


def test_admission_recovers_only_when_valid_evidence_and_fit_return() -> None:
    clock = Clock()
    store = Store()
    store.fail = True
    runtime = LocalRuntime()
    manager = a_manager(runtime, store, clock)
    light = snapshot(OTHER_APPS_LIGHT_MIB)

    for task in (TaskType.FAST, TaskType.HEAVY):
        assert run(runtime, light, manager, task).resource_blocked is True
    assert runtime.chats == []

    # The store answers again, with valid sizes.
    store.fail = False
    store.document = a_healthy_store().document
    clock.now += 6.0

    # Heavy needs 13,155 + 512 MiB by its installed size: with other apps
    # at 5.8 GB it still does not fit, so it still waits.
    busy = snapshot(OTHER_APPS_AT_START_MIB)
    assert run(runtime, busy, manager, TaskType.HEAVY).resource_blocked is True
    assert run(runtime, busy, manager, TaskType.FAST).success is True

    # With other apps at 2.0 GB it fits, and only then is it loaded.
    assert run(runtime, light, manager, TaskType.HEAVY).success is True
    assert runtime.chats == [FAST.name, HEAVY.name]


def test_a_failed_refresh_keeps_the_known_bound_and_does_not_hammer_the_store() -> None:
    clock = Clock()
    store = a_healthy_store()
    runtime = LocalRuntime()
    manager = a_manager(runtime, store, clock)

    needed, evidence = manager.footprint_mb(HEAVY)
    assert (needed, evidence) == (13_155, "installed")

    # The listing expires and the store stops answering.
    clock.now += 31.0
    store.fail = True
    reads_before = store.reads

    for _ in range(5):
        assert manager.footprint_mb(HEAVY) == (13_155, "installed")

    # One failed read, then a pause before the next attempt.
    assert store.reads == reads_before + 1


def test_a_valid_observation_is_used_while_the_store_is_unavailable() -> None:
    store = Store()
    store.fail = True
    runtime = LocalRuntime(
        running=[resident(HEAVY.name, HEAVY_RESIDENT_BYTES, HEAVY.context_tokens)]
    )
    manager = a_manager(runtime, store)
    manager.is_model_resident(HEAVY.name)  # the daemon reports its size
    runtime.running = []  # since unloaded

    assert manager.footprint_mb(HEAVY) == (12_150, "observed")

    # 12,150 + 512 MiB does not fit beside 5.8 GB of other apps...
    assert run(
        runtime, snapshot(OTHER_APPS_AT_START_MIB), manager, TaskType.HEAVY
    ).resource_blocked is True
    # ...and does beside 2.8 GB.
    assert run(
        runtime, snapshot(OTHER_APPS_AT_END_MIB), manager, TaskType.HEAVY
    ).success is True
    assert runtime.chats == [HEAVY.name]


@pytest.mark.parametrize(
    "size",
    ["12.7e9", "-12740009655", " 12740009655", "", "nan", True],
)
def test_a_malformed_observation_is_not_learned(size) -> None:
    store = Store()
    store.fail = True
    runtime = LocalRuntime(
        running=[
            BrainRuntimeModelStatus(
                name=HEAVY.name,
                size=size,  # type: ignore[arg-type]
                context=HEAVY.context_tokens,
            )
        ]
    )
    manager = a_manager(runtime, store)
    manager.is_model_resident(HEAVY.name)

    assert manager.footprint_mb(HEAVY) == (None, "unavailable")


def test_an_observation_at_a_different_context_is_not_trusted() -> None:
    store = Store()
    store.fail = True
    runtime = LocalRuntime(running=[resident(HEAVY.name, 2_000_000_000, 2048)])
    manager = a_manager(runtime, store)
    manager.is_model_resident(HEAVY.name)

    assert manager.footprint_mb(HEAVY) == (None, "unavailable")


def test_evidence_never_lowers_the_configured_estimate() -> None:
    runtime = LocalRuntime(
        running=[resident(FAST.name, FAST_RESIDENT_BYTES, FAST.context_tokens)]
    )
    manager = a_manager(runtime, a_healthy_store())
    manager.is_model_resident(FAST.name)

    # Fast was observed at 3,277 MiB, below its 3.6 GB estimate.
    assert manager.footprint_mb(FAST) == (3_687, "observed")


# ------------------------------------------------------ what stays usable


def test_an_already_resident_brain_is_reused_even_when_capacity_is_unknown() -> None:
    store = Store()
    store.fail = True
    runtime = LocalRuntime(
        running=[resident(HEAVY.name, HEAVY_RESIDENT_BYTES, HEAVY.context_tokens)]
    )
    manager = a_manager(runtime, store)

    result = run(
        runtime,
        snapshot(OTHER_APPS_AT_END_MIB + 12_149),
        manager,
        TaskType.HEAVY,
    )

    # No new load: reuse needs no capacity evidence.
    assert result.success is True
    assert runtime.chats == [HEAVY.name]


def test_a_machine_without_gpu_telemetry_still_runs_with_capacity_unknown() -> None:
    store = Store()
    store.fail = True
    runtime = LocalRuntime()
    manager = a_manager(runtime, store)

    result = run(runtime, snapshot(0, gpu=False), manager, TaskType.FAST)

    assert result.success is True
    assert runtime.chats == [FAST.name]


def test_a_brain_that_could_never_fit_this_card_keeps_working_with_offload() -> None:
    # 8 GB card: Heavy's real size exceeds the whole card, so waiting would
    # never help. Admission keeps the configured partial-offload behaviour
    # rather than making Heavy unusable on this hardware. This path does not
    # isolate other applications' VRAM; the model runtime decides placement.
    runtime = LocalRuntime()
    manager = a_manager(runtime, a_healthy_store())
    small = snapshot(600, total_mib=8_192)

    selection = manager.select_model(
        task_class=TaskClass.HEAVY,
        system=small.system,
        gpu=small.gpu,
    )

    assert selection.decision.value == "allow"
    assert selection.capacity_evidence == "installed-exceeds-card"
    assert run(runtime, small, manager, TaskType.HEAVY).success is True


def test_a_manager_built_without_a_store_reader_keeps_the_estimate() -> None:
    # Explicit injected or legacy backends (no local store): the old
    # estimate-only check, labelled as such. Production never builds this:
    # see test_the_production_orchestrator_reads_the_store_locally.
    runtime = Runtime()
    manager = a_manager(runtime, None)
    reading = snapshot(OTHER_APPS_AT_START_MIB)

    selection = manager.select_model(
        task_class=TaskClass.HEAVY,
        system=reading.system,
        gpu=reading.gpu,
    )

    assert selection.capacity_evidence == "configured"
    assert run(runtime, reading, manager, TaskType.FAST).success is True


# -------------------------------------------------------- production wiring


def test_the_production_orchestrator_reads_the_store_locally(monkeypatch) -> None:
    from core import local_ipc

    requests = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return a_healthy_store().document

    def get(url, **kwargs):
        requests.append((url, kwargs.get("timeout")))
        return Response()

    monkeypatch.setattr(local_ipc.requests_client, "get", get)
    runtime = LocalRuntime()

    orchestrator = Orchestrator(runtime=runtime)  # type: ignore[arg-type]

    assert orchestrator.model_manager.footprint_mb(HEAVY) == (13_155, "installed")
    assert requests == [("http://127.0.0.1:11434/api/tags", 2.0)]


def test_a_production_runtime_with_an_unusable_address_defers_new_loads(
    monkeypatch,
) -> None:
    from core import local_ipc

    def get(url, **kwargs):
        raise AssertionError("no read may be attempted for a non-local address")

    monkeypatch.setattr(local_ipc.requests_client, "get", get)
    runtime = LocalRuntime()
    runtime.base_url = "http://example.com:11434"  # type: ignore[misc]

    orchestrator = Orchestrator(runtime=runtime)  # type: ignore[arg-type]

    assert orchestrator.model_manager.footprint_mb(HEAVY) == (None, "unavailable")


def test_a_backend_without_a_store_gets_no_reader() -> None:
    orchestrator = Orchestrator(runtime=Runtime())  # type: ignore[arg-type]

    assert orchestrator.model_manager.footprint_mb(HEAVY) == (6_554, "configured")
