from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.activity_guard import (
    ActivityMode,
    ResourcePressure,
)
from core.brain_runtime import BrainRuntime
from core.model_registry import (
    ModelProfile,
    get_model,
)
from core.ollama_controller import OllamaController
from core.resource_guard import (
    GpuStatus,
    SystemStatus,
)
from core.resource_policy import (
    ResourceDecision,
    evaluate_resources,
)






def _gpu_for_capacity_admission(
    gpu: GpuStatus | None,
) -> GpuStatus | None:
    """
    Return GPU telemetry suitable for model capacity admission.

    Global GPU utilization is a contention signal, not a capacity signal.
    On Windows/NVIDIA it cannot reliably identify whether the load belongs
    to the user, Qronos, desktop composition, TTS, STT or another process.

    External/user contention is already represented separately by
    ResourcePressure and ActivityMode.

    Capacity admission must therefore continue enforcing:
    - VRAM occupancy
    - GPU temperature
    - CPU usage
    - RAM usage

    while raw global GPU utilization must not independently refuse a model.
    """
    if gpu is None:
        return None

    return GpuStatus(
        name=gpu.name,
        temperature_c=gpu.temperature_c,
        gpu_utilization_percent=None,
        vram_used_mb=gpu.vram_used_mb,
        vram_total_mb=gpu.vram_total_mb,
    )


class TaskClass(Enum):
    FAST = "fast"
    HEAVY = "heavy"


@dataclass(frozen=True)
class ModelSelection:
    """
    Which model to run, and whether the machine has room to run it.

    There is deliberately no "keep this one loaded" field. Nothing Qronos
    loads stays loaded: every model is unloaded as soon as it has answered.
    See :class:`ModelManager` for the measurements behind that.
    """

    model: ModelProfile
    decision: ResourceDecision


class ModelManager:
    """
    Choose models while respecting activity and resource pressure.

    Models are on demand, without exception. The Fast Brain used to be kept
    warm for ten minutes after answering, on the reasoning that loading is
    what makes a voice assistant feel slow. Measured on the development card,
    that trade does not pay for itself:

        Fast Brain, 8,192-token context, measured 2026-08-28
            resident                3,442 MiB, permanently
            saved per turn          about 1.7 s
            whole turn, loaded      2.5 s
            whole turn, from cold   4.2 s

    Three and a half gigabytes held continuously, so that an occasional turn
    is under two seconds quicker, is not a good use of somebody else's
    graphics card. It is also the difference between Qronos owning a fifth of
    a 16 GB card at rest and owning none of it.

    The saving is smaller than it looks, too, because the operating system
    caches the weights file in RAM. The 1.7 s above is a copy from memory
    onto the card. Only the first load after a restart reads the disk, and no
    keep-alive policy helps with that one.
    """

    def __init__(
        self,
        runtime: BrainRuntime | None = None,
    ) -> None:
        self.runtime = (
            runtime
            if runtime is not None
            else OllamaController()
        )

        # Temporary compatibility alias.
        # Remove after the MVP migration away from Ollama-specific naming.
        self.ollama = self.runtime

    def select_model(
        self,
        task_class: TaskClass,
        system: SystemStatus,
        gpu: GpuStatus | None,
        activity_mode: ActivityMode = ActivityMode.NORMAL,
        resource_pressure: ResourcePressure = ResourcePressure.NORMAL,
    ) -> ModelSelection:
        model = get_model(
            task_class.value
        )

        capacity_gpu = _gpu_for_capacity_admission(
            gpu
        )

        base_decision = evaluate_resources(
            system=system,
            gpu=capacity_gpu,
        )

        decision = base_decision

        activity_blocked = self._activity_blocks(
            task_class=task_class,
            activity_mode=activity_mode,
        )

        if activity_blocked:
            decision = ResourceDecision.BLOCK

        # Never load either model while the user's system is already under
        # measured pressure. Qronos waits for safe headroom instead.
        pressure_blocked = (
            resource_pressure
            is not ResourcePressure.NORMAL
        )

        if pressure_blocked:
            decision = ResourceDecision.BLOCK


        return ModelSelection(
            model=model,
            decision=decision,
        )

    def can_start(
        self,
        task_class: TaskClass,
        system: SystemStatus,
        gpu: GpuStatus | None,
        activity_mode: ActivityMode = ActivityMode.NORMAL,
        resource_pressure: ResourcePressure = ResourcePressure.NORMAL,
    ) -> bool:
        selection = self.select_model(
            task_class=task_class,
            system=system,
            gpu=gpu,
            activity_mode=activity_mode,
            resource_pressure=resource_pressure,
        )

        return (
            selection.decision
            is ResourceDecision.ALLOW
        )

    def unload_all(self) -> None:
        self.runtime.unload_all()

    @staticmethod
    def _activity_blocks(
        task_class: TaskClass,
        activity_mode: ActivityMode,
    ) -> bool:
        if task_class is not TaskClass.HEAVY:
            return False

        return activity_mode in {
            ActivityMode.GAMING_ASSIST,
            ActivityMode.GAMING_PERFORMANCE,
            ActivityMode.CREATOR_ASSIST,
            ActivityMode.CREATOR_PERFORMANCE,
        }


if __name__ == "__main__":
    print("Qronos Model Manager: ready")