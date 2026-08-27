from __future__ import annotations

import unittest

from core.web_extract import ExtractedPage, PageProblem
from core.web_fetch import (
    MIN_SECONDS_BETWEEN_SAME_HOST,
    FetchStatus,
    PageFetcher,
)
from core.web_friction import FrictionMemory, FrictionSignal


START = 1_800_000_000.0


class FakeClock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedTransport:
    """Returns a canned response per URL and records every request."""

    def __init__(
        self,
        default: tuple[int, str] = (200, "<html><body>x</body></html>"),
    ) -> None:
        self.default = default
        self.responses: dict[str, tuple[int, str]] = {}
        self.raises: dict[str, Exception] = {}
        self.requested: list[str] = []

    def set(self, url: str, status: int, body: str) -> None:
        self.responses[url] = (status, body)

    def fail(self, url: str, error: Exception) -> None:
        self.raises[url] = error

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

        if url in self.raises:
            raise self.raises[url]

        return self.responses.get(url, self.default)


class StubExtractor:
    """Returns a fixed page so fetch behaviour is tested in isolation."""

    def __init__(
        self,
        word_count: int = 500,
        problem: str = "",
    ) -> None:
        self.word_count = word_count
        self.problem = problem
        self.calls: list[str] = []

    def extract(self, html: str, url: str = "") -> ExtractedPage:
        self.calls.append(url)

        return ExtractedPage(
            url=url,
            title="stub title",
            text="word " * self.word_count,
            word_count=self.word_count,
            is_persian=False,
            problem=self.problem,
        )


HTML = "<html><head><title>t</title></head><body><p>hello</p></body></html>"


class FetchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.transport = ScriptedTransport(default=(200, HTML))
        self.extractor = StubExtractor()
        self.friction = FrictionMemory(path=":memory:", clock=self.clock)

        self.fetcher = PageFetcher(
            transport=self.transport,
            extractor=self.extractor,
            friction=self.friction,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.friction.close()


class TestFetchOne(FetchTestCase):
    def test_a_good_page_is_read(self) -> None:
        result = self.fetcher.fetch_one("https://example.com/a")

        self.assertIs(result.status, FetchStatus.OK)
        self.assertTrue(result.ok)
        self.assertEqual(result.page.word_count, 500)  # type: ignore[union-attr]

    def test_an_empty_url_is_refused_without_a_request(self) -> None:
        result = self.fetcher.fetch_one("  ")

        self.assertIs(result.status, FetchStatus.UNAVAILABLE)
        self.assertEqual(self.transport.requested, [])

    def test_an_honest_user_agent_is_sent(self) -> None:
        captured: dict[str, object] = {}

        def transport(method, url, *, data=None, headers=None, timeout=10):
            captured["headers"] = dict(headers or {})
            return 200, HTML

        fetcher = PageFetcher(
            transport=transport,
            extractor=self.extractor,
            friction=self.friction,
            clock=self.clock,
        )
        fetcher.fetch_one("https://example.com/a")

        self.assertIn("Qronos", str(captured["headers"]))

    def test_success_is_recorded_in_friction_memory(self) -> None:
        self.fetcher.fetch_one("https://example.com/a")

        record = self.friction.get("https://example.com/a")

        assert record is not None
        self.assertEqual(record.refusal_count, 0)
        self.assertGreater(record.last_success_at, 0.0)


class TestRefusalHandling(FetchTestCase):
    def test_403_is_a_refusal_and_is_remembered(self) -> None:
        self.transport.set("https://walled.com/a", 403, "")

        result = self.fetcher.fetch_one("https://walled.com/a")

        self.assertIs(result.status, FetchStatus.REFUSED)
        self.assertTrue(self.friction.should_skip("https://walled.com/a"))

    def test_429_is_a_refusal(self) -> None:
        self.transport.set("https://busy.com/a", 429, "")

        result = self.fetcher.fetch_one("https://busy.com/a")

        self.assertIs(result.status, FetchStatus.REFUSED)

        record = self.friction.get("https://busy.com/a")

        assert record is not None
        self.assertEqual(record.last_signal, FrictionSignal.RATE_LIMITED.value)

    def test_401_is_recorded_as_a_login_wall(self) -> None:
        self.transport.set("https://members.com/a", 401, "")

        self.fetcher.fetch_one("https://members.com/a")

        record = self.friction.get("https://members.com/a")

        assert record is not None
        self.assertEqual(record.last_signal, FrictionSignal.LOGIN_WALL.value)

    def test_a_500_is_not_held_against_the_site(self) -> None:
        # A server having a bad minute does not deserve a three-day cool-off.
        self.transport.set("https://broken.com/a", 500, "")

        result = self.fetcher.fetch_one("https://broken.com/a")

        self.assertIs(result.status, FetchStatus.UNAVAILABLE)
        self.assertFalse(self.friction.should_skip("https://broken.com/a"))

    def test_a_timeout_is_recorded(self) -> None:
        self.transport.fail("https://slow.com/a", TimeoutError("slow"))

        result = self.fetcher.fetch_one("https://slow.com/a")

        self.assertIs(result.status, FetchStatus.UNAVAILABLE)

        record = self.friction.get("https://slow.com/a")

        assert record is not None
        self.assertEqual(record.last_signal, FrictionSignal.TIMEOUT.value)

    def test_a_transport_error_never_propagates(self) -> None:
        # The caller has other pages to try; an exception would abandon them.
        self.transport.fail("https://gone.com/a", OSError("reset"))

        result = self.fetcher.fetch_one("https://gone.com/a")

        self.assertIs(result.status, FetchStatus.UNAVAILABLE)

    def test_a_wall_is_a_refusal_and_the_signal_is_mapped(self) -> None:
        fetcher = PageFetcher(
            transport=self.transport,
            extractor=StubExtractor(problem=PageProblem.CAPTCHA),
            friction=self.friction,
            clock=self.clock,
        )

        result = fetcher.fetch_one("https://walled.com/a")

        self.assertIs(result.status, FetchStatus.REFUSED)

        record = self.friction.get("https://walled.com/a")

        assert record is not None
        self.assertEqual(record.last_signal, FrictionSignal.CAPTCHA.value)

    def test_a_thin_page_is_not_held_against_the_site(self) -> None:
        # The page was readable, just short. Recording it would punish a site
        # for having a brief article.
        fetcher = PageFetcher(
            transport=self.transport,
            extractor=StubExtractor(word_count=20),
            friction=self.friction,
            clock=self.clock,
        )

        result = fetcher.fetch_one("https://thin.com/a")

        self.assertIs(result.status, FetchStatus.UNREADABLE)
        self.assertFalse(self.friction.should_skip("https://thin.com/a"))


class TestCoolingOff(FetchTestCase):
    def test_a_cooling_off_domain_is_not_requested_at_all(self) -> None:
        # The point of the whole mechanism: no wasted seconds, and the user is
        # never shown a CAPTCHA.
        self.friction.record_refusal(
            "https://walled.com/a",
            FrictionSignal.CAPTCHA,
        )

        result = self.fetcher.fetch_one("https://walled.com/b")

        self.assertIs(result.status, FetchStatus.SKIPPED_COOLING_OFF)
        self.assertEqual(self.transport.requested, [])

    def test_the_skip_reason_names_the_refusal_count(self) -> None:
        self.friction.record_refusal(
            "https://walled.com/a",
            FrictionSignal.CAPTCHA,
        )

        result = self.fetcher.fetch_one("https://walled.com/a")

        self.assertIn("1 time", result.detail)

    def test_after_the_cooloff_expires_it_is_tried_again(self) -> None:
        self.friction.record_refusal(
            "https://walled.com/a",
            FrictionSignal.CAPTCHA,
        )

        self.clock.advance(7 * 3_600.0)

        result = self.fetcher.fetch_one("https://walled.com/a")

        self.assertIs(result.status, FetchStatus.OK)


class TestGuards(FetchTestCase):
    def test_an_oversized_page_is_not_extracted(self) -> None:
        fetcher = PageFetcher(
            transport=self.transport,
            extractor=self.extractor,
            friction=self.friction,
            clock=self.clock,
            max_bytes=100,
        )
        self.transport.set(
            "https://huge.com/a",
            200,
            "<html><body>" + ("x" * 500) + "</body></html>",
        )

        result = fetcher.fetch_one("https://huge.com/a")

        self.assertIs(result.status, FetchStatus.TOO_LARGE)
        self.assertEqual(self.extractor.calls, [])

    def test_json_is_rejected_before_extraction(self) -> None:
        self.transport.set("https://api.com/a", 200, '{"key": "value"}')

        result = self.fetcher.fetch_one("https://api.com/a")

        self.assertIs(result.status, FetchStatus.NOT_HTML)
        self.assertEqual(self.extractor.calls, [])

    def test_an_empty_body_is_rejected(self) -> None:
        self.transport.set("https://blank.com/a", 200, "")

        result = self.fetcher.fetch_one("https://blank.com/a")

        self.assertIs(result.status, FetchStatus.NOT_HTML)

    def test_a_doctype_page_is_accepted(self) -> None:
        self.transport.set(
            "https://ok.com/a",
            200,
            "<!DOCTYPE html><html><body>x</body></html>",
        )

        result = self.fetcher.fetch_one("https://ok.com/a")

        self.assertIs(result.status, FetchStatus.OK)


class TestBatchFetch(FetchTestCase):
    def test_stops_once_the_budget_is_filled(self) -> None:
        urls = tuple(f"https://site{i}.com/a" for i in range(6))

        report = self.fetcher.fetch(urls, budget=3)

        self.assertEqual(report.count, 3)
        self.assertEqual(len(self.transport.requested), 3)

    def test_a_refused_page_does_not_consume_budget(self) -> None:
        self.transport.set("https://a.com/x", 403, "")

        report = self.fetcher.fetch(
            (
                "https://a.com/x",
                "https://b.com/x",
                "https://c.com/x",
                "https://d.com/x",
            ),
            budget=3,
        )

        self.assertEqual(report.count, 3)
        self.assertEqual(report.attempted, 4)

    def test_order_is_respected(self) -> None:
        report = self.fetcher.fetch(
            ("https://first.com/x", "https://second.com/x"),
            budget=2,
        )

        self.assertEqual(
            report.urls,
            ("https://first.com/x", "https://second.com/x"),
        )

    def test_a_cooling_off_domain_is_reported_as_skipped(self) -> None:
        self.friction.record_refusal(
            "https://walled.com/a",
            FrictionSignal.CAPTCHA,
        )

        report = self.fetcher.fetch(
            ("https://walled.com/a", "https://fine.com/a"),
        )

        self.assertEqual(report.count, 1)
        self.assertIn("https://walled.com/a", report.skipped_hosts)

    def test_running_out_of_candidates_is_not_an_error(self) -> None:
        report = self.fetcher.fetch(("https://only.com/a",), budget=5)

        self.assertEqual(report.count, 1)

    def test_no_candidates_yields_an_empty_report(self) -> None:
        report = self.fetcher.fetch(())

        self.assertEqual(report.count, 0)
        self.assertEqual(report.attempted, 0)

    def test_total_words_is_summed(self) -> None:
        report = self.fetcher.fetch(
            ("https://a.com/x", "https://b.com/x"),
            budget=2,
        )

        self.assertEqual(report.total_words, 1_000)

    def test_describe_reports_read_over_attempted(self) -> None:
        self.transport.set("https://a.com/x", 403, "")

        report = self.fetcher.fetch(
            ("https://a.com/x", "https://b.com/x"),
            budget=2,
        )

        self.assertIn("1/2", report.describe())


class TestHostPolitenessBookkeeping(FetchTestCase):
    def test_a_fresh_host_needs_no_wait(self) -> None:
        self.assertEqual(
            self.fetcher.seconds_until_host_ready("https://new.com/a"),
            0.0,
        )

    def test_a_recently_hit_host_reports_a_wait(self) -> None:
        self.fetcher.fetch_one("https://same.com/a")

        self.assertGreater(
            self.fetcher.seconds_until_host_ready("https://same.com/b"),
            0.0,
        )

    def test_the_wait_expires(self) -> None:
        self.fetcher.fetch_one("https://same.com/a")

        self.clock.advance(MIN_SECONDS_BETWEEN_SAME_HOST + 0.1)

        self.assertEqual(
            self.fetcher.seconds_until_host_ready("https://same.com/b"),
            0.0,
        )

    def test_bookkeeping_does_not_block_the_fetch(self) -> None:
        # Blocking inside a fetch would stall a voice answer for the sake of
        # politeness the caller may already be handling.
        before = self.clock()

        self.fetcher.fetch_one("https://same.com/a")
        self.fetcher.fetch_one("https://same.com/b")

        self.assertEqual(self.clock(), before)

    def test_an_unparseable_url_reports_no_wait(self) -> None:
        self.assertEqual(
            self.fetcher.seconds_until_host_ready("not a url"),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
