"""
Watching a camera: the permission, and the session that carries it.

Two things are being checked here, and they are different questions.

The **policy** questions are about the two new categories. Looking at the
screen is not a free action, and watching a camera is not a single one.

The **session** questions are about the shape that carries a grant through
time, which ``PermissionLevel`` cannot express because all five of its levels
describe how you confirm, not how long the yes lasts. Most of these tests are
about the session ending — on its own, when idle, when stopped — because a
session that only ends when somebody remembers to end it is the thing
``HIDDEN_SURVEILLANCE`` is named after.

Time is a fake clock throughout. A test that waits ten minutes to prove a
ten-minute limit is a test nobody runs.
"""

from __future__ import annotations

import unittest

from core.actions import ActionOutcome
from security import gate
from security.permissions import (
    ActionCategory,
    PermissionDecision,
    PermissionLevel,
    evaluate_action,
    get_permission_policy,
)
from security.watching import (
    DEFAULT_IDLE_SECONDS,
    DEFAULT_MAX_SECONDS,
    DEFAULT_MIN_FRAME_INTERVAL,
    WatchEnded,
    WatchRefused,
    WatchState,
    WatchingSession,
    begin_watching,
)


class FakeClock:
    """
    Time that only moves when a test moves it.

    A test that waits ten minutes to prove a ten-minute limit is a test nobody
    runs. Matches the shape the rest of the suite already uses.
    """

    def __init__(self, initial_time: float = 100.0) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


class TestTheScreenPolicy(unittest.TestCase):
    """
    Looking at the screen is not free.

    Nobody chooses what is on their screen at the moment somebody asks to look
    at it. A password manager may be open, or someone else's message.
    """

    def test_reading_the_screen_needs_a_shown_confirmation(self) -> None:
        self.assertEqual(
            get_permission_policy(ActionCategory.READ_SCREEN).level,
            PermissionLevel.UI_CONFIRMATION,
        )

    def test_a_spoken_yes_is_not_enough_for_the_screen(self) -> None:
        """
        A spoken yes is consent given without being shown what is about to be
        looked at, which is the one thing that matters here.
        """
        self.assertEqual(
            evaluate_action(ActionCategory.READ_SCREEN),
            PermissionDecision.REQUIRE_UI_CONFIRMATION,
        )

    def test_it_is_not_filed_under_reading_something_harmless(self) -> None:
        harmless = get_permission_policy(ActionCategory.READ_NON_SENSITIVE)
        screen = get_permission_policy(ActionCategory.READ_SCREEN)

        self.assertEqual(harmless.level, PermissionLevel.AUTO_ALLOW)
        self.assertGreater(screen.level, harmless.level)

    def test_nothing_on_the_machine_changes(self) -> None:
        self.assertTrue(get_permission_policy(ActionCategory.READ_SCREEN).reversible)


class TestTheCameraPolicy(unittest.TestCase):
    """Beginning to watch is the action; the frames are not."""

    def test_watching_the_camera_needs_a_shown_confirmation(self) -> None:
        self.assertEqual(
            get_permission_policy(ActionCategory.WATCH_CAMERA).level,
            PermissionLevel.UI_CONFIRMATION,
        )

    def test_hidden_surveillance_is_still_forbidden(self) -> None:
        """
        The new category must not have softened the old one. Capture with no
        indicator, or that the user did not start, is still that, and is still
        refused.
        """
        self.assertEqual(
            get_permission_policy(ActionCategory.HIDDEN_SURVEILLANCE).level,
            PermissionLevel.FORBIDDEN,
        )

    def test_there_is_still_no_session_permission_level(self) -> None:
        """
        The genuinely new idea here is the session, and the temptation was to
        make it a sixth level. It is not one: "session" is not a way of
        confirming, it is a property of the grant, and the levels are an
        ordered IntEnum that gets compared.
        """
        self.assertEqual(
            [level.name for level in PermissionLevel],
            [
                "AUTO_ALLOW",
                "VOICE_CONFIRMATION",
                "UI_CONFIRMATION",
                "TYPED_SECRET",
                "FORBIDDEN",
            ],
        )


class TestStartingToWatch(unittest.TestCase):
    """The gate decides; the session only carries what it decided."""

    def setUp(self) -> None:
        self.seen: list = []

    def begin(self, **kwargs) -> WatchingSession:
        defaults = dict(
            category=ActionCategory.WATCH_CAMERA,
            started_by="the user pressed Watch",
            summary="Watch the camera at a low frame rate.",
            audit=self.seen.append,
            approved=True,
        )
        defaults.update(kwargs)

        return begin_watching(**defaults)

    def test_an_approved_request_produces_a_session(self) -> None:
        session = self.begin()

        self.assertIs(session.state, WatchState.READY)
        self.assertIs(session.category, ActionCategory.WATCH_CAMERA)

    def test_a_session_does_not_start_itself(self) -> None:
        """
        Approval and watching are separate moments. Between them the user can
        change their mind, and nothing has been looked at.
        """
        session = self.begin()

        self.assertFalse(session.watching)
        self.assertFalse(session.indicator_visible)
        self.assertFalse(session.may_take_frame().allowed)

    def test_forgetting_to_ask_the_person_refuses(self) -> None:
        """
        The default is no. A caller that forgets the answer gets a refusal,
        which is the failure that costs nothing.
        """
        with self.assertRaises(WatchRefused):
            self.begin(approved=False)

    def test_hidden_surveillance_never_reaches_the_question(self) -> None:
        with self.assertRaises(WatchRefused) as caught:
            self.begin(
                category=ActionCategory.HIDDEN_SURVEILLANCE,
                approved=True,
            )

        self.assertIn("forbidden", str(caught.exception))

    def test_the_decision_is_recorded_either_way(self) -> None:
        """A refused request is the more interesting audit event, not less."""
        try:
            self.begin(approved=False)
        except WatchRefused:
            pass

        self.assertEqual(len(self.seen), 1)
        self.assertIs(self.seen[0].outcome, ActionOutcome.AWAITING_APPROVAL)

    def test_a_refusal_is_recorded_too(self) -> None:
        try:
            self.begin(category=ActionCategory.HIDDEN_SURVEILLANCE)
        except WatchRefused:
            pass

        self.assertEqual(len(self.seen), 1)
        self.assertIs(self.seen[0].outcome, ActionOutcome.REFUSED)

    def test_who_started_it_reaches_the_audit_trail(self) -> None:
        self.begin()

        self.assertEqual(
            self.seen[0].request.parameters["started_by"],
            "the user pressed Watch",
        )

    def test_a_session_that_cannot_say_who_started_it_is_not_a_session(
        self,
    ) -> None:
        """
        The whole distinction between watching and surveillance is that a
        person began it. A session that cannot name them cannot claim it.
        """
        with self.assertRaises(ValueError):
            WatchingSession(
                category=ActionCategory.WATCH_CAMERA,
                started_by="   ",
            )


class SessionCase(unittest.TestCase):
    """A started session on a clock that only moves when told to."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.session = WatchingSession(
            category=ActionCategory.WATCH_CAMERA,
            started_by="the user pressed Watch",
            clock=self.clock,
        )
        self.session.start()

    def take_a_frame(self) -> None:
        self.assertTrue(self.session.may_take_frame().allowed)
        self.session.took_frame()


class TestTakingFrames(SessionCase):
    def test_a_started_session_may_take_a_frame(self) -> None:
        self.assertTrue(self.session.may_take_frame().allowed)

    def test_the_indicator_is_up_while_it_is_watching(self) -> None:
        self.assertTrue(self.session.indicator_visible)
        self.assertTrue(self.session.may_take_frame().indicator_visible)

    def test_frames_are_counted(self) -> None:
        self.take_a_frame()
        self.clock.advance(DEFAULT_MIN_FRAME_INTERVAL)
        self.take_a_frame()

        self.assertEqual(self.session.frames, 2)

    def test_a_second_frame_too_soon_is_refused_without_ending_anything(
        self,
    ) -> None:
        self.take_a_frame()

        verdict = self.session.may_take_frame()

        self.assertFalse(verdict.allowed)
        self.assertTrue(self.session.watching)
        self.assertTrue(verdict.indicator_visible)

    def test_the_rate_cannot_be_turned_into_a_recording(self) -> None:
        """
        Asking faster does not get frames faster. The limit is what stops a
        watching session from being a video recorder.
        """
        self.take_a_frame()

        for _ in range(20):
            self.clock.advance(0.05)
            self.assertFalse(self.session.may_take_frame().allowed)

        self.assertEqual(self.session.frames, 1)

    def test_recording_a_frame_against_a_stopped_session_is_an_error(
        self,
    ) -> None:
        self.session.stop()

        with self.assertRaises(WatchRefused):
            self.session.took_frame()


class TestEnding(SessionCase):
    """
    The half that matters.

    Every one of these is a way a session ends that does not depend on anyone
    remembering to end it.
    """

    def test_stopping_takes_effect_on_the_very_next_frame(self) -> None:
        self.take_a_frame()
        self.session.stop()

        self.assertFalse(self.session.may_take_frame().allowed)

    def test_stopping_takes_the_indicator_down(self) -> None:
        self.session.stop()

        self.assertFalse(self.session.indicator_visible)
        self.assertFalse(self.session.may_take_frame().indicator_visible)

    def test_stopping_twice_is_not_an_error(self) -> None:
        """
        Stopping is the thing that must always work — from a signal handler, a
        window closing, or a second click on a button that already stopped it.
        """
        self.session.stop()
        self.session.stop()

        self.assertIs(self.session.ended, WatchEnded.STOPPED_BY_USER)

    def test_it_ends_on_its_own_at_the_time_limit(self) -> None:
        self.clock.advance(DEFAULT_MAX_SECONDS)

        verdict = self.session.may_take_frame()

        self.assertFalse(verdict.allowed)
        self.assertIs(self.session.ended, WatchEnded.REACHED_TIME_LIMIT)

    def test_it_ends_even_while_frames_are_being_taken(self) -> None:
        """
        Not an idle timeout in disguise. A session in constant use still stops
        at its limit, because the limit is on how long consent lasts and not
        on how busy it was.
        """
        while self.session.may_take_frame().allowed:
            self.session.took_frame()
            self.clock.advance(DEFAULT_MIN_FRAME_INTERVAL)

        self.assertIs(self.session.ended, WatchEnded.REACHED_TIME_LIMIT)
        self.assertGreater(self.session.frames, 100)

    def test_it_ends_when_nothing_has_asked_for_a_frame(self) -> None:
        """A session nobody takes frames from is being left on, not used."""
        self.clock.advance(DEFAULT_IDLE_SECONDS)

        self.assertFalse(self.session.may_take_frame().allowed)
        self.assertIs(self.session.ended, WatchEnded.WENT_IDLE)

    def test_the_time_limit_wins_over_the_idle_limit(self) -> None:
        """
        Both are past. The one reported should be the one that says consent
        expired, not the one that says nobody was looking.
        """
        self.clock.advance(DEFAULT_MAX_SECONDS + DEFAULT_IDLE_SECONDS)

        self.session.may_take_frame()

        self.assertIs(self.session.ended, WatchEnded.REACHED_TIME_LIMIT)

    def test_an_ended_session_cannot_be_started_again(self) -> None:
        """Starting again is a new grant, and needs to be asked for again."""
        self.session.stop()

        with self.assertRaises(WatchRefused):
            self.session.start()

    def test_the_time_left_is_reported_and_never_goes_negative(self) -> None:
        self.clock.advance(DEFAULT_MAX_SECONDS / 2)
        self.assertAlmostEqual(self.session.remaining(), DEFAULT_MAX_SECONDS / 2)

        self.clock.advance(DEFAULT_MAX_SECONDS)
        self.assertEqual(self.session.remaining(), 0.0)

    def test_a_stopped_session_says_why_it_stopped(self) -> None:
        self.clock.advance(DEFAULT_MAX_SECONDS)
        self.session.may_take_frame()

        self.assertIn("reached_time_limit", self.session.describe())


class TestNonsensicalSessions(unittest.TestCase):
    """Limits that are not limits are refused at construction."""

    def make(self, **kwargs) -> WatchingSession:
        defaults = dict(
            category=ActionCategory.WATCH_CAMERA,
            started_by="the user",
        )
        defaults.update(kwargs)

        return WatchingSession(**defaults)

    def test_a_session_that_cannot_run_is_refused(self) -> None:
        for seconds in (0.0, -1.0):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    self.make(max_seconds=seconds)

    def test_an_idle_limit_of_zero_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.make(idle_seconds=0.0)

    def test_a_negative_frame_interval_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.make(min_frame_interval=-1.0)

    def test_an_unlimited_frame_rate_is_allowed_but_must_be_asked_for(
        self,
    ) -> None:
        # Zero is legal — a caller doing its own pacing — but it is not the
        # default, and the default is what almost everything will use.
        self.assertGreater(DEFAULT_MIN_FRAME_INTERVAL, 0)
        self.assertEqual(self.make(min_frame_interval=0.0).min_frame_interval, 0)


class TestTheGateIsNotBypassed(unittest.TestCase):
    """
    The session grants nothing by itself.

    Constructing one directly is possible, and this says what that does and
    does not get you.
    """

    def test_a_directly_built_session_still_asks_before_every_frame(
        self,
    ) -> None:
        session = WatchingSession(
            category=ActionCategory.WATCH_CAMERA,
            started_by="a test",
        )

        self.assertFalse(session.may_take_frame().allowed)

    def test_the_gate_records_the_start_through_the_default_sink(self) -> None:
        seen: list = []
        previous = gate.set_default_audit_sink(seen.append)
        self.addCleanup(gate.set_default_audit_sink, previous)

        begin_watching(
            category=ActionCategory.WATCH_CAMERA,
            started_by="the user",
            summary="Watch the camera.",
            approved=True,
        )

        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
