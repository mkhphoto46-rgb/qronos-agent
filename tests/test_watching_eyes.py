"""
Watching something move, with no screen, no camera and no model.

The frame source and the describe function are both injected, and so is
``sleep`` — a watching session runs for minutes by design, and a test that
honours that honestly is a test nobody runs.

Almost every test here is about **stopping**. Taking frames is the easy half;
a loop that keeps taking them after the time limit, after the idle limit or
after somebody pressed stop looks exactly like a loop that does not, right up
until it does not stop.
"""

from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image

from core.screen_capture import CaptureRefused, CaptureUnavailable
from core.vision_image import prepare_bytes
from core.watching_eyes import (
    Eyes,
    FrameSource,
    Observation,
    ScreenRegionSource,
    WatchSummary,
    camera_available,
)
from security.permissions import ActionCategory
from security.watching import WatchEnded, WatchingSession


def a_picture():
    buffer = BytesIO()

    Image.new("RGB", (64, 48), (12, 34, 56)).save(buffer, format="PNG")

    return prepare_bytes(buffer.getvalue())


class MovingClock:
    """
    Time that only moves when something moves it.

    Starting at zero on purpose. The tests in ``test_watching_session.py``
    start at 100, and that is exactly why they could not catch a session that
    treated a clock reading of zero as "never started" and therefore never
    expired. See :class:`TestZeroIsARealTime`.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingSource(FrameSource):
    """A source that always works and says how often it was asked."""

    def __init__(self) -> None:
        self.reads = 0
        self.closed = 0

    def available(self) -> bool:
        return True

    def read_frame(self):
        self.reads += 1

        return a_picture()

    def close(self) -> None:
        self.closed += 1


class BrokenSource(CountingSource):
    def read_frame(self):
        self.reads += 1

        raise CaptureUnavailable("The window closed.")


class EyesCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MovingClock()
        self.source = CountingSource()
        self.described: list[str] = []

    def session(self, **kwargs) -> WatchingSession:
        defaults = dict(
            category=ActionCategory.WATCH_CAMERA,
            started_by="a test",
            max_seconds=10.0,
            idle_seconds=8.0,
            min_frame_interval=2.0,
            clock=self.clock,
        )
        defaults.update(kwargs)

        return WatchingSession(**defaults)

    def describe(self, answer: str = "a blue rectangle"):
        def describe_fn(question, images):
            self.described.append(question)

            return answer

        return describe_fn

    def eyes(self, session=None, source=None, **kwargs) -> Eyes:
        return Eyes(
            session=session or self.session(),
            source=source or self.source,
            describe_fn=kwargs.pop("describe_fn", None) or self.describe(),
            sleep=self.clock.advance,
            **kwargs,
        )


class TestWatching(EyesCase):
    def test_it_takes_frames_at_the_session_s_pace(self) -> None:
        summary = self.eyes().watch()

        self.assertEqual(
            [round(o.at, 1) for o in summary.observations],
            [0.0, 2.0, 4.0, 6.0, 8.0],
        )

    def test_each_frame_is_described(self) -> None:
        summary = self.eyes().watch()

        for observation in summary.observations:
            self.assertEqual(observation.description, "a blue rectangle")

    def test_frames_are_numbered_from_one(self) -> None:
        summary = self.eyes().watch()

        self.assertEqual(
            [o.number for o in summary.observations],
            list(range(1, len(summary.observations) + 1)),
        )

    def test_a_watcher_can_be_told_as_each_frame_arrives(self) -> None:
        """
        Waiting for a ten-minute session to finish before saying anything
        would make the feature useless.
        """
        seen: list[Observation] = []

        self.eyes(on_observation=seen.append).watch()

        self.assertEqual(len(seen), 5)

    def test_the_generator_hands_them_over_one_at_a_time(self) -> None:
        produced = []

        for observation in self.eyes().frames():
            produced.append(observation)

            # The source has been asked exactly as many times as frames have
            # come out, which is what "one at a time" means.
            self.assertEqual(self.source.reads, len(produced))

    def test_the_question_reaches_the_model(self) -> None:
        self.eyes().watch(question="Is anybody there?")

        self.assertEqual(set(self.described), {"Is anybody there?"})

    def test_it_starts_a_session_that_has_not_been_started(self) -> None:
        session = self.session()

        self.eyes(session=session).watch()

        self.assertIsNot(session.ended, WatchEnded.NOT_ENDED)

    def test_a_caller_can_cap_the_number_of_frames(self) -> None:
        summary = self.eyes().watch(max_frames=2)

        self.assertEqual(summary.frames, 2)


class TestStopping(EyesCase):
    """The half that matters."""

    def test_it_stops_at_the_session_s_time_limit(self) -> None:
        summary = self.eyes().watch()

        self.assertIs(summary.ended, WatchEnded.REACHED_TIME_LIMIT)

    def test_the_source_is_released_however_it_ends(self) -> None:
        self.eyes().watch()

        self.assertEqual(self.source.closed, 1)

    def test_the_source_is_released_when_the_caller_stops_consuming(
        self,
    ) -> None:
        """
        A generator abandoned half way still runs its cleanup, and the
        indicator still comes down.
        """
        session = self.session()
        frames = self.eyes(session=session).frames()

        next(frames)
        frames.close()

        self.assertEqual(self.source.closed, 1)
        self.assertFalse(session.indicator_visible)

    def test_the_indicator_comes_down_at_the_end(self) -> None:
        session = self.session()

        self.eyes(session=session).watch()

        self.assertFalse(session.indicator_visible)

    def test_a_frame_cap_stops_the_session_rather_than_leaving_it_open(
        self,
    ) -> None:
        """
        Otherwise a session with a frame cap would keep its indicator up and
        its permission alive with nothing watching.
        """
        session = self.session()

        self.eyes(session=session).watch(max_frames=1)

        self.assertFalse(session.watching)

    def test_a_source_that_fails_ends_the_session(self) -> None:
        source = BrokenSource()

        summary = self.eyes(source=source).watch()

        self.assertIs(summary.ended, WatchEnded.FAILED)
        self.assertEqual(summary.frames, 0)

    def test_a_source_refused_by_the_session_ends_it_too(self) -> None:
        class Refused(CountingSource):
            def read_frame(self):
                raise CaptureRefused("The session ended.")

        summary = self.eyes(source=Refused()).watch()

        self.assertIs(summary.ended, WatchEnded.FAILED)

    def test_a_model_that_fails_does_not_end_the_session(self) -> None:
        """
        One frame that could not be described is not the end of a watching
        session — the next may work, and saying so is more useful than
        stopping.
        """

        def explodes(question, images):
            raise RuntimeError("Ollama fell over.")

        summary = self.eyes(describe_fn=explodes).watch()

        self.assertIs(summary.ended, WatchEnded.REACHED_TIME_LIMIT)
        self.assertGreater(summary.frames, 1)
        self.assertIn("could not be described", summary.observations[0].description)

    def test_it_stops_when_the_session_goes_idle(self) -> None:
        """
        A caller that stops asking is a session nobody is watching through.
        """
        session = self.session(max_seconds=1000.0, idle_seconds=5.0)
        frames = self.eyes(session=session).frames()

        next(frames)
        self.clock.advance(5.0)

        with self.assertRaises(StopIteration):
            next(frames)

        self.assertIs(session.ended, WatchEnded.WENT_IDLE)

    def test_stopping_mid_session_takes_effect_on_the_next_frame(self) -> None:
        session = self.session()
        frames = self.eyes(session=session).frames()

        next(frames)
        session.stop()

        with self.assertRaises(StopIteration):
            next(frames)

        self.assertEqual(self.source.reads, 1)

    def test_waiting_never_runs_past_the_end_of_the_session(self) -> None:
        """
        A session with two seconds left and a ten-second interval still ends in
        two seconds, rather than sleeping through its own expiry.
        """
        session = self.session(max_seconds=3.0, min_frame_interval=60.0)

        self.eyes(session=session).watch()

        self.assertLessEqual(self.clock.now, 4.0)
        self.assertIs(session.ended, WatchEnded.REACHED_TIME_LIMIT)


class TestZeroIsARealTime(unittest.TestCase):
    """
    The bug this file found, kept as its own test so it cannot come back.

    ``self._started_at or now`` reads a clock value of zero as "not started",
    which makes the elapsed time zero for ever and the session immortal. It
    survived a full suite of session tests because every one of them used a
    fake clock starting at 100 — a fixture that could not express the failure
    it was written to catch, which is the same lesson the queue work learned.
    """

    def session(self, clock) -> WatchingSession:
        return WatchingSession(
            category=ActionCategory.WATCH_CAMERA,
            started_by="a test",
            max_seconds=5.0,
            idle_seconds=4.0,
            min_frame_interval=1.0,
            clock=clock,
        )

    def test_a_session_started_at_zero_still_expires(self) -> None:
        clock = MovingClock(0.0)
        session = self.session(clock)
        session.start()

        clock.advance(5.0)

        self.assertFalse(session.may_take_frame().allowed)
        self.assertIs(session.ended, WatchEnded.REACHED_TIME_LIMIT)

    def test_a_session_started_at_zero_still_goes_idle(self) -> None:
        clock = MovingClock(0.0)
        session = self.session(clock)
        session.start()

        clock.advance(4.0)

        self.assertFalse(session.may_take_frame().allowed)
        self.assertIs(session.ended, WatchEnded.WENT_IDLE)

    def test_a_frame_taken_at_zero_still_paces_the_next(self) -> None:
        clock = MovingClock(0.0)
        session = self.session(clock)
        session.start()
        session.took_frame()

        self.assertFalse(session.may_take_frame().allowed)
        self.assertTrue(session.watching)


class TestTheScreenAsASource(unittest.TestCase):
    """
    Frames from a window, taken under the session rather than the gate.

    A session is one decision covering many frames. Asking the gate again per
    frame would not be more careful; it would be a confirmation dialog every
    two seconds, which people learn to dismiss without reading.
    """

    def setUp(self) -> None:
        from core.screen_capture import DisplayGeometry, ScreenCapture

        self.asked: list = []

        def busy(width, height):
            return bytes(
                byte
                for index in range(width * height)
                for byte in (
                    index % 251,
                    (index * 7) % 241,
                    (index * 13) % 239,
                    0,
                )
            )

        self.capture = ScreenCapture(
            grab=lambda window: (
                self.asked.append(window) or (busy(64, 48), 64, 48)
            ),
            geometry_fn=lambda: DisplayGeometry(64, 48, 64, 48),
        )

        self.clock = MovingClock()
        self.session = WatchingSession(
            category=ActionCategory.WATCH_CAMERA,
            started_by="a test",
            max_seconds=10.0,
            idle_seconds=8.0,
            min_frame_interval=2.0,
            clock=self.clock,
        )

    def test_a_frame_comes_from_the_named_window(self) -> None:
        source = ScreenRegionSource(self.session, window=99, capture=self.capture)

        self.session.start()
        source.read_frame()

        self.assertEqual(self.asked, [99])

    def test_the_session_counts_the_frame(self) -> None:
        source = ScreenRegionSource(self.session, capture=self.capture)

        self.session.start()
        source.read_frame()

        self.assertEqual(self.session.frames, 1)

    def test_a_session_that_has_not_started_takes_nothing(self) -> None:
        source = ScreenRegionSource(self.session, capture=self.capture)

        with self.assertRaises(CaptureRefused):
            source.read_frame()

        self.assertEqual(self.asked, [])

    def test_a_stopped_session_takes_nothing(self) -> None:
        source = ScreenRegionSource(self.session, capture=self.capture)

        self.session.start()
        self.session.stop()

        with self.assertRaises(CaptureRefused):
            source.read_frame()

        self.assertEqual(self.asked, [])

    def test_an_expired_session_takes_nothing(self) -> None:
        source = ScreenRegionSource(self.session, capture=self.capture)

        self.session.start()
        self.clock.advance(10.0)

        with self.assertRaises(CaptureRefused):
            source.read_frame()

        self.assertEqual(self.asked, [])

    def test_it_says_what_it_is_watching(self) -> None:
        whole = ScreenRegionSource(self.session, capture=self.capture)
        one = ScreenRegionSource(self.session, window=99, capture=self.capture)

        self.assertIn("whole screen", whole.describe())
        self.assertIn("one window", one.describe())


class TestNothingIsKept(EyesCase):
    """
    What survives a watching session is sentences, not pictures.

    A session that left frames behind would be a recording made without
    anybody deciding to make one.
    """

    def test_an_observation_holds_a_description_of_the_picture(self) -> None:
        summary = self.eyes().watch()

        for observation in summary.observations:
            self.assertIsInstance(observation.picture, str)
            self.assertIn("64x48", observation.picture)

    def test_no_observation_holds_any_pixels(self) -> None:
        summary = self.eyes().watch()

        for observation in summary.observations:
            for value in vars(observation).values():
                self.assertNotIsInstance(value, bytes)


class TestTheCameraIsNotPretendedToExist(unittest.TestCase):
    """
    A driver that has never produced a frame is a promise, not a capability.

    This is the same failing the desktop's permission screen was pulled up for
    — offering "Camera / Webcam" with nothing behind it — and it would be worse
    to repeat it here, in the code, than to leave it undone and say so.
    """

    def test_the_camera_source_reports_itself_unavailable(self) -> None:
        self.assertFalse(camera_available())


class TestSummaries(EyesCase):
    def test_a_summary_counts_its_frames(self) -> None:
        self.assertEqual(self.eyes().watch().frames, 5)

    def test_a_summary_says_why_it_ended(self) -> None:
        self.assertIn("time_limit", self.eyes().watch().describe())

    def test_one_frame_is_not_described_as_frames(self) -> None:
        summary = WatchSummary(
            observations=(Observation(1, 0.0, "a thing"),),
            ended=WatchEnded.STOPPED_BY_USER,
        )

        self.assertIn("1 frame watched", summary.describe())


if __name__ == "__main__":
    unittest.main()
