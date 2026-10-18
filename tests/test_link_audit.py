from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.link_audit import (
    INVALID_DEVICE,
    UNPARSED_PEER,
    AuditEvent,
    AuditLog,
    AuditRecord,
)
from core.link_capability import AuthReason, LinkOp, LinkScope
from core.link_pairing import PairingRefusal
from core.link_protocol import ProtocolFault


START = 1_800_000_000.0
DEVICE = "0123456789abcdef"


class FakeClock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class AuditTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.log = AuditLog(path=None, clock=self.clock)


class TestRecording(AuditTestCase):
    def test_a_record_is_kept(self) -> None:
        self.log.record(AuditEvent.LINK_STARTED)

        self.assertEqual(len(self.log.records()), 1)

    def test_records_keep_their_order(self) -> None:
        self.log.record(AuditEvent.LINK_STARTED)
        self.log.record(AuditEvent.HANDSHAKE_OK, device_id=DEVICE)
        self.log.record(AuditEvent.LINK_STOPPED)

        self.assertEqual(
            [record.event for record in self.log.records()],
            [
                AuditEvent.LINK_STARTED,
                AuditEvent.HANDSHAKE_OK,
                AuditEvent.LINK_STOPPED,
            ],
        )

    def test_the_clock_is_recorded(self) -> None:
        self.clock.advance(42)

        record = self.log.record(AuditEvent.LINK_STARTED)

        self.assertEqual(record.at, START + 42)

    def test_events_can_be_counted(self) -> None:
        for _ in range(3):
            self.log.record(AuditEvent.HANDSHAKE_REFUSED)

        self.assertEqual(self.log.count(AuditEvent.HANDSHAKE_REFUSED), 3)
        self.assertEqual(self.log.count(AuditEvent.HANDSHAKE_OK), 0)

    def test_a_full_record_carries_every_field(self) -> None:
        record = self.log.record(
            AuditEvent.REQUEST_REFUSED,
            device_id=DEVICE,
            peer="192.168.1.42",
            scope=LinkScope.LOCAL_NETWORK,
            op=LinkOp.RUN_APP,
            reason=AuthReason.OUT_OF_SCOPE,
        )

        payload = record.to_json()

        self.assertEqual(payload["device"], DEVICE)
        self.assertEqual(payload["peer"], "192.168.1.42")
        self.assertEqual(payload["scope"], "local_network")
        self.assertEqual(payload["op"], "run_app")
        self.assertEqual(payload["reason"], "out_of_scope")

    def test_absent_fields_are_omitted_rather_than_null(self) -> None:
        payload = self.log.record(AuditEvent.LINK_STARTED).to_json()

        self.assertEqual(set(payload), {"at", "event"})

    def test_reasons_from_every_source_enum_are_accepted(self) -> None:
        # The reason field carries values from four different enums, and the
        # log should not need to know which.
        for reason in (
            AuthReason.NOT_GRANTED,
            PairingRefusal.EXPIRED,
            ProtocolFault.OVERSIZE,
        ):
            with self.subTest(reason=reason):
                record = self.log.record(
                    AuditEvent.REQUEST_REFUSED, reason=reason
                )

                self.assertEqual(record.reason, reason.value)


class TestDataMinimisation(AuditTestCase):
    def test_free_text_is_refused(self) -> None:
        # The one field that looks like it might take arbitrary text does not.
        # This is what stops a question, an answer or a file path reaching the
        # log through it.
        with self.assertRaises(TypeError) as caught:
            self.log.record(
                AuditEvent.REQUEST_ALLOWED,
                reason="the user asked about their bank balance",  # type: ignore[arg-type]
            )

        self.assertIn("free text", str(caught.exception))

    def test_a_number_is_also_refused(self) -> None:
        with self.assertRaises(TypeError):
            self.log.record(AuditEvent.REQUEST_ALLOWED, reason=42)  # type: ignore[arg-type]

    def test_the_record_has_no_field_for_request_parameters(self) -> None:
        # Enforced by the shape of the record rather than by a rule someone
        # has to remember.
        fields = set(AuditRecord.__dataclass_fields__)

        for forbidden in ("params", "payload", "body", "text", "detail", "query"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, fields)


class TestSanitising(AuditTestCase):
    def test_an_untrusted_identity_is_not_recorded_verbatim(self) -> None:
        # The identity in a handshake comes from the peer. An unbounded
        # attacker-chosen string does not belong in a log file.
        record = self.log.record(
            AuditEvent.HANDSHAKE_REFUSED, device_id="../../etc/passwd"
        )

        self.assertEqual(record.device_id, INVALID_DEVICE)

    def test_a_newline_in_an_identity_cannot_forge_a_log_line(self) -> None:
        record = self.log.record(
            AuditEvent.HANDSHAKE_REFUSED,
            device_id='aaaa"}\n{"event":"handshake_ok"',
        )

        self.assertEqual(record.device_id, INVALID_DEVICE)

    def test_a_valid_identity_is_recorded_as_it_is(self) -> None:
        record = self.log.record(AuditEvent.HANDSHAKE_OK, device_id=DEVICE)

        self.assertEqual(record.device_id, DEVICE)

    def test_an_unparseable_peer_is_marked_as_such(self) -> None:
        record = self.log.record(
            AuditEvent.HANDSHAKE_REFUSED, peer="somewhere else"
        )

        self.assertEqual(record.peer, UNPARSED_PEER)

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        record = self.log.record(AuditEvent.HANDSHAKE_OK, peer=" 192.168.1.5 ")

        self.assertEqual(record.peer, "192.168.1.5")

    def test_an_address_with_leading_zeros_is_not_parsed(self) -> None:
        # Python refuses these as ambiguous octal notation, and so does this
        # log. A peer address handed over by the operating system never has
        # them, so anything that does is not an address worth recording.
        record = self.log.record(AuditEvent.HANDSHAKE_OK, peer="192.168.001.5")

        self.assertEqual(record.peer, UNPARSED_PEER)

    def test_an_ipv6_address_is_normalised(self) -> None:
        record = self.log.record(
            AuditEvent.HANDSHAKE_OK, peer="FE80:0000:0000:0000:0000:0000:0000:0001"
        )

        self.assertEqual(record.peer, "fe80::1")

    def test_an_empty_device_or_peer_stays_empty(self) -> None:
        record = self.log.record(AuditEvent.LINK_STARTED)

        self.assertEqual(record.device_id, "")
        self.assertEqual(record.peer, "")


class TestDescription(AuditTestCase):
    def test_a_description_names_the_event(self) -> None:
        self.log.record(AuditEvent.HANDSHAKE_OK, device_id=DEVICE)

        self.assertIn("handshake_ok", self.log.describe())

    def test_a_description_names_the_operation_and_reason(self) -> None:
        record = self.log.record(
            AuditEvent.REQUEST_REFUSED,
            device_id=DEVICE,
            op=LinkOp.RUN_APP,
            reason=AuthReason.OUT_OF_SCOPE,
        )

        text = record.describe()

        self.assertIn("run_app", text)
        self.assertIn("out_of_scope", text)

    def test_a_description_is_limited_to_the_recent_lines(self) -> None:
        for _ in range(30):
            self.log.record(AuditEvent.HANDSHAKE_OK, device_id=DEVICE)

        self.assertEqual(len(self.log.describe(limit=5).splitlines()), 5)


class TestTheFile(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "link_audit.jsonl"
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def log(self, max_bytes: int = 1_000_000) -> AuditLog:
        return AuditLog(path=self.path, clock=self.clock, max_bytes=max_bytes)

    def lines(self) -> list[dict[str, object]]:
        text = self.path.read_text(encoding="utf-8")

        return [json.loads(line) for line in text.splitlines() if line]

    def test_a_record_reaches_the_file_as_one_json_line(self) -> None:
        self.log().record(AuditEvent.HANDSHAKE_OK, device_id=DEVICE)

        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(self.lines()[0]["event"], "handshake_ok")

    def test_records_append_rather_than_overwrite(self) -> None:
        first = self.log()
        first.record(AuditEvent.LINK_STARTED)

        second = self.log()
        second.record(AuditEvent.LINK_STOPPED)

        self.assertEqual(len(self.lines()), 2)

    def test_the_directory_is_created_if_missing(self) -> None:
        nested = Path(self.directory.name) / "deep" / "down" / "audit.jsonl"

        AuditLog(path=nested, clock=self.clock).record(AuditEvent.LINK_STARTED)

        self.assertTrue(nested.exists())

    def test_persian_is_not_escaped_in_the_file(self) -> None:
        # Nothing in the log is Persian today, but the encoder must not mangle
        # it if a name ever is.
        AuditLog(path=self.path, clock=self.clock).record(
            AuditEvent.LINK_STARTED
        )

        self.assertNotIn("\\u", self.path.read_text(encoding="utf-8"))

    def test_the_file_rolls_over_when_it_grows_too_large(self) -> None:
        log = self.log(max_bytes=400)

        for _ in range(40):
            log.record(AuditEvent.HANDSHAKE_OK, device_id=DEVICE)

        rolled = self.path.with_suffix(self.path.suffix + ".1")

        self.assertTrue(rolled.exists())
        self.assertLessEqual(self.path.stat().st_size, 400 + 200)

    def test_only_one_rollover_is_kept(self) -> None:
        log = self.log(max_bytes=200)

        for _ in range(120):
            log.record(AuditEvent.HANDSHAKE_OK, device_id=DEVICE)

        siblings = sorted(
            child.name for child in Path(self.directory.name).iterdir()
        )

        self.assertEqual(siblings, ["link_audit.jsonl", "link_audit.jsonl.1"])

    def test_a_log_that_cannot_be_written_does_not_break_the_link(self) -> None:
        # Losing an audit line is preferable to dropping the user's connection
        # over a full disk.
        blocked = Path(self.directory.name) / "a-file" / "audit.jsonl"
        (Path(self.directory.name) / "a-file").write_text("not a directory")

        log = AuditLog(path=blocked, clock=self.clock)
        record = log.record(AuditEvent.LINK_STARTED)

        self.assertEqual(record.event, AuditEvent.LINK_STARTED)
        self.assertEqual(len(log.records()), 1)


if __name__ == "__main__":
    unittest.main()
