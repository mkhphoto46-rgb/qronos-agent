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

No stage in this package needs the GPU. Later model stages (speech
recognition, Vision captions, embeddings) need a cross-process lease to the
backend governor first; until that contract exists they are pending.
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
class StageNeeds:
    stage: str
    gpu: bool = False
    disk_bytes: int = 0


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
        if needs.gpu:
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
            # A model stage must declare its model and VRAM to the governor's
            # model slot logic; that contract is not defined yet.
            raise ResourceRefused("gpu_lease_contract_pending", needs.stage)
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
