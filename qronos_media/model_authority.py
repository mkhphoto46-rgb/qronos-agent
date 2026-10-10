"""The runtime-issued authority for model-bearing media stages.

Qronos has one resource authority in the bridge process: the queue's
:class:`core.resource_governor.ResourceGovernor` plus the RAM/VRAM
:class:`core.resource_ownership.ResourceLedger`. A model stage (speech
recognition) runs only under a :class:`HostedModelAuthority` that the product
bridge issues from exactly those two objects (:func:`issue_hosted_authority`).
It is an object reference held by the in-process media host. Nothing a caller
can send (a DTO, a Boolean, a CLI flag, a database row, a requested model name
or memory size) creates or widens it: a stage names one of the declared
:data:`qronos_media.resources.DECLARED_MODELS` objects and gets that model's
declared budget.

Ownership is counted twice and freed once, on proof:

* the ledger records the declared budget (QRONOS, BACKGROUND), so the
  runtime's pressure reading does not mistake media's own model for outside
  load and the assistant cannot double-load beside it;
* the governor holds the HEAVY slot as a *managed* reservation, which its
  lease clock never expires.

Both stay counted until every child the lease started has provably exited.
:class:`ModelLease` is the spawn observer of :func:`qronos_media.tools.run_bounded`:
a spawn is counted *before* the process exists (``begin_spawn``), the owned
process and job handles are attached right after creation, and after teardown
the lease itself asks those handles: the process handle reports exit and the
job holds no process. No caller-supplied "exited" flag, acknowledgement or
expiry is ever proof. A release during a spawn handoff, or with a child whose
exit is not proven, frees nothing: the slot stays busy ("termination_unproven"),
every further model reserve is refused, and :meth:`HostedModelAuthority.reconcile`
retries the teardown and the proof. A lease that is released cannot start
another child.

Every media stage under this authority also yields: before and during work it
refuses while the assistant's own foreground turn (voice, text, screen) is
active, while Qronos is paused, after Stop/shutdown/revocation (``preempt``),
in gaming or creator modes, and under critical pressure.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Any, Callable

from qronos_media.resources import (
    DEFAULT_DISK_RESERVE_BYTES, _YIELD_MODES, ActivityReader, GovernorAuthority, Lease, ModelSpec, ResourceRefused,
    StageNeeds, _common_checks, guard_activity, is_declared,
)

_ISSUED = object()  # only issue_hosted_authority holds this
#: How long a release waits for a torn-down child before calling its exit unproven.
TEARDOWN_WAIT_SECONDS = 2.0
MAX_EVENTS = 256


@dataclass
class ChildRecord:
    """The exact identity of one child a lease started, and the handles that can prove its exit."""

    pid: int
    creation_time: int
    child: Any            # subprocess.Popen (the owned process handle)
    containment: Any      # qronos_media.containment.Containment (the owned job handle) or None
    attached_at: float
    ended_at: float | None = None
    proven: bool = False

    def describe(self) -> dict[str, object]:
        return {"pid": self.pid, "creationTime": self.creation_time, "proven": self.proven,
                "jobContained": self.containment is not None}


class ModelLease:
    """A granted model slot and the children started under it (a run_bounded SpawnObserver)."""

    def __init__(self, authority: "HostedModelAuthority", task_id: str, spec: ModelSpec, granted_at: float,
                 ledger_id: str) -> None:
        self.task_id = task_id
        self.spec = spec
        self.granted_at = granted_at
        self.expires_at = None
        self.ledger_id = ledger_id
        self._authority = authority
        self._lock = threading.Lock()
        self._pending = 0
        self._live: list[ChildRecord] = []
        self.children: list[ChildRecord] = []
        self.closed = False

    # ---- SpawnObserver (called by run_bounded)

    def begin_spawn(self) -> None:
        reason = self._authority.yield_reason()
        with self._lock:
            if self.closed:
                raise ResourceRefused("lease_released", self.task_id)
            if reason is not None:
                raise ResourceRefused(reason, self.task_id)
            self._pending += 1
        self._authority._event("spawn_begin", self.task_id)

    def spawn_failed(self) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)
        self._authority._event("spawn_failed", self.task_id)
        self._authority._after_child(self)

    def attach(self, child: Any, containment: Any) -> None:
        creation = containment.creation_time(child) if containment is not None else 0
        record = ChildRecord(child.pid, creation, child, containment, time.monotonic())
        with self._lock:
            self._pending = max(0, self._pending - 1)
            self._live.append(record)
            self.children.append(record)
            released_meanwhile = self.closed
        self._authority._event("attach", self.task_id, pid=child.pid, creationTime=creation)
        if released_meanwhile:
            # Released during the spawn handoff: this child must not run on; it is
            # torn down now and still counted until its exit is proven.
            _teardown(record)

    def finish(self, child: Any, containment: Any) -> bool:
        with self._lock:
            record = next((r for r in self._live if r.child is child), None)
            if record is None:
                # Already proven by the authority (a concurrent release tore it down and asked the
                # handles); a child this lease never attached is never reported as freed.
                known = next((r for r in self.children if r.child is child), None)
                return bool(known is not None and known.proven)
            proven = _proven(record)
            if proven:
                record.proven, record.ended_at = True, time.monotonic()
                self._live.remove(record)
        self._authority._event("child_end", self.task_id, pid=record.pid, proven=proven)
        self._authority._after_child(self)
        return proven

    # ---- state

    def close(self) -> None:
        with self._lock:
            self.closed = True

    def outstanding(self, *, teardown: bool) -> bool:
        """True while a spawn is in its handoff or any started child's exit is not proven."""
        with self._lock:
            pending = self._pending
            live = list(self._live)
        if teardown:
            for record in live:
                _teardown(record)
        still: list[ChildRecord] = []
        for record in live:
            if _proven(record):
                record.proven, record.ended_at = True, time.monotonic()
                self._authority._event("child_end", self.task_id, pid=record.pid, proven=True, by="authority")
                if record.containment is not None:
                    record.containment.close()  # proven: the job may close now
                for stream in (getattr(record.child, "stdout", None), getattr(record.child, "stderr", None)):
                    try:
                        if stream is not None:
                            stream.close()  # nothing can hold them any more
                    except OSError:
                        pass
            else:
                still.append(record)
        with self._lock:
            self._live = [r for r in self._live if r in still or r not in live]
            return self._pending > 0 or pending > 0 or bool(self._live)


def _proven(record: ChildRecord) -> bool:
    """From the owned handles only: the process exited and (when contained) the whole job is empty."""
    if record.containment is None:
        return record.child.poll() is not None
    return bool(record.containment.exit_proven(record.child))


def _teardown(record: ChildRecord) -> None:
    """Terminate the whole job and the process, then wait (bounded) until the handles say they are gone."""
    if record.containment is not None:
        try:
            record.containment.terminate()
        except OSError:
            pass
    try:
        if record.child.poll() is None:
            record.child.kill()
        record.child.wait(timeout=TEARDOWN_WAIT_SECONDS)
    except Exception:  # noqa: BLE001 - an unkillable child is reported as unproven, never as freed
        pass
    deadline = time.monotonic() + TEARDOWN_WAIT_SECONDS
    while not _proven(record) and time.monotonic() < deadline:
        time.sleep(0.02)  # job accounting can trail TerminateJobObject briefly


class HostedModelAuthority:
    """CPU/disk stages through the bridge governor (LIGHT) plus declared model stages (HEAVY, managed)."""

    def __init__(
        self,
        token: object,
        *,
        governor: Any,
        ledger: Any,
        disk_root: Path,
        foreground_busy: Callable[[], bool],
        paused: Callable[[], bool],
        activity: ActivityReader | None = None,
        reserve_bytes: int = DEFAULT_DISK_RESERVE_BYTES,
    ) -> None:
        if token is not _ISSUED:
            raise TypeError("a HostedModelAuthority is issued by the Qronos runtime only")
        from core.resource_governor import Weight
        from core.resource_ownership import ResourceBudget, ResourceOwner, WorkloadPriority

        self._weight = Weight.HEAVY
        self._budget, self._owner, self._priority = ResourceBudget, ResourceOwner.QRONOS, WorkloadPriority.BACKGROUND
        self.governor = governor
        self.ledger = ledger
        self.disk_root = disk_root
        self.activity = activity or guard_activity()
        self.reserve_bytes = reserve_bytes
        self._foreground_busy = foreground_busy
        self._paused = paused
        self._light = GovernorAuthority(governor, disk_root, activity=self.activity, reserve_bytes=reserve_bytes)
        self._lock = threading.RLock()
        self._slot: ModelLease | None = None
        self._preempted: str | None = None
        self._closed = False
        self.events: deque[dict[str, object]] = deque(maxlen=MAX_EVENTS)

    # ---- yielding

    def preempt(self, reason: str) -> None:
        """Stop/Pause/shutdown/revocation: running work stops at its next check; nothing new starts."""
        self._preempted = str(reason or "preempted")
        self._event("preempt", self._preempted)

    def clear_preemption(self) -> None:
        if not self._closed:
            self._preempted = None

    def close(self) -> None:
        self._closed = True
        self.preempt("closed")
        self.reconcile()

    def yield_reason(self) -> str | None:
        if self._closed:
            return "closed"
        if self._preempted is not None:
            return self._preempted
        try:
            if self._foreground_busy():
                return "foreground"
            if self._paused():
                return "paused"
        except Exception:  # noqa: BLE001 - an unreadable runtime state is not permission to run
            return "runtime_state_unavailable"
        try:
            mode, _ = self.activity()
        except Exception:  # noqa: BLE001
            return "activity_unavailable"
        if mode in _YIELD_MODES:
            return "activity_mode"
        return None

    # ---- MediaResourceAuthority

    def reserve(self, task_id: str, needs: StageNeeds) -> Lease | ModelLease:
        reason = self.yield_reason()
        if reason is not None:
            raise ResourceRefused(reason, needs.stage)
        if not needs.model_bearing:
            return self._light.reserve(task_id, needs)
        if needs.gpu or not is_declared(needs.model):
            raise ResourceRefused("undeclared_model", needs.stage)
        spec = needs.model
        with self._lock:
            if not self.reconcile():
                busy = self._slot
                raise ResourceRefused("termination_unproven" if busy is not None and busy.closed else "model_busy",
                                      busy.task_id if busy is not None else "")
            mode, pressure = _common_checks(needs, self.activity, self.disk_root, self.reserve_bytes)
            from core.resource_ownership import DuplicateWorkloadReservation

            try:
                owned = self.ledger.reserve(owner=self._owner, workload=f"media-model:{spec.name}",
                                            priority=self._priority,
                                            budget=self._budget(vram_mb=spec.vram_mb, ram_mb=spec.ram_mb))
            except DuplicateWorkloadReservation:
                raise ResourceRefused("model_double_load", spec.name) from None
            from core.resource_governor import Weight

            weight = Weight.LIGHT if spec.weight == "light" else Weight.HEAVY
            try:
                grant = self.governor.request(task_id, weight, activity_mode=mode, resource_pressure=pressure,
                                              managed=True)
            except BaseException:
                self.ledger.release(owned.reservation_id)
                raise
            if not grant.granted or grant.reservation is None:
                self.ledger.release(owned.reservation_id)
                raise ResourceRefused(grant.refusal.value if grant.refusal else "refused", grant.detail)
            lease = ModelLease(self, task_id, spec, grant.reservation.granted_at, owned.reservation_id)
            self._slot = lease
        self._event("reserve", task_id, model=spec.name, vramMb=spec.vram_mb, ledger=owned.reservation_id)
        return lease

    def renew(self, lease: Lease | ModelLease) -> Lease | ModelLease:
        reason = self.yield_reason()
        if reason is not None:
            raise ResourceRefused(reason, lease.task_id)
        if not isinstance(lease, ModelLease):
            return self._light.renew(lease)
        with self._lock:
            if lease is not self._slot or lease.closed or self.governor.renew(lease.task_id) is None:
                raise ResourceRefused("lease_lost", lease.task_id)
        return lease

    def release(self, lease: Lease | ModelLease) -> None:
        if not isinstance(lease, ModelLease):
            self._light.release(lease)
            return
        with self._lock:
            lease.close()
            self._event("release_requested", lease.task_id)
            self._try_free(lease, teardown=True)

    # ---- proof and freeing

    def reconcile(self) -> bool:
        """Free a released slot whose children are now proven gone. True when no model slot is held."""
        with self._lock:
            slot = self._slot
            if slot is None:
                return True
            if not slot.closed:
                return False
            return self._try_free(slot, teardown=True)

    def _after_child(self, lease: ModelLease) -> None:
        if lease.closed:
            with self._lock:
                if self._slot is lease:
                    self._try_free(lease, teardown=False)

    def _try_free(self, lease: ModelLease, *, teardown: bool) -> bool:
        if lease.outstanding(teardown=teardown):
            self._event("termination_unproven", lease.task_id,
                        children=[c.describe() for c in lease.children if not c.proven])
            return False
        if self._slot is lease:
            self.ledger.release(lease.ledger_id)
            self.governor.release(lease.task_id)
            self._slot = None
            self._event("released", lease.task_id, ledger=lease.ledger_id)
        return True

    def status(self) -> dict[str, object]:
        with self._lock:
            slot = self._slot
            return {
                "modelSlot": None if slot is None else {
                    "task": slot.task_id, "model": slot.spec.name, "released": slot.closed,
                    "state": "termination_unproven" if slot.closed else "held",
                    "children": [c.describe() for c in slot.children],
                },
                "yieldReason": self.yield_reason(),
            }

    def _event(self, kind: str, task: str, **detail: object) -> None:
        self.events.append({"event": kind, "task": task, "at": time.monotonic(), **detail})


def issue_hosted_authority(**bindings: Any) -> HostedModelAuthority:
    """The runtime's own issue path (core/runtime_bridge.py). Bindings are runtime objects, never request data."""
    return HostedModelAuthority(_ISSUED, **bindings)


def is_issued(authority: object) -> bool:
    return type(authority) is HostedModelAuthority and getattr(authority, "_light", None) is not None
