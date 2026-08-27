from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.web_rate_limit import (
    DUCKDUCKGO_BUCKET,
    BucketConfig,
    BucketState,
    InMemoryStateStore,
    JsonFileStateStore,
    RateLimiter,
)


START = 1_800_000_000.0


class FakeClock:
    """A clock the test drives, so nothing has to wait four minutes."""

    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestBucketConfig(unittest.TestCase):
    def test_rejects_zero_capacity(self) -> None:
        with self.assertRaises(ValueError):
            BucketConfig(name="x", capacity=0, refill_seconds=10.0)

    def test_rejects_non_positive_refill(self) -> None:
        with self.assertRaises(ValueError):
            BucketConfig(name="x", capacity=1, refill_seconds=0.0)

    def test_duckduckgo_bucket_matches_the_measured_limit(self) -> None:
        # Measured 2026-08-27 from two countries: two requests succeed, the
        # third returns zero results, recovery takes about four minutes.
        self.assertEqual(DUCKDUCKGO_BUCKET.capacity, 2)
        self.assertEqual(DUCKDUCKGO_BUCKET.refill_seconds, 120.0)
        self.assertEqual(DUCKDUCKGO_BUCKET.penalty_seconds, 240.0)


class TestBucketBehaviour(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.limiter = RateLimiter(clock=self.clock)

    def test_two_searches_succeed_then_the_third_is_refused(self) -> None:
        # The measured envelope, reproduced exactly.
        self.assertTrue(self.limiter.acquire("duckduckgo").allowed)
        self.assertTrue(self.limiter.acquire("duckduckgo").allowed)
        self.assertFalse(self.limiter.acquire("duckduckgo").allowed)

    def test_refusal_reports_when_to_retry(self) -> None:
        self.limiter.acquire("duckduckgo")
        self.limiter.acquire("duckduckgo")

        verdict = self.limiter.acquire("duckduckgo")

        self.assertGreater(verdict.retry_after_seconds, 0.0)
        self.assertLessEqual(verdict.retry_after_seconds, 120.0)

    def test_a_token_returns_after_the_refill_interval(self) -> None:
        self.limiter.acquire("duckduckgo")
        self.limiter.acquire("duckduckgo")

        self.clock.advance(120.0)

        self.assertTrue(self.limiter.acquire("duckduckgo").allowed)

    def test_tokens_do_not_accumulate_past_capacity(self) -> None:
        # An idle hour must not bank thirty searches to fire at once — that
        # burst is exactly what the provider refuses.
        self.clock.advance(3_600.0)

        self.assertTrue(self.limiter.acquire("duckduckgo").allowed)
        self.assertTrue(self.limiter.acquire("duckduckgo").allowed)
        self.assertFalse(self.limiter.acquire("duckduckgo").allowed)

    def test_check_does_not_consume_a_token(self) -> None:
        for _ in range(5):
            self.assertTrue(self.limiter.check("duckduckgo").allowed)

        self.assertTrue(self.limiter.acquire("duckduckgo").allowed)
        self.assertTrue(self.limiter.acquire("duckduckgo").allowed)
        self.assertFalse(self.limiter.check("duckduckgo").allowed)

    def test_an_unlimited_provider_is_always_allowed(self) -> None:
        # Wikipedia and Stack Exchange have their own generous quotas. Treating
        # them as scarce would push work onto the one provider that is.
        for _ in range(50):
            self.assertTrue(self.limiter.acquire("wikipedia_fa").allowed)

        self.assertFalse(self.limiter.is_limited("wikipedia_fa"))

    def test_a_fresh_bucket_starts_full(self) -> None:
        verdict = self.limiter.check("duckduckgo")

        self.assertEqual(verdict.tokens_remaining, 2.0)


class TestRefusalPenalty(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.limiter = RateLimiter(clock=self.clock)

    def test_a_refusal_empties_the_bucket(self) -> None:
        self.limiter.record_refusal("duckduckgo")

        self.assertFalse(self.limiter.check("duckduckgo").allowed)

    def test_the_penalty_outlasts_a_normal_refill(self) -> None:
        # Probing during a cooldown appeared to extend it, so a refusal must
        # not be retried through after one refill interval.
        self.limiter.record_refusal("duckduckgo")

        self.clock.advance(120.0)

        self.assertFalse(self.limiter.check("duckduckgo").allowed)

    def test_the_penalty_expires(self) -> None:
        self.limiter.record_refusal("duckduckgo")

        self.clock.advance(241.0)

        self.assertTrue(self.limiter.check("duckduckgo").allowed)

    def test_penalty_reports_a_retry_time(self) -> None:
        self.limiter.record_refusal("duckduckgo")

        verdict = self.limiter.check("duckduckgo")

        self.assertGreater(verdict.retry_after_seconds, 200.0)

    def test_success_clears_a_stale_penalty(self) -> None:
        self.limiter.record_refusal("duckduckgo")
        self.clock.advance(241.0)

        self.limiter.acquire("duckduckgo")
        self.limiter.record_success("duckduckgo")

        self.assertEqual(
            self.limiter.check("duckduckgo").retry_after_seconds,
            0.0,
        )

    def test_success_does_not_refund_the_spent_token(self) -> None:
        self.limiter.acquire("duckduckgo")
        self.limiter.record_success("duckduckgo")

        self.assertEqual(
            self.limiter.check("duckduckgo").tokens_remaining,
            1.0,
        )

    def test_refusal_on_an_unlimited_provider_is_a_no_op(self) -> None:
        self.limiter.record_refusal("wikipedia_fa")

        self.assertTrue(self.limiter.check("wikipedia_fa").allowed)


class TestPersistence(unittest.TestCase):
    def test_state_survives_a_new_limiter(self) -> None:
        # Without this, restarting Qronos would reset the bucket and the first
        # searches after a restart would walk straight into the limit.
        clock = FakeClock()
        store = InMemoryStateStore()

        first = RateLimiter(store=store, clock=clock)
        first.acquire("duckduckgo")
        first.acquire("duckduckgo")

        second = RateLimiter(store=store, clock=clock)

        self.assertFalse(second.check("duckduckgo").allowed)

    def test_a_penalty_survives_a_restart(self) -> None:
        clock = FakeClock()
        store = InMemoryStateStore()

        first = RateLimiter(store=store, clock=clock)
        first.record_refusal("duckduckgo")

        second = RateLimiter(store=store, clock=clock)

        self.assertFalse(second.check("duckduckgo").allowed)

    def test_refill_continues_across_a_restart(self) -> None:
        clock = FakeClock()
        store = InMemoryStateStore()

        first = RateLimiter(store=store, clock=clock)
        first.acquire("duckduckgo")
        first.acquire("duckduckgo")

        clock.advance(120.0)

        second = RateLimiter(store=store, clock=clock)

        self.assertTrue(second.check("duckduckgo").allowed)

    def test_reset_clears_one_provider(self) -> None:
        limiter = RateLimiter(clock=FakeClock())
        limiter.record_refusal("duckduckgo")

        limiter.reset("duckduckgo")

        self.assertTrue(limiter.check("duckduckgo").allowed)

    def test_reset_clears_everything(self) -> None:
        limiter = RateLimiter(clock=FakeClock())
        limiter.record_refusal("duckduckgo")

        limiter.reset()

        self.assertTrue(limiter.check("duckduckgo").allowed)


class TestJsonFileStateStore(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.path = Path(self._temporary.name) / "nested" / "state.json"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_missing_file_loads_empty(self) -> None:
        self.assertEqual(JsonFileStateStore(self.path).load(), {})

    def test_round_trip(self) -> None:
        store = JsonFileStateStore(self.path)

        store.save(
            {
                "duckduckgo": BucketState(
                    tokens=0.5,
                    last_refill_at=START,
                    blocked_until=START + 240.0,
                )
            }
        )

        loaded = JsonFileStateStore(self.path).load()

        self.assertEqual(loaded["duckduckgo"].tokens, 0.5)
        self.assertEqual(loaded["duckduckgo"].blocked_until, START + 240.0)

    def test_save_creates_the_parent_directory(self) -> None:
        JsonFileStateStore(self.path).save({})

        self.assertTrue(self.path.is_file())

    def test_no_temporary_file_is_left_behind(self) -> None:
        JsonFileStateStore(self.path).save({})

        leftovers = [
            entry.name
            for entry in self.path.parent.iterdir()
            if entry.name.endswith(".tmp")
        ]

        self.assertEqual(leftovers, [])

    def test_corrupt_file_loads_empty_rather_than_raising(self) -> None:
        # An empty bucket refuses requests until it refills, which is the safe
        # direction. Failing to start over a corrupt counter file is not.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ not json", encoding="utf-8")

        self.assertEqual(JsonFileStateStore(self.path).load(), {})

    def test_unexpected_shape_loads_empty(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([1, 2]), encoding="utf-8")

        self.assertEqual(JsonFileStateStore(self.path).load(), {})

    def test_limiter_writes_through_to_the_file(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(
            store=JsonFileStateStore(self.path),
            clock=clock,
        )

        limiter.acquire("duckduckgo")

        self.assertTrue(self.path.is_file())

        payload = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertIn("duckduckgo", payload)


if __name__ == "__main__":
    unittest.main()
