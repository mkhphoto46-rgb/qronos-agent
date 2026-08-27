from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from urllib.parse import quote, urlencode

import requests


# Honest self-identification. This says what Qronos is; it does not claim to be
# a browser. The difference matters: making an HTTP client indistinguishable
# from Chrome (TLS fingerprint impersonation) defeats a detection mechanism and
# is forbidden in this architecture. Declaring a name is not.
USER_AGENT = "Mozilla/5.0 (compatible; Qronos/0.1; local personal assistant)"

DEFAULT_TIMEOUT_SECONDS = 10


class ProviderStatus(Enum):
    """Outcome of one provider call."""

    OK = "ok"

    # DuckDuckGo does not announce throttling. It returns HTTP 200 with zero
    # results, which is indistinguishable from a query that genuinely matched
    # nothing. Both are reported as this, and the rate limiter treats it as a
    # refusal — a few minutes on other sources is cheaper than hammering a
    # provider that has already said no.
    EMPTY_OR_THROTTLED = "empty_or_throttled"

    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class SearchResult:
    """One result from a provider."""

    title: str
    url: str
    snippet: str = ""
    provider: str = ""

    @property
    def has_snippet(self) -> bool:
        return bool(self.snippet.strip())


@dataclass(frozen=True)
class ProviderResponse:
    """What a provider returned, including why it returned nothing."""

    provider: str
    status: ProviderStatus
    results: tuple[SearchResult, ...] = field(default_factory=tuple)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is ProviderStatus.OK

    @property
    def count(self) -> int:
        return len(self.results)

    @property
    def snippet_count(self) -> int:
        return sum(1 for result in self.results if result.has_snippet)


class Transport(Protocol):
    """
    The HTTP call, injectable so tests never touch the network.

    Returns ``(status_code, text)``. Raising is reserved for transport failure;
    an HTTP error status is data, not an exception.
    """

    def __call__(
        self,
        method: str,
        url: str,
        *,
        data: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[int, str]:
        ...


def requests_transport(
    method: str,
    url: str,
    *,
    data: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    """
    Default transport built on :mod:`requests`.

    The response is decoded as UTF-8 explicitly rather than trusting charset
    detection: guessing wrong turns Persian into mojibake, and the query that
    produced it looked fine going out.
    """
    response = requests.request(
        method,
        url,
        data=data.encode("utf-8") if data is not None else None,
        headers=headers or {},
        timeout=timeout,
    )

    response.encoding = "utf-8"

    return response.status_code, response.text


def _clean_html_text(value: str) -> str:
    """Strip inner tags and decode entities from a fragment of result HTML."""
    without_tags = re.sub(r"<[^>]+>", "", value)

    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


class SearchProvider(Protocol):
    """Common shape for every provider."""

    name: str

    def search(self, query: str, limit: int = 10) -> ProviderResponse:
        ...


class DuckDuckGoProvider:
    """
    DuckDuckGo's no-JavaScript HTML endpoint.

    **The endpoint requires POST.** A GET returns an anti-bot page with zero
    results; a POST with the query as form data returns ten real results. This
    single difference is what makes browser-free search possible, and it was
    found by testing rather than from documentation.

    A ``User-Agent`` header is also required — with none at all the response
    contains no results.

    Verified on 2026-08-27 from two countries, in English and Persian, with
    result quality equal to a real browser. Persian queries return relevant
    Persian-language sources, which no other keyless HTTP provider managed.

    Each response also carries a snippet per result. That is worth more than it
    sounds: ten descriptions often answer the question outright, so no pages
    need downloading and no second search is needed.
    """

    name = "duckduckgo"
    endpoint = "https://html.duckduckgo.com/html/"

    # Each result lives in its own block. Parsing block by block keeps a title
    # attached to its own URL and snippet, which parallel lists do not
    # guarantee when a result is missing a snippet.
    _BLOCK = re.compile(
        r'<div class="result[^"]*results_links[^"]*".*?(?=<div class="result'
        r'[^"]*results_links|<div class="nav-link"|</body>)',
        re.S,
    )

    # Title and href come from the same anchor, so they cannot be mismatched.
    _TITLE = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.S,
    )

    _SNIPPET = re.compile(
        r'class="result__snippet"[^>]*>(.*?)</a>',
        re.S,
    )

    def __init__(
        self,
        transport: Transport | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.transport: Transport = (
            transport if transport is not None else requests_transport
        )
        self.timeout = timeout

    def search(self, query: str, limit: int = 10) -> ProviderResponse:
        if not query.strip():
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail="Empty query.",
            )

        body = urlencode({"q": query})

        try:
            status_code, text = self.transport(
                "POST",
                self.endpoint,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=self.timeout,
            )
        except Exception as exc:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                detail=f"Transport failure: {exc}",
            )

        if status_code != 200:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                detail=f"HTTP {status_code}",
            )

        results = self.parse(text, limit=limit)

        if not results:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.EMPTY_OR_THROTTLED,
                detail=(
                    "Zero results on HTTP 200. DuckDuckGo does not "
                    "distinguish throttling from an empty result set."
                ),
            )

        return ProviderResponse(
            provider=self.name,
            status=ProviderStatus.OK,
            results=results,
        )

    def parse(self, page: str, limit: int = 10) -> tuple[SearchResult, ...]:
        """
        Extract results from the HTML.

        Public so a recorded page can be parsed in a test without any
        transport at all.
        """
        results: list[SearchResult] = []

        for block in self._BLOCK.findall(page):
            title_match = self._TITLE.search(block)

            if title_match is None:
                continue

            url = html.unescape(title_match.group(1)).strip()
            title = _clean_html_text(title_match.group(2))

            if not url or not title:
                continue

            snippet_match = self._SNIPPET.search(block)
            snippet = (
                _clean_html_text(snippet_match.group(1))
                if snippet_match is not None
                else ""
            )

            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    provider=self.name,
                )
            )

            if len(results) >= limit:
                break

        return tuple(results)


class WikipediaProvider:
    """
    Wikipedia's search API.

    The floor of the whole system: keyless, unmetered in any way that matters
    for one user, and it has never refused a request in testing. When every
    other provider is throttled, Qronos is still not blind.

    Narrow by nature — it answers encyclopedic questions and nothing else — so
    it is routed to rather than used as a general fallback.

    ``language`` selects the wiki. ``fa`` is tried first for Persian queries;
    Persian Wikipedia exists and returns real articles.
    """

    def __init__(
        self,
        language: str = "en",
        transport: Transport | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.language = language
        self.name = f"wikipedia_{language}"

        self.transport: Transport = (
            transport if transport is not None else requests_transport
        )
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        return f"https://{self.language}.wikipedia.org/w/api.php"

    def search(self, query: str, limit: int = 10) -> ProviderResponse:
        if not query.strip():
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail="Empty query.",
            )

        params = urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": str(max(1, min(limit, 50))),
                "format": "json",
            }
        )

        try:
            status_code, text = self.transport(
                "GET",
                f"{self.endpoint}?{params}",
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        except Exception as exc:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                detail=f"Transport failure: {exc}",
            )

        if status_code != 200:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                detail=f"HTTP {status_code}",
            )

        return self._parse(text, limit=limit)

    def _parse(self, text: str, limit: int) -> ProviderResponse:
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail=f"Invalid JSON: {exc}",
            )

        if not isinstance(payload, dict):
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail="Unexpected payload shape.",
            )

        entries = (
            payload.get("query", {}).get("search", [])
            if isinstance(payload.get("query"), dict)
            else []
        )

        if not isinstance(entries, list):
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail="Unexpected payload shape.",
            )

        results: list[SearchResult] = []

        for entry in entries[:limit]:
            if not isinstance(entry, dict):
                continue

            title = str(entry.get("title", "")).strip()

            if not title:
                continue

            results.append(
                SearchResult(
                    title=title,
                    url=(
                        f"https://{self.language}.wikipedia.org/wiki/"
                        f"{quote(title.replace(' ', '_'))}"
                    ),
                    # The API returns the snippet with search-term highlighting
                    # markup, which is noise for a model to read.
                    snippet=_clean_html_text(str(entry.get("snippet", ""))),
                    provider=self.name,
                )
            )

        if not results:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.EMPTY_OR_THROTTLED,
                detail="No matching articles.",
            )

        return ProviderResponse(
            provider=self.name,
            status=ProviderStatus.OK,
            results=tuple(results),
        )


class StackExchangeProvider:
    """
    Stack Exchange search, no key required.

    Roughly 300 requests a day without a key, on its own quota — nothing to do
    with DuckDuckGo's bucket. Routed to for technical and programming
    questions, where it absorbs queries that would otherwise spend the scarce
    general-web budget.

    English only in practice.
    """

    name = "stackexchange"
    endpoint = "https://api.stackexchange.com/2.3/search/advanced"

    def __init__(
        self,
        site: str = "stackoverflow",
        transport: Transport | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.site = site

        self.transport: Transport = (
            transport if transport is not None else requests_transport
        )
        self.timeout = timeout

    def search(self, query: str, limit: int = 10) -> ProviderResponse:
        if not query.strip():
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail="Empty query.",
            )

        params = urlencode(
            {
                "order": "desc",
                "sort": "relevance",
                "q": query,
                "site": self.site,
                "pagesize": str(max(1, min(limit, 30))),
                "filter": "default",
            }
        )

        try:
            status_code, text = self.transport(
                "GET",
                f"{self.endpoint}?{params}",
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        except Exception as exc:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                detail=f"Transport failure: {exc}",
            )

        if status_code != 200:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                detail=f"HTTP {status_code}",
            )

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail=f"Invalid JSON: {exc}",
            )

        if not isinstance(payload, dict):
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail="Unexpected payload shape.",
            )

        items = payload.get("items", [])

        if not isinstance(items, list):
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail="Unexpected payload shape.",
            )

        results: list[SearchResult] = []

        for item in items[:limit]:
            if not isinstance(item, dict):
                continue

            title = _clean_html_text(str(item.get("title", "")))
            url = str(item.get("link", "")).strip()

            if not title or not url:
                continue

            # There is no snippet field; the answer count and score are the
            # useful signal about whether the question was resolved.
            answers = item.get("answer_count", 0)
            score = item.get("score", 0)
            accepted = bool(item.get("is_answered", False))

            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=(
                        f"{answers} answers, score {score}"
                        f"{', accepted' if accepted else ''}"
                    ),
                    provider=self.name,
                )
            )

        if not results:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.EMPTY_OR_THROTTLED,
                detail="No matching questions.",
            )

        return ProviderResponse(
            provider=self.name,
            status=ProviderStatus.OK,
            results=tuple(results),
        )


class MarginaliaProvider:
    """
    Marginalia's public search API.

    Free, keyless, returns JSON, and its own quota. An independent index that
    favours text-heavy and non-commercial pages, so it is useful for long-tail
    English topics and returned nothing at all for Persian in testing.

    The API states a ``CC-BY-NC-SA 4.0`` licence on its responses, which is a
    non-commercial term. That has to be checked before Qronos is distributed
    commercially; it is not a problem for personal use.
    """

    name = "marginalia"
    endpoint = "https://api.marginalia.nu/public/search"

    def __init__(
        self,
        transport: Transport | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.transport: Transport = (
            transport if transport is not None else requests_transport
        )
        self.timeout = timeout

    def search(self, query: str, limit: int = 10) -> ProviderResponse:
        if not query.strip():
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail="Empty query.",
            )

        try:
            status_code, text = self.transport(
                "GET",
                f"{self.endpoint}/{quote(query, safe='')}",
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        except Exception as exc:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                detail=f"Transport failure: {exc}",
            )

        if status_code != 200:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                detail=f"HTTP {status_code}",
            )

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail=f"Invalid JSON: {exc}",
            )

        if not isinstance(payload, dict):
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail="Unexpected payload shape.",
            )

        entries = payload.get("results", [])

        if not isinstance(entries, list):
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.MALFORMED,
                detail="Unexpected payload shape.",
            )

        results: list[SearchResult] = []

        for entry in entries[:limit]:
            if not isinstance(entry, dict):
                continue

            url = str(entry.get("url", "")).strip()
            title = _clean_html_text(str(entry.get("title", "")))

            if not url or not title:
                continue

            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=_clean_html_text(
                        str(entry.get("description", ""))
                    ),
                    provider=self.name,
                )
            )

        if not results:
            return ProviderResponse(
                provider=self.name,
                status=ProviderStatus.EMPTY_OR_THROTTLED,
                detail="No matching pages.",
            )

        return ProviderResponse(
            provider=self.name,
            status=ProviderStatus.OK,
            results=tuple(results),
        )


def main() -> None:
    """Run a live search against DuckDuckGo. Spends one token of real budget."""
    import sys

    query = " ".join(sys.argv[1:]) or "python dataclass"

    provider = DuckDuckGoProvider()
    response = provider.search(query)

    print(f"=== {provider.name}: {query!r} ===")
    print(f"status: {response.status.value}")

    if response.detail:
        print(f"detail: {response.detail}")

    for index, result in enumerate(response.results, start=1):
        print(f"\n[{index}] {result.title}")
        print(f"    {result.url}")

        if result.snippet:
            print(f"    {result.snippet[:120]}")


if __name__ == "__main__":
    main()
