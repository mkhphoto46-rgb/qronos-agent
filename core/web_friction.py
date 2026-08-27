from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from core.config import CONFIG
from core.web_provenance import registrable_host


DEFAULT_FRICTION_PATH = CONFIG.paths.data / "web_friction.sqlite3"

SECONDS_PER_HOUR = 3_600.0
SECONDS_PER_DAY = 86_400.0

# Escalating cool-off. A site having a bad afternoon comes back; a site that
# genuinely walls automated clients quietly leaves Qronos's world. The user is
# never shown a CAPTCHA during an ordinary search — the result is skipped and
# another source is used instead.
COOLOFF_LADDER: tuple[float, ...] = (
    6 * SECONDS_PER_HOUR,
    3 * SECONDS_PER_DAY,
    30 * SECONDS_PER_DAY,
)

# Beyond the ladder the domain is effectively skipped, revisited about monthly
# in case whatever was blocking has been removed.
PERMANENT_RETRY_SECONDS = 30 * SECONDS_PER_DAY


class FrictionSignal(Enum):
    """
    Why a page refused to be read.

    Every one of these means "this source is not worth the wait", not "retry
    harder". None of them is surfaced to the user mid-search.
    """

    CAPTCHA = "captcha"
    FORBIDDEN = "forbidden"              # HTTP 403
    RATE_LIMITED = "rate_limited"        # HTTP 429
    CHALLENGE = "challenge"              # Cloudflare-style interstitial
    CONSENT_WALL = "consent_wall"
    LOGIN_WALL = "login_wall"
    EMPTY_AFTER_FETCH = "empty_after_fetch"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"


@dataclass(frozen=True)
class FrictionRecord:
    """What is known about one domain's willingness to be read."""

    host: str
    refusal_count: int
    last_refusal_at: float
    cooloff_until: float
    last_success_at: float = 0.0
    last_signal: str = ""

    def is_cooling_off(self, now: float) -> bool:
        return now < self.cooloff_until

    def remaining_cooloff(self, now: float) -> float:
        return max(0.0, self.cooloff_until - now)

    @property
    def is_on_skip_list(self) -> bool:
        """Past the end of the ladder: skipped, revisited about monthly."""
        return self.refusal_count > len(COOLOFF_LADDER)


def cooloff_for(refusal_count: int) -> float:
    """
    How long to wait after the nth refusal.

    Escalating rather than fixed, because a first refusal is often transient
    and a fourth is not.
    """
    if refusal_count <= 0:
        return 0.0

    if refusal_count <= len(COOLOFF_LADDER):
        return COOLOFF_LADDER[refusal_count - 1]

    return PERMANENT_RETRY_SECONDS


class FrictionMemory:
    """
    Remembers which domains refuse to be read, and stops asking them.

    Qronos never shows the user a CAPTCHA during an ordinary search. When a
    page refuses, the refusal is written down, that result is skipped, and
    another source is used. Over weeks this learns the shape of the readable
    web from this machine and this connection, and stops spending seconds on
    doors that do not open.

    Deliberately scoped to **pages**, not search providers. An earlier design
    applied it to providers too, and testing showed that is useless there:
    Mojeek issues a CAPTCHA on the very first request and never recovers, so a
    cool-off has nothing to wait for. Real CAPTCHAs and consent walls live on
    the pages being fetched, which is where this belongs.

    A SQLite table and arithmetic. No model, nothing measured, no judgement of
    whether a site is any good — only whether it answered.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_FRICTION_PATH,
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

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS friction (
                host            TEXT PRIMARY KEY,
                refusal_count   INTEGER NOT NULL,
                last_refusal_at REAL NOT NULL,
                cooloff_until   REAL NOT NULL,
                last_success_at REAL NOT NULL DEFAULT 0,
                last_signal     TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._connection.commit()

    # ----------------------------------------------------------------- reads

    def get(self, url_or_host: str) -> FrictionRecord | None:
        host = self._host_of(url_or_host)

        if not host:
            return None

        row = self._connection.execute(
            "SELECT * FROM friction WHERE host = ?",
            (host,),
        ).fetchone()

        if row is None:
            return None

        return FrictionRecord(
            host=str(row["host"]),
            refusal_count=int(row["refusal_count"]),
            last_refusal_at=float(row["last_refusal_at"]),
            cooloff_until=float(row["cooloff_until"]),
            last_success_at=float(row["last_success_at"]),
            last_signal=str(row["last_signal"]),
        )

    def should_skip(self, url_or_host: str) -> bool:
        """
        True when this domain is cooling off and should not be fetched.

        An unparseable URL is skipped as well: a path Qronos cannot even name
        is not one it should be opening.
        """
        host = self._host_of(url_or_host)

        if not host:
            return True

        record = self.get(host)

        if record is None:
            return False

        return record.is_cooling_off(self.clock())

    def filter_urls(self, urls: tuple[str, ...]) -> tuple[str, ...]:
        """Drop URLs whose domain is cooling off, preserving order."""
        return tuple(url for url in urls if not self.should_skip(url))

    def record_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM friction"
        ).fetchone()

        return int(row["n"]) if row is not None else 0

    def cooling_off_hosts(self) -> tuple[str, ...]:
        now = self.clock()

        rows = self._connection.execute(
            "SELECT host FROM friction WHERE cooloff_until > ? "
            "ORDER BY cooloff_until DESC",
            (now,),
        ).fetchall()

        return tuple(str(row["host"]) for row in rows)

    # ---------------------------------------------------------------- writes

    def record_refusal(
        self,
        url_or_host: str,
        signal: FrictionSignal,
    ) -> FrictionRecord | None:
        """
        Note that this domain refused, and escalate its cool-off.

        Returns the updated record, or None when the URL could not be named.
        """
        host = self._host_of(url_or_host)

        if not host:
            return None

        now = self.clock()
        existing = self.get(host)
        count = (existing.refusal_count if existing else 0) + 1

        record = FrictionRecord(
            host=host,
            refusal_count=count,
            last_refusal_at=now,
            cooloff_until=now + cooloff_for(count),
            last_success_at=(
                existing.last_success_at if existing else 0.0
            ),
            last_signal=signal.value,
        )

        self._write(record)

        return record

    def record_success(self, url_or_host: str) -> None:
        """
        Note that this domain answered, and clear its refusal history.

        A full reset rather than a decrement: a site that works now is a site
        that works, and carrying old refusals forward would keep punishing it
        for an outage that is over.
        """
        host = self._host_of(url_or_host)

        if not host:
            return

        now = self.clock()

        self._write(
            FrictionRecord(
                host=host,
                refusal_count=0,
                last_refusal_at=0.0,
                cooloff_until=0.0,
                last_success_at=now,
                last_signal="",
            )
        )

    def forget(self, url_or_host: str) -> None:
        host = self._host_of(url_or_host)

        if not host:
            return

        self._connection.execute(
            "DELETE FROM friction WHERE host = ?",
            (host,),
        )
        self._connection.commit()

    def clear(self) -> None:
        self._connection.execute("DELETE FROM friction")
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _host_of(url_or_host: str) -> str:
        return registrable_host(url_or_host)

    def _write(self, record: FrictionRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO friction
                (host, refusal_count, last_refusal_at, cooloff_until,
                 last_success_at, last_signal)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(host) DO UPDATE SET
                refusal_count   = excluded.refusal_count,
                last_refusal_at = excluded.last_refusal_at,
                cooloff_until   = excluded.cooloff_until,
                last_success_at = excluded.last_success_at,
                last_signal     = excluded.last_signal
            """,
            (
                record.host,
                record.refusal_count,
                record.last_refusal_at,
                record.cooloff_until,
                record.last_success_at,
                record.last_signal,
            ),
        )
        self._connection.commit()


def main() -> None:
    """Show the cool-off ladder escalating."""
    fake_now = [1_800_000_000.0]

    memory = FrictionMemory(
        path=":memory:",
        clock=lambda: fake_now[0],
    )

    url = "https://walled.example.com/article"

    print("=== friction ladder ===")

    for attempt in range(1, 6):
        record = memory.record_refusal(url, FrictionSignal.CAPTCHA)

        assert record is not None

        remaining = record.remaining_cooloff(fake_now[0])

        print(
            f"refusal {attempt}: cool off "
            f"{remaining / SECONDS_PER_HOUR:7.1f} hours  "
            f"skip_list={record.is_on_skip_list}"
        )

    print("\none success clears the history:")
    memory.record_success(url)

    print(f"  should_skip = {memory.should_skip(url)}")

    memory.close()


if __name__ == "__main__":
    main()
