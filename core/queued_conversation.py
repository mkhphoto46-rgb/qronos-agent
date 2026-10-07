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

Cancellation is honest about what it can do. Every run of a request gets
its own local inference cancellation token, bound around the model call, so
the model requests made for that run - and only those - can be closed:

    the user cancels it          -> the request is closed; terminal
    Qronos closes                -> the request is closed; interrupted
    the user's own turn starts   -> the request is closed; deferred
    Qronos is paused             -> the request is closed; deferred

Closing a request closes this run's own connections to the local model
server. It does not unload a model, and it does not prove when the server
has finished with the GPU. Whatever the closed request would have produced
is never attached. A result already committed to the conversation cannot be
un-sent, so a stop arriving after that is refused rather than misreported,
and a later run gets a fresh token that nothing aimed at an earlier run can
cancel.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Sequence

from core.arithmetic_fast_path import solve_simple_arithmetic
from core.brain_runtime import BrainMessage
from core.inference_cancellation import (
    LocalInferenceCancellation,
    inference_cancellation_scope,
)
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

    Two kinds of stop reach it from other threads. Terminal ones - the user
    cancels it, Qronos closes - end the request. Run-scoped ones - the user's
    own turn starts, Qronos is paused - end only the current run, which then
    goes back in line. Both close the current run's model request through its
    cancellation token. The commit point decides, under one lock, whether a
    stop or the answer came first.
    """

    #: Run-scoped stops, by token reason.
    PREEMPTED = "preempted"
    PAUSED = "runtime_paused"

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

        # The current run's token; None between runs.
        self._run: LocalInferenceCancellation | None = None

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
        The user cancels the request. False once its answer is committed.

        Cancelling queued work and cancelling running work are the same call:
        either way nothing it produces from now on is attached anywhere, and
        a run under way has its model request closed.
        """
        return self._end("user_stop")

    def interrupt(self) -> bool:
        """Qronos is closing. Like cancel, but recorded as an interruption."""
        return self._end("runtime_shutdown")

    def preempt(self) -> bool:
        """
        The user's own turn started. Closes only the current run's request.

        The request itself goes back in line. False when nothing is running
        or the answer is already committed.
        """
        return self._stop_run(self.PREEMPTED)

    def pause(self) -> bool:
        """Qronos was paused. Closes only the current run's request."""
        return self._stop_run(self.PAUSED)

    def _end(self, reason: str) -> bool:
        with self._lock:
            if self._committed:
                return False

            self._cancelled = True

            if self._run is not None:
                self._run.cancel(reason)

            return True

    def _stop_run(self, reason: str) -> bool:
        with self._lock:
            if self._committed or self._run is None:
                return False

            return self._run.cancel(reason)

    # ------------------------------------------------------ worker side

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def begin_run(self) -> LocalInferenceCancellation | None:
        """
        A fresh token for one run, or None when the request has ended.

        Never reused: a stop aimed at an earlier run - or still held by
        anything that saw that run's token - cannot reach this one.
        """
        with self._lock:
            if self._cancelled or self._committed:
                return None

            self._run = LocalInferenceCancellation()
            return self._run

    def end_run(self, token: LocalInferenceCancellation) -> None:
        with self._lock:
            if self._run is token:
                self._run = None

    def commit(self) -> bool:
        """
        Claim the right to deliver. False when any stop came first.

        The single point where stops and delivery are ordered. Checking for
        a stop and then delivering would leave a window in which one lands
        between the two and the answer is attached anyway.
        """
        with self._lock:
            if self._cancelled:
                return False

            if self._run is not None and self._run.cancelled:
                return False

            self._committed = True
            return True

    @property
    def preempted(self) -> bool:
        """Whether the current run was stopped for the user's own turn."""
        with self._lock:
            return (
                self._run is not None
                and self._run.reason == self.PREEMPTED
            )


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
        release_brains: Callable[[frozenset[str] | None], None],
        already_answered: Callable[[str], bool] = lambda request_id: False,
        resident_brains: Callable[[], frozenset[str] | None] = lambda: None,
        storage_blocks_start: Callable[[], str | None] = lambda: None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._orchestrator = orchestrator
        self._route = route
        self._record_answer = record_answer
        self._notify_sink = notify
        self._foreground_busy = foreground_busy
        self._runtime_paused = runtime_paused
        self._release_brains = release_brains
        self._already_answered = already_answered
        self._resident_brains = resident_brains
        self._storage_blocks_start = storage_blocks_start
        self._clock = clock

    def _notify(
        self,
        work: QueuedConversationWork,
        phase: ConversationPhase,
        fields: dict,
    ) -> None:
        """
        Publish progress, never for work the user has already cancelled.

        A cancelled request's conversation was told "cancelled" when the
        cancel was accepted. Anything this worker says afterwards - that it
        is running, that it deferred - would be late output contradicting
        that, and could land on a newer request reusing the same id.
        """
        if work.cancelled:
            return

        self._notify_sink(work, phase, fields)

    def execute(self, work: QueuedConversationWork) -> WorkResult:
        started = self._clock()
        # [called the brain, brains resident just before it (None: unknown)]
        loaded_model: list = [False, None]
        token = work.begin_run()

        if token is None:
            return WorkResult(
                WorkOutcome.CANCELLED,
                "Cancelled before it ran.",
            )

        try:
            result = self._execute(work, token, started, loaded_model)
        except Exception as exc:
            # A stop surfaces from deep in the model call as an exception.
            # It is reported as the stop it was, never as a failure, and its
            # text - transport detail - is never shown.
            result = self._stopped(work, token) or self._failed(
                work,
                started,
                f"{exc}",
            )
        finally:
            work.end_run(token)

            # Outside the run's cancellation scope, so giving back what this
            # run loaded is never itself cut short by the run's stop.
            if loaded_model[0]:
                self._release_after_run(loaded_model[1])

        return result

    def _stopped(
        self,
        work: QueuedConversationWork,
        token: LocalInferenceCancellation,
    ) -> WorkResult | None:
        """
        What a stop means for this run, or None when it was not stopped.

        Terminal stops end the request. A run stopped for the user's turn or
        for Pause goes back in line without using an attempt: nothing was
        wrong with it.
        """
        if work.cancelled:
            return self._cancelled_while_running()

        reason = token.reason

        if reason is None:
            return None

        if reason == QueuedConversationWork.PREEMPTED:
            return self._deferred_for_foreground(work)

        return self._deferred(
            work,
            HoldReason.PAUSED,
            "Qronos was paused while this ran. Its model request was "
            "closed, and it will run again after Resume.",
            count_attempt=False,
        )

    # ------------------------------------------------------------ the run

    def _execute(
        self,
        work: QueuedConversationWork,
        token: LocalInferenceCancellation,
        started: float,
        loaded_model: list[bool],
    ) -> WorkResult:
        request = work.request

        waiting = self._must_wait(work)

        if waiting is not None:
            return waiting

        # One request, one answer. If the conversation already holds an
        # answer for this request - its first run reached the history and
        # then Qronos stopped before the queue could record it - running it
        # again would answer twice.
        if self._already_answered(request.request_id):
            self._notify(
                work,
                ConversationPhase.ANSWERED,
                {"alreadyAnswered": True},
            )
            return WorkResult(
                WorkOutcome.DONE,
                "This request already has an answer; it was not run again.",
            )

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

        stopped = self._stopped(work, token)

        if stopped is not None:
            return stopped

        self._notify(work, ConversationPhase.RUNNING, {})

        response = ""

        if request.route is TaskType.FAST:
            arithmetic = solve_simple_arithmetic(request.text)

            if arithmetic is not None:
                response = arithmetic.spoken_text

        if not response:
            stopped = self._stopped(work, token)

            if stopped is not None:
                # Stopped while it was being routed: never start a model
                # call whose output would only be thrown away.
                return stopped

            plan = TaskPlan(goal=request.text)
            plan.add_step(
                task_type=request.route,
                description=request.text,
            )

            orchestrator = self._orchestrator()

            try:
                loaded_model[1] = self._resident_brains()
            except Exception:
                loaded_model[1] = None

            loaded_model[0] = True

            try:
                # Every model request this run makes, however deep in the
                # Orchestrator, inherits this run's token from the scope.
                with inference_cancellation_scope(token):
                    results = orchestrator.execute_plan(
                        plan,
                        conversation_messages=request.context,
                        # Attributed as background work in the resource
                        # ledger. Admission is unchanged: the Orchestrator's
                        # resource and activity checks apply as to a direct
                        # turn.
                        priority=WorkloadPriority.BACKGROUND,
                    )
            except Exception:
                stopped = self._stopped(work, token)

                if stopped is not None:
                    return stopped

                raise

            # The Orchestrator reports a closed request as a failed step. A
            # stop is checked first so that is never shown as a failure.
            stopped = self._stopped(work, token)

            if stopped is not None:
                return stopped

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
            return self._stopped(work, token) or self._cancelled_while_running()

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

        blocked = self._storage_blocks_start()

        if blocked:
            # Starting would leave the saved queue saying "queued" for work
            # that ran. A crash would then replay it unasked. Waiting is the
            # honest option; the check retries the save every time.
            return self._deferred(
                work,
                HoldReason.STORAGE_UNAVAILABLE,
                blocked,
                count_attempt=False,
            )

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
                "Cancelled while running. Its local model request was closed "
                "and nothing it produced was attached."
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

    def _release_after_run(self, resident_before: frozenset[str] | None) -> None:
        """
        Give back what this run loaded, unless the user's turn now owns it.

        Background work must not keep a brain resident after it is done. But
        unloading while the user's own turn is running could pull the model
        out from under that turn, which is exactly the competition this
        module exists to avoid. In that case the foreground turn manages
        residency, as it always has.

        ``resident_before`` is what was loaded before this run touched the
        brain. Only what it added is given back: a brain that was already
        warm for the user's own conversation is not this run's to evict.
        """
        if self._foreground_is_busy():
            return

        try:
            self._release_brains(resident_before)
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
