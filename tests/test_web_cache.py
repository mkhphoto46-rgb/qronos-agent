from __future__ import annotations

import unittest

from core.web_cache import (
    SECONDS_PER_DAY,
    SECONDS_PER_MINUTE,
    ContentFreshness,
    WebCache,
    cache_key,
    classify_freshness,
    ttl_for,
)
from core.web_providers import SearchResult


START = 1_800_000_000.0


class FakeClock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_results(count: int = 3) -> tuple[SearchResult, ...]:
    return tuple(
        SearchResult(
            title=f"result {index}",
            url=f"https://example.org/{index}",
            snippet=f"snippet {index}",
            provider="duckduckgo",
        )
        for index in range(count)
    )


class TestFreshnessClassification(unittest.TestCase):
    def test_persian_price_query_is_volatile(self) -> None:
        self.assertIs(
            classify_freshness("قیمت دلار امروز"),
            ContentFreshness.VOLATILE,
        )

    def test_english_weather_query_is_volatile(self) -> None:
        self.assertIs(
            classify_freshness("weather in Tehran"),
            ContentFreshness.VOLATILE,
        )

    def test_comparison_query_is_discussion(self) -> None:
        self.assertIs(
            classify_freshness("بهترین لپ تاپ برای برنامه نویسی"),
            ContentFreshness.DISCUSSION,
        )

    def test_how_to_query_is_technical(self) -> None:
        self.assertIs(
            classify_freshness("چطور پایتون نصب کنم"),
            ContentFreshness.TECHNICAL,
        )

    def test_biographical_query_is_reference(self) -> None:
        # A birth date never changes, so re-searching it is pure waste.
        self.assertIs(
            classify_freshness("تاریخ تولد کوروش"),
            ContentFreshness.REFERENCE,
        )

    def test_definition_query_is_reference(self) -> None:
        self.assertIs(
            classify_freshness("what is a monad"),
            ContentFreshness.REFERENCE,
        )

    def test_every_freshness_level_is_reachable(self) -> None:
        # A level nothing routes to is dead code pretending to be policy.
        reached = {
            classify_freshness(query)
            for query in (
                "قیمت طلا",
                "best laptop",
                "how to install python",
                "capital of France",
            )
        }

        self.assertEqual(reached, set(ContentFreshness))

    def test_most_perishable_wins_when_markers_collide(self) -> None:
        # A stale price is the worse failure, so volatile beats technical.
        self.assertIs(
            classify_freshness("چطور قیمت دلار رو ببینم"),
            ContentFreshness.VOLATILE,
        )

    def test_unmatched_query_falls_to_one_day(self) -> None:
        # Assuming an unrecognised query is stable for ninety days is the
        # riskier guess.
        self.assertIs(
            classify_freshness("zzzz qqqq"),
            ContentFreshness.DISCUSSION,
        )

    def test_ttls_increase_with_stability(self) -> None:
        self.assertLess(
            ttl_for(ContentFreshness.VOLATILE),
            ttl_for(ContentFreshness.DISCUSSION),
        )
        self.assertLess(
            ttl_for(ContentFreshness.DISCUSSION),
            ttl_for(ContentFreshness.TECHNICAL),
        )
        self.assertLess(
            ttl_for(ContentFreshness.TECHNICAL),
            ttl_for(ContentFreshness.REFERENCE),
        )

    def test_volatile_is_minutes_not_hours(self) -> None:
        self.assertEqual(
            ttl_for(ContentFreshness.VOLATILE),
            15 * SECONDS_PER_MINUTE,
        )


class TestCacheKey(unittest.TestCase):
    def test_key_includes_the_provider(self) -> None:
        self.assertNotEqual(
            cache_key("x", "duckduckgo"),
            cache_key("x", "wikipedia_fa"),
        )

    def test_two_spellings_share_one_key(self) -> None:
        # The point of normalising first: two spellings of the same query must
        # not cost two searches out of a budget of two.
        self.assertEqual(
            cache_key("نرم‌افزار", "duckduckgo"),
            cache_key("نرم افزار", "duckduckgo"),
        )

    def test_arabic_and_persian_letters_share_one_key(self) -> None:
        self.assertEqual(
            cache_key("مديريت", "duckduckgo"),
            cache_key("مدیریت", "duckduckgo"),
        )

    def test_case_is_ignored(self) -> None:
        self.assertEqual(
            cache_key("Python Dataclass", "d"),
            cache_key("python dataclass", "d"),
        )


class CacheTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.cache = WebCache(path=":memory:", clock=self.clock)

    def tearDown(self) -> None:
        self.cache.close()


class TestCacheReadWrite(CacheTestCase):
    def test_stored_results_come_back(self) -> None:
        self.cache.put("python dataclass", "duckduckgo", make_results())

        entry = self.cache.get("python dataclass", "duckduckgo")

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(len(entry.results), 3)
        self.assertEqual(entry.results[0].title, "result 0")

    def test_a_miss_returns_none(self) -> None:
        self.assertIsNone(self.cache.get("nothing here", "duckduckgo"))

    def test_persian_results_survive_the_round_trip(self) -> None:
        results = (
            SearchResult(
                title="سازماندهی فایل",
                url="https://example.ir/فایل",
                snippet="مرتب کردن فایل‌ها",
                provider="duckduckgo",
            ),
        )

        self.cache.put("دسته بندی فایل", "duckduckgo", results)

        entry = self.cache.get("دسته بندی فایل", "duckduckgo")

        assert entry is not None
        self.assertEqual(entry.results[0].title, "سازماندهی فایل")
        self.assertIn("مرتب", entry.results[0].snippet)

    def test_a_repeated_query_hits_the_cache(self) -> None:
        self.cache.put("قیمت دلار", "duckduckgo", make_results())

        self.assertTrue(self.cache.has_fresh("قیمت دلار", "duckduckgo"))

    def test_alternate_spelling_hits_the_same_entry(self) -> None:
        self.cache.put("نرم‌افزار مديريت", "duckduckgo", make_results())

        self.assertTrue(
            self.cache.has_fresh("نرم افزار مدیریت", "duckduckgo")
        )

    def test_writing_the_same_key_replaces_rather_than_duplicates(self) -> None:
        self.cache.put("x", "duckduckgo", make_results(2))
        self.cache.put("x", "duckduckgo", make_results(5))

        entry = self.cache.get("x", "duckduckgo")

        assert entry is not None
        self.assertEqual(len(entry.results), 5)
        self.assertEqual(self.cache.entry_count(), 1)

    def test_providers_are_cached_separately(self) -> None:
        self.cache.put("x", "duckduckgo", make_results(1))
        self.cache.put("x", "wikipedia_fa", make_results(2))

        self.assertEqual(self.cache.entry_count(), 2)


class TestExpiry(CacheTestCase):
    def test_a_volatile_entry_expires_in_minutes(self) -> None:
        self.cache.put("قیمت دلار", "duckduckgo", make_results())

        self.clock.advance(16 * SECONDS_PER_MINUTE)

        self.assertIsNone(self.cache.get("قیمت دلار", "duckduckgo"))

    def test_a_reference_entry_survives_a_month(self) -> None:
        self.cache.put("capital of France", "duckduckgo", make_results())

        self.clock.advance(30 * SECONDS_PER_DAY)

        self.assertIsNotNone(self.cache.get("capital of France", "duckduckgo"))

    def test_an_expired_entry_is_still_reachable_when_stale_is_allowed(
        self,
    ) -> None:
        # When every provider is throttled, an answer from an hour ago that is
        # labelled as such beats no answer at all.
        self.cache.put("قیمت دلار", "duckduckgo", make_results())

        self.clock.advance(SECONDS_PER_DAY)

        self.assertIsNone(self.cache.get("قیمت دلار", "duckduckgo"))

        stale = self.cache.get("قیمت دلار", "duckduckgo", allow_stale=True)

        self.assertIsNotNone(stale)
        assert stale is not None
        self.assertFalse(stale.is_fresh(self.clock()))

    def test_age_is_reported(self) -> None:
        self.cache.put("x", "duckduckgo", make_results())

        self.clock.advance(90.0)

        entry = self.cache.get("x", "duckduckgo")

        assert entry is not None
        self.assertAlmostEqual(entry.age_seconds(self.clock()), 90.0)

    def test_explicit_freshness_overrides_classification(self) -> None:
        self.cache.put(
            "قیمت دلار",
            "duckduckgo",
            make_results(),
            freshness=ContentFreshness.REFERENCE,
        )

        self.clock.advance(SECONDS_PER_DAY)

        self.assertIsNotNone(self.cache.get("قیمت دلار", "duckduckgo"))

    def test_purge_removes_only_expired_entries(self) -> None:
        self.cache.put("قیمت دلار", "duckduckgo", make_results())
        self.cache.put("capital of France", "duckduckgo", make_results())

        self.clock.advance(SECONDS_PER_DAY)

        removed = self.cache.purge_expired()

        self.assertEqual(removed, 1)
        self.assertEqual(self.cache.entry_count(), 1)


class TestTrimming(CacheTestCase):
    def test_trim_removes_oldest_first(self) -> None:
        for index in range(5):
            self.cache.put(f"query {index}", "duckduckgo", make_results(4))
            self.clock.advance(1.0)

        before = self.cache.total_bytes()

        self.cache.trim_to(before // 2)

        self.assertLessEqual(self.cache.total_bytes(), before // 2)
        self.assertIsNone(self.cache.get("query 0", "duckduckgo"))
        self.assertIsNotNone(self.cache.get("query 4", "duckduckgo"))

    def test_trim_terminates_when_nothing_is_left(self) -> None:
        self.cache.put("x", "duckduckgo", make_results())

        removed = self.cache.trim_to(0)

        self.assertEqual(removed, 1)
        self.assertEqual(self.cache.entry_count(), 0)

    def test_clear_empties_the_cache(self) -> None:
        self.cache.put("x", "duckduckgo", make_results())

        self.cache.clear()

        self.assertEqual(self.cache.entry_count(), 0)
        self.assertEqual(self.cache.total_bytes(), 0)


class TestRobustness(CacheTestCase):
    def test_storing_zero_results_is_allowed(self) -> None:
        self.cache.put("x", "duckduckgo", ())

        entry = self.cache.get("x", "duckduckgo")

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.results, ())

    def test_corrupt_payload_is_reported_as_a_miss(self) -> None:
        self.cache.put("x", "duckduckgo", make_results())

        self.cache._connection.execute(
            "UPDATE search_cache SET results = ?",
            ("not json",),
        )
        self.cache._connection.commit()

        self.assertIsNone(self.cache.get("x", "duckduckgo"))

    def test_unknown_freshness_value_falls_back(self) -> None:
        self.cache.put("x", "duckduckgo", make_results())

        self.cache._connection.execute(
            "UPDATE search_cache SET freshness = ?",
            ("nonsense",),
        )
        self.cache._connection.commit()

        entry = self.cache.get("x", "duckduckgo", allow_stale=True)

        assert entry is not None
        self.assertIs(entry.freshness, ContentFreshness.DISCUSSION)


if __name__ == "__main__":
    unittest.main()
