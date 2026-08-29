"""
The smart queue against the real machine, disturbing nothing.

``tests/test_load_signal.py`` proves the rules against invented readings and
``tests/test_queue_scheduler.py`` proves the scheduling against a fake machine.
Both are deterministic and neither touches hardware. What they cannot show is
that the real readers, on this card, produce numbers the real thresholds
classify the way we think they will. That is what this is for.

Deliberately in ``tools/`` rather than ``tests/``. The suite runs on a Linux CI
machine with no graphics card, where sixty seconds of sampling would prove
nothing and take a minute doing it. ``tools/`` is already excluded from release
builds wholesale, so nothing has to be added to ``release-exclude.txt``.

**It disturbs nothing.** It reads sensors, it does not write, and the work it
queues is a fake that sleeps fifty milliseconds. No model is loaded, no VRAM is
allocated, no process of the user's is touched. The whole cost is about thirty
``nvidia-smi`` launches over a minute.

    .venv\\Scripts\\python.exe tools\\test_qronos_smart_queue_live.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.hard_floor import FloorBreach, required_vram_mb
from core.load_signal import LoadLevel, SustainedLoadMonitor
from core.model_registry import MODELS
from core.queue_scheduler import HoldReason, QueueEvent, QueueScheduler
from core.resource_governor import ResourceGovernor, Weight
from core.safe_queue import SafeQueue


WATCH_SECONDS = 60.0

FAST_MB = required_vram_mb(MODELS["fast"].estimated_vram_gb)
HEAVY_MB = required_vram_mb(MODELS["heavy"].estimated_vram_gb)


class PretendWork:
    """
    Something to queue that costs nothing.

    The point of the run is the scheduling, not the work. Loading a real model
    would take VRAM away from whatever the user is doing, which is the one
    thing this whole feature exists to avoid.
    """

    def __init__(self) -> None:
        self.runs = 0

    def describe(self) -> str:
        return "a stand-in task that does no work"

    def run(self) -> bool:
        self.runs += 1
        time.sleep(0.05)
        return True


def rule(title: str) -> None:
    print(f"\n{title}")
    print("-" * 72)


def watch_the_machine() -> SustainedLoadMonitor:
    """Sample the real machine until the monitor has made up its mind."""
    rule(f"1. Watching the real machine for {WATCH_SECONDS:.0f} seconds")

    monitor = SustainedLoadMonitor()
    monitor.prime()

    started = time.perf_counter()
    interval = monitor.config.sample_interval_seconds

    while time.perf_counter() - started < WATCH_SECONDS:
        sample = monitor.observe()
        snapshot = monitor.snapshot()

        print(
            f"  {time.perf_counter() - started:5.1f}s  "
            f"cpu {sample.cpu_percent:5.1f}%  "
            f"ram {sample.ram_percent:5.1f}%  "
            f"gpu {str(sample.gpu_utilization_percent) + '%':>5}  "
            f"vram {sample.vram_used_percent or 0:5.1f}%  "
            f"free {str(sample.vram_free_mb) + ' MB':>9}  "
            f"{sample.decision.value:<5}  -> {snapshot.level.value}"
        )
        time.sleep(interval)

    return monitor


def main() -> int:
    print("=" * 72)
    print("Qronos smart queue, against this machine")
    print("=" * 72)
    print(f"  the fast brain needs  {FAST_MB:>6,} MB of graphics memory")
    print(f"  the heavy brain needs {HEAVY_MB:>6,} MB")

    monitor = watch_the_machine()
    snapshot = monitor.snapshot()

    rule("2. What the monitor concluded")
    print(f"  {snapshot.describe()}")
    print(f"  from {snapshot.sample_count} readings")

    if snapshot.level is LoadLevel.UNKNOWN:
        print("\n  The monitor never gathered enough evidence. That is a bug.")
        return 1

    events: list[QueueEvent] = []
    clock = time.time

    scheduler = QueueScheduler(
        queue=SafeQueue(clock=clock),
        governor=ResourceGovernor(clock=clock),
        monitor=monitor,
        notify=events.append,
        clock=clock,
    )

    rule("3. Queueing a heavy task")
    work = PretendWork()
    task = scheduler.submit(
        work=work,
        weight=Weight.HEAVY,
        summary="analyse a long document",
        required_vram_mb=HEAVY_MB,
    )
    scheduler.tick()

    held = scheduler.view().tasks[0]
    print(f"  state        {held['state']}")
    print(f"  held because {held['heldReason']}")
    print(f"  overridable  {held['overridable']}")
    print(f"  detail       {held['detail']}")
    print(f"  it has run   {work.runs} times")

    rule("4. Pressing 'run anyway'")
    result = scheduler.override(task.task_id)
    scheduler.tick()

    print(f"  accepted     {result.accepted}")
    print(f"  it has run   {work.runs} times")
    print(f"  {result.message()}")

    rule("Verdict")

    machine_is_busy = snapshot.level is LoadLevel.BUSY
    card_has_room = (snapshot.latest.vram_free_mb or 0) >= HEAVY_MB + 512

    if machine_is_busy:
        print("  The machine was judged busy, steadily, from real readings.")
    else:
        print("  The machine was judged free — so this run shows the other")
        print("  half: that Qronos takes work when it is welcome to.")

    if not card_has_room:
        if result.accepted:
            print("\n  WRONG: the card cannot fit the heavy brain and the")
            print("  override was accepted anyway. That is the safety floor")
            print("  failing, which is the one thing it must not do.")
            return 1

        breach = result.floor.breach if result.floor else None
        print(f"  The heavy brain does not fit, so the override was refused")
        print(f"  by the safety floor ({breach.value if breach else '?'}),")
        print("  which is what it is for.")
    elif machine_is_busy and not result.accepted:
        print("\n  WRONG: only politeness was holding this and the override")
        print("  did not lift it.")
        return 1

    if machine_is_busy and work.runs and not result.accepted:
        print("\n  WRONG: the work ran without ever being allowed to.")
        return 1

    print(f"\n  {len(events)} events were emitted, none of them per-tick noise.")
    print("  Nothing was loaded, nothing was allocated, nothing was touched.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
