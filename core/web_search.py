from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.persian_text import contains_marker, is_mostly_persian, normalise
from core.web_cache import ContentFreshness, WebCache, classify_freshness
from core.web_provenance import ProvenanceStrip, build_strip
from core.web_providers import (
    DuckDuckGoProvider,
    MarginaliaProvider,
    ProviderStatus,
    SearchProvider,
    SearchResult,
    StackExchangeProvider,
    WikipediaProvider,
)
from core.web_query import SearchQuery
from core.web_rate_limit import RateLimiter


class QueryCategory(Enum):
    """
    What kind of question this is, which decides where to look.

    Routing exists to protect the scarce provider. DuckDuckGo allows roughly
    two searches every four minutes; Wikipedia and Stack Exchange are
    effectively unmetered. Sending an encyclopedic question to Wikipedia costs
    nothing and leaves the general-web budget intact for a question that
    actually needs it.
    """

    ENCYCLOPEDIC = "encyclopedic"
    TECHNICAL = "technical"
    LONG_TAIL = "long_tail"
    GENERAL = "general"


class SearchOutcome(Enum):
    """How a search ended."""

    OK = "ok"
    FROM_CACHE = "from_cache"
    FROM_STALE_CACHE = "from_stale_cache"
    RATE_LIMITED = "rate_limited"
    NO_RESULTS = "no_results"
    UNAVAILABLE = "unavailable"


# Deterministic routing markers. Kept narrow on purpose: a question that does
# not clearly belong to a specialist source goes to the general provider, which
# is the correct default even though it is the expensive one.
ENCYCLOPEDIC_MARKERS = (
    "کی بود", "چی هست", "چیست", "کیست", "تعریف", "تاریخ تولد",
    "پایتخت", "جمعیت", "معنی", "یعنی چی",
    "who was", "what is", "what are", "definition of", "meaning of",
    "capital of", "population of", "born in",
)

TECHNICAL_MARKERS = (
    "خطا", "ارور", "کد", "برنامه نویسی", "پایتون", "جاوا", "اسکریپت",
    "کامپایل", "دیباگ", "exception", "traceback",
    "error", "exception", "stack trace", "compile", "debug", "syntax",
    "python", "javascript", "typescript", "rust", "sql", "regex",
    "api", "library", "framework", "npm", "pip",
)


@dataclass(frozen=True)
class SearchReport:
    """
    Everything one search produced.

    ``results`` is what a brain should read. ``snippets_sufficient`` is the
    signal that no page downloads are needed: DuckDuckGo returns a description
    with every result, and ten descriptions frequently answer the question
    outright. That path is roughly five times faster and spends no extra
    search budget.
    """

    query: SearchQuery
    category: QueryCategory
    outcome: SearchOutcome
    results: tuple[SearchResult, ...] = field(default_factory=tuple)
    providers_tried: tuple[str, ...] = field(default_factory=tuple)
    provider_used: str = ""
    detail: str = ""
    cache_age_seconds: float = 0.0
    freshness: ContentFreshness = ContentFreshness.DISCUSSION

    @property
    def ok(self) -> bool:
        return self.outcome in (
            SearchOutcome.OK,
            SearchOutcome.FROM_CACHE,
            SearchOutcome.FROM_STALE_CACHE,
        )

    @property
    def count(self) -> int:
        return len(self.results)

    @property
    def snippet_count(self) -> int:
        return sum(1 for result in self.results if result.has_snippet)

    @property
    def snippets_sufficient(self) -> bool:
        """
        True when the snippets alone are probably enough to answer.

        Three results carrying a real description is the threshold. Below that
        there is not enough corroboration to answer without opening pages.
        """
        substantial = sum(
            1
            for result in self.results
            if len(result.snippet.strip()) >= 60
        )

        return substantial >= 3

    @property
    def provenance(self) -> ProvenanceStrip:
        return build_strip(tuple(result.url for result in self.results))

    def describe(self) -> str:
        return (
            f"{self.outcome.value}: {self.count} results "
            f"({self.snippet_count} with snippets) "
            f"via {self.provider_used or 'none'} "
            f"[{self.category.value}]"
        )


def classify_category(query: str) -> QueryCategory:
    """
    Decide which family of sources fits this question.

    Specialist categories are checked first; anything unmatched is GENERAL.
    Being wrong here is cheap in one direction and not the other: routing a
    general question to Wikipedia returns nothing useful and then falls
    through, while routing an encyclopedic question to the general provider
    merely spends budget that did not need spending.
    """
    text = normalise(query).lower()

    if contains_marker(text, TECHNICAL_MARKERS):
        return QueryCategory.TECHNICAL

    if contains_marker(text, ENCYCLOPEDIC_MARKERS):
        return QueryCategory.ENCYCLOPEDIC

    return QueryCategory.GENERAL


class WebSearch:
    """
    The search layer. No model, no page downloading, no answering.

    Its whole job is to turn an approved query into results, as cheaply as
    possible, without ever exceeding a provider's limit.

    Order of operations, and every step is deterministic:

    1. Cache lookup. A repeated question must not cost a search.
    2. Category routing. Specialist sources are unmetered; use them.
    3. Rate-limit check on the general provider.
    4. One request. Never a fan-out — the measured budget cannot absorb it.
    5. Record success or refusal so the limiter learns.
    6. Stale cache as a last resort, clearly labelled.

    Providers are injected, so tests run with fakes and no network.
    """

    def __init__(
        self,
        general: SearchProvider | None = None,
        wikipedia_fa: SearchProvider | None = None,
        wikipedia_en: SearchProvider | None = None,
        technical: SearchProvider | None = None,
        long_tail: SearchProvider | None = None,
        cache: WebCache | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.general = general if general is not None else DuckDuckGoProvider()

        self.wikipedia_fa = (
            wikipedia_fa
            if wikipedia_fa is not None
            else WikipediaProvider(language="fa")
        )
        self.wikipedia_en = (
            wikipedia_en
            if wikipedia_en is not None
            else WikipediaProvider(language="en")
        )
        self.technical = (
            technical if technical is not None else StackExchangeProvider()
        )
        self.long_tail = (
            long_tail if long_tail is not None else MarginaliaProvider()
        )

        self.cache = cache if cache is not None else WebCache()
        self.limiter = limiter if limiter is not None else RateLimiter()

    # ------------------------------------------------------------------ search

    def search(
        self,
        query: SearchQuery,
        limit: int = 10,
        use_cache: bool = True,
    ) -> SearchReport:
        """
        Run one search for an already-approved query.

        Takes a :class:`~core.web_query.SearchQuery` rather than a string, so a
        raw utterance cannot reach the network without passing the privacy
        gate first.
        """
        if query.is_empty:
            return SearchReport(
                query=query,
                category=QueryCategory.GENERAL,
                outcome=SearchOutcome.NO_RESULTS,
                detail="Empty query.",
            )

        category = classify_category(query.text)
        freshness = classify_freshness(query.text)
        chain = self._provider_chain(category, query)

        tried: list[str] = []

        for provider in chain:
            tried.append(provider.name)

            if use_cache:
                cached = self.cache.get(query.text, provider.name)

                if cached is not None:
                    return SearchReport(
                        query=query,
                        category=category,
                        outcome=SearchOutcome.FROM_CACHE,
                        results=cached.results,
                        providers_tried=tuple(tried),
                        provider_used=provider.name,
                        cache_age_seconds=cached.age_seconds(
                            self.cache.clock()
                        ),
                        freshness=cached.freshness,
                    )

            verdict = self.limiter.acquire(provider.name)

            if not verdict.allowed:
                # Do not retry through a cooldown. Move on; if nothing else
                # answers, stale cache is tried at the end.
                continue

            response = provider.search(query.text, limit=limit)

            if response.status is ProviderStatus.OK:
                self.limiter.record_success(provider.name)

                if use_cache:
                    self.cache.put(
                        query.text,
                        provider.name,
                        response.results,
                        freshness=freshness,
                    )

                return SearchReport(
                    query=query,
                    category=category,
                    outcome=SearchOutcome.OK,
                    results=response.results,
                    providers_tried=tuple(tried),
                    provider_used=provider.name,
                    freshness=freshness,
                )

            if response.status is ProviderStatus.EMPTY_OR_THROTTLED:
                # Zero results on a 200 cannot be told apart from throttling,
                # so it counts as a refusal. Over-reacting costs a few minutes
                # of other sources; under-reacting means hammering a provider
                # that already refused.
                self.limiter.record_refusal(provider.name)

        return self._fallback(query, category, tuple(tried), freshness)

    # --------------------------------------------------------------- fallback

    def _fallback(
        self,
        query: SearchQuery,
        category: QueryCategory,
        tried: tuple[str, ...],
        freshness: ContentFreshness,
    ) -> SearchReport:
        """
        Last resort: a stale cached answer, clearly labelled as stale.

        An answer from an hour ago that the user is told is an hour old beats
        no answer, and beats an answer invented from the model's own knowledge
        while pretending it came from the web.
        """
        for name in tried:
            stale = self.cache.get(query.text, name, allow_stale=True)

            if stale is not None and stale.results:
                return SearchReport(
                    query=query,
                    category=category,
                    outcome=SearchOutcome.FROM_STALE_CACHE,
                    results=stale.results,
                    providers_tried=tried,
                    provider_used=name,
                    detail=(
                        "Every provider was unavailable or rate limited. "
                        "These results are from an earlier search and may be "
                        "out of date."
                    ),
                    cache_age_seconds=stale.age_seconds(self.cache.clock()),
                    freshness=stale.freshness,
                )

        limited = any(
            not self.limiter.check(name).allowed
            for name in tried
            if self.limiter.is_limited(name)
        )

        if limited:
            retry = max(
                (
                    self.limiter.check(name).retry_after_seconds
                    for name in tried
                    if self.limiter.is_limited(name)
                ),
                default=0.0,
            )

            return SearchReport(
                query=query,
                category=category,
                outcome=SearchOutcome.RATE_LIMITED,
                providers_tried=tried,
                detail=(
                    "The search budget is exhausted. Try again in about "
                    f"{retry / 60:.0f} minute(s)."
                ),
                freshness=freshness,
            )

        return SearchReport(
            query=query,
            category=category,
            outcome=SearchOutcome.NO_RESULTS,
            providers_tried=tried,
            detail="No provider returned any results.",
            freshness=freshness,
        )

    # ---------------------------------------------------------------- routing

    def _provider_chain(
        self,
        category: QueryCategory,
        query: SearchQuery,
    ) -> tuple[SearchProvider, ...]:
        """
        Build the ordered list of providers to try.

        Unmetered specialists come first where the category fits, so the
        general provider's budget is only spent when nothing cheaper will do.
        Persian queries prefer Persian Wikipedia; testing showed Marginalia
        returns nothing for Persian, so it is left out of Persian chains
        entirely rather than wasting a call.
        """
        persian = query.is_persian or is_mostly_persian(query.text)

        if category is QueryCategory.ENCYCLOPEDIC:
            if persian:
                return (self.wikipedia_fa, self.wikipedia_en, self.general)

            return (self.wikipedia_en, self.general)

        if category is QueryCategory.TECHNICAL:
            if persian:
                # Stack Exchange is English-only in practice, so a Persian
                # technical question goes straight to the general provider.
                return (self.general, self.wikipedia_fa)

            return (self.technical, self.general)

        if category is QueryCategory.LONG_TAIL and not persian:
            return (self.long_tail, self.general)

        return (self.general,)

    # ------------------------------------------------------------ maintenance

    def purge_cache(self) -> int:
        return self.cache.purge_expired()

    def budget_status(self) -> str:
        verdict = self.limiter.check(self.general.name)

        if verdict.allowed:
            return (
                f"{self.general.name}: "
                f"{verdict.tokens_remaining:.1f} searches available"
            )

        return (
            f"{self.general.name}: exhausted, retry in "
            f"{verdict.retry_after_seconds / 60:.0f} minute(s)"
        )


def main() -> None:
    """Run one live search end to end. Spends real search budget."""
    import sys

    from core.web_query import build_query

    utterance = " ".join(sys.argv[1:]) or "قیمت دلار امروز رو سرچ کن"

    search = WebSearch(cache=WebCache(path=":memory:"))
    query = build_query(utterance)

    print(f"utterance : {utterance}")
    print(f"query     : {query.text}")
    print(f"category  : {classify_category(query.text).value}")
    print(f"budget    : {search.budget_status()}")
    print()

    report = search.search(query)

    print(report.describe())

    if report.detail:
        print(f"detail: {report.detail}")

    print(f"snippets sufficient: {report.snippets_sufficient}")
    print()

    for index, result in enumerate(report.results[:5], start=1):
        print(f"[{index}] {result.title}")
        print(f"    {result.url}")

        if result.snippet:
            print(f"    {result.snippet[:110]}")

    print()
    print(report.provenance.render_persian())


if __name__ == "__main__":
    main()
