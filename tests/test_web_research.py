from __future__ import annotations

import json
import unittest

from core.web_answer import AnswerRejection
from core.web_cache import WebCache
from core.web_extract import ExtractedPage
from core.web_fetch import PageFetcher
from core.web_friction import FrictionMemory
from core.web_providers import ProviderResponse, ProviderStatus, SearchResult
from core.web_query import build_query
from core.web_rate_limit import RateLimiter
from core.web_research import (
    ResearchEvent,
    ResearchPhase,
    WebResearch,
)
from core.web_search import WebSearch


START = 1_800_000_000.0


class FakeClock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    def __init__(
        self,
        name: str = "duckduckgo",
        count: int = 5,
        snippet_length: int = 90,
        status: ProviderStatus = ProviderStatus.OK,
    ) -> None:
        self.name = name
        self.count = count
        self.snippet_length = snippet_length
        self.status = status
        self.calls = 0

    def search(self, query: str, limit: int = 10) -> ProviderResponse:
        self.calls += 1

        if self.status is not ProviderStatus.OK:
            return ProviderResponse(
                provider=self.name,
                status=self.status,
                detail="scripted",
            )

        return ProviderResponse(
            provider=self.name,
            status=ProviderStatus.OK,
            results=tuple(
                SearchResult(
                    title=f"result {index}",
                    url=f"https://site{index}.example.com/page",
                    snippet="s" * self.snippet_length,
                    provider=self.name,
                )
                for index in range(self.count)
            ),
        )


class FakeTransport:
    def __init__(self, body: str) -> None:
        self.body = body
        self.requested: list[str] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        data: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 10,
    ) -> tuple[int, str]:
        self.requested.append(url)

        return 200, self.body


class StubExtractor:
    def extract(self, html: str, url: str = "") -> ExtractedPage:
        return ExtractedPage(
            url=url,
            title="fetched title",
            text="fetched body " * 200,
            word_count=400,
            is_persian=False,
        )


def good_answer(prompt: str) -> str:
    return json.dumps(
        {
            "answered": True,
            "answer": "The answer is here [1].",
            "claims": [{"statement": "it is here", "citations": ["[1]"]}],
        }
    )


def bad_answer(prompt: str) -> str:
    return json.dumps(
        {
            "answered": True,
            "answer": "Confidently wrong [9].",
            "claims": [{"statement": "wrong", "citations": ["[9]"]}],
        }
    )


class ResearchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.provider = FakeProvider()
        self.cache = WebCache(path=":memory:", clock=self.clock)
        self.limiter = RateLimiter(clock=self.clock)
        self.friction = FrictionMemory(path=":memory:", clock=self.clock)
        self.transport = FakeTransport(
            "<html><head><title>t</title></head><body><p>x</p></body></html>"
        )

        self.search = WebSearch(
            general=self.provider,
            wikipedia_fa=FakeProvider("wikipedia_fa", count=0,
                                      status=ProviderStatus.UNAVAILABLE),
            wikipedia_en=FakeProvider("wikipedia_en", count=0,
                                      status=ProviderStatus.UNAVAILABLE),
            technical=FakeProvider("stackexchange", count=0,
                                   status=ProviderStatus.UNAVAILABLE),
            long_tail=FakeProvider("marginalia", count=0,
                                   status=ProviderStatus.UNAVAILABLE),
            cache=self.cache,
            limiter=self.limiter,
        )

        self.fetcher = PageFetcher(
            transport=self.transport,
            extractor=StubExtractor(),
            friction=self.friction,
            clock=self.clock,
        )

        self.events: list[ResearchEvent] = []

        self.research = WebResearch(
            search=self.search,
            fetcher=self.fetcher,
            on_event=self.events.append,
        )

    def tearDown(self) -> None:
        self.cache.close()
        self.friction.close()

    def run_research(self, utterance: str = "قیمت دلار امروز", **kwargs):
        return self.research.research(build_query(utterance), **kwargs)


class TestFastPath(ResearchTestCase):
    def test_substantial_snippets_mean_no_page_is_opened(self) -> None:
        # The common case, and roughly five times faster than fetching.
        result = self.run_research()

        self.assertFalse(result.used_pages)
        self.assertEqual(self.transport.requested, [])

    def test_the_evidence_is_built_from_snippets(self) -> None:
        result = self.run_research()

        self.assertGreater(result.package.snippet_count, 0)
        self.assertEqual(result.package.page_count, 0)

    def test_a_prompt_is_produced_without_a_brain(self) -> None:
        # Which is what makes the whole pipeline testable with no model.
        result = self.run_research()

        self.assertTrue(result.prompt)
        self.assertIsNone(result.answer)


class TestFetchPath(ResearchTestCase):
    def test_thin_snippets_trigger_page_reading(self) -> None:
        self.provider.snippet_length = 5

        result = self.run_research()

        self.assertTrue(result.used_pages)
        self.assertGreater(len(self.transport.requested), 0)

    def test_force_fetch_opens_pages_even_when_snippets_suffice(self) -> None:
        result = self.run_research(force_fetch=True)

        self.assertTrue(result.used_pages)

    def test_the_page_budget_is_respected(self) -> None:
        self.provider.snippet_length = 5
        self.provider.count = 10

        result = self.run_research()

        self.assertLessEqual(result.package.page_count, 3)
        self.assertLessEqual(len(self.transport.requested), 3)

    def test_pages_are_ranked_ahead_of_snippets(self) -> None:
        self.provider.snippet_length = 5

        result = self.run_research()

        self.assertEqual(result.package.items[0].kind.value, "page")


class TestEventStream(ResearchTestCase):
    def test_searching_is_announced_before_any_network_call(self) -> None:
        # Speaking first is what makes the wait feel short.
        self.run_research()

        self.assertIs(self.events[0].phase, ResearchPhase.SEARCHING)

    def test_the_phases_run_in_order(self) -> None:
        self.provider.snippet_length = 5

        self.run_research()

        phases = [event.phase for event in self.events]

        self.assertEqual(phases[0], ResearchPhase.SEARCHING)
        self.assertIn(ResearchPhase.READING, phases)
        self.assertIn(ResearchPhase.ANSWERING, phases)

    def test_reading_progress_is_a_real_count(self) -> None:
        # Pages read out of pages planned. Never a fabricated timer.
        self.provider.snippet_length = 5

        self.run_research()

        reading = [
            event
            for event in self.events
            if event.phase is ResearchPhase.READING and event.is_measurable
        ]

        self.assertTrue(reading)

        final = reading[-1]

        self.assertEqual(final.done, 3)
        self.assertEqual(final.total, 3)
        self.assertAlmostEqual(final.progress or 0.0, 1.0)

    def test_answering_reports_no_progress_value(self) -> None:
        # Token generation has no honest fraction, so the UI must show an
        # indeterminate state rather than guess.
        self.run_research()

        answering = [
            event
            for event in self.events
            if event.phase is ResearchPhase.ANSWERING
        ]

        self.assertTrue(answering)
        self.assertFalse(answering[0].is_measurable)
        self.assertIsNone(answering[0].progress)

    def test_progress_never_exceeds_one(self) -> None:
        for event in self.events:
            if event.progress is not None:
                self.assertLessEqual(event.progress, 1.0)

    def test_events_are_recorded_on_the_result_too(self) -> None:
        result = self.run_research()

        self.assertEqual(len(result.events), len(self.events))

    def test_a_failure_emits_a_failed_phase(self) -> None:
        self.provider.status = ProviderStatus.UNAVAILABLE

        self.run_research()

        self.assertIs(self.events[-1].phase, ResearchPhase.FAILED)


class TestWithABrain(ResearchTestCase):
    def test_a_good_answer_is_accepted(self) -> None:
        self.research.answer_fn = good_answer

        result = self.run_research()

        self.assertTrue(result.ok)
        assert result.answer is not None
        self.assertIn("The answer is here", result.answer.text)

    def test_a_fabricated_citation_is_refused(self) -> None:
        self.research.answer_fn = bad_answer

        result = self.run_research()

        self.assertFalse(result.ok)
        assert result.answer is not None
        self.assertIs(
            result.answer.rejection,
            AnswerRejection.FABRICATED_CITATION,
        )

    def test_a_refused_answer_does_not_reach_the_user(self) -> None:
        self.research.answer_fn = bad_answer

        result = self.run_research()

        self.assertNotIn("Confidently wrong", result.render())

    def test_a_brain_that_raises_does_not_break_the_pipeline(self) -> None:
        def explode(prompt: str) -> str:
            raise RuntimeError("model crashed")

        self.research.answer_fn = explode

        result = self.run_research()

        self.assertFalse(result.ok)
        self.assertIs(self.events[-1].phase, ResearchPhase.FAILED)

    def test_the_brain_sees_the_untrusted_fence(self) -> None:
        captured: dict[str, str] = {}

        def capture(prompt: str) -> str:
            captured["prompt"] = prompt
            return good_answer(prompt)

        self.research.answer_fn = capture

        self.run_research()

        self.assertIn("UNTRUSTED_EXTERNAL_CONTENT", captured["prompt"])

    def test_the_brain_is_told_which_labels_are_valid(self) -> None:
        captured: dict[str, str] = {}

        def capture(prompt: str) -> str:
            captured["prompt"] = prompt
            return good_answer(prompt)

        self.research.answer_fn = capture

        self.run_research()

        self.assertIn("Valid citation labels", captured["prompt"])


class TestProvenance(ResearchTestCase):
    def test_only_cited_sources_are_shown(self) -> None:
        self.research.answer_fn = good_answer

        result = self.run_research()

        self.assertIn("site0.example.com", result.provenance_text)
        self.assertNotIn("site4.example.com", result.provenance_text)

    def test_without_an_answer_every_source_read_is_shown(self) -> None:
        # A refusal should still show where Qronos looked.
        result = self.run_research()

        self.assertIn("site0.example.com", result.provenance_text)
        self.assertIn("site4.example.com", result.provenance_text)

    def test_provenance_appears_in_the_rendered_answer(self) -> None:
        self.research.answer_fn = good_answer

        result = self.run_research()

        self.assertIn("خوانده شد", result.render())


class TestFailureModes(ResearchTestCase):
    def test_a_failed_search_yields_no_answer_and_no_fetch(self) -> None:
        self.provider.status = ProviderStatus.UNAVAILABLE

        result = self.run_research()

        self.assertFalse(result.ok)
        self.assertIsNone(result.fetch)
        self.assertIsNone(result.answer)

    def test_a_rate_limited_search_explains_itself(self) -> None:
        self.limiter.record_refusal("duckduckgo")

        result = self.run_research()

        self.assertFalse(result.ok)
        self.assertIn("minute", result.render())

    def test_results_with_no_usable_snippet_and_no_pages_yield_nothing(
        self,
    ) -> None:
        self.provider.snippet_length = 0
        self.transport.body = "not html at all"

        result = self.run_research()

        self.assertTrue(result.package.is_empty)
        self.assertIsNone(result.answer)
        self.assertIs(self.events[-1].phase, ResearchPhase.FAILED)

    def test_a_cached_repeat_costs_no_provider_call(self) -> None:
        self.run_research()
        self.run_research()

        self.assertEqual(self.provider.calls, 1)

    def test_describe_summarises_the_run(self) -> None:
        self.research.answer_fn = good_answer

        result = self.run_research()

        described = result.describe()

        self.assertIn("answered", described)
        self.assertIn("snippets", described)


if __name__ == "__main__":
    unittest.main()
