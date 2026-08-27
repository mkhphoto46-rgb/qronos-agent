from __future__ import annotations

import unittest

from core.web_cache import WebCache
from core.web_providers import (
    ProviderResponse,
    ProviderStatus,
    SearchResult,
)
from core.web_query import PrivacyLevel, SearchQuery, build_query
from core.web_rate_limit import RateLimiter
from core.web_search import (
    QueryCategory,
    SearchOutcome,
    WebSearch,
    classify_category,
)


START = 1_800_000_000.0


class FakeClock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    """A provider that returns a scripted response and counts its calls."""

    def __init__(
        self,
        name: str,
        status: ProviderStatus = ProviderStatus.OK,
        result_count: int = 3,
        snippet_length: int = 80,
    ) -> None:
        self.name = name
        self.status = status
        self.result_count = result_count
        self.snippet_length = snippet_length
        self.calls: list[str] = []

    def search(self, query: str, limit: int = 10) -> ProviderResponse:
        self.calls.append(query)

        if self.status is not ProviderStatus.OK:
            return ProviderResponse(
                provider=self.name,
                status=self.status,
                detail="scripted failure",
            )

        results = tuple(
            SearchResult(
                title=f"{self.name} result {index}",
                url=f"https://{self.name}-{index}.example.com/page",
                snippet="x" * self.snippet_length,
                provider=self.name,
            )
            for index in range(min(self.result_count, limit))
        )

        return ProviderResponse(
            provider=self.name,
            status=ProviderStatus.OK,
            results=results,
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


class SearchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()

        self.general = FakeProvider("duckduckgo")
        self.wiki_fa = FakeProvider("wikipedia_fa")
        self.wiki_en = FakeProvider("wikipedia_en")
        self.technical = FakeProvider("stackexchange")
        self.long_tail = FakeProvider("marginalia")

        self.cache = WebCache(path=":memory:", clock=self.clock)
        self.limiter = RateLimiter(clock=self.clock)

        self.search = WebSearch(
            general=self.general,
            wikipedia_fa=self.wiki_fa,
            wikipedia_en=self.wiki_en,
            technical=self.technical,
            long_tail=self.long_tail,
            cache=self.cache,
            limiter=self.limiter,
        )

    def tearDown(self) -> None:
        self.cache.close()

    def run_search(self, utterance: str, **kwargs: object) -> object:
        query = build_query(utterance)

        return self.search.search(query, **kwargs)  # type: ignore[arg-type]


class TestCategoryRouting(unittest.TestCase):
    def test_technical_question_is_technical(self) -> None:
        self.assertIs(
            classify_category("python dataclass error"),
            QueryCategory.TECHNICAL,
        )

    def test_persian_technical_question_is_technical(self) -> None:
        self.assertIs(
            classify_category("خطای پایتون"),
            QueryCategory.TECHNICAL,
        )

    def test_definitional_question_is_encyclopedic(self) -> None:
        self.assertIs(
            classify_category("capital of France"),
            QueryCategory.ENCYCLOPEDIC,
        )

    def test_persian_definitional_question_is_encyclopedic(self) -> None:
        self.assertIs(
            classify_category("پایتخت فرانسه چیست"),
            QueryCategory.ENCYCLOPEDIC,
        )

    def test_anything_else_is_general(self) -> None:
        self.assertIs(
            classify_category("قیمت دلار امروز"),
            QueryCategory.GENERAL,
        )

    def test_api_inside_capital_does_not_route_as_technical(self) -> None:
        # Boundary matching, not substring: "api" is inside "capital".
        self.assertIsNot(
            classify_category("capital of France"),
            QueryCategory.TECHNICAL,
        )


class TestProviderSelection(SearchTestCase):
    def test_persian_encyclopedic_question_prefers_persian_wikipedia(
        self,
    ) -> None:
        # Unmetered specialist first, so the scarce general budget survives.
        self.run_search("پایتخت فرانسه چیست")

        self.assertEqual(self.wiki_fa.call_count, 1)
        self.assertEqual(self.general.call_count, 0)

    def test_english_encyclopedic_question_uses_english_wikipedia(self) -> None:
        self.run_search("what is a monad")

        self.assertEqual(self.wiki_en.call_count, 1)
        self.assertEqual(self.general.call_count, 0)

    def test_english_technical_question_uses_stack_exchange(self) -> None:
        self.run_search("python dataclass error")

        self.assertEqual(self.technical.call_count, 1)
        self.assertEqual(self.general.call_count, 0)

    def test_persian_technical_question_skips_stack_exchange(self) -> None:
        # Stack Exchange is English-only in practice, so calling it for a
        # Persian question would waste a round trip for nothing.
        self.run_search("خطای پایتون رو سرچ کن")

        self.assertEqual(self.technical.call_count, 0)
        self.assertEqual(self.general.call_count, 1)

    def test_general_question_goes_straight_to_the_general_provider(
        self,
    ) -> None:
        self.run_search("قیمت دلار امروز")

        self.assertEqual(self.general.call_count, 1)

    def test_marginalia_is_never_called_for_persian(self) -> None:
        # It returned zero results for Persian in testing.
        self.run_search("قیمت دلار امروز")

        self.assertEqual(self.long_tail.call_count, 0)


class TestSingleSearchRule(SearchTestCase):
    def test_one_question_costs_at_most_one_provider_call(self) -> None:
        # The measured budget cannot absorb a fan-out.
        self.run_search("قیمت دلار امروز")

        total = (
            self.general.call_count
            + self.wiki_fa.call_count
            + self.wiki_en.call_count
            + self.technical.call_count
            + self.long_tail.call_count
        )

        self.assertEqual(total, 1)

    def test_a_successful_provider_stops_the_chain(self) -> None:
        self.run_search("پایتخت فرانسه چیست")

        self.assertEqual(self.wiki_fa.call_count, 1)
        self.assertEqual(self.wiki_en.call_count, 0)

    def test_the_chain_falls_through_a_failing_provider(self) -> None:
        self.wiki_fa.status = ProviderStatus.UNAVAILABLE

        report = self.run_search("پایتخت فرانسه چیست")

        self.assertEqual(self.wiki_fa.call_count, 1)
        self.assertEqual(self.wiki_en.call_count, 1)
        self.assertTrue(report.ok)  # type: ignore[attr-defined]


class TestCacheBehaviour(SearchTestCase):
    def test_a_repeated_question_does_not_cost_a_second_call(self) -> None:
        self.run_search("قیمت دلار امروز")
        self.run_search("قیمت دلار امروز")

        self.assertEqual(self.general.call_count, 1)

    def test_a_cached_answer_is_labelled_as_cached(self) -> None:
        self.run_search("قیمت دلار امروز")
        report = self.run_search("قیمت دلار امروز")

        self.assertIs(report.outcome, SearchOutcome.FROM_CACHE)  # type: ignore[attr-defined]
        self.assertTrue(report.ok)  # type: ignore[attr-defined]

    def test_an_alternate_spelling_hits_the_same_cache_entry(self) -> None:
        # Two spellings of one question must not cost two of two searches.
        self.run_search("سرچ کن نرم‌افزار مديريت")
        self.run_search("سرچ کن نرم افزار مدیریت")

        self.assertEqual(self.general.call_count, 1)

    def test_a_cached_answer_reports_its_age(self) -> None:
        self.run_search("قیمت دلار امروز")

        self.clock.advance(60.0)

        report = self.run_search("قیمت دلار امروز")

        self.assertAlmostEqual(report.cache_age_seconds, 60.0)  # type: ignore[attr-defined]

    def test_an_expired_entry_triggers_a_fresh_search(self) -> None:
        self.run_search("قیمت دلار امروز")

        self.clock.advance(16 * 60.0)

        self.clock.advance(240.0)  # let the rate limiter refill
        self.run_search("قیمت دلار امروز")

        self.assertEqual(self.general.call_count, 2)

    def test_caching_can_be_turned_off(self) -> None:
        self.run_search("قیمت دلار امروز", use_cache=False)
        self.clock.advance(240.0)
        self.run_search("قیمت دلار امروز", use_cache=False)

        self.assertEqual(self.general.call_count, 2)

    def test_a_cache_hit_spends_no_rate_limit_token(self) -> None:
        self.run_search("قیمت دلار امروز")

        before = self.limiter.check("duckduckgo").tokens_remaining

        self.run_search("قیمت دلار امروز")

        self.assertEqual(
            self.limiter.check("duckduckgo").tokens_remaining,
            before,
        )


class TestRateLimitInteraction(SearchTestCase):
    def test_the_third_search_in_a_burst_is_refused(self) -> None:
        for index in range(2):
            self.run_search(f"قیمت دلار روز {index}")

        report = self.run_search("قیمت دلار روز سوم")

        self.assertIs(report.outcome, SearchOutcome.RATE_LIMITED)  # type: ignore[attr-defined]
        self.assertEqual(self.general.call_count, 2)

    def test_a_refused_search_says_when_to_retry(self) -> None:
        for index in range(2):
            self.run_search(f"قیمت دلار روز {index}")

        report = self.run_search("قیمت دلار روز سوم")

        self.assertIn("minute", report.detail)  # type: ignore[attr-defined]

    def test_a_zero_result_response_is_recorded_as_a_refusal(self) -> None:
        # DuckDuckGo returns HTTP 200 with no results when throttled, and does
        # not distinguish that from an empty result set. Treating it as a
        # refusal is the safe reading.
        self.general.status = ProviderStatus.EMPTY_OR_THROTTLED

        self.run_search("قیمت دلار امروز")

        self.assertFalse(self.limiter.check("duckduckgo").allowed)

    def test_the_penalty_stops_an_immediate_retry(self) -> None:
        self.general.status = ProviderStatus.EMPTY_OR_THROTTLED

        self.run_search("قیمت دلار یک")
        self.general.status = ProviderStatus.OK
        report = self.run_search("قیمت دلار دو")

        self.assertIs(report.outcome, SearchOutcome.RATE_LIMITED)  # type: ignore[attr-defined]
        self.assertEqual(self.general.call_count, 1)

    def test_unmetered_providers_are_not_throttled(self) -> None:
        for index in range(10):
            self.run_search(f"پایتخت کشور {index} چیست")

        self.assertEqual(self.wiki_fa.call_count, 10)

    def test_budget_status_is_reportable(self) -> None:
        status = self.search.budget_status()

        self.assertIn("duckduckgo", status)


class TestStaleFallback(SearchTestCase):
    def test_a_stale_answer_is_served_when_everything_is_refused(self) -> None:
        # An answer from earlier, labelled as such, beats no answer — and beats
        # inventing one from the model's own knowledge.
        self.run_search("قیمت دلار امروز")

        self.clock.advance(16 * 60.0)  # entry expires
        self.limiter.record_refusal("duckduckgo")

        report = self.run_search("قیمت دلار امروز")

        self.assertIs(report.outcome, SearchOutcome.FROM_STALE_CACHE)  # type: ignore[attr-defined]
        self.assertTrue(report.results)  # type: ignore[attr-defined]

    def test_a_stale_answer_warns_that_it_may_be_out_of_date(self) -> None:
        self.run_search("قیمت دلار امروز")
        self.clock.advance(16 * 60.0)
        self.limiter.record_refusal("duckduckgo")

        report = self.run_search("قیمت دلار امروز")

        self.assertIn("out of date", report.detail)  # type: ignore[attr-defined]

    def test_a_stale_answer_reports_its_age(self) -> None:
        self.run_search("قیمت دلار امروز")
        self.clock.advance(16 * 60.0)
        self.limiter.record_refusal("duckduckgo")

        report = self.run_search("قیمت دلار امروز")

        self.assertGreater(report.cache_age_seconds, 900.0)  # type: ignore[attr-defined]

    def test_no_stale_entry_means_an_honest_refusal(self) -> None:
        self.limiter.record_refusal("duckduckgo")

        report = self.run_search("قیمت دلار امروز")

        self.assertIs(report.outcome, SearchOutcome.RATE_LIMITED)  # type: ignore[attr-defined]
        self.assertEqual(report.count, 0)  # type: ignore[attr-defined]


class TestSnippetSufficiency(SearchTestCase):
    def test_three_substantial_snippets_are_enough(self) -> None:
        # The fast path: answer from descriptions, download nothing.
        report = self.run_search("قیمت دلار امروز")

        self.assertTrue(report.snippets_sufficient)  # type: ignore[attr-defined]

    def test_short_snippets_are_not_enough(self) -> None:
        self.general.snippet_length = 10

        report = self.run_search("قیمت دلار امروز")

        self.assertFalse(report.snippets_sufficient)  # type: ignore[attr-defined]

    def test_too_few_results_are_not_enough(self) -> None:
        self.general.result_count = 2

        report = self.run_search("قیمت دلار امروز")

        self.assertFalse(report.snippets_sufficient)  # type: ignore[attr-defined]


class TestFailureModes(SearchTestCase):
    def test_an_empty_query_is_refused_without_any_call(self) -> None:
        query = SearchQuery(
            text="",
            level=PrivacyLevel.USER_ONLY,
            is_persian=False,
        )

        report = self.search.search(query)

        self.assertIs(report.outcome, SearchOutcome.NO_RESULTS)
        self.assertEqual(self.general.call_count, 0)

    def test_all_providers_unavailable_reports_no_results(self) -> None:
        for provider in (
            self.general,
            self.wiki_fa,
            self.wiki_en,
            self.technical,
            self.long_tail,
        ):
            provider.status = ProviderStatus.UNAVAILABLE

        report = self.run_search("قیمت دلار امروز")

        self.assertIs(report.outcome, SearchOutcome.NO_RESULTS)  # type: ignore[attr-defined]

    def test_providers_tried_is_reported(self) -> None:
        report = self.run_search("پایتخت فرانسه چیست")

        self.assertIn("wikipedia_fa", report.providers_tried)  # type: ignore[attr-defined]

    def test_search_requires_a_gated_query_not_a_string(self) -> None:
        # A raw utterance must not be able to reach the network without
        # passing the privacy gate.
        with self.assertRaises(AttributeError):
            self.search.search("قیمت دلار")  # type: ignore[arg-type]


class TestReportSurface(SearchTestCase):
    def test_provenance_is_built_from_the_results(self) -> None:
        report = self.run_search("قیمت دلار امروز")

        self.assertEqual(len(report.provenance.entries), 3)  # type: ignore[attr-defined]

    def test_describe_mentions_the_outcome_and_provider(self) -> None:
        report = self.run_search("قیمت دلار امروز")

        described = report.describe()  # type: ignore[attr-defined]

        self.assertIn("duckduckgo", described)
        self.assertIn("ok", described)

    def test_the_gated_query_is_carried_through(self) -> None:
        report = self.run_search("برام قیمت دلار رو سرچ کن")

        self.assertEqual(report.query.text, "قیمت دلار")  # type: ignore[attr-defined]

    def test_purge_cache_is_exposed(self) -> None:
        self.run_search("قیمت دلار امروز")
        self.clock.advance(16 * 60.0)

        self.assertEqual(self.search.purge_cache(), 1)


if __name__ == "__main__":
    unittest.main()
