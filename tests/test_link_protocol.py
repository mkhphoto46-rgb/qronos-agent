from __future__ import annotations

import json
import unittest

from core.link_protocol import (
    LENGTH_PREFIX_BYTES,
    MAX_FRAME_BYTES,
    Event,
    FrameKind,
    ProtocolError,
    ProtocolFault,
    Request,
    Response,
    decode_payload,
    encode_frame,
    encode_payload,
    frame_kind,
    parse_frame,
    read_frame,
    reader_over,
)


class TestEncoding(unittest.TestCase):
    def test_a_frame_carries_its_own_length(self) -> None:
        frame = encode_frame(Request(id=1, op="ping"))

        declared = int.from_bytes(frame[:LENGTH_PREFIX_BYTES], "big")

        self.assertEqual(declared, len(frame) - LENGTH_PREFIX_BYTES)

    def test_persian_travels_as_utf8_not_as_escapes(self) -> None:
        # Escaping would roughly triple the size of every Persian query.
        frame = encode_frame(Request(id=1, op="search", params={"q": "دلار"}))

        self.assertIn("دلار".encode("utf-8"), frame)
        self.assertNotIn(b"\\u", frame)

    def test_an_oversized_payload_is_refused_at_encode_time(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            encode_payload({"kind": "event", "topic": "x",
                            "data": {"blob": "y" * (MAX_FRAME_BYTES + 10)}})

        self.assertIs(caught.exception.fault, ProtocolFault.OVERSIZE)

    def test_an_unserialisable_payload_is_refused(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            encode_payload({"kind": "event", "topic": "x", "data": {"s": {1, 2}}})

        self.assertIs(caught.exception.fault, ProtocolFault.MALFORMED_JSON)

    def test_a_successful_response_omits_the_error_fields(self) -> None:
        payload = Response.success(3, {"a": 1}).to_payload()

        self.assertIn("result", payload)
        self.assertNotIn("error", payload)

    def test_a_failed_response_omits_the_result_field(self) -> None:
        payload = Response.failure(3, "denied", "no").to_payload()

        self.assertIn("error", payload)
        self.assertNotIn("result", payload)


class TestRoundTrip(unittest.TestCase):
    def test_a_request_survives_the_trip(self) -> None:
        original = Request(id=7, op="search", params={"query": "قیمت دلار"})

        parsed = parse_frame(
            decode_payload(encode_frame(original)[LENGTH_PREFIX_BYTES:])
        )

        self.assertEqual(parsed, original)

    def test_a_response_survives_the_trip(self) -> None:
        original = Response.success(7, {"answer": "بله"})

        parsed = parse_frame(
            decode_payload(encode_frame(original)[LENGTH_PREFIX_BYTES:])
        )

        self.assertEqual(parsed, original)

    def test_an_event_survives_the_trip(self) -> None:
        original = Event(topic="reading", data={"done": 2, "total": 3})

        parsed = parse_frame(
            decode_payload(encode_frame(original)[LENGTH_PREFIX_BYTES:])
        )

        self.assertEqual(parsed, original)

    def test_several_frames_read_back_in_order(self) -> None:
        frames = (
            Request(id=1, op="ping"),
            Event(topic="searching"),
            Response.success(1, True),
        )

        read = reader_over(b"".join(encode_frame(f) for f in frames))
        seen = []

        while True:
            payload = read_frame(read)

            if payload is None:
                break

            seen.append(parse_frame(payload))

        self.assertEqual(tuple(seen), frames)


class TestReading(unittest.TestCase):
    def test_a_clean_end_of_stream_returns_none(self) -> None:
        # Which is how a peer hanging up between frames is told apart from one
        # hanging up mid-frame.
        self.assertIsNone(read_frame(reader_over(b"")))

    def test_a_short_length_prefix_is_a_fault(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            read_frame(reader_over(b"\x00\x01"))

        self.assertIs(caught.exception.fault, ProtocolFault.MALFORMED_LENGTH)

    def test_a_zero_length_frame_is_a_fault(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            read_frame(reader_over(b"\x00\x00\x00\x00"))

        self.assertIs(caught.exception.fault, ProtocolFault.MALFORMED_LENGTH)

    def test_a_huge_declared_length_is_refused_before_reading(self) -> None:
        # The whole point: a peer claiming a gigabyte must not cause a
        # gigabyte to be reserved. Only the four-byte prefix is consumed.
        consumed = []

        def read(count: int) -> bytes:
            consumed.append(count)

            return (0xFFFFFFFF).to_bytes(4, "big") if count == 4 else b""

        with self.assertRaises(ProtocolError) as caught:
            read_frame(read)

        self.assertIs(caught.exception.fault, ProtocolFault.OVERSIZE)
        self.assertEqual(consumed, [LENGTH_PREFIX_BYTES])

    def test_a_truncated_body_is_a_fault(self) -> None:
        frame = encode_frame(Request(id=1, op="ping"))

        with self.assertRaises(ProtocolError) as caught:
            read_frame(reader_over(frame[:-3]))

        self.assertIs(caught.exception.fault, ProtocolFault.TRUNCATED)


class TestDecoding(unittest.TestCase):
    def test_malformed_json_is_a_fault(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            decode_payload(b"{ not json")

        self.assertIs(caught.exception.fault, ProtocolFault.MALFORMED_JSON)

    def test_invalid_utf8_is_a_fault(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            decode_payload(b"\xff\xfe\x00")

        self.assertIs(caught.exception.fault, ProtocolFault.MALFORMED_JSON)

    def test_a_json_array_is_not_a_payload(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            decode_payload(b"[1, 2, 3]")

        self.assertIs(caught.exception.fault, ProtocolFault.NOT_AN_OBJECT)

    def test_a_missing_kind_is_a_fault(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            frame_kind({"id": 1})

        self.assertIs(caught.exception.fault, ProtocolFault.MISSING_FIELD)

    def test_an_unknown_kind_is_a_fault(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            frame_kind({"kind": "command"})

        self.assertIs(caught.exception.fault, ProtocolFault.UNKNOWN_KIND)

    def test_every_kind_is_recognised(self) -> None:
        for kind in FrameKind:
            with self.subTest(kind=kind):
                self.assertIs(frame_kind({"kind": kind.value}), kind)


class TestFieldValidation(unittest.TestCase):
    def test_a_request_without_an_op_is_refused(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            parse_frame({"kind": "request", "id": 1})

        self.assertIs(caught.exception.fault, ProtocolFault.MISSING_FIELD)

    def test_a_boolean_id_is_not_an_integer(self) -> None:
        # bool is a subclass of int, so ``true`` would otherwise pass as an id.
        with self.assertRaises(ProtocolError) as caught:
            parse_frame({"kind": "request", "id": True, "op": "ping"})

        self.assertIs(caught.exception.fault, ProtocolFault.BAD_FIELD_TYPE)

    def test_a_string_id_is_refused(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            parse_frame({"kind": "request", "id": "1", "op": "ping"})

        self.assertIs(caught.exception.fault, ProtocolFault.BAD_FIELD_TYPE)

    def test_non_object_params_are_refused(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            parse_frame({"kind": "request", "id": 1, "op": "x", "params": []})

        self.assertIs(caught.exception.fault, ProtocolFault.BAD_FIELD_TYPE)

    def test_missing_params_default_to_empty(self) -> None:
        frame = parse_frame({"kind": "request", "id": 1, "op": "ping"})

        self.assertEqual(frame.params, {})

    def test_a_response_needs_a_boolean_ok(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            parse_frame({"kind": "response", "id": 1, "ok": "yes"})

        self.assertIs(caught.exception.fault, ProtocolFault.BAD_FIELD_TYPE)

    def test_an_event_needs_a_topic(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            parse_frame({"kind": "event", "data": {}})

        self.assertIs(caught.exception.fault, ProtocolFault.MISSING_FIELD)

    def test_non_object_event_data_is_refused(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            parse_frame({"kind": "event", "topic": "x", "data": "text"})

        self.assertIs(caught.exception.fault, ProtocolFault.BAD_FIELD_TYPE)


class TestFaultsAreDistinguishable(unittest.TestCase):
    def test_the_fault_is_readable_from_the_exception(self) -> None:
        # The server logs these, and a burst of one says something different
        # from a burst of another.
        error = ProtocolError(ProtocolFault.OVERSIZE, "1 GB")

        self.assertIs(error.fault, ProtocolFault.OVERSIZE)
        self.assertIn("oversize", str(error))
        self.assertIn("1 GB", str(error))

    def test_a_fault_with_no_detail_still_reads(self) -> None:
        self.assertEqual(str(ProtocolError(ProtocolFault.TRUNCATED)), "truncated")


class TestAtTheSizeLimit(unittest.TestCase):
    def test_a_frame_exactly_at_the_limit_is_accepted(self) -> None:
        # Built by trimming a payload until its encoding is exactly the cap, so
        # the boundary itself is exercised rather than approximated.
        filler = "y" * (MAX_FRAME_BYTES - 64)
        payload = {"kind": "event", "topic": "x", "data": {"b": filler}}

        while len(json.dumps(payload, separators=(",", ":")).encode()) < MAX_FRAME_BYTES:
            filler += "y"
            payload["data"] = {"b": filler}

        while len(json.dumps(payload, separators=(",", ":")).encode()) > MAX_FRAME_BYTES:
            filler = filler[:-1]
            payload["data"] = {"b": filler}

        frame = encode_payload(payload)

        self.assertEqual(len(frame) - LENGTH_PREFIX_BYTES, MAX_FRAME_BYTES)
        self.assertEqual(read_frame(reader_over(frame)), payload)


if __name__ == "__main__":
    unittest.main()
