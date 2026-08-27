"""
Wire protocol for the Qronos device link.

The phone and the PC exchange length-prefixed JSON frames:

    +--------+-----------------------------+
    | len(4) | UTF-8 JSON payload          |
    +--------+-----------------------------+

Four bytes of big-endian length, then the payload. Three kinds of frame travel
over it: a request from the phone, a response from the PC, and an unsolicited
event from the PC so the phone can show progress while a long answer is being
produced.

Two rules shape the whole module.

The declared length is checked against a maximum *before* anything is
allocated, so a hostile or broken peer cannot make Qronos reserve a gigabyte by
claiming it is about to send one.

Nothing here touches a socket. Reading is expressed as a callable that returns
exactly the number of bytes asked for, which lets every framing test run with no
network, no threads and no timers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol


# Four bytes is enough for any frame we would ever accept and keeps the header
# aligned for the reader on the phone side.
LENGTH_PREFIX_BYTES = 4

# One mebibyte. Requests are small; the largest realistic frame is a long
# answer with its evidence, which is far below this. A push-to-talk audio
# clip is sent as several frames rather than one, so this does not need to
# accommodate a whole recording.
MAX_FRAME_BYTES = 1 << 20

# Field names, kept in one place so the client and server cannot drift.
FIELD_KIND = "kind"
FIELD_ID = "id"
FIELD_OP = "op"
FIELD_PARAMS = "params"
FIELD_OK = "ok"
FIELD_RESULT = "result"
FIELD_ERROR = "error"
FIELD_CODE = "code"
FIELD_TOPIC = "topic"
FIELD_DATA = "data"


class FrameKind(Enum):
    """What a frame is."""

    REQUEST = "request"
    RESPONSE = "response"
    EVENT = "event"


class ProtocolFault(Enum):
    """
    Why a frame was refused.

    These are named rather than collapsed into one error because the server
    logs them and a burst of one particular fault says something different
    from a burst of another.
    """

    OVERSIZE = "oversize"
    TRUNCATED = "truncated"
    MALFORMED_LENGTH = "malformed_length"
    MALFORMED_JSON = "malformed_json"
    NOT_AN_OBJECT = "not_an_object"
    UNKNOWN_KIND = "unknown_kind"
    MISSING_FIELD = "missing_field"
    BAD_FIELD_TYPE = "bad_field_type"


class ProtocolError(Exception):
    """A frame could not be encoded or decoded."""

    def __init__(self, fault: ProtocolFault, detail: str = "") -> None:
        self.fault = fault
        self.detail = detail

        message = fault.value if not detail else f"{fault.value}: {detail}"
        super().__init__(message)


class ReadExactly(Protocol):
    """Returns exactly ``count`` bytes, or fewer only at end of stream."""

    def __call__(self, count: int) -> bytes:  # pragma: no cover - protocol
        ...


# --------------------------------------------------------------------- frames


@dataclass(frozen=True)
class Request:
    """A phone asking the PC to do one thing."""

    id: int
    op: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            FIELD_KIND: FrameKind.REQUEST.value,
            FIELD_ID: self.id,
            FIELD_OP: self.op,
            FIELD_PARAMS: dict(self.params),
        }


@dataclass(frozen=True)
class Response:
    """The PC's answer to exactly one request."""

    id: int
    ok: bool
    result: Any = None
    error: str = ""
    code: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            FIELD_KIND: FrameKind.RESPONSE.value,
            FIELD_ID: self.id,
            FIELD_OK: self.ok,
        }

        if self.ok:
            payload[FIELD_RESULT] = self.result
        else:
            payload[FIELD_ERROR] = self.error
            payload[FIELD_CODE] = self.code

        return payload

    @classmethod
    def failure(cls, request_id: int, code: str, error: str) -> Response:
        return cls(id=request_id, ok=False, error=error, code=code)

    @classmethod
    def success(cls, request_id: int, result: Any) -> Response:
        return cls(id=request_id, ok=True, result=result)


@dataclass(frozen=True)
class Event:
    """
    Progress, pushed from the PC without being asked.

    This is what lets the phone say "searching", then "reading page 2 of 3",
    using the same phases the research pipeline already reports.
    """

    topic: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            FIELD_KIND: FrameKind.EVENT.value,
            FIELD_TOPIC: self.topic,
            FIELD_DATA: dict(self.data),
        }


Frame = Request | Response | Event


# ------------------------------------------------------------------- encoding


def encode_payload(payload: dict[str, Any]) -> bytes:
    """
    Turn one payload into a complete frame, length prefix included.

    ``ensure_ascii`` is off so Persian text travels as UTF-8 rather than as
    three times its length in escape sequences.
    """

    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            ProtocolFault.MALFORMED_JSON,
            f"payload is not serialisable: {exc}",
        ) from exc

    if len(body) > MAX_FRAME_BYTES:
        raise ProtocolError(
            ProtocolFault.OVERSIZE,
            f"{len(body)} bytes exceeds the {MAX_FRAME_BYTES} byte limit",
        )

    prefix = len(body).to_bytes(LENGTH_PREFIX_BYTES, "big")

    return prefix + body


def encode_frame(frame: Frame) -> bytes:
    """Encode a request, response or event."""
    return encode_payload(frame.to_payload())


# ------------------------------------------------------------------- decoding


def read_frame(read_exactly: ReadExactly) -> dict[str, Any] | None:
    """
    Read one frame.

    Returns ``None`` at a clean end of stream, which is how a peer hanging up
    between frames is distinguished from one hanging up mid-frame.

    The length is validated before the body is requested. That ordering is the
    whole defence against a declared-length attack, so it is not an
    implementation detail.
    """

    prefix = read_exactly(LENGTH_PREFIX_BYTES)

    if not prefix:
        return None

    if len(prefix) < LENGTH_PREFIX_BYTES:
        raise ProtocolError(
            ProtocolFault.MALFORMED_LENGTH,
            f"length prefix was {len(prefix)} bytes",
        )

    length = int.from_bytes(prefix, "big")

    if length == 0:
        raise ProtocolError(ProtocolFault.MALFORMED_LENGTH, "zero length")

    if length > MAX_FRAME_BYTES:
        raise ProtocolError(
            ProtocolFault.OVERSIZE,
            f"peer declared {length} bytes",
        )

    body = read_exactly(length)

    if len(body) < length:
        raise ProtocolError(
            ProtocolFault.TRUNCATED,
            f"expected {length} bytes, received {len(body)}",
        )

    return decode_payload(body)


def decode_payload(body: bytes) -> dict[str, Any]:
    """Decode one frame body into a payload object."""

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(
            ProtocolFault.MALFORMED_JSON,
            str(exc),
        ) from exc

    if not isinstance(payload, dict):
        raise ProtocolError(
            ProtocolFault.NOT_AN_OBJECT,
            f"payload was {type(payload).__name__}",
        )

    return payload


def frame_kind(payload: dict[str, Any]) -> FrameKind:
    """Read the kind out of a payload, refusing anything unrecognised."""

    raw = payload.get(FIELD_KIND)

    if not isinstance(raw, str):
        raise ProtocolError(
            ProtocolFault.MISSING_FIELD,
            f"{FIELD_KIND} is missing or not a string",
        )

    try:
        return FrameKind(raw)
    except ValueError as exc:
        raise ProtocolError(ProtocolFault.UNKNOWN_KIND, raw) from exc


def parse_frame(payload: dict[str, Any]) -> Frame:
    """Turn a decoded payload into the frame it claims to be."""

    kind = frame_kind(payload)

    if kind is FrameKind.REQUEST:
        return _parse_request(payload)

    if kind is FrameKind.RESPONSE:
        return _parse_response(payload)

    return _parse_event(payload)


def _require(payload: dict[str, Any], name: str, expected: type) -> Any:
    if name not in payload:
        raise ProtocolError(ProtocolFault.MISSING_FIELD, name)

    value = payload[name]

    # bool is a subclass of int, so an id of ``true`` would otherwise pass.
    if expected is int and isinstance(value, bool):
        raise ProtocolError(ProtocolFault.BAD_FIELD_TYPE, f"{name} is bool")

    if not isinstance(value, expected):
        raise ProtocolError(
            ProtocolFault.BAD_FIELD_TYPE,
            f"{name} is {type(value).__name__}, expected {expected.__name__}",
        )

    return value


def _parse_request(payload: dict[str, Any]) -> Request:
    params = payload.get(FIELD_PARAMS, {})

    if not isinstance(params, dict):
        raise ProtocolError(
            ProtocolFault.BAD_FIELD_TYPE,
            f"{FIELD_PARAMS} is {type(params).__name__}",
        )

    return Request(
        id=_require(payload, FIELD_ID, int),
        op=_require(payload, FIELD_OP, str),
        params=params,
    )


def _parse_response(payload: dict[str, Any]) -> Response:
    ok = _require(payload, FIELD_OK, bool)

    return Response(
        id=_require(payload, FIELD_ID, int),
        ok=ok,
        result=payload.get(FIELD_RESULT),
        error=str(payload.get(FIELD_ERROR, "")),
        code=str(payload.get(FIELD_CODE, "")),
    )


def _parse_event(payload: dict[str, Any]) -> Event:
    data = payload.get(FIELD_DATA, {})

    if not isinstance(data, dict):
        raise ProtocolError(
            ProtocolFault.BAD_FIELD_TYPE,
            f"{FIELD_DATA} is {type(data).__name__}",
        )

    return Event(topic=_require(payload, FIELD_TOPIC, str), data=data)


def reader_over(chunks: bytes) -> Callable[[int], bytes]:
    """
    A ``read_exactly`` over a fixed byte string.

    Present so tests, and the demo below, can drive the reader without a
    socket.
    """

    position = 0

    def read(count: int) -> bytes:
        nonlocal position

        piece = chunks[position:position + count]
        position += len(piece)

        return piece

    return read


def main() -> None:
    """Round-trip one frame of each kind."""

    frames: tuple[Frame, ...] = (
        Request(id=1, op="search", params={"query": "قیمت دلار امروز"}),
        Response.success(1, {"answer": "..."}),
        Event(topic="reading", data={"done": 2, "total": 3}),
    )

    stream = b"".join(encode_frame(frame) for frame in frames)
    read = reader_over(stream)

    print(f"{len(frames)} frames in {len(stream)} bytes")

    while True:
        payload = read_frame(read)

        if payload is None:
            break

        print(" ", parse_frame(payload))


if __name__ == "__main__":
    main()
