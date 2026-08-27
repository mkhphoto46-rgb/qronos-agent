from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from core.web_extract import (
    ExtractedPage,
    Extractor,
    PageProblem,
    TrafilaturaExtractor,
)
from core.web_friction import FrictionMemory, FrictionSignal
from core.web_providers import USER_AGENT, Transport, requests_transport


DEFAULT_TIMEOUT_SECONDS = 8

# A page larger than this is almost certainly not an article. Bounding the read
# stops one pathological page consuming the whole fetch budget, and nothing
# beyond the word cap would be used anyway.
MAX_PAGE_BYTES = 3_000_000

# How many pages one question may open. Three is the point where a small model
# starts losing the thread rather than gaining corroboration.
DEFAULT_PAGE_BUDGET = 3

# Politeness. Not a legal requirement, but hammering one host is both rude and
# the fastest way to earn a refusal that Friction Memory then has to sit out.
MIN_SECONDS_BETWEEN_SAME_HOST = 1.0


class FetchStatus(Enum):
    """Outcome of one page fetch."""

    OK = "ok"
    SKIPPED_COOLING_OFF = "skipped_cooling_off"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"
    TOO_LARGE = "too_large"
    NOT_HTML = "not_html"
    UNREADABLE = "unreadable"


# Which friction signal to record for an HTTP status. Anything not listed is
# treated as a transport problem rather than a refusal, so a server having a
# bad minute does not earn a three-day cool-off.
STATUS_SIGNALS: dict[int, FrictionSignal] = {
    401: FrictionSignal.LOGIN_WALL,
    403: FrictionSignal.FORBIDDEN,
    429: FrictionSignal.RATE_LIMITED,
}

PROBLEM_SIGNALS: dict[str, FrictionSignal] = {
    PageProblem.CAPTCHA: FrictionSignal.CAPTCHA,
    PageProblem.CHALLENGE: FrictionSignal.CHALLENGE,
    PageProblem.CONSENT_WALL: FrictionSignal.CONSENT_WALL,
    PageProblem.LOGIN_WALL: FrictionSignal.LOGIN_WALL,
    PageProblem.EMPTY: FrictionSignal.EMPTY_AFTER_FETCH,
}


@dataclass(frozen=True)
class FetchResult:
    """What happened when one URL was opened."""

    url: str
    status: FetchStatus
    page: ExtractedPage | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is FetchStatus.OK and self.page is not None

    @property
    def text(self) -> str:
        return self.page.text if self.page is not None else ""


@dataclass(frozen=True)
class FetchReport:
    """The outcome of opening several pages for one question."""

    pages: tuple[ExtractedPage, ...] = field(default_factory=tuple)
    results: tuple[FetchResult, ...] = field(default_factory=tuple)
    attempted: int = 0

    @property
    def count(self) -> int:
        return len(self.pages)

    @property
    def total_words(self) -> int:
        return sum(page.word_count for page in self.pages)

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(page.url for page in self.pages)

    @property
    def skipped_hosts(self) -> tuple[str, ...]:
        return tuple(
            result.url
            for result in self.results
            if result.status is FetchStatus.SKIPPED_COOLING_OFF
        )

    def describe(self) -> str:
        return (
            f"{self.count}/{self.attempted} pages read, "
            f"{self.total_words} words"
        )


class PageFetcher:
    """
    Opens pages and turns them into clean text.

    Three things it will not do, each for a reason:

    * It will not open a domain that is cooling off. Friction Memory decides,
      and the user is never shown a CAPTCHA during an ordinary search.
    * It will not follow the search results blindly past its page budget.
      Three articles is where a small model stops gaining and starts drowning.
    * It will not treat a wall as content. A Cloudflare interstitial extracts
      cleanly into the words "checking your browser", and a model handed that
      would answer the user's question with it.

    Transport, extractor, friction memory and clock are all injected, so the
    whole thing is testable with no network and no waiting.
    """

    def __init__(
        self,
        transport: Transport | None = None,
        extractor: Extractor | None = None,
        friction: FrictionMemory | None = None,
        clock: Callable[[], float] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        page_budget: int = DEFAULT_PAGE_BUDGET,
        max_bytes: int = MAX_PAGE_BYTES,
    ) -> None:
        import time

        self.transport: Transport = (
            transport if transport is not None else requests_transport
        )
        self.extractor: Extractor = (
            extractor if extractor is not None else TrafilaturaExtractor()
        )
        self.friction = (
            friction if friction is not None else FrictionMemory()
        )
        self.clock: Callable[[], float] = (
            clock if clock is not None else time.time
        )

        self.timeout = timeout
        self.page_budget = page_budget
        self.max_bytes = max_bytes

        self._last_host_fetch: dict[str, float] = {}

    # ------------------------------------------------------------------ single

    def fetch_one(self, url: str) -> FetchResult:
        """
        Open one URL, or explain why it was not opened.

        Never raises. A page that cannot be read is a result with a status, not
        an exception, because the caller has other pages to try.
        """
        if not url.strip():
            return FetchResult(
                url=url,
                status=FetchStatus.UNAVAILABLE,
                detail="Empty URL.",
            )

        if self.friction.should_skip(url):
            record = self.friction.get(url)

            return FetchResult(
                url=url,
                status=FetchStatus.SKIPPED_COOLING_OFF,
                detail=(
                    "This domain refused "
                    f"{record.refusal_count if record else 0} time(s) and is "
                    "cooling off."
                ),
            )

        self._respect_host_delay(url)

        try:
            status_code, body = self.transport(
                "GET",
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "fa,en;q=0.8",
                },
                timeout=self.timeout,
            )
        except TimeoutError as exc:
            self.friction.record_refusal(url, FrictionSignal.TIMEOUT)

            return FetchResult(
                url=url,
                status=FetchStatus.UNAVAILABLE,
                detail=f"Timed out: {exc}",
            )
        except Exception as exc:
            self.friction.record_refusal(
                url,
                FrictionSignal.TRANSPORT_ERROR,
            )

            return FetchResult(
                url=url,
                status=FetchStatus.UNAVAILABLE,
                detail=f"Transport failure: {exc}",
            )

        if status_code in STATUS_SIGNALS:
            self.friction.record_refusal(url, STATUS_SIGNALS[status_code])

            return FetchResult(
                url=url,
                status=FetchStatus.REFUSED,
                detail=f"HTTP {status_code}",
            )

        if status_code != 200:
            # Not recorded as a refusal: a 500 is the server's problem and it
            # does not deserve a three-day cool-off.
            return FetchResult(
                url=url,
                status=FetchStatus.UNAVAILABLE,
                detail=f"HTTP {status_code}",
            )

        if len(body.encode("utf-8", errors="ignore")) > self.max_bytes:
            return FetchResult(
                url=url,
                status=FetchStatus.TOO_LARGE,
                detail=(
                    f"Page exceeds {self.max_bytes} bytes and was not read."
                ),
            )

        if not self._looks_like_html(body):
            return FetchResult(
                url=url,
                status=FetchStatus.NOT_HTML,
                detail="Response does not look like an HTML document.",
            )

        page = self.extractor.extract(body, url=url)

        if page.problem:
            signal = PROBLEM_SIGNALS.get(
                page.problem,
                FrictionSignal.EMPTY_AFTER_FETCH,
            )
            self.friction.record_refusal(url, signal)

            return FetchResult(
                url=url,
                status=FetchStatus.REFUSED,
                page=page,
                detail=f"Page could not be read: {page.problem}",
            )

        if not page.is_substantial:
            # Not a refusal — the page was readable, just thin. Recording it
            # would punish a site for having a short article.
            return FetchResult(
                url=url,
                status=FetchStatus.UNREADABLE,
                page=page,
                detail=(
                    f"Only {page.word_count} words of content; too thin to "
                    "use."
                ),
            )

        self.friction.record_success(url)

        return FetchResult(url=url, status=FetchStatus.OK, page=page)

    # ------------------------------------------------------------------- batch

    def fetch(
        self,
        urls: tuple[str, ...],
        budget: int | None = None,
    ) -> FetchReport:
        """
        Open pages until the budget is filled or the candidates run out.

        Walks the list in order, so the best-ranked results are tried first,
        and stops as soon as enough pages have been read. A skipped or refused
        page does not consume budget — only a page actually read does.
        """
        limit = budget if budget is not None else self.page_budget

        results: list[FetchResult] = []
        pages: list[ExtractedPage] = []

        for url in urls:
            if len(pages) >= limit:
                break

            result = self.fetch_one(url)
            results.append(result)

            if result.ok and result.page is not None:
                pages.append(result.page)

        return FetchReport(
            pages=tuple(pages),
            results=tuple(results),
            attempted=len(results),
        )

    # ---------------------------------------------------------------- helpers

    def _respect_host_delay(self, url: str) -> None:
        """
        Record the fetch time for a host so repeat hits can be spaced.

        Only bookkeeping — this does not sleep. A caller fetching concurrently
        can read :attr:`seconds_until_host_ready` and decide. Blocking inside a
        fetch would stall a voice answer for the sake of politeness the caller
        may already be handling.
        """
        from core.web_provenance import registrable_host

        host = registrable_host(url)

        if host:
            self._last_host_fetch[host] = self.clock()

    def seconds_until_host_ready(self, url: str) -> float:
        """How long to wait before hitting this host again, if at all."""
        from core.web_provenance import registrable_host

        host = registrable_host(url)

        if not host or host not in self._last_host_fetch:
            return 0.0

        elapsed = self.clock() - self._last_host_fetch[host]

        return max(0.0, MIN_SECONDS_BETWEEN_SAME_HOST - elapsed)

    @staticmethod
    def _looks_like_html(body: str) -> bool:
        """
        Cheap check that the response is a document rather than JSON or binary.

        An extractor handed JSON returns nothing useful, and the wasted call is
        avoidable.
        """
        head = body[:2_000].lstrip().lower()

        if not head:
            return False

        return (
            head.startswith("<!doctype html")
            or head.startswith("<html")
            or "<html" in head
            or "<body" in head
        )


def main() -> None:
    """Fetch and extract a real page. Makes one live request."""
    import sys

    url = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "https://fa.wikipedia.org/wiki/پایتون_(زبان_برنامه‌نویسی)"
    )

    fetcher = PageFetcher(friction=FrictionMemory(path=":memory:"))
    result = fetcher.fetch_one(url)

    print(f"=== {url} ===")
    print(f"status : {result.status.value}")

    if result.detail:
        print(f"detail : {result.detail}")

    if result.page is not None:
        print(f"title  : {result.page.title}")
        print(f"words  : {result.page.word_count}")
        print(f"persian: {result.page.is_persian}")
        print(f"cut    : {result.page.truncated}")
        print()
        print(result.page.text[:400])

    fetcher.friction.close()


if __name__ == "__main__":
    main()
