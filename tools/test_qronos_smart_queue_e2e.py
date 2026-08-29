"""
The whole queue, end to end, through the real bridge process.

``tools/test_qronos_smart_queue_live.py`` proves the sensor: that the real
readers on this card produce numbers the real thresholds call sustained-busy.
This proves everything downstream of it — the protocol, the process boundary,
the scheduler thread, the override, the refusal, the release and the shutdown —
by starting ``core.runtime_bridge`` as a child process with piped stdin and
stdout, exactly the way ``desktop/src-tauri/src/runtime/mod.rs`` does, and
talking to it in JSON lines.

Every event printed is a real line off the real wire, so the transcript of a
run is the machine-readable record of it.

**The machine's state is arranged, deliberately, and here is why.**

An earlier version of this harness took the machine as it found it. It passed
while the card was full and then failed twenty minutes later when the user's
other work released it — reporting five failures that were nothing but the
weather. A test that means something different depending on what else is
running proves nothing on either occasion.

So the readings are supplied here rather than measured, through a seam that
only exists when QRONOS_QUEUE_DEMO is set. It is the sensor that is
substituted, never the machine, and nothing of the user's is ever touched.
Everything downstream of the reading — the dwell, the scheduler, the governor,
the safety floor, the protocol, the process boundary — is the real thing.

The task that cannot fit likewise asks for more graphics memory than any card
has, so that step means the same thing on any machine.

What this therefore does **not** prove is that the real sensor calls this
machine busy. That is the other harness's job:
``tools/test_qronos_smart_queue_live.py`` takes sixty seconds of real readings
and arranges nothing at all. The two together are the argument; neither is on
its own.

    .venv\\Scripts\\python.exe tools\\test_qronos_smart_queue_e2e.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


TICK_SECONDS = 0.25
STARTUP_TIMEOUT = 60.0
SETTLE_TIMEOUT = 40.0

#: More than any graphics card, so the safety floor refuses regardless of what
#: the machine happens to be doing while this runs.
IMPOSSIBLE_VRAM_MB = 900_000


class Bridge:
    """
    The runtime as a child process, spoken to the way Rust speaks to it.

    Events are drained on their own thread rather than read on demand. Reading
    on demand made the first version of this harness race the scheduler and
    report two failures that were the test's fault, not the product's.
    """

    def __init__(self, demo: bool = True) -> None:
        environment = dict(os.environ)
        environment["QRONOS_QUEUE_TICK_SECONDS"] = str(TICK_SECONDS)
        environment["PYTHONIOENCODING"] = "utf-8"

        if demo:
            environment["QRONOS_QUEUE_DEMO"] = "1"
        else:
            environment.pop("QRONOS_QUEUE_DEMO", None)

        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "core.runtime_bridge"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=environment,
        )

        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self.process.stdout is not None

        for line in self.process.stdout:
            if not line.strip():
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = {"eventType": "UNPARSEABLE", "message": line.strip()}

            with self._lock:
                self.events.append(event)

    # ------------------------------------------------------------- talking

    def send(self, quiet: bool = False, **payload) -> None:
        line = json.dumps(payload)

        if not quiet:
            print(f"      -> {line}")

        assert self.process.stdin is not None
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def mark(self) -> int:
        with self._lock:
            return len(self.events)

    def since(self, mark: int) -> list[dict]:
        with self._lock:
            return self.events[mark:]

    def wait_for(
        self,
        event_type: str,
        after: int = 0,
        timeout: float = 20.0,
    ) -> dict | None:
        deadline = time.time() + timeout

        while time.time() < deadline:
            for event in self.since(after):
                if event["eventType"] == event_type:
                    print(
                        f"      <- {event['eventType']}  "
                        f"{str(event.get('message', ''))[:88]}"
                    )
                    return event

            time.sleep(0.05)

        return None

    # ------------------------------------------------------------- reading

    def latest_queue(self) -> dict | None:
        with self._lock:
            for event in reversed(self.events):
                if event["eventType"] == "queue_changed":
                    return json.loads(event["message"])

        return None

    def queues(self) -> list[dict]:
        with self._lock:
            return [
                json.loads(event["message"])
                for event in self.events
                if event["eventType"] == "queue_changed"
            ]

    def settle(
        self,
        predicate,
        timeout: float = SETTLE_TIMEOUT,
        description: str = "",
    ) -> dict | None:
        """
        Poll until the queue looks a certain way, or give up.

        The scheduler is a thread on a timer; asking it a question the
        instant after telling it something is asking before it has looked.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            self.send(quiet=True, command="queue_list")
            time.sleep(TICK_SECONDS * 3)

            queue = self.latest_queue()

            if queue is not None and predicate(queue):
                return queue

        print(f"      (gave up waiting for {description or 'the queue'})")

        return None

    def task(self, task_id: str) -> dict | None:
        queue = self.latest_queue()

        if queue is None:
            return None

        return next(
            (task for task in queue["tasks"] if task["taskId"] == task_id),
            None,
        )

    def submit(self, summary: str, **extra) -> str:
        mark = self.mark()
        self.send(command="queue_submit", summary=summary, **extra)
        event = self.wait_for("queue_task_queued", after=mark)

        if event is None:
            raise AssertionError(f"the bridge never queued {summary!r}")

        return json.loads(event["message"])["taskId"]

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.send(quiet=True, command="shutdown")
                self.process.wait(timeout=15)
            except Exception:
                self.process.kill()

        for stream in (self.process.stdin, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


# ---------------------------------------------------------------- reporting

RESULTS: list[tuple[str, bool]] = []


def rule(title: str) -> None:
    print(f"\n{title}")
    print("-" * 74)


def check(label: str, passed: bool, detail: str = "") -> bool:
    RESULTS.append((label, bool(passed)))
    print(f"  [{'ok' if passed else 'FAILED'}] {label}")

    if detail and not passed:
        print(f"         {detail}")

    return bool(passed)


# ------------------------------------------------------------------- checks


def check_startup(bridge: Bridge) -> None:
    rule("1. It starts, and does nothing it was not asked to do")

    ready = bridge.wait_for("runtime_ready", timeout=STARTUP_TIMEOUT)
    check("it announced itself", ready is not None)

    # No sampler, no scheduler, no nvidia-smi until somebody wants the queue.
    time.sleep(1.0)
    unprompted = [
        event["eventType"]
        for event in bridge.since(0)
        if event["eventType"] != "runtime_ready"
    ]
    check(
        "and said nothing else unprompted",
        unprompted == [],
        f"it also said {unprompted}",
    )


def check_the_queue_starts_on_demand(bridge: Bridge) -> None:
    rule("2. Asking for the queue is what starts it")

    mark = bridge.mark()
    bridge.send(command="queue_list")
    event = bridge.wait_for("queue_changed", after=mark, timeout=STARTUP_TIMEOUT)

    check("a queue came back", event is not None)

    queue = bridge.latest_queue() or {}
    check("and it is empty", queue.get("tasks") == [])

    # Reported, not asserted. What the machine happens to be doing right now
    # is real information worth having in the transcript, and it is exactly
    # the thing that must not be allowed to decide whether this run passes.
    print(f"         the real machine currently reads: {queue.get('level')}")


def make_busy(bridge: Bridge) -> None:
    """Tell the monitor the machine is loaded, and wait for it to agree."""
    bridge.send(quiet=True, command="queue_debug_load", state="busy")
    time.sleep(TICK_SECONDS * 6)


def check_a_busy_machine_holds_work(bridge: Bridge) -> str:
    rule("3. On a busy machine, work is held rather than refused")
    print("         (readings supplied, not measured — see the module note)")

    make_busy(bridge)

    # Zero graphics memory on purpose. The safety floor is not the subject
    # here — the politeness hold is — and a task that could not fit would be
    # stopped by the floor first and prove nothing about manners.
    task_id = bridge.submit(
        "tidy up this week's notes",
        weight="light",
        requiredVramMb=0,
    )

    held = bridge.settle(
        lambda queue: any(
            task["taskId"] == task_id and task["heldReason"] == "sustained_load"
            for task in queue["tasks"]
        ),
        description="the hold reason to settle",
    )

    task = bridge.task(task_id) or {}
    print(f"         state       {task.get('state')}")
    print(f"         held by     {task.get('heldReason')}")
    print(f"         overridable {task.get('overridable')}")
    print(f"         detail      {task.get('detail')}")

    check("it is waiting, not failed", task.get("state") == "queued")
    check(
        "because the machine is busy, not because of a limit",
        task.get("heldReason") == "sustained_load",
        f"it said {task.get('heldReason')}",
    )
    check("and the user is offered a way past it", task.get("overridable") is True)
    check("the reason is in words a person can read", bool(task.get("detail")))

    return task_id


def check_it_stays_held(bridge: Bridge, task_id: str) -> None:
    rule("4. It stays held while nothing changes")

    mark = bridge.mark()
    time.sleep(TICK_SECONDS * 20)
    bridge.send(quiet=True, command="queue_list")
    time.sleep(TICK_SECONDS * 3)

    task = bridge.task(task_id) or {}
    check(
        "twenty ticks later it has still not run",
        task.get("state") == "queued",
        f"it is {task.get('state')}",
    )

    # The pump ran twenty times. If it announced itself each time it would
    # flood a pipe the voice spectrum already keeps busy.
    #
    # The allowance is worked out rather than guessed, because on a live
    # machine things genuinely do change while the test is running and a real
    # change is a real event. Each level change legitimately produces two
    # updates — the level itself, and the hold reason it alters — and this
    # check makes one queue_list of its own. Anything beyond that is the pump
    # talking for the sake of it.
    window = bridge.since(mark)
    chatter = sum(1 for e in window if e["eventType"] == "queue_changed")
    level_changes = sum(
        1 for e in window if e["eventType"] == "queue_hold_state"
    )
    allowance = 1 + level_changes * 2

    check(
        "and the pump spoke only when something actually changed",
        chatter <= allowance,
        f"{chatter} updates across twenty ticks, "
        f"{level_changes} of which the machine's own changes explain",
    )


def check_override_runs_it(bridge: Bridge, task_id: str) -> None:
    rule("5. Pressing 'run anyway' lifts a hold that is only politeness")

    mark = bridge.mark()
    bridge.send(command="queue_override", taskId=task_id)

    started = bridge.wait_for("queue_task_started", after=mark)
    check("it started", started is not None)

    finished = bridge.wait_for("queue_task_finished", after=mark)
    check("and finished", finished is not None)

    if finished:
        check(
            "and reported success",
            json.loads(finished["message"]).get("success") is True,
        )


def check_the_floor_cannot_be_overridden(bridge: Bridge) -> None:
    rule("6. A task that cannot fit is refused, and the button will not help")

    make_busy(bridge)

    # Larger than any card, so this step means the same thing whatever else is
    # running on the machine today.
    task_id = bridge.submit(
        "load something enormous",
        weight="heavy",
        requiredVramMb=IMPOSSIBLE_VRAM_MB,
    )

    bridge.settle(
        lambda queue: any(
            task["taskId"] == task_id and task["heldReason"] == "safety_floor"
            for task in queue["tasks"]
        ),
        description="the floor to take hold",
    )

    task = bridge.task(task_id) or {}
    print(f"         held by     {task.get('heldReason')}")
    print(f"         overridable {task.get('overridable')}")

    check(
        "the safety floor holds it, not politeness",
        task.get("heldReason") == "safety_floor",
        f"it said {task.get('heldReason')}",
    )
    check(
        "so no override button is offered at all",
        task.get("overridable") is False,
    )

    mark = bridge.mark()
    bridge.send(command="queue_override", taskId=task_id)
    refused = bridge.wait_for("queue_override_refused", after=mark)

    check("pressing it regardless is refused", refused is not None)

    if refused:
        detail = json.loads(refused["message"])
        print(
            f"         needs {detail.get('requiredVramMb')} MB, "
            f"{detail.get('freeVramMb')} MB free, "
            f"breach {detail.get('breach')}"
        )
        check(
            "and the refusal carries the numbers",
            detail.get("requiredVramMb") == IMPOSSIBLE_VRAM_MB,
        )

    # The dangerous version of this bug: the refusal is reported, a flag is set
    # anyway, and the work starts unannounced a few seconds later.
    mark = bridge.mark()
    time.sleep(TICK_SECONDS * 20)
    sneaked = [
        event
        for event in bridge.since(mark)
        if event["eventType"] == "queue_task_started"
    ]
    check("and it does not sneak in on a later tick", sneaked == [])

    task = bridge.task(task_id) or {}
    check("it is still waiting", task.get("state") == "queued")

    bridge.send(quiet=True, command="queue_cancel", taskId=task_id)
    bridge.settle(
        lambda queue: all(
            task["taskId"] != task_id or task["state"] == "cancelled"
            for task in queue["tasks"]
        ),
        timeout=10.0,
        description="the cancel to land",
    )
    check(
        "cancelling it works",
        (bridge.task(task_id) or {}).get("state") == "cancelled",
    )


def check_pause_and_resume(bridge: Bridge) -> None:
    rule("7. The queue can be held by hand, and let go again")

    bridge.send(command="queue_set_paused", paused=True)
    bridge.settle(
        lambda queue: queue["paused"] is True,
        timeout=10.0,
        description="the pause",
    )
    check("pausing is reflected in the queue", (bridge.latest_queue() or {}).get("paused") is True)

    task_id = bridge.submit(
        "something to hold",
        weight="light",
        requiredVramMb=0,
    )

    mark = bridge.mark()
    bridge.send(quiet=True, command="queue_debug_load", state="free")
    time.sleep(TICK_SECONDS * 16)

    started = [
        event
        for event in bridge.since(mark)
        if event["eventType"] == "queue_task_started"
    ]
    check(
        "and nothing runs while paused, however free the machine looks",
        started == [],
    )

    mark = bridge.mark()
    bridge.send(command="queue_set_paused", paused=False)
    check(
        "resuming releases it",
        bridge.wait_for("queue_task_started", after=mark) is not None,
    )
    bridge.wait_for("queue_task_finished", after=mark)


def check_it_runs_when_the_machine_frees(bridge: Bridge) -> None:
    rule("8. It runs by itself when the machine frees up")
    print("         NOTE: the sensor is substituted here, not the machine.")
    print("         Nothing of the user's is touched. Everything downstream")
    print("         of the reading is the real thing.")

    make_busy(bridge)

    task_id = bridge.submit(
        "file away the meeting notes",
        weight="light",
        requiredVramMb=0,
    )

    bridge.settle(
        lambda queue: any(
            task["taskId"] == task_id and task["heldReason"] == "sustained_load"
            for task in queue["tasks"]
        ),
        description="the hold",
    )
    check(
        "it is held first",
        (bridge.task(task_id) or {}).get("heldReason") == "sustained_load",
    )

    mark = bridge.mark()
    bridge.send(command="queue_debug_load", state="free")

    started = bridge.wait_for("queue_task_started", after=mark)
    check("with nobody pressing anything, it started", started is not None)
    check(
        "and completed",
        bridge.wait_for("queue_task_finished", after=mark) is not None,
    )


def check_ordering(bridge: Bridge) -> None:
    rule("9. Work runs in the order it arrived")

    make_busy(bridge)

    first = bridge.submit("the first thing", weight="light", requiredVramMb=0)
    second = bridge.submit("the second thing", weight="light", requiredVramMb=0)

    mark = bridge.mark()
    bridge.send(quiet=True, command="queue_debug_load", state="free")

    order: list[str] = []
    deadline = time.time() + 25.0

    while time.time() < deadline and len(order) < 2:
        for event in bridge.since(mark):
            if event["eventType"] == "queue_task_started":
                task_id = json.loads(event["message"])["taskId"]

                if task_id not in order:
                    order.append(task_id)

        time.sleep(0.05)

    check(
        "the first submitted ran first",
        order[:2] == [first, second],
        f"order was {order}",
    )


def check_revisions_only_go_forward(bridge: Bridge) -> None:
    rule("10. The interface can always tell which snapshot is newer")

    revisions = [queue["revision"] for queue in bridge.queues()]

    check(
        "revisions never go backwards",
        revisions == sorted(revisions),
        f"{revisions[:20]}...",
    )
    check("and there were plenty of them", len(revisions) > 5)


def check_bad_input(bridge: Bridge) -> None:
    rule("11. Nonsense over the pipe is refused, and nothing dies")

    cases = [
        ("an unknown weight", {"command": "queue_submit", "summary": "x",
                               "weight": "enormous"}),
        ("an empty summary", {"command": "queue_submit", "summary": "   "}),
        ("a cancel with no task id", {"command": "queue_cancel"}),
        ("a pause that is not a boolean",
         {"command": "queue_set_paused", "paused": "yes"}),
        ("a fractional memory requirement",
         {"command": "queue_submit", "summary": "x", "requiredVramMb": 1.5}),
        ("a negative memory requirement",
         {"command": "queue_submit", "summary": "x", "requiredVramMb": -1}),
    ]

    for label, payload in cases:
        mark = bridge.mark()
        bridge.send(quiet=True, **payload)
        error = bridge.wait_for("runtime_error", after=mark, timeout=10.0)
        check(f"{label} is refused", error is not None)

    mark = bridge.mark()
    bridge.send(quiet=True, command="ping")
    check(
        "and the bridge is still answering afterwards",
        bridge.wait_for("runtime_pong", after=mark, timeout=10.0) is not None,
    )


def check_shutdown(bridge: Bridge) -> None:
    rule("12. It shuts down cleanly with the pump running")

    mark = bridge.mark()
    bridge.send(command="shutdown")

    check(
        "it acknowledged",
        bridge.wait_for("runtime_stopping", after=mark) is not None,
    )

    code = bridge.process.wait(timeout=30)
    check(f"it exited with {code}", code == 0)

    assert bridge.process.stderr is not None
    stderr = bridge.process.stderr.read().strip()

    check("and wrote nothing to stderr", stderr == "", stderr[:400])


def check_demo_commands_are_off_by_default() -> None:
    rule("13. The demonstration seam is not there unless it is switched on")

    bridge = Bridge(demo=False)

    try:
        bridge.wait_for("runtime_ready", timeout=STARTUP_TIMEOUT)
        bridge.send(quiet=True, command="queue_list")
        bridge.wait_for("queue_changed", timeout=STARTUP_TIMEOUT)

        mark = bridge.mark()
        bridge.send(quiet=True, command="queue_debug_load", state="free")

        check(
            "a bridge without the flag refuses to fake its readings",
            bridge.wait_for("runtime_error", after=mark, timeout=10.0)
            is not None,
        )
    finally:
        bridge.close()


def main() -> int:
    print("=" * 74)
    print("Qronos smart queue, end to end through the real bridge process")
    print("=" * 74)

    bridge = Bridge()

    try:
        check_startup(bridge)
        check_the_queue_starts_on_demand(bridge)

        task_id = check_a_busy_machine_holds_work(bridge)
        check_it_stays_held(bridge, task_id)
        check_override_runs_it(bridge, task_id)

        check_the_floor_cannot_be_overridden(bridge)
        check_pause_and_resume(bridge)
        check_it_runs_when_the_machine_frees(bridge)
        check_ordering(bridge)
        check_revisions_only_go_forward(bridge)
        check_bad_input(bridge)
        check_shutdown(bridge)
    finally:
        bridge.close()

    check_demo_commands_are_off_by_default()

    rule("Verdict")
    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"  {passed} of {len(RESULTS)} checks passed")
    print(f"  {len(bridge.events)} events came off the wire")

    for label, ok in RESULTS:
        if not ok:
            print(f"    FAILED: {label}")

    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
