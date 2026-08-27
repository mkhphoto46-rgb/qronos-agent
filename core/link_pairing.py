"""
Pairing a phone with the PC.

The key never travels over the network. It is displayed on the PC's screen as a
QR code and read by the phone's camera, so there is no exchange to intercept and
nothing for a man-in-the-middle to sit inside. That single property is what
removes the hardest part of the problem, and it is why the rest of this module
is mostly about closing the window afterwards.

Four rules, each enforced here rather than left to the caller:

    Out of band   the key is shown, never sent
    Time limited  120 seconds, then the pending device is discarded
    Single use    one window pairs one device; opening a second cancels the first
    Local only    the completing peer must be on a nearby network

The last one is what makes Layer 2's untrusted relay acceptable. Because a
device can only ever be enrolled from the local network, a hostile relay cannot
add a phone — it can only carry traffic for phones the user paired in the room.

The payload is never written to disk and never logged. ``PairingPayload``
redacts its key in ``repr`` for the same reason ``DeviceRecord`` does.
"""

from __future__ import annotations

import base64
import socket
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlparse

from core.link_capability import LinkScope, scope_for_peer
from core.link_devices import (
    DeviceRecord,
    DeviceRegistry,
    DeviceStatus,
    SECRET_BYTES,
    is_valid_device_id,
)


# Long enough to pick up a phone and open the app, short enough that a window
# left open by accident is not an exposure worth worrying about.
PAIRING_WINDOW_SECONDS = 120.0

PAIRING_SCHEME = "qronos"
PAIRING_ACTION = "pair"
PAYLOAD_VERSION = 1

DEFAULT_PORT = 47_711


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


class PairingPayloadError(Exception):
    """A scanned payload could not be understood."""


class PairingRefusal(Enum):
    """Why a pairing attempt was refused."""

    NO_WINDOW = "no_window"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"
    WRONG_DEVICE = "wrong_device"
    NOT_LOCAL_NETWORK = "not_local_network"
    UNKNOWN_DEVICE = "unknown_device"
    NOT_PENDING = "not_pending"


def _b64url_encode(raw: bytes) -> str:
    """Base64url without padding, so the QR stays as small as possible."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)

    return base64.urlsafe_b64decode(text + padding)


@dataclass(frozen=True)
class PairingPayload:
    """
    What the QR code contains.

    A URI rather than bare JSON so a phone's scanner can deep-link straight
    into the app, the way ``otpauth://`` codes do.
    """

    host: str
    port: int
    device_id: str
    secret: bytes
    expires_at: float
    version: int = PAYLOAD_VERSION

    def __repr__(self) -> str:
        return (
            f"PairingPayload(host={self.host!r}, port={self.port}, "
            f"device_id={self.device_id!r}, "
            f"secret=<redacted {len(self.secret)} bytes>, "
            f"expires_at={self.expires_at})"
        )

    def to_uri(self) -> str:
        query = urlencode(
            {
                "v": self.version,
                "h": self.host,
                "p": self.port,
                "d": self.device_id,
                "k": _b64url_encode(self.secret),
                "e": f"{self.expires_at:.0f}",
            }
        )

        return f"{PAIRING_SCHEME}://{PAIRING_ACTION}?{query}"

    @classmethod
    def from_uri(cls, text: str) -> PairingPayload:
        """
        Parse a scanned code, refusing anything that is not exactly right.

        Every failure is a refusal. There is no partial parse and no default
        filled in for a missing field: a payload that is not fully understood
        is not a payload to pair with.
        """

        parsed = urlparse(text.strip())

        if parsed.scheme != PAIRING_SCHEME:
            raise PairingPayloadError(
                f"expected the {PAIRING_SCHEME} scheme, got {parsed.scheme!r}"
            )

        # urlparse puts the part after // into netloc.
        action = parsed.netloc or parsed.path.lstrip("/")

        if action != PAIRING_ACTION:
            raise PairingPayloadError(f"unknown action {action!r}")

        fields = parse_qs(parsed.query)

        def single(name: str) -> str:
            values = fields.get(name)

            if not values or len(values) != 1:
                raise PairingPayloadError(f"missing field {name!r}")

            return values[0]

        try:
            version = int(single("v"))
            port = int(single("p"))
            expires_at = float(single("e"))
        except ValueError as exc:
            raise PairingPayloadError(f"malformed number: {exc}") from exc

        if version != PAYLOAD_VERSION:
            raise PairingPayloadError(
                f"payload version {version} is not supported"
            )

        if not 1 <= port <= 65_535:
            raise PairingPayloadError(f"port {port} is out of range")

        device_id = single("d")

        if not is_valid_device_id(device_id):
            raise PairingPayloadError(f"bad device id {device_id!r}")

        try:
            secret = _b64url_decode(single("k"))
        except (ValueError, base64.binascii.Error) as exc:
            raise PairingPayloadError(f"malformed key: {exc}") from exc

        if len(secret) != SECRET_BYTES:
            raise PairingPayloadError(
                f"key is {len(secret)} bytes, expected {SECRET_BYTES}"
            )

        host = single("h")

        if not host:
            raise PairingPayloadError("empty host")

        return cls(
            host=host,
            port=port,
            device_id=device_id,
            secret=secret,
            expires_at=expires_at,
            version=version,
        )


@dataclass(frozen=True)
class PairingOutcome:
    """The result of a phone trying to complete pairing."""

    accepted: bool
    refusal: PairingRefusal | None = None
    record: DeviceRecord | None = None

    def describe(self) -> str:
        if self.accepted and self.record is not None:
            return f"paired {self.record.name} ({self.record.device_id})"

        reason = "unknown" if self.refusal is None else self.refusal.value

        return f"refused: {reason}"


@dataclass(frozen=True)
class OpenWindow:
    """The one pairing window that may be open at a time."""

    device_id: str
    opened_at: float
    expires_at: float
    payload: PairingPayload
    used: bool = False


def local_address() -> str:
    """
    The PC's address on its own network, for the QR code.

    Asks the operating system which local address it would use to reach the
    outside world. No packets are sent — a connected UDP socket only causes a
    routing decision — but it is the only reliable way to pick the right
    interface on a machine with several.

    Falls back to loopback, which makes pairing fail visibly on a machine with
    no network rather than handing out an address that cannot work.
    """

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        probe.connect(("8.8.8.8", 80))

        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class PairingService:
    """
    Opens and closes pairing windows.

    Holds at most one window. Opening a second cancels the first and discards
    its pending device, so a window forgotten on screen cannot be completed
    later by whoever finds it.
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        host: str | None = None,
        port: int = DEFAULT_PORT,
        window_seconds: float = PAIRING_WINDOW_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        self.registry = registry
        self.host = host if host is not None else local_address()
        self.port = port
        self.window_seconds = window_seconds
        self.clock: Clock = clock if clock is not None else time.time
        self._window: OpenWindow | None = None

    # ------------------------------------------------------------------ state

    @property
    def is_open(self) -> bool:
        """True while a window is open and unexpired and unused."""

        window = self._window

        if window is None:
            return False

        return not window.used and self.clock() < window.expires_at

    @property
    def window(self) -> OpenWindow | None:
        return self._window

    def seconds_remaining(self) -> float:
        if self._window is None:
            return 0.0

        return max(0.0, self._window.expires_at - self.clock())

    # ---------------------------------------------------------------- actions

    def open(self, name: str) -> PairingPayload:
        """
        Start pairing and return what the QR code should contain.

        The caller displays the URI. It must not log it or write it anywhere.
        """

        self.cancel()

        now = self.clock()
        record = self.registry.create(name=name)

        payload = PairingPayload(
            host=self.host,
            port=self.port,
            device_id=record.device_id,
            secret=record.secret,
            expires_at=now + self.window_seconds,
        )

        self._window = OpenWindow(
            device_id=record.device_id,
            opened_at=now,
            expires_at=payload.expires_at,
            payload=payload,
        )

        return payload

    def cancel(self) -> None:
        """
        Close the window, discarding an unpaired device.

        A device that completed pairing is left alone; only a still-pending one
        is removed, so cancelling does not undo a successful pairing that has
        already happened.
        """

        window = self._window
        self._window = None

        if window is None:
            return

        record = self.registry.get(window.device_id)

        if record is not None and record.status is DeviceStatus.PENDING:
            self.registry.remove(window.device_id)

    def expire_if_due(self) -> bool:
        """
        Close an expired window. True if one was closed.

        Called from the server's accept loop so an expired window is cleaned up
        even if nothing ever connects.
        """

        window = self._window

        if window is None or window.used:
            return False

        if self.clock() < window.expires_at:
            return False

        self.cancel()

        return True

    def complete(self, device_id: str, peer_address: str) -> PairingOutcome:
        """
        Finish pairing for a device that has just authenticated.

        Called after a successful handshake, because a successful handshake is
        already proof the phone holds the key. This method's job is only to
        check that it is the right device, in time, from the right place.

        Checks run in the order that reveals least: whether a window exists at
        all comes before anything about the device.
        """

        window = self._window

        if window is None:
            return PairingOutcome(False, PairingRefusal.NO_WINDOW)

        if window.used:
            return PairingOutcome(False, PairingRefusal.ALREADY_USED)

        if self.clock() >= window.expires_at:
            self.cancel()

            return PairingOutcome(False, PairingRefusal.EXPIRED)

        if device_id != window.device_id:
            return PairingOutcome(False, PairingRefusal.WRONG_DEVICE)

        if scope_for_peer(peer_address) is not LinkScope.LOCAL_NETWORK:
            return PairingOutcome(False, PairingRefusal.NOT_LOCAL_NETWORK)

        record = self.registry.get(device_id)

        if record is None:
            return PairingOutcome(False, PairingRefusal.UNKNOWN_DEVICE)

        if record.status is not DeviceStatus.PENDING:
            return PairingOutcome(False, PairingRefusal.NOT_PENDING)

        activated = self.registry.activate(device_id)

        # Mark the window used before returning, so a second attempt on the
        # same window is refused even if it arrives in the same instant.
        self._window = OpenWindow(
            device_id=window.device_id,
            opened_at=window.opened_at,
            expires_at=window.expires_at,
            payload=window.payload,
            used=True,
        )

        return PairingOutcome(True, None, activated)


def main() -> None:
    """Open a window, pair through it, then try to reuse it."""

    registry = DeviceRegistry(path=None)
    service = PairingService(registry, host="192.168.1.10", port=DEFAULT_PORT)

    payload = service.open("Amin's phone")

    print("QR contains a", len(payload.to_uri()), "character URI")
    print("payload repr :", payload)
    print("round trip   :",
          PairingPayload.from_uri(payload.to_uri()).device_id
          == payload.device_id)
    print()
    print("from the LAN     :",
          service.complete(payload.device_id, "192.168.1.42").describe())
    print("reused window    :",
          service.complete(payload.device_id, "192.168.1.42").describe())

    payload = service.open("second phone")
    print("from the internet:",
          service.complete(payload.device_id, "203.0.113.7").describe())


if __name__ == "__main__":
    main()
