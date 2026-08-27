from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from core.config import CONFIG
from core.persian_text import contains_marker, normalise
from core.web_providers import SearchResult


DEFAULT_CACHE_PATH = CONFIG.paths.data / "web_cache.sqlite3"

SECONDS_PER_MINUTE = 60.0
SECONDS_PER_HOUR = 3_600.0
SECONDS_PER_DAY = 86_400.0


class ContentFreshness(Enum):
    """
    How quickly the answer to a query goes stale.

    A single fixed lifetime is wrong in both directions: a currency rate is
    worthless after minutes, while a language reference page is still accurate
    months later. Caching both for the same period either serves stale prices
    or re-searches documentation that never changed.
    """

    VOLATILE = "volatile"        # prices, weather, live news
    DISCUSSION = "discussion"    # forums, opinions, "best X"
    TECHNICAL = "technical"      # documentation, APIs, how-to
    REFERENCE = "reference"      # encyclopedic, historical, definitional


TTL_SECONDS: dict[ContentFreshness, float] = {
    ContentFreshness.VOLATILE: 15 * SECONDS_PER_MINUTE,
    ContentFreshness.DISCUSSION: 1 * SECONDS_PER_DAY,
    ContentFreshness.TECHNICAL: 30 * SECONDS_PER_DAY,
    ContentFreshness.REFERENCE: 90 * SECONDS_PER_DAY,
}


# Deterministic keyword classification, in Persian and English. Getting this
# wrong is low-stakes — the cost is a slightly stale or slightly fresh cache
# entry, never a wrong action — which is why a keyword table is acceptable here
# where it would not be for routing a command.
VOLATILE_MARKERS = (
    "قیمت", "نرخ", "دلار", "طلا", "بورس", "ارز", "سکه",
    "آب و هوا", "هوا", "اخبار", "خبر", "امروز", "الان", "زنده",
    "price", "rate", "weather", "news", "today", "now", "live",
    "stock", "exchange rate", "score",
)

DISCUSSION_MARKERS = (
    "بهترین", "مقایسه", "نظر", "تجربه", "پیشنهاد", "توصیه", "کدام بهتر",
    "best", "vs", "versus", "compare", "review", "opinion",
    "recommend", "should i", "which is better",
)

TECHNICAL_MARKERS = (
    "چطور", "چگونه", "آموزش", "نصب", "خطا", "ارور", "کد", "تنظیم",
    "how to", "howto", "tutorial", "install", "error", "fix",
    "documentation", "docs", "api", "syntax", "config",
)

# Facts that do not change: definitions, history, geography, biography. These
# earn the longest lifetime because re-searching them is pure waste.
REFERENCE_MARKERS = (
    "تاریخ تولد", "تاریخ وفات", "متولد", "کی بود", "چیست", "کیست",
    "تعریف", "معنی", "یعنی چی", "پایتخت", "جمعیت", "مساحت",
    "who was", "who is", "what is", "what are", "definition",
    "meaning of", "capital of", "population of", "born in", "died in",
    "history of", "invented",
)


def classify_freshness(query: str) -> ContentFreshness:
    """
    Decide how long an answer to this query stays useful.

    Checked most-perishable first: a query mentioning both a price and a
    tutorial is treated as volatile, because serving a stale price is the worse
    failure.

    An unmatched query falls to ``DISCUSSION`` — one day — rather than
    ``REFERENCE``. Assuming an unrecognised query is stable for ninety days is
    the riskier guess, and one day still saves the repeat searches that matter.
    """
    text = normalise(query).lower()

    if contains_marker(text, VOLATILE_MARKERS):
        return ContentFreshness.VOLATILE

    if contains_marker(text, DISCUSSION_MARKERS):
        return ContentFreshness.DISCUSSION

    if contains_marker(text, TECHNICAL_MARKERS):
        return ContentFreshness.TECHNICAL

    if contains_marker(text, REFERENCE_MARKERS):
        return ContentFreshness.REFERENCE

    return ContentFreshness.DISCUSSION


def ttl_for(freshness: ContentFreshness) -> float:
    return TTL_SECONDS[freshness]


def cache_key(query: str, provider: str) -> str:
    """
    Build a stable cache key.

    The query is normalised first, so «نرم‌افزار» and «نرم افزار» — which differ
    only by a zero-width joiner — hit the same entry instead of costing two
    searches out of a budget of two.
    """
    return f"{provider}::{normalise(query).lower()}"


@dataclass(frozen=True)
class CacheEntry:
    """A cached provider response."""

    key: str
    query: str
    provider: str
    results: tuple[SearchResult, ...]
    stored_at: float
    expires_at: float
    freshness: ContentFreshness

    def is_fresh(self, now: float) -> bool:
        return now < self.expires_at

    def age_seconds(self, now: float) -> float:
        return max(0.0, now - self.stored_at)


class WebCache:
    """
    SQLite cache for search results.

    Its first job is speed, but its more important job is protecting the search
    budget: DuckDuckGo allows roughly two searches every four minutes, so a
    repeated question that re-searches is a genuine cost rather than a small
    inefficiency.

    Results are stored, not whole pages, which keeps the file small. Cached
    entries are Qronos-owned disposable data and never enter user memory — web
    content mixing into what Qronos knows about the user is a separate concern
    with separate rules.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_CACHE_PATH,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.path = Path(path)
        self.clock: Callable[[], float] = (
            clock if clock is not None else time.time
        )

        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row

        self._create_schema()

    # ---------------------------------------------------------------- schema

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS search_cache (
                key         TEXT PRIMARY KEY,
                query       TEXT NOT NULL,
                provider    TEXT NOT NULL,
                results     TEXT NOT NULL,
                stored_at   REAL NOT NULL,
                expires_at  REAL NOT NULL,
                freshness   TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_search_cache_expires
                ON search_cache (expires_at);
            """
        )
        self._connection.commit()

    # ----------------------------------------------------------------- reads

    def get(
        self,
        query: str,
        provider: str,
        allow_stale: bool = False,
    ) -> CacheEntry | None:
        """
        Look up a cached response.

        ``allow_stale=True`` returns an expired entry anyway. That is for the
        case where every provider is throttled: an answer from an hour ago,
        clearly labelled as such, beats no answer at all.
        """
        row = self._connection.execute(
            "SELECT * FROM search_cache WHERE key = ?",
            (cache_key(query, provider),),
        ).fetchone()

        if row is None:
            return None

        entry = self._row_to_entry(row)

        if entry is None:
            return None

        if not allow_stale and not entry.is_fresh(self.clock()):
            return None

        return entry

    def has_fresh(self, query: str, provider: str) -> bool:
        return self.get(query, provider) is not None

    def entry_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM search_cache"
        ).fetchone()

        return int(row["n"]) if row is not None else 0

    def total_bytes(self) -> int:
        """
        Approximate size of the stored payloads.

        Measures the JSON rather than the file, so it is comparable across
        SQLite's page allocation and does not jump when the file grows.
        """
        row = self._connection.execute(
            "SELECT COALESCE(SUM(LENGTH(results)), 0) AS n FROM search_cache"
        ).fetchone()

        return int(row["n"]) if row is not None else 0

    # ---------------------------------------------------------------- writes

    def put(
        self,
        query: str,
        provider: str,
        results: Iterable[SearchResult],
        freshness: ContentFreshness | None = None,
    ) -> CacheEntry:
        """Store a response, replacing any existing entry for the same key."""
        now = self.clock()

        resolved_freshness = (
            freshness
            if freshness is not None
            else classify_freshness(query)
        )

        stored = tuple(results)

        entry = CacheEntry(
            key=cache_key(query, provider),
            query=query,
            provider=provider,
            results=stored,
            stored_at=now,
            expires_at=now + ttl_for(resolved_freshness),
            freshness=resolved_freshness,
        )

        self._connection.execute(
            """
            INSERT INTO search_cache
                (key, query, provider, results, stored_at, expires_at,
                 freshness)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                query      = excluded.query,
                results    = excluded.results,
                stored_at  = excluded.stored_at,
                expires_at = excluded.expires_at,
                freshness  = excluded.freshness
            """,
            (
                entry.key,
                entry.query,
                entry.provider,
                json.dumps(
                    [
                        {
                            "title": result.title,
                            "url": result.url,
                            "snippet": result.snippet,
                            "provider": result.provider,
                        }
                        for result in stored
                    ],
                    ensure_ascii=False,
                ),
                entry.stored_at,
                entry.expires_at,
                entry.freshness.value,
            ),
        )
        self._connection.commit()

        return entry

    def purge_expired(self) -> int:
        """Delete expired entries. Returns how many were removed."""
        cursor = self._connection.execute(
            "DELETE FROM search_cache WHERE expires_at < ?",
            (self.clock(),),
        )
        self._connection.commit()

        return cursor.rowcount if cursor.rowcount > 0 else 0

    def trim_to(self, max_bytes: int) -> int:
        """
        Delete the oldest entries until the payload total fits.

        Oldest-first, so the most recently useful answers survive longest.
        """
        removed = 0

        while self.total_bytes() > max_bytes:
            row = self._connection.execute(
                "SELECT key FROM search_cache "
                "ORDER BY stored_at ASC LIMIT 1"
            ).fetchone()

            if row is None:
                break

            self._connection.execute(
                "DELETE FROM search_cache WHERE key = ?",
                (row["key"],),
            )
            removed += 1

        if removed:
            self._connection.commit()

        return removed

    def clear(self) -> None:
        self._connection.execute("DELETE FROM search_cache")
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    # --------------------------------------------------------------- helpers

    def _row_to_entry(self, row: sqlite3.Row) -> CacheEntry | None:
        try:
            payload = json.loads(row["results"])
        except (json.JSONDecodeError, TypeError):
            return None

        if not isinstance(payload, list):
            return None

        results = tuple(
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("snippet", "")),
                provider=str(item.get("provider", "")),
            )
            for item in payload
            if isinstance(item, dict)
        )

        try:
            freshness = ContentFreshness(row["freshness"])
        except ValueError:
            freshness = ContentFreshness.DISCUSSION

        return CacheEntry(
            key=str(row["key"]),
            query=str(row["query"]),
            provider=str(row["provider"]),
            results=results,
            stored_at=float(row["stored_at"]),
            expires_at=float(row["expires_at"]),
            freshness=freshness,
        )


def main() -> None:
    """Show freshness classification on a few queries."""
    samples = [
        "قیمت دلار امروز",
        "بهترین لپ تاپ برای برنامه نویسی",
        "چطور پایتون نصب کنم",
        "python dataclass",
        "تاریخ تولد کوروش",
        "weather in Tehran",
    ]

    print("=== cache freshness classification ===")

    for sample in samples:
        freshness = classify_freshness(sample)
        ttl = ttl_for(freshness)

        if ttl < SECONDS_PER_HOUR:
            readable = f"{ttl / SECONDS_PER_MINUTE:.0f} minutes"
        elif ttl < SECONDS_PER_DAY:
            readable = f"{ttl / SECONDS_PER_HOUR:.0f} hours"
        else:
            readable = f"{ttl / SECONDS_PER_DAY:.0f} days"

        print(f"{freshness.value:12s} {readable:12s} {sample}")


if __name__ == "__main__":
    main()
