"""
Shared Qronos resource ownership and reservation ledger.

The Resource Governor must never confuse resource usage created by an admitted
Qronos workload with new external/user pressure.

This module is intentionally policy-light. It records ownership and reservations.
The policy engine decides whether work may start, continue, pause, or be evicted.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable
from uuid import uuid4


class ResourceOwner(str, Enum):
    USER = "user"
    QRONOS = "qronos"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class WorkloadPriority(str, Enum):
    USER_FOREGROUND = "user_foreground"
    ACTIVE_QRONOS_REQUEST = "active_qronos_request"
    VOICE_OUTPUT = "voice_output"
    INTERACTIVE_WARM = "interactive_warm"
    BACKGROUND = "background"
    IDLE_RESIDENT = "idle_resident"


class WorkloadState(str, Enum):
    QUEUED = "queued"
    ADMITTED = "admitted"
    STARTING = "starting"
    RUNNING = "running"
    HOT_IDLE = "hot_idle"
    PAUSED = "paused"
    EVICTING = "evicting"
    RELEASED = "released"
    FAILED = "failed"


@dataclass(frozen=True)
class ResourceBudget:
    """
    Declared resource budget for one workload.

    Values are reservations, not claims about exact instantaneous usage.
    """

    vram_mb: int = 0
    ram_mb: int = 0

    def __post_init__(self) -> None:
        if self.vram_mb < 0:
            raise ValueError("vram_mb cannot be negative")

        if self.ram_mb < 0:
            raise ValueError("ram_mb cannot be negative")


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    owner: ResourceOwner
    workload: str
    priority: WorkloadPriority
    budget: ResourceBudget
    state: WorkloadState
    created_at: float
    updated_at: float


class ReservationNotFound(KeyError):
    pass


class DuplicateWorkloadReservation(RuntimeError):
    pass


class ResourceLedger:
    """
    Thread-safe in-memory ledger of Qronos resource reservations.

    The ledger does not measure the machine and does not kill workloads.
    It answers a simpler and critical question:

        "Which resource demand did Qronos itself intentionally admit?"

    That answer is later combined with telemetry to estimate external pressure.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reservations: dict[str, Reservation] = {}

    def reserve(
        self,
        *,
        owner: ResourceOwner,
        workload: str,
        priority: WorkloadPriority,
        budget: ResourceBudget,
        state: WorkloadState = WorkloadState.ADMITTED,
        allow_duplicate_workload: bool = False,
    ) -> Reservation:
        workload = workload.strip()

        if not workload:
            raise ValueError("workload cannot be empty")

        with self._lock:
            if not allow_duplicate_workload:
                for existing in self._reservations.values():
                    if (
                        existing.owner is owner
                        and existing.workload == workload
                        and existing.state
                        not in {
                            WorkloadState.RELEASED,
                            WorkloadState.FAILED,
                        }
                    ):
                        raise DuplicateWorkloadReservation(
                            f"{owner.value}:{workload} already has "
                            "an active reservation"
                        )

            now = time.monotonic()

            reservation = Reservation(
                reservation_id=uuid4().hex,
                owner=owner,
                workload=workload,
                priority=priority,
                budget=budget,
                state=state,
                created_at=now,
                updated_at=now,
            )

            self._reservations[
                reservation.reservation_id
            ] = reservation

            return reservation

    def update_state(
        self,
        reservation_id: str,
        state: WorkloadState,
    ) -> Reservation:
        with self._lock:
            current = self._get_required(
                reservation_id
            )

            updated = Reservation(
                reservation_id=current.reservation_id,
                owner=current.owner,
                workload=current.workload,
                priority=current.priority,
                budget=current.budget,
                state=state,
                created_at=current.created_at,
                updated_at=time.monotonic(),
            )

            self._reservations[
                reservation_id
            ] = updated

            return updated

    def release(
        self,
        reservation_id: str,
    ) -> Reservation:
        return self.update_state(
            reservation_id,
            WorkloadState.RELEASED,
        )

    def fail(
        self,
        reservation_id: str,
    ) -> Reservation:
        return self.update_state(
            reservation_id,
            WorkloadState.FAILED,
        )

    def remove(
        self,
        reservation_id: str,
    ) -> None:
        with self._lock:
            if reservation_id not in self._reservations:
                raise ReservationNotFound(
                    reservation_id
                )

            del self._reservations[
                reservation_id
            ]

    def get(
        self,
        reservation_id: str,
    ) -> Reservation | None:
        with self._lock:
            return self._reservations.get(
                reservation_id
            )

    def active(
        self,
        owner: ResourceOwner | None = None,
    ) -> tuple[Reservation, ...]:
        with self._lock:
            result = []

            for reservation in self._reservations.values():
                if reservation.state in {
                    WorkloadState.RELEASED,
                    WorkloadState.FAILED,
                }:
                    continue

                if (
                    owner is not None
                    and reservation.owner is not owner
                ):
                    continue

                result.append(reservation)

            return tuple(result)

    def all(
        self,
    ) -> tuple[Reservation, ...]:
        with self._lock:
            return tuple(
                self._reservations.values()
            )

    def reserved_vram_mb(
        self,
        owner: ResourceOwner | None = None,
    ) -> int:
        return sum(
            item.budget.vram_mb
            for item in self.active(owner)
        )

    def reserved_ram_mb(
        self,
        owner: ResourceOwner | None = None,
    ) -> int:
        return sum(
            item.budget.ram_mb
            for item in self.active(owner)
        )

    def reservations_for_workload(
        self,
        workload: str,
    ) -> tuple[Reservation, ...]:
        workload = workload.strip()

        with self._lock:
            return tuple(
                item
                for item in self._reservations.values()
                if item.workload == workload
            )

    def purge_finished(self) -> int:
        """
        Remove RELEASED and FAILED records.

        Returns the number removed.
        """
        with self._lock:
            removable = [
                reservation_id
                for reservation_id, reservation
                in self._reservations.items()
                if reservation.state
                in {
                    WorkloadState.RELEASED,
                    WorkloadState.FAILED,
                }
            ]

            for reservation_id in removable:
                del self._reservations[
                    reservation_id
                ]

            return len(removable)

    def _get_required(
        self,
        reservation_id: str,
    ) -> Reservation:
        reservation = self._reservations.get(
            reservation_id
        )

        if reservation is None:
            raise ReservationNotFound(
                reservation_id
            )

        return reservation


GLOBAL_RESOURCE_LEDGER = ResourceLedger()
