from __future__ import annotations

import threading
import unittest

from core.link_commands import (
    CommandLedger,
    CommandState,
    RequestBudget,
    command_fingerprint,
    is_valid_command_id,
)


DEVICE = "0123456789abcdef"
OTHER = "fedcba9876543210"
CID = "a" * 32
CID2 = "b" * 32


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def ok(result: object = "answer") -> dict[str, object]:
    return {"ok": True, "result": result}


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.ledger = CommandLedger(clock=self.clock)
        self.fp = command_fingerprint("ask", {"question": "سلام"})


class TestOnce(LedgerTestCase):
    def test_a_new_command_is_new(self) -> None:
        self.assertIs(self.ledger.begin(DEVICE, CID, self.fp).state, CommandState.NEW)

    def test_a_retry_while_running_is_not_run_again(self) -> None:
        self.ledger.begin(DEVICE, CID, self.fp)

        self.assertIs(
            self.ledger.begin(DEVICE, CID, self.fp).state, CommandState.IN_PROGRESS
        )

    def test_a_retry_after_finishing_gets_the_same_answer(self) -> None:
        self.ledger.begin(DEVICE, CID, self.fp)
        self.ledger.finish(DEVICE, CID, ok("پاسخ"))

        decision = self.ledger.begin(DEVICE, CID, self.fp)

        self.assertIs(decision.state, CommandState.DONE)
        self.assertEqual(decision.outcome, ok("پاسخ"))

    def test_the_same_id_with_different_parameters_is_a_conflict(self) -> None:
        # Otherwise a retry could be rewritten into a different command that
        # skips execution and collects the first command's answer.
        self.ledger.begin(DEVICE, CID, self.fp)
        self.ledger.finish(DEVICE, CID, ok())

        other = command_fingerprint("ask", {"question": "something else"})

        self.assertIs(self.ledger.begin(DEVICE, CID, other).state, CommandState.CONFLICT)

    def test_ids_are_per_device(self) -> None:
        self.ledger.begin(DEVICE, CID, self.fp)
        self.ledger.finish(DEVICE, CID, ok("mine"))

        self.assertIs(self.ledger.begin(OTHER, CID, self.fp).state, CommandState.NEW)

    def test_concurrent_copies_are_claimed_exactly_once(self) -> None:
        results: list[CommandState] = []
        barrier = threading.Barrier(8)

        def attempt() -> None:
            barrier.wait()
            results.append(self.ledger.begin(DEVICE, CID, self.fp).state)

        threads = [threading.Thread(target=attempt) for _ in range(8)]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        self.assertEqual(results.count(CommandState.NEW), 1)
        self.assertEqual(results.count(CommandState.IN_PROGRESS), 7)

    def test_fingerprints_ignore_key_order_but_not_values(self) -> None:
        self.assertEqual(
            command_fingerprint("ask", {"a": 1, "b": 2}),
            command_fingerprint("ask", {"b": 2, "a": 1}),
        )
        self.assertNotEqual(
            command_fingerprint("ask", {"a": 1}),
            command_fingerprint("search", {"a": 1}),
        )

    def test_the_fingerprint_does_not_contain_the_question(self) -> None:
        self.assertNotIn("سلام", self.fp)


class TestBounds(LedgerTestCase):
    def test_finished_entries_expire(self) -> None:
        self.ledger.begin(DEVICE, CID, self.fp)
        self.ledger.finish(DEVICE, CID, ok())
        self.clock.advance(self.ledger.ttl + 1)

        self.assertIs(self.ledger.begin(DEVICE, CID, self.fp).state, CommandState.NEW)

    def test_running_entries_do_not_expire(self) -> None:
        # Expiring a command that is still running would let a retry run it a
        # second time, which is exactly what the ledger exists to prevent.
        self.ledger.begin(DEVICE, CID, self.fp)
        self.clock.advance(self.ledger.ttl * 10)

        self.assertIs(
            self.ledger.begin(DEVICE, CID, self.fp).state, CommandState.IN_PROGRESS
        )

    def test_each_device_has_a_cap(self) -> None:
        ledger = CommandLedger(max_per_device=3, clock=self.clock)

        for n in range(10):
            cid = f"{n:032x}"
            ledger.begin(DEVICE, cid, self.fp)
            ledger.finish(DEVICE, cid, ok())

        self.assertEqual(ledger.size(DEVICE), 3)

    def test_a_full_ledger_of_running_commands_refuses_more(self) -> None:
        ledger = CommandLedger(max_per_device=2, clock=self.clock)
        ledger.begin(DEVICE, CID, self.fp)
        ledger.begin(DEVICE, CID2, self.fp)

        self.assertIs(
            ledger.begin(DEVICE, "c" * 32, self.fp).state, CommandState.IN_PROGRESS
        )
        self.assertEqual(ledger.size(DEVICE), 2)

    def test_an_answer_too_large_to_keep_still_stops_a_rerun(self) -> None:
        ledger = CommandLedger(max_result_bytes=100, clock=self.clock)
        ledger.begin(DEVICE, CID, self.fp)
        ledger.finish(DEVICE, CID, ok("x" * 1000))

        decision = ledger.begin(DEVICE, CID, self.fp)

        self.assertIs(decision.state, CommandState.DONE)
        self.assertIsNone(decision.outcome)
        self.assertEqual(ledger.retained_bytes, 0)

    def test_the_total_kept_is_bounded_and_oldest_goes_first(self) -> None:
        ledger = CommandLedger(max_result_bytes=1000, max_total_bytes=1500, clock=self.clock)

        for n in range(3):
            cid = f"{n:032x}"
            ledger.begin(DEVICE, cid, self.fp)
            self.clock.advance(1)
            ledger.finish(DEVICE, cid, ok("x" * 600))

        self.assertLessEqual(ledger.retained_bytes, 1500)
        self.assertIsNone(ledger.begin(DEVICE, f"{0:032x}", self.fp).outcome)
        self.assertIsNotNone(ledger.begin(DEVICE, f"{2:032x}", self.fp).outcome)

    def test_forgetting_a_device_releases_its_answers(self) -> None:
        self.ledger.begin(DEVICE, CID, self.fp)
        self.ledger.finish(DEVICE, CID, ok("x" * 100))
        self.ledger.forget_device(DEVICE)

        self.assertEqual(self.ledger.retained_bytes, 0)
        self.assertEqual(self.ledger.size(DEVICE), 0)


class TestCommandIds(unittest.TestCase):
    def test_shape(self) -> None:
        self.assertTrue(is_valid_command_id("0123456789abcdef0123456789abcdef"))

        for bad in ("", "A" * 32, "a" * 31, "a" * 33, "g" * 32, None, 5):
            with self.subTest(value=bad):
                self.assertFalse(is_valid_command_id(bad))


class TestRequestBudget(unittest.TestCase):
    def test_a_burst_then_a_trickle(self) -> None:
        clock = FakeClock()
        budget = RequestBudget(capacity=3, refill_per_second=0.5, clock=clock)

        self.assertEqual([budget.take() for _ in range(4)], [True, True, True, False])

        clock.advance(2.0)
        self.assertTrue(budget.take())
        self.assertFalse(budget.take())

    def test_refill_stops_at_capacity(self) -> None:
        clock = FakeClock()
        budget = RequestBudget(capacity=2, refill_per_second=10, clock=clock)
        clock.advance(100)

        self.assertEqual([budget.take() for _ in range(3)], [True, True, False])


if __name__ == "__main__":
    unittest.main()
