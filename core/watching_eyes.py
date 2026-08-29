"""
Watching something move, a frame at a time, while it is visibly happening.

:mod:`security.watching` decides *whether* watching may happen and for how
long. This decides *what* is watched and *what comes of it*: a source of
frames, a model that says what is in one, and a loop that asks the session's
permission before every single frame rather than once at the start.

That last part is the whole design. A loop that checked at the beginning would
keep taking frames after the time limit, after the idle limit and after
somebody pressed stop, and would look exactly like a loop that did not — right
up until it did not stop.

**What is watched.** A frame source is anything that can produce a picture on
demand. Two are worth having:

    A region of the screen, which is built and tested here. It is what makes
    "watch that video and tell me when the build finishes" work, and it is
    also how the camera path was exercised on a machine with no camera.

    A camera device, which is **not built**. There is no camera on the machine
    this was developed on, and shipping a driver that has never produced a
    frame would be promising a capability rather than having one — the same
    thing the desktop's permission screen was criticised for. The seam it
    plugs into is here and is exercised; the twenty lines that talk to Windows
    Media Capture are not written, and :func:`camera_available` says so.

**What comes of it.** Every frame is described and only the description is
kept. Frames are held in memory, sent, and dropped — nothing accumulates, and
what a person can look back at afterwards is a list of sentences rather than a
recording. That is not a saving, it is the point: a watching session that left
frames behind would be a recording made without anybody deciding to make one.

**The model is reloaded for every frame, deliberately.** Measured against real
footage: 0 of 23 frames found it still on the card, so about 2.2 s of every
4.2 s is spent loading a model that is about to be asked the same question
again. Holding it for the length of the session would roughly halve that, and
would arguably be within the rules — a session is one bounded operation the
user can see and stop, not the idle keep-alive the no-residency rule was
written against.

It is not done, and that is a decision rather than an oversight. Watching costs
3.3 GB of somebody's graphics card, and a frame every four seconds already
answers what a camera session is asked: is somebody there, are they facing the
screen. Qronos yields resources rather than reserving them, and this is a place
where the cheap two seconds is not worth the card. See ``docs/qronos_vision.md``
for the measurements and for what revisiting it would involve.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

from core.screen_capture import (
    CaptureRefused,
    CaptureUnavailable,
    ScreenCapture,
    available as screen_available,
)
from core.vision_image import PreparedImage
from security.watching import WatchEnded, WatchingSession


@dataclass(frozen=True)
class Observation:
    """One frame, described. The frame itself is already gone."""

    number: int
    at: float
    description: str

    #: What the picture was, for the record. Never the picture.
    picture: str = ""

    def describe(self) -> str:
        return f"frame {self.number} at {self.at:.1f}s: {self.description}"


@dataclass(frozen=True)
class WatchSummary:
    """What a whole watching session came to."""

    observations: tuple[Observation, ...]
    ended: WatchEnded
    reason: str = ""

    @property
    def frames(self) -> int:
        return len(self.observations)

    def describe(self) -> str:
        return (
            f"{self.frames} frame{'s' if self.frames != 1 else ''} watched, "
            f"stopped because {self.ended.value}"
        )


class FrameSource(ABC):
    """Something that can produce a picture on demand."""

    @abstractmethod
    def available(self) -> bool:
        """True when a frame could be taken. Takes none, starts nothing."""

    @abstractmethod
    def read_frame(self) -> PreparedImage:
        """One frame, now, ready to send to a model."""

    def describe(self) -> str:
        return type(self).__name__

    def close(self) -> None:
        """Release anything held. Called however the session ends."""


class ScreenRegionSource(FrameSource):
    """
    Frames from one window on the screen.

    The permission was granted to the session, so frames are taken under it
    rather than each asking the gate again. That is what a session *is*: one
    decision covering many frames, bounded in time and visible while it runs.
    """

    def __init__(
        self,
        session: WatchingSession,
        window: int | None = None,
        capture: ScreenCapture | None = None,
    ) -> None:
        self.session = session
        self.window = window
        self.capture = capture if capture is not None else ScreenCapture()

    def available(self) -> bool:
        return self.capture.health_check()

    def read_frame(self) -> PreparedImage:
        return self.capture.capture_under(
            self.session, window=self.window
        ).image

    def describe(self) -> str:
        if self.window:
            return "one window on the screen"

        return "the whole screen"


def camera_available() -> bool:
    """
    Whether a camera can be watched.

    Always False, and honestly so. The frame source for a camera device is not
    written: there was no camera on the machine this was developed on, and a
    driver that has never produced a frame is a promise rather than a
    capability. Everything it would plug into is here and is tested through
    :class:`ScreenRegionSource`.
    """
    return False


#: What a frame is asked, and the order is the point.
#:
#: An earlier version put "whether a person is visible and facing the camera"
#: at the end of a general request to describe the frame, and the model
#: answered it about seven times in ten — it is a request for one or two
#: sentences, and the last clause is the one that gets dropped. Measured across
#: runs against real footage: 21 of 21, 20 of 21, then 15 of 21.
#:
#: Asking for the two facts *first*, as their own question, is the fix. They
#: are also the two a camera is watched for at all: is somebody there, and are
#: they facing you. Everything else is context.
WATCH_INSTRUCTION = (
    "This is one frame from a live view. Answer in this order:\n"
    "1. Is a person visible? Say yes or no.\n"
    "2. If yes, are they facing the camera or turned away? Say which.\n"
    "3. Then, in one sentence, what else is in the frame.\n"
    "Be factual and brief. Do not guess at anything you cannot see."
)


class Eyes:
    """
    A watching session, a source of frames, and something that describes them.

    Nothing starts until :meth:`frames` or :meth:`watch` is called, and
    everything stops when it returns — including when it returns because an
    exception went through it.

    ``sleep`` is injected so that a test can prove the pacing without waiting
    for it. A watching session runs for minutes by design, and a test that
    honours that honestly is a test nobody runs.
    """

    def __init__(
        self,
        session: WatchingSession,
        source: FrameSource,
        describe_fn: Callable[[str, Sequence[PreparedImage]], str],
        on_observation: Callable[[Observation], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session
        self.source = source
        self.describe_fn = describe_fn
        self.on_observation = on_observation
        self.sleep = sleep

    def health_check(self) -> bool:
        """True when watching could begin. Starts nothing, asks nobody."""
        return self.source.available()

    def frames(
        self,
        question: str = WATCH_INSTRUCTION,
        max_frames: int | None = None,
    ) -> Iterator[Observation]:
        """
        Observations, one at a time, until the session ends.

        A generator rather than a list, because a person watching something
        happen wants to see each frame described as it arrives — waiting for a
        ten-minute session to finish before saying anything would make the
        feature useless.

        The session is asked before **every** frame. A loop that asked once at
        the start would keep going past the time limit, past the idle limit and
        past somebody pressing stop, and would look exactly like a loop that
        did not — right up until it did not stop.
        """
        if not self.session.watching:
            self.session.start()

        started = self.session.clock()
        taken = 0

        try:
            while True:
                if max_frames is not None and taken >= max_frames:
                    return

                verdict = self.session.may_take_frame()

                if not verdict.allowed:
                    if not self.session.watching:
                        # Over: the limit passed, it went idle, or it was
                        # stopped. The reason is on the session.
                        return

                    # Not over, just too soon. Wait out the rest of the
                    # interval rather than spinning, and ask again — because
                    # while waiting, the session may end.
                    self.sleep(self._wait_for())
                    continue

                observation = self._one_frame(question, taken + 1, started)

                if observation is None:
                    self.session.stop(WatchEnded.FAILED)
                    return

                self.session.took_frame()
                taken += 1

                if self.on_observation is not None:
                    self.on_observation(observation)

                yield observation

        finally:
            # However this ended — limit, stop, failure, an exception on its
            # way past, or a caller that stopped consuming — the source is
            # released and the indicator comes down.
            self.source.close()

            if self.session.watching:
                self.session.stop(WatchEnded.STOPPED_BY_USER)

    def watch(
        self,
        question: str = WATCH_INSTRUCTION,
        max_frames: int | None = None,
    ) -> WatchSummary:
        """
        The same, run to the end, with everything it saw.

        ``max_frames`` is a second limit for a caller that wants one, and never
        a substitute for the session's own: the session ends on time, on
        idleness and on being stopped, and a frame count knows about none of
        those.
        """
        observations = tuple(self.frames(question, max_frames))

        return WatchSummary(
            observations=observations,
            ended=self.session.ended,
            reason=self.session.may_take_frame().reason,
        )

    def _wait_for(self) -> float:
        """
        How long to wait before asking again.

        Never longer than the session has left, so a session with two seconds
        remaining and a ten-second interval still ends in two seconds.
        """
        return max(0.05, min(self.session.min_frame_interval, self.session.remaining()))

    def _one_frame(
        self,
        question: str,
        number: int,
        started: float,
    ) -> Observation | None:
        try:
            frame = self.source.read_frame()
        except (CaptureUnavailable, CaptureRefused, OSError):
            return None

        try:
            description = self.describe_fn(question, (frame,))
        except Exception as error:
            # One frame that could not be described is not the end of a
            # watching session. The next one may work, and saying so is more
            # useful than stopping.
            description = f"This frame could not be described: {error}"

        return Observation(
            number=number,
            at=self.session.clock() - started,
            description=(description or "").strip(),
            picture=frame.describe(),
        )


def screen_is_watchable() -> bool:
    """True when the screen can be used as a source of frames."""
    return screen_available()
