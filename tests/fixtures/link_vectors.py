"""
Test vectors for the phone app, produced from the Python definitions.

The phone implements the same bytes as ``core/link_protocol.py``,
``core/link_pairing.py`` and ``core/link_relay_wire.py``. Its tests read
``mobile/protocol/link-vectors.json``; ``tests/test_link_mobile_contract.py``
regenerates the vectors here and fails if the committed file differs, so the
two sides cannot drift apart silently.

Regenerate after a deliberate protocol change with:

    python -m tests.fixtures.link_vectors

Every key and identity in here is a fixed test value, not a real device's.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.link_pairing import PairingPayload, _b64url_encode
from core.link_protocol import Event, Response, encode_frame, encode_payload
from core.link_relay_wire import (
    host_hello,
    phone_hello,
    route_id_for_secret,
    route_key_for_secret,
)


ROOT = Path(__file__).resolve().parent.parent.parent
VECTORS_PATH = ROOT / "mobile" / "protocol" / "link-vectors.json"

SECRETS = (bytes(range(32)), bytes(range(255, 223, -1)))
DEVICE_ID = "0123456789abcdef"


def _relay() -> list[dict[str, str]]:
    return [
        {
            "secret": secret.hex(),
            "route_key": route_key_for_secret(secret).hex(),
            "route_id": route_id_for_secret(secret).hex(),
            "phone_hello": phone_hello(secret).encode().hex(),
            "host_hello": host_hello(secret).encode().hex(),
        }
        for secret in SECRETS
    ]


def _frames() -> list[dict[str, object]]:
    cases: list[tuple[object, dict[str, object]]] = [
        (
            Response.success(7, {"answer": "پاسخ فارسی", "performed_actions": []}),
            {"kind": "response", "id": 7, "ok": True, "result": {"answer": "پاسخ فارسی", "performed_actions": []}},
        ),
        (
            Response.failure(8, "out_of_scope", "That is not allowed over this connection."),
            {"kind": "response", "id": 8, "ok": False, "code": "out_of_scope", "error": "That is not allowed over this connection."},
        ),
        (
            Event(topic="thinking", data={"phase": "answering", "progress": None}),
            {"kind": "event", "topic": "thinking", "data": {"phase": "answering", "progress": None}},
        ),
        (
            Event(topic="voice_reply", data={"seq": 0, "format": "wav", "data": "UklGRg==", "last": True}),
            {"kind": "event", "topic": "voice_reply", "data": {"seq": 0, "format": "wav", "data": "UklGRg==", "last": True}},
        ),
    ]

    return [{"bytes": encode_frame(frame).hex(), "expect": expect} for frame, expect in cases]


def _bad_frames() -> list[dict[str, str]]:
    request = encode_payload({"kind": "request", "id": 1, "op": "ping", "params": {}})

    return [
        {"bytes": "00000000", "fault": "malformed_length"},
        {"bytes": "7fffffff", "fault": "oversize"},
        {"bytes": "ffffffff", "fault": "oversize"},
        {"bytes": "000000107b7d", "fault": "truncated"},
        {"bytes": "0003", "fault": "malformed_length"},
        {"bytes": encode_payload_raw(b"{nope").hex(), "fault": "malformed_json"},
        {"bytes": encode_payload_raw(b'{"kind":"gossip"}').hex(), "fault": "unknown_kind"},
        {"bytes": request.hex(), "fault": "unexpected_frame"},
    ]


def encode_payload_raw(body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + body


def _pairing() -> dict[str, object]:
    key = SECRETS[0]
    payload = PairingPayload(
        host="192.168.1.20",
        port=47711,
        device_id=DEVICE_ID,
        secret=key,
        expires_at=1_800_000_120.0,
    )
    good = payload.to_uri()
    k = _b64url_encode(key)

    def uri(**fields: str) -> str:
        base = {"v": "1", "h": "192.168.1.20", "p": "47711", "d": DEVICE_ID, "k": k, "e": "1800000120"}
        base.update(fields)
        query = "&".join(f"{name}={value}" for name, value in base.items() if value is not None)

        return f"qronos://pair?{query}"

    return {
        "valid": [
            {
                "uri": good,
                "host": "192.168.1.20",
                "port": 47711,
                "device_id": DEVICE_ID,
                "key": key.hex(),
                "expires_at": 1_800_000_120.0,
            }
        ],
        "invalid": [
            {"uri": good.replace("qronos://", "https://", 1), "why": "scheme"},
            {"uri": good.replace("://pair?", "://unpair?", 1), "why": "action"},
            {"uri": uri(v="2"), "why": "version"},
            {"uri": uri(p="0"), "why": "port"},
            {"uri": uri(p="70000"), "why": "port"},
            {"uri": uri(d="0123456789ABCDEF"), "why": "device id"},
            {"uri": uri(d="0123"), "why": "device id"},
            {"uri": uri(k=_b64url_encode(key[:31])), "why": "key length"},
            {"uri": uri(k="!!!"), "why": "key encoding"},
            {"uri": uri(h=""), "why": "host"},
            {"uri": good + "&d=" + DEVICE_ID, "why": "repeated field"},
            {"uri": "qronos://pair?v=1&h=x&p=1&d=" + DEVICE_ID + "&e=1", "why": "missing key"},
        ],
    }


def build_vectors() -> dict[str, object]:
    return {
        "comment": "Generated from the Qronos Python link modules by tests/fixtures/link_vectors.py. Test values only.",
        "relay": _relay(),
        "frames": _frames(),
        "bad_frames": _bad_frames(),
        "pairing": _pairing(),
        "max_frame_bytes": 1 << 20,
    }


def render() -> str:
    return json.dumps(build_vectors(), ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    VECTORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    VECTORS_PATH.write_text(render(), encoding="utf-8", newline="\n")
    print("wrote", VECTORS_PATH.relative_to(ROOT))


if __name__ == "__main__":
    main()
