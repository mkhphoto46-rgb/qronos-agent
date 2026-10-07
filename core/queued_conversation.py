"""
A conversation request that waits in the Smart Queue, then is really answered.

The queue used to hold a placeholder that produced nothing. This is the work it
was built for: a typed Fast/Heavy request the machine could not take on when it
was asked — the user's own turn was still running, or resource policy refused
the brain — answered later by the same Orchestrator, the same model slot and
the same resource checks a direct text turn uses, and delivered back to the
request it belongs to.

What it deliberately is not:

    Not a way to run actions. Only Fast and Heavy reasoning is queueable.
    Anything that routes to Computer, Vision or Browser changes something,
    looks at something, or reaches the internet, and each of those has its
    own visible approval path. Nothing in the background takes those paths on
    the user's behalf. The route is checked again when the work runs, not only
    when it was submitted.

    Not a second brain. The model is loaded and admitted by the Orchestrator,
    which re-reads the machine immediately before loading. When that check
    says no, the work defers back into the queue instead of failing.

    Not in competition with the user. The user's own turn owns the model
    phase; queued work does not start while one is running, a turn that
    starts mid-run marks the work preempted, and the work never unloads
    models while the user's turn may be using them.

Cancellation is honest about what it can do. The local model call cannot be
interrupted mid-generation through the current brain runtime, so a cancel
while it runs means: the answer it produces is discarded rather than
attached, and the task reports itself cancelled. A result already committed
to the conversation cannot be un-sent, so a cancel arriving after that is
refused rather than misreported.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Sequence

from core.arithmetic_fast_path import solve_simple_arithmetic
from core.brain_runtime import BrainMessage
from core.queue_scheduler import HoldReason, WorkOutcome, WorkResult
from core.resource_ownership import WorkloadPriority
from core.task_plan import TaskPlan
from core.task_router import TaskType


#: The only routes the background queue may run. Everything else needs the
#: user present to approve it.
QUEUEABLE_ROUTES = frozenset({TaskType.FAST, TaskType.HEAVY})

#: How much of the request is shown as the task's summary in the queue.
SUMMARY_LENGTH = 120


class ConversationPhase(Enum):
    """Where a queued request is, as its conversation shows it."""

    QUEUED = "queued"
    RUNNING = "running"
    DEFERRED = "deferred"
    ANSWERED = "answered"
    FAILED = "failed"
    CANCELLED = "cancelled"

    #: The runtime closed while the request was running, so how far it got
    #: is unknown. It is never run again automatically; the user resumes it.
    INTERRUPTED = "interrupted"

    #: Held by the user; runs again only when resumed.
    PAUSED = "paused"


@dataclass(frozen=True)
class ConversationRequest:
    """
    One typed request, and the conversation it was asked in.

    ``context`` is the conversation as it stood when the request was made,
    captured then rather than when it runs. An answer produced twenty minutes
    later is still an answer to the question in the context it was asked in,
    and the request itself is not duplicated into its own history.
    """

    request_id: str
    text: str
    route: TaskType
    context: tuple[BrainMessage, ...] = ()

    def summary(self) -> str:
        flattened = " ".join(self.text.split())

        if len(flattened) <= SUMMARY_LENGTH:
            return flattened

        return flattened[: SUMMARY_LENGTH - 1].rstrip() + "…"


class QueuedConversationWork:
    """
    The runnable the scheduler holds for one request.

    Carries the two flags a running request can be told about from another
    thread — cancelled, preempted — and the commit point that decides which
    of "cancelled" and "answered" is true when both race.
    """

    def __init__(
        self,
        request: ConversationRequest,
        executor: "ConversationQueueExecutor",
    ) -> None:
        self.request = request
        self.task_id = ""
        self._executor = executor

        self._lock = threading.Lock()
        self._cancelled = False
        self._committed = False
        self._preempted = threading.Event()

    # ------------------------------------------------------ scheduler side

    def describe(self) -> str:
        return self.request.summary()

    def public_fields(self) -> dict[str, Any]:
        return {
            "kind": "conversation",
            "requestId": self.request.request_id,
            "route": self.request.route.value,
        }

    def run(self) -> WorkResult:
        return self._executor.execute(self)

    def cancel(self) -> bool:
        """
        Ask the work to stop. False once its answer has been committed.

        Cancelling queued work and cancelling running work are the same call:
        either way nothing it produces from now on is attached anywhere.
        """
        with self._lock:
            if self._committed:
                return False

            self._cancelled = True
            return True

    # ------------------------------------------------------ worker side

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def commit(self) -> bool:
        """
        Claim the right to deliver. False when cancelled first.

        The single point where cancel and delivery are ordered. Checking
        ``cancelled`` and then delivering would leave a window in which a
        cancel lands between the two and the answer is attached anyway.
        """
        with self._lock:
            if self._cancelled:
                return False

            self._committed = True
            return True

    def preempt(self) -> None:
        """The user started a turn of their own while this was running."""
        self._preempted.set()

    @property
    def preempted(self) -> bool:
        return self._preempted.is_set()


ProgressNotifier = Callable[
    [QueuedConversationWork, ConversationPhase, dict],
    None,
]


class ConversationQueueExecutor:
    """
    Runs one queued request through the production brain path.

    Every collaborator is injected so the whole decision is testable with a
    fake orchestrator and no model, which is the only way a test suite should
    ever exercise it.
    """

    def __init__(
        self,
        *,
        orchestrator: Callable[[], Any],
        route: Callable[[str], TaskType],
        record_answer: Callable[[ConversationRequest, str], None],
        notify: ProgressNotifier,
        foreground_busy: Callable[[], bool],
        runtime_paused: Callable[[], bool],
        release_brains: Callable[[], None],
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._orchestrator = orchestrator
        self._route = route
        self._record_answer = record_answer
        self._notify = notify
        self._foreground_busy = foreground_busy
        self._runtime_paused = runtime_paused
        self._release_brains = release_brains
        self._clock = clock

    def execute(self, work: QueuedConversationWork) -> WorkResult:
        started = self._clock()
        loaded_model = [False]

        try:
            result = self._execute(work, started, loaded_model)
        except Exception as exc:
            result = self._failed(work, started, f"{exc}")
        finally:
            if loaded_model[0]:
                self._release_after_run()

        return result

    # ------------------------------------------------------------ the run

    def _execute(
        self,
        work: QueuedConversationWork,
        started: float,
        loaded_model: list[bool],
    ) -> WorkResult:
        request = work.request

        if work.cancelled:
            return WorkResult(
                WorkOutcome.CANCELLED,
                "Cancelled before it ran.",
            )

        waiting = self._must_wait(work)

        if waiting is not None:
            return waiting

        # Routing is deterministic, so this should agree with submission. It
        # is asked again anyway: the rule is that the queue never runs an
        # action, and a rule checked once at the door is a rule that can be
        # walked around by whatever changes between the door and the run.
        route_now = self._route(request.text)

        if route_now not in QUEUEABLE_ROUTES:
            return self._failed(
                work,
                started,
                (
                    f"This request now routes to {route_now.value}, which "
                    "needs your approval. The background queue only answers "
                    "questions; ask again directly."
                ),
            )

        self._notify(work, ConversationPhase.RUNNING, {})

        response = ""

        if request.route is TaskType.FAST:
            arithmetic = solve_simple_arithmetic(request.text)

            if arithmetic is not None:
                response = arithmetic.spoken_text

        if not response:
            plan = TaskPlan(goal=request.text)
            plan.add_step(
                task_type=request.route,
                description=request.text,
            )

            orchestrator = self._orchestrator()
            loaded_model[0] = True

            try:
                results = orchestrator.execute_plan(
                    plan,
                    conversation_messages=request.context,
                    # Attributed as background work in the resource ledger.
                    # Admission is unchanged: the Orchestrator's resource and
                    # activity checks apply exactly as to a direct turn.
                    priority=WorkloadPriority.BACKGROUND,
                )
            except Exception as exc:
                if work.preempted and not work.cancelled:
                    return self._deferred_for_foreground(work)

                raise exc

            if work.cancelled:
                return self._cancelled_while_running()

            if self._runtime_paused():
                return self._deferred(
                    work,
                    HoldReason.PAUSED,
                    "Qronos was paused while this ran; it will run again "
                    "after Resume.",
                    count_attempt=False,
                )

            if not results:
                return self._failed(
                    work,
                    started,
                    "Qronos did not return an execution result.",
                )

            outcome = results[-1]

            if not outcome.success:
                if getattr(outcome, "resource_blocked", False):
                    return self._deferred(
                        work,
                        HoldReason.SUSTAINED_LOAD,
                        "Resource policy refused the brain when this came to "
                        "run. It will try again when the machine is free.",
                    )

                if work.preempted:
                    return self._deferred_for_foreground(work)

                return self._failed(
                    work,
                    started,
                    outcome.error or "Qronos task failed.",
                )

            response = (outcome.output or "").strip()

        if not response:
            return self._failed(
                work,
                started,
                "Qronos returned an empty response.",
            )

        if not work.commit():
            return self._cancelled_while_running()

        persisted = True

        try:
            self._record_answer(request, response)
        except Exception:
            # The answer exists and the user asked for it. Losing the history
            # write must not lose the answer too; it is still delivered, and
            # marked as not persisted.
            persisted = False

        self._notify(
            work,
            ConversationPhase.ANSWERED,
            {
                "text": response,
                "persisted": persisted,
                "elapsedSeconds": round(self._clock() - started, 3),
            },
        )

        return WorkResult(WorkOutcome.DONE)

    def _must_wait(self, work: QueuedConversationWork) -> WorkResult | None:
        if self._runtime_paused():
            return self._deferred(
                work,
                HoldReason.PAUSED,
                "Qronos is paused.",
                count_attempt=False,
            )

        if self._foreground_is_busy():
            return self._deferred_for_foreground(work)

        return None

    # ------------------------------------------------------------ outcomes

    def _deferred_for_foreground(
        self,
        work: QueuedConversationWork,
    ) -> WorkResult:
        return self._deferred(
            work,
            HoldReason.FOREGROUND_ACTIVE,
            "Your own turn took priority; this will run again after it.",
            count_attempt=False,
        )

    def _deferred(
        self,
        work: QueuedConversationWork,
        hold: HoldReason,
        detail: str,
        count_attempt: bool = True,
    ) -> WorkResult:
        self._notify(
            work,
            ConversationPhase.DEFERRED,
            {"reason": hold.value, "detail": detail},
        )

        return WorkResult(
            WorkOutcome.DEFERRED,
            detail,
            hold=hold,
            count_attempt=count_attempt,
        )

    @staticmethod
    def _cancelled_while_running() -> WorkResult:
        # No notification: the cancel was announced when it was accepted, and
        # anything said now would be late output for a request the user has
        # already let go of.
        return WorkResult(
            WorkOutcome.CANCELLED,
            (
                "Cancelled while running. The local model call was allowed "
                "to finish and its output was discarded."
            ),
        )

    def _failed(
        self,
        work: QueuedConversationWork,
        started: float,
        error: str,
    ) -> WorkResult:
        if not work.cancelled:
            self._notify(
                work,
                ConversationPhase.FAILED,
                {
                    "error": error,
                    "elapsedSeconds": round(self._clock() - started, 3),
                },
            )

        return WorkResult(WorkOutcome.FAILED, error)

    # ------------------------------------------------------------ cleanup

    def _foreground_is_busy(self) -> bool:
        try:
            return bool(self._foreground_busy())
        except Exception:
            return True

    def _release_after_run(self) -> None:
        """
        Give the model slot back, unless the user's turn now owns it.

        Background work must not keep a brain resident after it is done. But
        unloading while the user's own turn is running could pull the model
        out from under that turn, which is exactly the competition this
        module exists to avoid. In that case the foreground turn manages
        residency, as it always has.
        """
        if self._foreground_is_busy():
            return

        try:
            self._release_brains()
        except Exception:
            pass


def build_request(
    *,
    request_id: str,
    text: str,
    route: TaskType,
    context: Sequence[BrainMessage] = (),
) -> ConversationRequest:
    """A request, refused here if the queue may never run it."""
    if route not in QUEUEABLE_ROUTES:
        raise ValueError(
            f"A {route.value} request cannot be queued. The background queue "
            "only answers Fast or Heavy questions; actions, vision and "
            "browser work need your approval while you are present."
        )

    return ConversationRequest(
        request_id=request_id,
        text=text,
        route=route,
        context=tuple(context),
    )
