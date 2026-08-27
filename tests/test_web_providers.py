from __future__ import annotations

import unittest
from urllib.parse import parse_qs

from core.web_providers import (
    USER_AGENT,
    DuckDuckGoProvider,
    MarginaliaProvider,
    ProviderStatus,
    StackExchangeProvider,
    WikipediaProvider,
)
from tests.fixtures.web_responses import (
    DDG_ENGLISH_HTML,
    DDG_PERSIAN_HTML,
    DDG_THROTTLED_HTML,
    MARGINALIA_JSON,
    STACKEXCHANGE_JSON,
    WIKIPEDIA_EMPTY_JSON,
    WIKIPEDIA_FA_JSON,
)


class RecordingTransport:
    """
    A transport that returns a canned response and records the call.

    Recording matters as much as the response: the whole browser-free approach
    rests on sending POST with a User-Agent, so the tests assert on what left
    the process, not only on what came back.
    """

    def __init__(
        self,
        status_code: int = 200,
        body: str = "",
        raises: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.raises = raises
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        data: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 10,
    ) -> tuple[int, str]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "data": data,
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )

        if self.raises is not None:
            raise self.raises

        return self.status_code, self.body

    @property
    def last(self) -> dict[str, object]:
        return self.calls[-1]


class TestDuckDuckGoRequestShape(unittest.TestCase):
    """The request shape is the discovery this provider exists for."""

    def test_uses_post_not_get(self) -> None:
        # A GET returns an anti-bot page with zero results. This one detail is
        # what makes browser-free search work at all.
        transport = RecordingTransport(body=DDG_ENGLISH_HTML)

        DuckDuckGoProvider(transport=transport).search("python dataclass")

        self.assertEqual(transport.last["method"], "POST")

    def test_query_travels_as_form_encoded_body(self) -> None:
        transport = RecordingTransport(body=DDG_ENGLISH_HTML)

        DuckDuckGoProvider(transport=transport).search("python dataclass")

        body = parse_qs(str(transport.last["data"]))

        self.assertEqual(body["q"], ["python dataclass"])

    def test_persian_query_is_percent_encoded_to_ascii(self) -> None:
        # Encoding the query to pure ASCII before it is sent removes every
        # opportunity for something in the chain to mangle it.
        transport = RecordingTransport(body=DDG_PERSIAN_HTML)

        DuckDuckGoProvider(transport=transport).search("دسته بندی فایل")

        data = str(transport.last["data"])

        self.assertTrue(data.isascii())
        self.assertEqual(
            parse_qs(data)["q"],
            ["دسته بندی فایل"],
        )

    def test_sends_a_user_agent(self) -> None:
        # With no User-Agent at all the response contains zero results.
        transport = RecordingTransport(body=DDG_ENGLISH_HTML)

        DuckDuckGoProvider(transport=transport).search("x")

        headers = transport.last["headers"]

        self.assertIn("User-Agent", headers)  # type: ignore[operator]

    def test_user_agent_identifies_qronos_honestly(self) -> None:
        # It names Qronos rather than claiming to be a browser. Impersonating
        # a browser's fingerprint is a different act and is not done here.
        self.assertIn("Qronos", USER_AGENT)

    def test_sends_form_content_type(self) -> None:
        transport = RecordingTransport(body=DDG_ENGLISH_HTML)

        DuckDuckGoProvider(transport=transport).search("x")

        self.assertEqual(
            transport.last["headers"]["Content-Type"],  # type: ignore[index]
            "application/x-www-form-urlencoded",
        )


class TestDuckDuckGoParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = DuckDuckGoProvider(
            transport=RecordingTransport(body=DDG_ENGLISH_HTML)
        )

    def test_parses_english_results(self) -> None:
        response = self.provider.search("python dataclass")

        self.assertIs(response.status, ProviderStatus.OK)
        self.assertEqual(response.count, 3)

    def test_title_url_and_snippet_stay_together(self) -> None:
        # Parsing block by block rather than as parallel lists. A result
        # missing a snippet must not shift every later snippet onto the wrong
        # title.
        response = self.provider.search("python dataclass")

        first = response.results[0]

        self.assertIn("dataclasses", first.title)
        self.assertEqual(
            first.url,
            "https://docs.python.org/3/library/dataclasses.html",
        )
        self.assertIn("decorator", first.snippet)

        third = response.results[2]

        self.assertIn("Real Python", third.title)
        self.assertEqual(third.url, "https://realpython.com/python-data-classes/")
        self.assertIn("data classes", third.snippet)

    def test_a_result_without_a_snippet_is_still_kept(self) -> None:
        response = self.provider.search("python dataclass")

        second = response.results[1]

        self.assertIn("What are data classes", second.title)
        self.assertEqual(second.snippet, "")
        self.assertFalse(second.has_snippet)

    def test_entities_are_decoded_and_inner_tags_removed(self) -> None:
        response = self.provider.search("python dataclass")

        self.assertNotIn("&mdash;", response.results[0].title)
        self.assertNotIn("<b>", response.results[0].snippet)

    def test_snippet_count_reflects_reality(self) -> None:
        response = self.provider.search("python dataclass")

        self.assertEqual(response.snippet_count, 2)

    def test_limit_is_respected(self) -> None:
        response = self.provider.search("python dataclass", limit=2)

        self.assertEqual(response.count, 2)


class TestDuckDuckGoPersianParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = DuckDuckGoProvider(
            transport=RecordingTransport(body=DDG_PERSIAN_HTML)
        )

    def test_parses_persian_results(self) -> None:
        response = self.provider.search("دسته بندی فایل")

        self.assertIs(response.status, ProviderStatus.OK)
        self.assertEqual(response.count, 3)

    def test_persian_titles_survive_decoding(self) -> None:
        response = self.provider.search("دسته بندی فایل")

        titles = " ".join(result.title for result in response.results)

        self.assertIn("سازماندهی", titles)
        self.assertIn("ویکی", titles)

    def test_persian_snippets_survive_decoding(self) -> None:
        response = self.provider.search("دسته بندی فایل")

        self.assertIn("مرتب", response.results[0].snippet)

    def test_percent_encoded_urls_are_preserved_verbatim(self) -> None:
        # The URL must stay exactly as served: re-encoding or decoding it would
        # break the fetch that follows.
        response = self.provider.search("دسته بندی فایل")

        self.assertTrue(
            response.results[1].url.startswith(
                "https://fa.wikipedia.org/wiki/%D8%B3"
            )
        )


class TestDuckDuckGoFailureModes(unittest.TestCase):
    def test_throttled_page_reports_empty_or_throttled(self) -> None:
        # HTTP 200, a valid page, zero result blocks. There is no error text,
        # which is exactly why the status name does not claim to know which
        # of the two it was.
        provider = DuckDuckGoProvider(
            transport=RecordingTransport(body=DDG_THROTTLED_HTML)
        )

        response = provider.search("anything")

        self.assertIs(response.status, ProviderStatus.EMPTY_OR_THROTTLED)
        self.assertEqual(response.count, 0)
        self.assertIn("throttl", response.detail.lower())

    def test_non_200_reports_unavailable(self) -> None:
        provider = DuckDuckGoProvider(
            transport=RecordingTransport(status_code=503, body="")
        )

        response = provider.search("anything")

        self.assertIs(response.status, ProviderStatus.UNAVAILABLE)
        self.assertIn("503", response.detail)

    def test_transport_failure_is_caught_not_raised(self) -> None:
        # A network error must not propagate: the caller has other providers
        # to try and an unhandled exception would abandon them.
        provider = DuckDuckGoProvider(
            transport=RecordingTransport(raises=OSError("connection reset"))
        )

        response = provider.search("anything")

        self.assertIs(response.status, ProviderStatus.UNAVAILABLE)
        self.assertIn("connection reset", response.detail)

    def test_empty_query_is_rejected_without_a_request(self) -> None:
        transport = RecordingTransport(body=DDG_ENGLISH_HTML)

        response = DuckDuckGoProvider(transport=transport).search("   ")

        self.assertIs(response.status, ProviderStatus.MALFORMED)
        self.assertEqual(transport.calls, [])

    def test_garbage_html_yields_no_results_rather_than_crashing(self) -> None:
        provider = DuckDuckGoProvider(
            transport=RecordingTransport(body="<html><body>?</body></html>")
        )

        response = provider.search("anything")

        self.assertIs(response.status, ProviderStatus.EMPTY_OR_THROTTLED)

    def test_parse_is_usable_without_any_transport(self) -> None:
        # Lets a recorded page be checked directly, which is how these tests
        # stay offline.
        results = DuckDuckGoProvider().parse(DDG_ENGLISH_HTML)

        self.assertEqual(len(results), 3)


class TestWikipediaProvider(unittest.TestCase):
    def test_parses_persian_articles(self) -> None:
        provider = WikipediaProvider(
            language="fa",
            transport=RecordingTransport(body=WIKIPEDIA_FA_JSON),
        )

        response = provider.search("فایل")

        self.assertIs(response.status, ProviderStatus.OK)
        self.assertEqual(response.count, 2)
        self.assertIn("سامانه", response.results[0].title)

    def test_builds_an_article_url_from_the_title(self) -> None:
        provider = WikipediaProvider(
            language="fa",
            transport=RecordingTransport(body=WIKIPEDIA_FA_JSON),
        )

        response = provider.search("فایل")

        self.assertTrue(
            response.results[0].url.startswith(
                "https://fa.wikipedia.org/wiki/"
            )
        )

    def test_search_highlighting_markup_is_stripped(self) -> None:
        # The API wraps matched terms in <span class="searchmatch">, which is
        # noise for a model reading the snippet.
        provider = WikipediaProvider(
            language="fa",
            transport=RecordingTransport(body=WIKIPEDIA_FA_JSON),
        )

        response = provider.search("فایل")

        self.assertNotIn("searchmatch", response.results[0].snippet)
        self.assertNotIn("<span", response.results[0].snippet)

    def test_language_selects_the_wiki_and_names_the_provider(self) -> None:
        transport = RecordingTransport(body=WIKIPEDIA_FA_JSON)
        provider = WikipediaProvider(language="fa", transport=transport)

        provider.search("x")

        self.assertEqual(provider.name, "wikipedia_fa")
        self.assertIn("fa.wikipedia.org", str(transport.last["url"]))

    def test_uses_get(self) -> None:
        transport = RecordingTransport(body=WIKIPEDIA_FA_JSON)

        WikipediaProvider(transport=transport).search("x")

        self.assertEqual(transport.last["method"], "GET")

    def test_no_articles_reports_empty(self) -> None:
        provider = WikipediaProvider(
            transport=RecordingTransport(body=WIKIPEDIA_EMPTY_JSON)
        )

        response = provider.search("qqqqzzz")

        self.assertIs(response.status, ProviderStatus.EMPTY_OR_THROTTLED)

    def test_invalid_json_reports_malformed(self) -> None:
        provider = WikipediaProvider(
            transport=RecordingTransport(body="not json at all")
        )

        response = provider.search("x")

        self.assertIs(response.status, ProviderStatus.MALFORMED)

    def test_unexpected_shape_reports_malformed(self) -> None:
        provider = WikipediaProvider(
            transport=RecordingTransport(body='["a", "list"]')
        )

        response = provider.search("x")

        self.assertIs(response.status, ProviderStatus.MALFORMED)


class TestStackExchangeProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = RecordingTransport(body=STACKEXCHANGE_JSON)
        self.provider = StackExchangeProvider(transport=self.transport)

    def test_parses_questions(self) -> None:
        response = self.provider.search("python dataclass")

        self.assertIs(response.status, ProviderStatus.OK)
        self.assertEqual(response.count, 2)
        self.assertIn("nested dict", response.results[0].title)

    def test_snippet_carries_answer_signal(self) -> None:
        # There is no description field, so the useful signal is whether the
        # question was actually resolved.
        response = self.provider.search("python dataclass")

        self.assertIn("5 answers", response.results[0].snippet)
        self.assertIn("accepted", response.results[0].snippet)
        self.assertNotIn("accepted", response.results[1].snippet)

    def test_requests_no_api_key(self) -> None:
        # 300 requests a day without a key is what makes this usable at all.
        self.provider.search("x")

        url = str(self.transport.last["url"])

        self.assertNotIn("key=", url)
        self.assertNotIn("access_token", url)

    def test_site_is_configurable(self) -> None:
        provider = StackExchangeProvider(
            site="superuser",
            transport=self.transport,
        )

        provider.search("x")

        self.assertIn("site=superuser", str(self.transport.last["url"]))


class TestMarginaliaProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = RecordingTransport(body=MARGINALIA_JSON)
        self.provider = MarginaliaProvider(transport=self.transport)

    def test_parses_results(self) -> None:
        response = self.provider.search("python dataclass")

        self.assertIs(response.status, ProviderStatus.OK)
        self.assertEqual(response.count, 2)
        self.assertIn("PyTorch", response.results[0].title)

    def test_description_becomes_the_snippet(self) -> None:
        response = self.provider.search("python dataclass")

        self.assertIn("PyTorch JIT", response.results[0].snippet)

    def test_query_is_path_encoded(self) -> None:
        self.provider.search("a b/c")

        url = str(self.transport.last["url"])

        self.assertIn("a%20b%2Fc", url)


class TestProviderContract(unittest.TestCase):
    """Behaviour every provider must share."""

    def make_all(self, body: str) -> tuple[object, ...]:
        transport = RecordingTransport(body=body)

        return (
            DuckDuckGoProvider(transport=transport),
            WikipediaProvider(transport=transport),
            StackExchangeProvider(transport=transport),
            MarginaliaProvider(transport=transport),
        )

    def test_every_provider_rejects_an_empty_query(self) -> None:
        for provider in self.make_all("{}"):
            with self.subTest(provider=provider.name):  # type: ignore[attr-defined]
                response = provider.search("  ")  # type: ignore[attr-defined]

                self.assertIs(response.status, ProviderStatus.MALFORMED)

    def test_every_provider_survives_a_transport_failure(self) -> None:
        transport = RecordingTransport(raises=TimeoutError("slow"))

        providers = (
            DuckDuckGoProvider(transport=transport),
            WikipediaProvider(transport=transport),
            StackExchangeProvider(transport=transport),
            MarginaliaProvider(transport=transport),
        )

        for provider in providers:
            with self.subTest(provider=provider.name):
                response = provider.search("x")

                self.assertIs(response.status, ProviderStatus.UNAVAILABLE)

    def test_every_provider_stamps_its_own_name_on_results(self) -> None:
        cases = (
            (DuckDuckGoProvider, DDG_ENGLISH_HTML),
            (WikipediaProvider, WIKIPEDIA_FA_JSON),
            (StackExchangeProvider, STACKEXCHANGE_JSON),
            (MarginaliaProvider, MARGINALIA_JSON),
        )

        for factory, body in cases:
            provider = factory(transport=RecordingTransport(body=body))
            response = provider.search("x")

            with self.subTest(provider=provider.name):
                self.assertTrue(response.results)

                for result in response.results:
                    self.assertEqual(result.provider, provider.name)


if __name__ == "__main__":
    unittest.main()
