"""
A clock a test can move by hand.

Three test files had already written this class out, character for character,
before a fourth was about to. Time-dependent behaviour is the bulk of what the
queue and the load monitor do, so the copies were going to keep coming.

Nothing here is clever. It is a callable that returns a number, which is what
every ``clock: Clock | None`` parameter in ``core/`` expects, and a method to
move that number. Tests that use it never sleep, so the suite stays fast and
stops depending on how loaded the machine running it happens to be.
"""

from __future__ import annotations


class FakeClock:
    """A stopped clock that only moves when a test says so."""

    def __init__(self, initial_time: float = 1000.0) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        """Move time forward. Negative values are refused."""
        if seconds < 0:
            raise ValueError("A fake clock does not run backwards.")

        self.current_time += seconds
