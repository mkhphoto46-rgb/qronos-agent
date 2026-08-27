from __future__ import annotations

import unittest

from core.web_friction import (
    COOLOFF_LADDER,
    PERMANENT_RETRY_SECONDS,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    FrictionMemory,
    FrictionSignal,
    cooloff_for,
)


START = 1_800_000_000.0
URL = "https://walled.example.com/article"
OTHER = "https://friendly.example.org/page"


class FakeClock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestCooloffLadder(unittest.TestCase):
    def test_no_refusals_means_no_cooloff(self) -> None:
        self.assertEqual(cooloff_for(0), 0.0)

    def test_ladder_escalates(self) -> None:
        # A first refusal is often transient; a fourth is not.
        first = cooloff_for(1)
        second = cooloff_for(2)
        third = cooloff_for(3)

        self.assertLess(first, second)
        self.assertLess(second, third)

    def test_ladder_values_match_the_plan(self) -> None:
        self.assertEqual(cooloff_for(1), 6 * SECONDS_PER_HOUR)
        self.assertEqual(cooloff_for(2), 3 * SECONDS_PER_DAY)
        self.assertEqual(cooloff_for(3), 30 * SECONDS_PER_DAY)

    def test_past_the_ladder_is_a_monthly_revisit(self) -> None:
        for count in (4, 5, 50):
            with self.subTest(count=count):
                self.assertEqual(
                    cooloff_for(count),
                    PERMANENT_RETRY_SECONDS,
                )

    def test_ladder_is_strictly_increasing(self) -> None:
        self.assertEqual(list(COOLOFF_LADDER), sorted(COOLOFF_LADDER))


class FrictionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.memory = FrictionMemory(path=":memory:", clock=self.clock)

    def tearDown(self) -> None:
        self.memory.close()


class TestRecording(FrictionTestCase):
    def test_an_unknown_domain_is_not_skipped(self) -> None:
        self.assertFalse(self.memory.should_skip(URL))
        self.assertIsNone(self.memory.get(URL))

    def test_a_refusal_starts_a_cooloff(self) -> None:
        self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        self.assertTrue(self.memory.should_skip(URL))

    def test_the_cooloff_expires(self) -> None:
        self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        self.clock.advance(6 * SECONDS_PER_HOUR + 1)

        self.assertFalse(self.memory.should_skip(URL))

    def test_repeat_refusals_escalate(self) -> None:
        self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)
        self.clock.advance(7 * SECONDS_PER_HOUR)

        record = self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        assert record is not None
        self.assertEqual(record.refusal_count, 2)
        self.assertAlmostEqual(
            record.remaining_cooloff(self.clock()),
            3 * SECONDS_PER_DAY,
        )

    def test_the_signal_is_remembered(self) -> None:
        self.memory.record_refusal(URL, FrictionSignal.LOGIN_WALL)

        record = self.memory.get(URL)

        assert record is not None
        self.assertEqual(record.last_signal, "login_wall")

    def test_skip_list_after_the_ladder_runs_out(self) -> None:
        for _ in range(4):
            self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        record = self.memory.get(URL)

        assert record is not None
        self.assertTrue(record.is_on_skip_list)

    def test_success_clears_the_history_entirely(self) -> None:
        # A site that works now is a site that works. Carrying old refusals
        # forward would keep punishing it for an outage that is over.
        for _ in range(3):
            self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        self.memory.record_success(URL)

        record = self.memory.get(URL)

        assert record is not None
        self.assertEqual(record.refusal_count, 0)
        self.assertFalse(self.memory.should_skip(URL))

    def test_after_a_success_the_ladder_restarts_from_the_bottom(self) -> None:
        for _ in range(3):
            self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        self.memory.record_success(URL)

        record = self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        assert record is not None
        self.assertEqual(record.refusal_count, 1)


class TestScoping(FrictionTestCase):
    def test_refusals_are_per_domain(self) -> None:
        self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        self.assertTrue(self.memory.should_skip(URL))
        self.assertFalse(self.memory.should_skip(OTHER))

    def test_different_paths_on_one_domain_share_a_record(self) -> None:
        # The wall belongs to the site, not the article.
        self.memory.record_refusal(
            "https://walled.example.com/a",
            FrictionSignal.CAPTCHA,
        )

        self.assertTrue(
            self.memory.should_skip("https://walled.example.com/b")
        )

    def test_www_prefix_does_not_create_a_second_record(self) -> None:
        self.memory.record_refusal(
            "https://www.walled.example.com/a",
            FrictionSignal.CAPTCHA,
        )

        self.assertTrue(self.memory.should_skip(URL))
        self.assertEqual(self.memory.record_count(), 1)

    def test_an_unparseable_url_is_skipped(self) -> None:
        # A path Qronos cannot even name is not one it should be opening.
        self.assertTrue(self.memory.should_skip("not a url"))

    def test_recording_against_an_unparseable_url_is_a_no_op(self) -> None:
        self.assertIsNone(
            self.memory.record_refusal("not a url", FrictionSignal.CAPTCHA)
        )
        self.assertEqual(self.memory.record_count(), 0)


class TestFiltering(FrictionTestCase):
    def test_cooling_off_urls_are_dropped(self) -> None:
        self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        remaining = self.memory.filter_urls((URL, OTHER))

        self.assertEqual(remaining, (OTHER,))

    def test_order_is_preserved(self) -> None:
        urls = (
            "https://a.example.com/1",
            "https://b.example.com/2",
            "https://c.example.com/3",
        )

        self.memory.record_refusal(urls[1], FrictionSignal.CAPTCHA)

        self.assertEqual(
            self.memory.filter_urls(urls),
            (urls[0], urls[2]),
        )

    def test_cooling_off_hosts_are_listable(self) -> None:
        self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        self.assertIn("walled.example.com", self.memory.cooling_off_hosts())

    def test_expired_records_do_not_appear_as_cooling_off(self) -> None:
        self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        self.clock.advance(7 * SECONDS_PER_HOUR)

        self.assertEqual(self.memory.cooling_off_hosts(), ())


class TestMaintenance(FrictionTestCase):
    def test_forget_removes_a_record(self) -> None:
        self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)

        self.memory.forget(URL)

        self.assertIsNone(self.memory.get(URL))

    def test_clear_removes_everything(self) -> None:
        self.memory.record_refusal(URL, FrictionSignal.CAPTCHA)
        self.memory.record_refusal(OTHER, FrictionSignal.FORBIDDEN)

        self.memory.clear()

        self.assertEqual(self.memory.record_count(), 0)

    def test_records_persist_across_instances_on_one_file(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "friction.sqlite3"
            clock = FakeClock()

            first = FrictionMemory(path=path, clock=clock)
            first.record_refusal(URL, FrictionSignal.CAPTCHA)
            first.close()

            second = FrictionMemory(path=path, clock=clock)

            self.assertTrue(second.should_skip(URL))
            second.close()


if __name__ == "__main__":
    unittest.main()
