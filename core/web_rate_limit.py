from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.config import CONFIG


# Measured on 2026-08-27 against html.duckduckgo.com from two different
# countries, which returned the same figures — so this is DuckDuckGo's own
# policy rather than an IP reputation effect:
#
#   requests 1 and 2   ->  10 results each
#   request 3 onward   ->  0 results
#   recovery           ->  about 4 minutes (242 seconds measured)
#
# Capacity 2 with one token every 2 minutes reproduces that envelope: two
# searches available in a burst, sustained rate of one per two minutes.
DUCKDUCKGO_CAPACITY = 2
DUCKDUCKGO_REFILL_SECONDS = 120.0

# Probing during a cooldown appeared to extend it, so a refusal is recorded as
# a penalty that holds the bucket empty rather than something to retry through.
DUCKDUCKGO_PENALTY_SECONDS = 240.0

DEFAULT_STATE_PATH = CONFIG.paths.data / "web_rate_limit.json"


class Clock(Protocol):
    """A source of monotonic-ish wall time, injectable for tests."""

    def __call__(self) -> float:
        ...


@dataclass(frozen=True)
class BucketState:
    """Serialisable token-bucket state."""

    tokens: float
    last_refill_at: float
    blocked_until: float = 0.0

    def to_json(self) -> dict[str, float]:
        return {
            "tokens": self.tokens,
            "last_refill_at": self.last_refill_at,
            "blocked_until": self.blocked_until,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> BucketState:
        return cls(
            tokens=float(data.get("tokens", 0.0)),  # type: ignore[arg-type]
            last_refill_at=float(
                data.get("last_refill_at", 0.0)  # type: ignore[arg-type]
            ),
            blocked_until=float(
                data.get("blocked_until", 0.0)  # type: ignore[arg-type]
            ),
        )


class StateStore(Protocol):
    """Persistence for bucket state, keyed by provider name."""

    def load(self) -> dict[str, BucketState]:
        ...

    def save(self, states: dict[str, BucketState]) -> None:
        ...


class InMemoryStateStore:
    """Non-persistent store, used by tests."""

    def __init__(self) -> None:
        self._states: dict[str, BucketState] = {}
        self.save_count = 0

    def load(self) -> dict[str, BucketState]:
        return dict(self._states)

    def save(self, states: dict[str, BucketState]) -> None:
        self._states = dict(states)
        self.save_count += 1


class JsonFileStateStore:
    """
    Bucket state persisted to a small JSON file.

    Persistence matters more than it looks. Without it, restarting Qronos would
    reset every bucket to full and the first few searches after a restart would
    walk straight into a provider's limit — and a user who restarts because
    something felt slow is exactly the user about to get throttled.

    Written via a temporary file and :func:`os.replace` so a crash mid-write
    leaves the previous state rather than a truncated file.
    """

    def __init__(self, path: str | Path = DEFAULT_STATE_PATH) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, BucketState]:
        if not self.path.is_file():
            return {}

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt state file is not worth failing over. Starting from
            # empty is the conservative direction: an empty bucket refuses
            # requests until it refills, which is safer than granting them.
            return {}

        if not isinstance(raw, dict):
            return {}

        states: dict[str, BucketState] = {}

        for name, payload in raw.items():
            if isinstance(payload, dict):
                states[str(name)] = BucketState.from_json(payload)

        return states

    def save(self, states: dict[str, BucketState]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            name: state.to_json()
            for name, state in states.items()
        }

        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.tmp"
        )

        try:
            temporary.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class BucketConfig:
    """Limits for one provider."""

    name: str
    capacity: int
    refill_seconds: float
    penalty_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError(
                f"capacity must be at least 1: {self.capacity}"
            )

        if self.refill_seconds <= 0:
            raise ValueError(
                f"refill_seconds must be positive: {self.refill_seconds}"
            )


# Wikipedia, Stack Exchange and Marginalia are not rate-limited here. They
# have their own generous limits, they are not throttled by DuckDuckGo's
# bucket, and treating them as scarce would push work onto the one provider
# that genuinely is.
DUCKDUCKGO_BUCKET = BucketConfig(
    name="duckduckgo",
    capacity=DUCKDUCKGO_CAPACITY,
    refill_seconds=DUCKDUCKGO_REFILL_SECONDS,
    penalty_seconds=DUCKDUCKGO_PENALTY_SECONDS,
)


@dataclass(frozen=True)
class Verdict:
    """Whether a request may proceed, and when to try again if not."""

    allowed: bool
    reason: str
    retry_after_seconds: float = 0.0
    tokens_remaining: float = 0.0


class RateLimiter:
    """
    Token buckets for search providers.

    Two jobs. It stops Qronos walking into a provider's limit, and when a
    provider refuses anyway it records that as a penalty so nothing retries
    through the cooldown.

    The clock is injected so the whole thing is testable without waiting.
    """

    def __init__(
        self,
        buckets: tuple[BucketConfig, ...] = (DUCKDUCKGO_BUCKET,),
        store: StateStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        import time

        self.clock: Clock = clock if clock is not None else time.time
        self.buckets = {bucket.name: bucket for bucket in buckets}

        self.store: StateStore = (
            store if store is not None else InMemoryStateStore()
        )

        self._states: dict[str, BucketState] = self.store.load()

    # ---------------------------------------------------------------- queries

    def is_limited(self, provider: str) -> bool:
        """True when this provider has a configured bucket."""
        return provider in self.buckets

    def check(self, provider: str) -> Verdict:
        """
        Report whether a request would be allowed, without consuming a token.

        A provider with no configured bucket is always allowed: absence of a
        limit is not the same as a limit of zero.
        """
        bucket = self.buckets.get(provider)

        if bucket is None:
            return Verdict(
                allowed=True,
                reason=f"{provider} has no configured rate limit.",
            )

        now = self.clock()
        state = self._refilled(bucket, now)

        if now < state.blocked_until:
            return Verdict(
                allowed=False,
                reason=(
                    f"{provider} refused a recent request; holding off until "
                    "the cooldown expires."
                ),
                retry_after_seconds=state.blocked_until - now,
                tokens_remaining=state.tokens,
            )

        if state.tokens < 1.0:
            missing = 1.0 - state.tokens

            return Verdict(
                allowed=False,
                reason=f"{provider} budget exhausted.",
                retry_after_seconds=missing * bucket.refill_seconds,
                tokens_remaining=state.tokens,
            )

        return Verdict(
            allowed=True,
            reason=f"{provider} has budget available.",
            tokens_remaining=state.tokens,
        )

    # -------------------------------------------------------------- mutations

    def acquire(self, provider: str) -> Verdict:
        """
        Consume one token if available.

        Returns the same verdict shape as :meth:`check`, but on success the
        token is spent. Callers must treat a refusal as final for this attempt
        and route elsewhere rather than looping.
        """
        verdict = self.check(provider)

        if not verdict.allowed:
            return verdict

        bucket = self.buckets.get(provider)

        if bucket is None:
            return verdict

        now = self.clock()
        state = self._refilled(bucket, now)

        self._states[provider] = BucketState(
            tokens=state.tokens - 1.0,
            last_refill_at=now,
            blocked_until=state.blocked_until,
        )

        self._persist()

        return Verdict(
            allowed=True,
            reason=verdict.reason,
            tokens_remaining=self._states[provider].tokens,
        )

    def record_refusal(self, provider: str) -> None:
        """
        Record that the provider refused a request.

        Empties the bucket and starts the penalty window. DuckDuckGo does not
        say "you are throttled" — it returns zero results — so the caller
        cannot tell a refusal from a genuinely empty result set. Treating both
        as a refusal is the safe reading: it costs a few minutes of using other
        sources, while the alternative is hammering a provider that has already
        said no.
        """
        bucket = self.buckets.get(provider)

        if bucket is None:
            return

        now = self.clock()

        self._states[provider] = BucketState(
            tokens=0.0,
            last_refill_at=now,
            blocked_until=now + bucket.penalty_seconds,
        )

        self._persist()

    def record_success(self, provider: str) -> None:
        """
        Clear any penalty after a successful request.

        The token already spent is not refunded; this only lifts a cooldown
        that has evidently ended.
        """
        bucket = self.buckets.get(provider)

        if bucket is None:
            return

        state = self._states.get(provider)

        if state is None or state.blocked_until == 0.0:
            return

        self._states[provider] = BucketState(
            tokens=state.tokens,
            last_refill_at=state.last_refill_at,
            blocked_until=0.0,
        )

        self._persist()

    def reset(self, provider: str | None = None) -> None:
        """Clear state for one provider, or all of them."""
        if provider is None:
            self._states.clear()
        else:
            self._states.pop(provider, None)

        self._persist()

    # ---------------------------------------------------------------- helpers

    def _refilled(
        self,
        bucket: BucketConfig,
        now: float,
    ) -> BucketState:
        """
        Return the bucket's state brought up to date at ``now``.

        A bucket with no recorded state starts full. That is the right default
        for a fresh installation, and the persisted state stops it being a
        loophole across restarts.
        """
        state = self._states.get(bucket.name)

        if state is None:
            return BucketState(
                tokens=float(bucket.capacity),
                last_refill_at=now,
            )

        elapsed = max(0.0, now - state.last_refill_at)
        gained = elapsed / bucket.refill_seconds

        return BucketState(
            tokens=min(float(bucket.capacity), state.tokens + gained),
            last_refill_at=now,
            blocked_until=state.blocked_until,
        )

    def _persist(self) -> None:
        self.store.save(dict(self._states))


def main() -> None:
    """Show the DuckDuckGo bucket behaving as measured."""
    fake_now = [1_800_000_000.0]

    def clock() -> float:
        return fake_now[0]

    limiter = RateLimiter(clock=clock)

    print("=== DuckDuckGo bucket: capacity 2, one token per 2 minutes ===")

    for attempt in range(1, 5):
        verdict = limiter.acquire("duckduckgo")
        print(
            f"attempt {attempt}: allowed={verdict.allowed} "
            f"tokens={verdict.tokens_remaining:.2f} "
            f"retry_after={verdict.retry_after_seconds:.0f}s"
        )

    print("\n... 2 minutes pass ...")
    fake_now[0] += 120.0

    verdict = limiter.acquire("duckduckgo")
    print(f"after refill: allowed={verdict.allowed}")

    print("\nprovider refuses (0 results):")
    limiter.record_refusal("duckduckgo")
    verdict = limiter.check("duckduckgo")
    print(
        f"  allowed={verdict.allowed} "
        f"retry_after={verdict.retry_after_seconds:.0f}s"
    )


if __name__ == "__main__":
    main()
