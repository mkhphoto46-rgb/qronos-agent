"""
The relay's copy of the rendezvous wire format.

The definition lives in the Qronos repository at ``core/link_relay_wire.py``.
The relay is deployed on its own, on a machine that has none of Qronos on it,
so it carries this small copy rather than importing the product. A test in the
Qronos suite (``tests/test_link_relay_server.py``) fails if the two differ.

    hello   = magic(4) "QRL1" | role(1) | token(32)
    status  = one byte from the relay

The relay needs exactly one computation: a host presents a route key and is
filed under its hash, the route id, which is what a phone asks for.
"""

from __future__ import annotations

import hashlib
from enum import Enum


MAGIC = b"QRL1"
TOKEN_BYTES = 32
HELLO_BYTES = len(MAGIC) + 1 + TOKEN_BYTES

ROUTE_ID_LABEL = b"qronos relay route id v1"

ROLE_HOST = 1
ROLE_PHONE = 2


class Status(Enum):
    PARKED = 0x00
    SPLICED = 0x01
    KEEPALIVE = 0x02
    NO_HOST = 0x10
    ROUTE_FULL = 0x11
    BAD_HELLO = 0x12
    OVER_CAPACITY = 0x13
    PARK_EXPIRED = 0x14

    @property
    def byte(self) -> bytes:
        return bytes([self.value])


class BadHello(ValueError):
    """Thirty-seven bytes that are not a hello."""


def parse_hello(raw: bytes) -> tuple[int, bytes]:
    """
    Return ``(role, route_id)`` for one hello, or raise ``BadHello``.

    For a host the route id is computed from the key it presented, so a host
    can only ever be filed under a route whose key it holds.
    """

    if len(raw) != HELLO_BYTES or raw[: len(MAGIC)] != MAGIC:
        raise BadHello("not a hello")

    role = raw[len(MAGIC)]
    token = bytes(raw[len(MAGIC) + 1:])

    if role == ROLE_HOST:
        return role, hashlib.sha256(ROUTE_ID_LABEL + token).digest()

    if role == ROLE_PHONE:
        return role, token

    raise BadHello("unknown role")
