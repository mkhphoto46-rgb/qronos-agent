"""Who may let a media stage run (handoff s34), without a second governor.

Qronos has one resource authority, :class:`core.resource_governor.ResourceGovernor`,
living in the backend process. A media process that built its own would
reserve against nothing the assistant knows about. So media stages ask a
:class:`MediaResourceAuthority`, and there are exactly two real ones:

* :class:`GovernorAuthority` wraps the backend's governor when the media
  service is hosted in that process (the integration hook Root leases).
* :class:`StandaloneCpuAuthority` is for the command-line tool. It grants CPU
  and disk stages only and refuses anything that would need the GPU or a
  model, because it cannot see the other runtime's video memory.

Both yield to gaming and creator activity: indexing a film is hours of disk
and decode, which is not "light" from the user's point of view whatever the
instantaneous readings say. Both keep a disk free-space reserve.

A stage that runs a model (speech recognition today; embeddings and Vision
captions later) is *model-bearing*, whatever device it uses: a CPU whisper
run still takes gigabytes and competes with the assistant's own voice turn.
Neither authority above grants one. Only the runtime-owned
:class:`qronos_media.model_authority.HostedModelAuthority`, which the product
bridge issues from its own governor and ledger, does. A model stage names one
of the declared :data:`DECLARED_MODELS` objects; a caller cannot ask for a
model or a memory size of its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import time
from typing import Callable, Protocol

from core.activity_guard import ActivityGuard, ActivityMode, ResourcePressure

DEFAULT_DISK_RESERVE_BYTES = 2 * 1024**3

_YIELD_MODES = frozenset({
    ActivityMode.GAMING_ASSIST,
    ActivityMode.GAMING_PERFORMANCE,
    ActivityMode.CREATOR_ASSIST,
    ActivityMode.CREATOR_PERFORMANCE,
})


@dataclass(frozen=True)
class ModelAsset:
    """A provisioned file, checked read-only by size and SHA-256 before any use (never executed here)."""

    parts: tuple[str, ...]          # below CONFIG.paths.runtime_assets
    size: int
    sha256: str


@dataclass(frozen=True, eq=False)
class ModelSpec:
    """One declared model. Compared by identity: only the objects in DECLARED_MODELS exist."""

    name: str
    vram_mb: int                    # declared conservative estimate, reserved in the ledger
    ram_mb: int
    executable: ModelAsset | None = None   # file-provisioned models (whisper.cpp)
    weights: ModelAsset | None = None
    weight: str = "heavy"           # governor weight: "heavy" (GPU-capable, one owner) or "light" (CPU)
    served_by: str = "file"         # "file" (a contained child) or "memory_sidecar" (the existing CPU sidecar)
    dimension: int = 0              # embedding models: the exact vector length


#: whisper.cpp large-v3-turbo q5_0 through whisper-cli, the same assets as Qronos
#: voice (core/whisper_cpp_runtime.py paths). The 2 GiB figure is a declared,
#: conservative profile, not a measured footprint.
WHISPER_LARGE_V3_TURBO_Q5_0 = ModelSpec(
    name="whisper.cpp/ggml-large-v3-turbo-q5_0",
    vram_mb=2048,
    ram_mb=2048,
    executable=ModelAsset(("whisper", "bin", "Release", "whisper-cli.exe"), 489_984,
                          "8df0133301fcab305a3ff56b3cda5915560679c760fb3182df920a9097116bcd"),
    weights=ModelAsset(("whisper", "models", "ggml-large-v3-turbo-q5_0.bin"), 574_041_195,
                       "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"),
)

#: bge-m3 (F16, 1024 dimensions) served by Qronos's existing CPU-only memory sidecar (core/memory_runtime.py,
#: 127.0.0.1:11435, num_gpu 0). Media never starts that server; it only uses the runtime's owned one. CPU work
#: still needs the shared admission: a LIGHT managed reservation plus a declared RAM budget in the ledger.
BGE_M3 = ModelSpec(
    name="ollama/bge-m3:latest F16",
    vram_mb=0,
    ram_mb=2048,
    weight="light",
    served_by="memory_sidecar",
    dimension=1024,
)

DECLARED_MODELS: tuple[ModelSpec, ...] = (WHISPER_LARGE_V3_TURBO_Q5_0, BGE_M3)


def is_declared(model: object) -> bool:
    return any(model is declared for declared in DECLARED_MODELS)


@dataclass(frozen=True)
class StageNeeds:
    stage: str
    gpu: bool = False
    disk_bytes: int = 0
    model: ModelSpec | None = None

    @property
    def model_bearing(self) -> bool:
        return self.model is not None or self.gpu


@dataclass(frozen=True)
class Lease:
    task_id: str
    granted_at: float
    expires_at: float | None


class ResourceRefused(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class MediaResourceAuthority(Protocol):
    def reserve(self, task_id: str, needs: StageNeeds) -> Lease: ...
    def renew(self, lease: Lease) -> Lease: ...
    def release(self, lease: Lease) -> None: ...


ActivityReader = Callable[[], tuple[ActivityMode, ResourcePressure]]


def guard_activity(guard: ActivityGuard | None = None) -> ActivityReader:
    detector = guard or ActivityGuard()

    def read() -> tuple[ActivityMode, ResourcePressure]:
        state = detector.detect()
        return state.mode, state.resource_pressure

    return read


def _common_checks(needs: StageNeeds, activity: ActivityReader, disk_root: Path, reserve_bytes: int) -> tuple[ActivityMode, ResourcePressure]:
    mode, pressure = activity()
    if mode in _YIELD_MODES:
        raise ResourceRefused("activity_mode", mode.value)
    if pressure is ResourcePressure.CRITICAL:
        raise ResourceRefused("resource_pressure", pressure.value)
    try:
        free = shutil.disk_usage(disk_root).free
    except OSError:
        raise ResourceRefused("disk_unreadable", str(disk_root)) from None
    if free - max(0, needs.disk_bytes) < reserve_bytes:
        raise ResourceRefused("disk_reserve", f"{free} bytes free")
    return mode, pressure


class StandaloneCpuAuthority:
    """CPU/disk-only grants for a media process that cannot see the GPU owner."""

    def __init__(
        self,
        disk_root: Path,
        *,
        activity: ActivityReader | None = None,
        reserve_bytes: int = DEFAULT_DISK_RESERVE_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.disk_root = disk_root
        self.activity = activity or guard_activity()
        self.reserve_bytes = reserve_bytes
        self.clock = clock

    def reserve(self, task_id: str, needs: StageNeeds) -> Lease:
        if needs.model_bearing:
            # CPU-only is not model-free: a model run needs the runtime's shared admission.
            raise ResourceRefused("requires_qronos_governor", needs.stage)
        _common_checks(needs, self.activity, self.disk_root, self.reserve_bytes)
        return Lease(task_id, self.clock(), None)

    def renew(self, lease: Lease) -> Lease:
        _common_checks(StageNeeds("renew"), self.activity, self.disk_root, self.reserve_bytes)
        return lease

    def release(self, lease: Lease) -> None:
        return None


class GovernorAuthority:
    """Reserve through the backend's single ResourceGovernor (in-process hosting)."""

    def __init__(
        self,
        governor,
        disk_root: Path,
        *,
        activity: ActivityReader | None = None,
        reserve_bytes: int = DEFAULT_DISK_RESERVE_BYTES,
    ) -> None:
        from core.resource_governor import Weight

        self._light = Weight.LIGHT
        self.governor = governor
        self.disk_root = disk_root
        self.activity = activity or guard_activity()
        self.reserve_bytes = reserve_bytes

    def reserve(self, task_id: str, needs: StageNeeds) -> Lease:
        if needs.gpu:
            # An undeclared GPU lease has no contract at all.
            raise ResourceRefused("gpu_lease_contract_pending", needs.stage)
        if needs.model_bearing:
            # Declared model stages go through the runtime-issued HostedModelAuthority only.
            raise ResourceRefused("requires_trusted_model_authority", needs.stage)
        mode, pressure = _common_checks(needs, self.activity, self.disk_root, self.reserve_bytes)
        grant = self.governor.request(task_id, self._light, activity_mode=mode, resource_pressure=pressure)
        if not grant.granted or grant.reservation is None:
            reason = grant.refusal.value if grant.refusal else "refused"
            raise ResourceRefused(reason, grant.detail)
        return Lease(task_id, grant.reservation.granted_at, grant.reservation.expires_at)

    def renew(self, lease: Lease) -> Lease:
        _common_checks(StageNeeds("renew"), self.activity, self.disk_root, self.reserve_bytes)
        renewed = self.governor.renew(lease.task_id)
        if renewed is None:
            raise ResourceRefused("lease_lost", lease.task_id)
        return Lease(lease.task_id, renewed.granted_at, renewed.expires_at)

    def release(self, lease: Lease) -> None:
        self.governor.release(lease.task_id)
