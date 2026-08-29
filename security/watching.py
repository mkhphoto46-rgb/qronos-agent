"""
Watching something over time, with consent that stays visible while it lasts.

``PermissionLevel`` has five levels and they all describe *how* you confirm:
say yes, click yes, type a secret, or never. None of them describes *how long*
a yes lasts. That is fine for every action Qronos has had so far, because they
all happen once — open this, write that, send this message. Watching a camera
does not happen once.

**The answer is not a sixth level.** "Session" is not a way of confirming; it
is a property of the grant. Adding it to the enum would make one list answer
two questions, and every existing comparison over that list — it is an
``IntEnum``, so the levels are ordered and compared — would silently start
comparing incomparable things.

So a watching session is its own object, and the permission that starts it is
an ordinary UI confirmation. Four rules, and all four are what make watching
different from surveillance:

    **The user starts it.** Never Qronos, never a model, never a plan step
    that decided it needed a look. Something a person did begins it, and the
    gate sees that as one action.

    **It is visible the whole time.** An indicator, for as long as frames are
    being taken. Not a notification when it starts — a state you can see while
    it is happening.

    **It ends by itself.** A hard maximum, so a session that is forgotten
    stops anyway. Forgetting is the normal case, not the exceptional one.

    **Stopping is always one action away**, and stopping is immediate: the
    frame after ``stop()`` does not happen.

That is how a video call already works, and Qronos already has the shape in
:class:`core.conversation_session.ConversationSession`.

``HIDDEN_SURVEILLANCE`` keeps meaning exactly what it says and stays FORBIDDEN:
capture with no indicator, or that the user did not start, is that category and
is refused. This module is what makes that line drawable, because now there is
something on the other side of it.

Nothing here captures anything. It decides whether a frame may be taken; the
thing that takes it comes later and asks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from core.actions import ActionRequest
from security import gate
from security.permissions import ActionCategory


#: How long a session may run before it ends on its own, whatever anyone
#: remembers. Ten minutes: long enough to be useful for the thing a camera
#: session is for, short enough that forgetting one is not a day of being
#: watched.
DEFAULT_MAX_SECONDS = 600.0

#: How long a session may go unused before it ends. A watching session nobody
#: is taking frames from is not being watched, it is being left on.
DEFAULT_IDLE_SECONDS = 120.0

#: The slowest sensible rate. One frame every two seconds is plenty for "is
#: someone there, are they facing the screen" and is two orders of magnitude
#: below what a recording would be — which is the point: this cannot be turned
#: into a recording by asking for frames faster.
DEFAULT_MIN_FRAME_INTERVAL = 2.0


Clock = Callable[[], float]


class WatchState(Enum):
    """Where a watching session is in its life."""

    #: Created, approved, not yet started. Nothing is being watched.
    READY = "ready"

    #: Frames may be taken, and the indicator must be showing.
    WATCHING = "watching"

    #: Over. Sessions do not restart; a new one needs a new permission.
    STOPPED = "stopped"


class WatchEnded(Enum):
    """Why a session stopped, which the user is entitled to know."""

    NOT_ENDED = "not_ended"
    STOPPED_BY_USER = "stopped_by_user"
    REACHED_TIME_LIMIT = "reached_time_limit"
    WENT_IDLE = "went_idle"
    FAILED = "failed"


class WatchRefused(Exception):
    """A session could not be started, or a frame could not be taken."""


@dataclass(frozen=True)
class FrameVerdict:
    """Whether one frame may be taken right now, and why not if not."""

    allowed: bool
    reason: str = ""

    #: True while the indicator must be visible to the user. It is a separate
    #: field from ``allowed`` on purpose: a session that has just ended must
    #: turn its indicator off, and one that is refusing a frame for being too
    #: soon must not.
    indicator_visible: bool = False


@dataclass
class WatchingSession:
    """
    Permission to watch, for a while, visibly.

    Built through :func:`begin_watching`, which is where the gate is consulted.
    Constructing one directly is possible and is what the tests do; it grants
    nothing on its own, because the thing that takes frames asks
    :meth:`may_take_frame` and that is what says no.
    """

    category: ActionCategory
    started_by: str
    max_seconds: float = DEFAULT_MAX_SECONDS
    idle_seconds: float = DEFAULT_IDLE_SECONDS
    min_frame_interval: float = DEFAULT_MIN_FRAME_INTERVAL
    clock: Clock = time.monotonic

    state: WatchState = WatchState.READY
    ended: WatchEnded = WatchEnded.NOT_ENDED
    frames: int = 0

    _started_at: float | None = field(default=None, repr=False)
    _last_frame_at: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.max_seconds <= 0:
            raise ValueError(
                "A watching session that cannot run is not a session."
            )

        if self.idle_seconds <= 0:
            raise ValueError("The idle limit must be a positive number.")

        if self.min_frame_interval < 0:
            raise ValueError("A frame interval cannot be negative.")

        if not self.started_by.strip():
            # Who started it is not decoration. The whole distinction between
            # watching and surveillance is that a person began it, and a
            # session that cannot say who did cannot claim that.
            raise ValueError(
                "A watching session must record who started it."
            )

    # ------------------------------------------------------------------ life

    def start(self) -> None:
        """Begin watching. The indicator goes up from here."""
        if self.state is WatchState.STOPPED:
            raise WatchRefused(
                "That watching session has ended. Starting again needs "
                "permission again."
            )

        if self.state is WatchState.WATCHING:
            return

        now = self.clock()

        self.state = WatchState.WATCHING
        self._started_at = now
        self._last_frame_at = now

    def stop(self, reason: WatchEnded = WatchEnded.STOPPED_BY_USER) -> None:
        """
        End it, now.

        Idempotent, and it never raises: stopping is the thing that must always
        work, including from a signal handler, a window closing, or a second
        click on a button that already stopped it.
        """
        if self.state is WatchState.STOPPED:
            return

        self.state = WatchState.STOPPED
        self.ended = reason

    # ----------------------------------------------------------------- state

    @property
    def watching(self) -> bool:
        return self.state is WatchState.WATCHING

    @property
    def indicator_visible(self) -> bool:
        """
        Whether the user must be able to see that this is running.

        Read straight from the state rather than tracked separately, because a
        flag that can disagree with the state is a flag that will, and the
        disagreement is called hidden surveillance.
        """
        return self.state is WatchState.WATCHING

    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0

        return self.clock() - self._started_at

    def remaining(self) -> float:
        """Seconds left before it ends on its own. Never negative."""
        if self.state is not WatchState.WATCHING:
            return 0.0

        return max(0.0, self.max_seconds - self.elapsed())

    # ---------------------------------------------------------------- frames

    def may_take_frame(self) -> FrameVerdict:
        """
        Whether a frame may be taken right now.

        Asked before every frame, not once at the start. That is what makes the
        time limit and the stop button real: a session that checked at the
        beginning would keep taking frames after either.

        Expiry is decided here rather than by a timer, so a session cannot
        outlive its limit because a thread did not get scheduled.
        """
        if self.state is WatchState.READY:
            return FrameVerdict(
                allowed=False,
                reason="That watching session has not been started.",
            )

        if self.state is WatchState.STOPPED:
            return FrameVerdict(
                allowed=False,
                reason=f"That watching session ended: {self.ended.value}.",
            )

        now = self.clock()

        if now - (self._started_at or now) >= self.max_seconds:
            self.stop(WatchEnded.REACHED_TIME_LIMIT)

            return FrameVerdict(
                allowed=False,
                reason=(
                    "That watching session reached its time limit and "
                    "stopped."
                ),
            )

        if now - (self._last_frame_at or now) >= self.idle_seconds:
            self.stop(WatchEnded.WENT_IDLE)

            return FrameVerdict(
                allowed=False,
                reason=(
                    "Nothing has looked at that camera for a while, so the "
                    "session stopped."
                ),
            )

        if (
            self.frames
            and now - (self._last_frame_at or 0.0) < self.min_frame_interval
        ):
            # Not an error and not the end of the session — just too soon.
            # The indicator stays up, because watching is still happening.
            return FrameVerdict(
                allowed=False,
                reason="It is too soon for another frame.",
                indicator_visible=True,
            )

        return FrameVerdict(allowed=True, indicator_visible=True)

    def took_frame(self) -> None:
        """Record that a frame was actually taken."""
        if self.state is not WatchState.WATCHING:
            raise WatchRefused(
                "A frame was recorded against a session that is not watching."
            )

        self.frames += 1
        self._last_frame_at = self.clock()

    def describe(self) -> str:
        if self.state is WatchState.WATCHING:
            return (
                f"Watching {self.category.value} for another "
                f"{self.remaining():.0f}s, {self.frames} frames so far."
            )

        if self.state is WatchState.STOPPED:
            return (
                f"Stopped watching {self.category.value} "
                f"({self.ended.value}), {self.frames} frames taken."
            )

        return f"Ready to watch {self.category.value}, not started."


def begin_watching(
    category: ActionCategory,
    started_by: str,
    summary: str,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
    min_frame_interval: float = DEFAULT_MIN_FRAME_INTERVAL,
    clock: Clock | None = None,
    audit: gate.AuditSink | None = None,
    approved: bool = False,
) -> WatchingSession:
    """
    Ask the gate for permission to start watching, and return the session.

    ``approved`` is the person's answer to the confirmation the gate asks for.
    It is a required piece of information rather than an optional one, and the
    default is no: a caller that forgets it gets a refusal, which is the
    failure that costs nothing.

    A category the gate refuses outright — ``HIDDEN_SURVEILLANCE``, say — never
    reaches the approval question at all. There is no answer to it.
    """
    request = ActionRequest(
        category=category,
        target=category.value,
        summary=summary,
        parameters={
            "started_by": started_by,
            "max_seconds": float(max_seconds),
        },
    )

    verdict = gate.evaluate(request, audit=audit)

    if verdict.refused:
        raise WatchRefused(verdict.reason)

    if verdict.needs_approval and not approved:
        raise WatchRefused(
            f"{verdict.reason} Watching did not start."
        )

    return WatchingSession(
        category=category,
        started_by=started_by,
        max_seconds=max_seconds,
        idle_seconds=idle_seconds,
        min_frame_interval=min_frame_interval,
        clock=clock if clock is not None else time.monotonic,
    )
