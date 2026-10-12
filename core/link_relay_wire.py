"""
The few bytes a relay is allowed to understand.

Layer 2 puts an untrusted relay between the phone and the PC. The relay's whole
job is to put two connections together and copy bytes, so the only thing it
needs to read is which two connections belong together. This module defines
that, and nothing else: a fixed 37-byte hello from each side, and one status
byte back. Everything after the status byte is the Layer 1 TLS session, end to
end, which the relay cannot read, alter or inject into.

    hello   = magic(4) "QRL1" | role(1) | token(32)
    status  = one byte from the relay, possibly repeated (keepalives)

The PC is the *host*. Its token is a route key, a credential derived from the
device's pairing key. The phone's token is the route id, the SHA-256 of that
key. The relay files a host under the hash of what it presented, so a host can
only ever be parked under a route whose key it holds, and the phone, which
asks by id, never has to send the key at all.

What the relay learns, stated plainly because it is the price of the design:
one opaque route per remote-enabled device, the key for it once the PC has
parked, timings and byte counts. What it cannot learn is the pairing key: the
route key is an HMAC of it, which is one-way. What it can do with the route key
is pretend to be the PC to the phone, and that fails at the inner handshake
because it does not hold the pairing key. A hostile relay can therefore deny
service and observe metadata. It cannot read, alter, inject or enrol.

Nothing here touches a socket. The phone app and the relay implement the same
bytes, and their test vectors are produced from this module, so this is the
definition the other two are checked against.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from enum import Enum


MAGIC = b"QRL1"

TOKEN_BYTES = 32

HELLO_BYTES = len(MAGIC) + 1 + TOKEN_BYTES

# Domain-separation labels. Changing either changes every route, which is a
# deliberate, versioned break rather than something to do casually.
ROUTE_KEY_LABEL = b"qronos relay route key v1"
ROUTE_ID_LABEL = b"qronos relay route id v1"


class RelayRole(Enum):
    """Which end of a route a connection is."""

    # The PC. It dials out and parks, waiting for its phone.
    HOST = 1

    # The phone. It dials in and asks to be joined to a parked host.
    PHONE = 2


class RelayStatus(Enum):
    """
    The relay's one-byte answers.

    Below 0x10 is progress; 0x10 and above ends the connection. A client treats
    any byte it does not recognise as the end, so a newer relay cannot keep an
    older client waiting.
    """

    # Host only: filed under its route and waiting for a phone.
    PARKED = 0x00

    # Both sides: joined. Every byte after this one belongs to the other end.
    SPLICED = 0x01

    # Host only, while parked: the relay is still there. Sent so a NAT or a
    # middlebox does not drop an idle connection, and so the PC can tell a
    # relay that has gone from one with nobody to join.
    KEEPALIVE = 0x02

    # Phone: no host is parked for this route.
    NO_HOST = 0x10

    # Host: this route already has as many parked hosts as it may.
    ROUTE_FULL = 0x11

    # Either: the hello was not a hello.
    BAD_HELLO = 0x12

    # Either: the relay is at its connection limit.
    OVER_CAPACITY = 0x13

    # Host: parked for as long as a parking may last. Dial again.
    PARK_EXPIRED = 0x14

    @property
    def is_terminal(self) -> bool:
        return self.value >= 0x10


class RelayWireError(Exception):
    """Bytes that are not a valid hello."""


@dataclass(frozen=True)
class RelayHello:
    """One side's opening message."""

    role: RelayRole
    token: bytes

    def __post_init__(self) -> None:
        if len(self.token) != TOKEN_BYTES:
            raise RelayWireError(
                f"token is {len(self.token)} bytes, expected {TOKEN_BYTES}"
            )

    def __repr__(self) -> str:
        # A host's token is a credential. Neither kind is worth printing.
        return f"RelayHello(role={self.role.name}, token=<redacted>)"

    def encode(self) -> bytes:
        return MAGIC + bytes([self.role.value]) + self.token

    @property
    def route_id(self) -> bytes:
        """The route this hello is for, whichever side sent it."""

        if self.role is RelayRole.HOST:
            return route_id_for_key(self.token)

        return self.token


def decode_hello(raw: bytes) -> RelayHello:
    """
    Parse exactly one hello, refusing anything else.

    The relay reads exactly ``HELLO_BYTES`` before calling this, so a short or
    long buffer here is a caller bug, and is refused rather than truncated.
    """

    if len(raw) != HELLO_BYTES:
        raise RelayWireError(f"hello is {len(raw)} bytes, expected {HELLO_BYTES}")

    if raw[: len(MAGIC)] != MAGIC:
        raise RelayWireError("bad magic")

    try:
        role = RelayRole(raw[len(MAGIC)])
    except ValueError as exc:
        raise RelayWireError(f"unknown role {raw[len(MAGIC)]}") from exc

    return RelayHello(role=role, token=bytes(raw[len(MAGIC) + 1:]))


def decode_status(value: int) -> RelayStatus | None:
    """The status a byte stands for, or ``None`` for one this side does not know."""

    try:
        return RelayStatus(value)
    except ValueError:
        return None


def route_key_for_secret(secret: bytes) -> bytes:
    """
    The host's credential for one device's route.

    An HMAC of the pairing key, so the relay that is shown it learns nothing it
    could use to authenticate as either end of the link itself.
    """

    if not secret:
        raise ValueError("a route needs a pairing key")

    return hmac.new(secret, ROUTE_KEY_LABEL, hashlib.sha256).digest()


def route_id_for_key(route_key: bytes) -> bytes:
    """The public name of a route: what the phone asks for."""

    return hashlib.sha256(ROUTE_ID_LABEL + route_key).digest()


def route_id_for_secret(secret: bytes) -> bytes:
    return route_id_for_key(route_key_for_secret(secret))


def host_hello(secret: bytes) -> RelayHello:
    """What the PC sends to park for one device."""

    return RelayHello(role=RelayRole.HOST, token=route_key_for_secret(secret))


def phone_hello(secret: bytes) -> RelayHello:
    """What the phone sends to reach its PC."""

    return RelayHello(role=RelayRole.PHONE, token=route_id_for_secret(secret))


def main() -> None:
    """Show the hello for a throwaway key, and that both ends meet."""

    import secrets

    secret = secrets.token_bytes(32)
    host = host_hello(secret)
    phone = phone_hello(secret)

    print("hello size :", HELLO_BYTES, "bytes")
    print("host       :", host)
    print("phone      :", phone)
    print("same route :", host.route_id == phone.route_id)
    print("round trip :", decode_hello(phone.encode()) == phone)


if __name__ == "__main__":
    main()
