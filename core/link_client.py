"""
The phone side, in Python.

This is the reference implementation of the client half of the link. It exists
for two reasons: the tests drive the real server through it, so the protocol is
exercised end to end rather than in halves, and whoever writes the phone app has
a working specification to read instead of a prose description to interpret.

The phone app has to do exactly what this does:

    1. Scan the QR code and parse the ``qronos://pair`` URI.
    2. Store the device id and key in the platform keystore, protected by
       biometrics or a PIN. This module keeps them in memory, which is the one
       thing the app must not copy.
    3. Connect with TLS 1.3, offering the device id as the PSK identity.
    4. Send length-prefixed JSON requests, read responses, and treat any event
       frame that arrives as progress rather than as an answer.

There is no certificate to check and no fingerprint to pin. A server that does
not hold the same key cannot complete the handshake, so reaching the point where
frames flow is itself the proof that the PC is the right one.
"""

from __future__ import annotations

import socket
import ssl
from typing import Any, Callable

from core.link_pairing import PairingPayload
from core.link_protocol import (
    Event,
    ProtocolError,
    Request,
    Response,
    encode_frame,
    parse_frame,
    read_frame,
)
from core.link_transport import client_context, reader_for


DEFAULT_TIMEOUT_SECONDS = 30.0


class LinkClientError(Exception):
    """The client could not complete what it was asked to do."""


class LinkClient:
    """One connection to one PC."""

    def __init__(
        self,
        host: str,
        port: int,
        device_id: str,
        secret: bytes,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.device_id = device_id
        self.secret = secret
        self.timeout = timeout
        self.on_event = on_event

        self._connection: ssl.SSLSocket | None = None
        self._next_id = 1
        self._events: list[Event] = []

    def __repr__(self) -> str:
        """The key never appears, here or anywhere else."""

        return (
            f"LinkClient(host={self.host!r}, port={self.port}, "
            f"device_id={self.device_id!r}, "
            f"connected={self._connection is not None})"
        )

    # ----------------------------------------------------------- construction

    @classmethod
    def from_payload(
        cls,
        payload: PairingPayload,
        host: str | None = None,
        port: int | None = None,
        **kwargs: Any,
    ) -> LinkClient:
        """
        Build a client from a parsed pairing payload.

        ``host`` and ``port`` override what the payload carries, which is only
        needed when the PC is listening on an ephemeral port — the tests, and
        the demo.
        """

        return cls(
            host=host if host is not None else payload.host,
            port=port if port is not None else payload.port,
            device_id=payload.device_id,
            secret=payload.secret,
            **kwargs,
        )

    @classmethod
    def from_uri(cls, uri: str, **kwargs: Any) -> LinkClient:
        """Build a client straight from a scanned QR code."""
        return cls.from_payload(PairingPayload.from_uri(uri), **kwargs)

    # ------------------------------------------------------------- connection

    @property
    def connected(self) -> bool:
        return self._connection is not None

    def connect(self) -> None:
        if self._connection is not None:
            raise LinkClientError("already connected")

        context = client_context(self.device_id, self.secret)

        try:
            raw = socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )
        except OSError as exc:
            raise LinkClientError(f"cannot reach {self.host}: {exc}") from exc

        try:
            connection = context.wrap_socket(raw)
        except (ssl.SSLError, OSError) as exc:
            raw.close()

            # The common causes are a revoked device and a key that no longer
            # matches. They are indistinguishable from here, on purpose: the
            # server does not say which.
            raise LinkClientError(
                f"the computer refused this device: {exc}"
            ) from exc

        connection.settimeout(self.timeout)
        self._connection = connection

    def close(self) -> None:
        connection, self._connection = self._connection, None

        if connection is None:
            return

        try:
            connection.close()
        except OSError:
            pass

    def __enter__(self) -> LinkClient:
        self.connect()

        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def tls_summary(self) -> str:
        """What was negotiated, for a diagnostics screen."""

        if self._connection is None:
            return "not connected"

        cipher = self._connection.cipher()

        return f"{self._connection.version()} {cipher[0] if cipher else '?'}"

    # --------------------------------------------------------------- requests

    def call(
        self,
        op: str,
        _timeout: float | None = None,
        **params: Any,
    ) -> Response:
        """
        Send one request and wait for its response.

        Event frames arriving while waiting are progress, not answers: they are
        handed to ``on_event`` and collected, and the wait continues.
        """

        connection = self._connection

        if connection is None:
            raise LinkClientError("not connected")

        request_id = self._next_id
        self._next_id += 1
        self._events = []

        if _timeout is not None:
            connection.settimeout(_timeout)

        try:
            connection.sendall(
                encode_frame(Request(id=request_id, op=op, params=params))
            )
        except (OSError, ssl.SSLError) as exc:
            raise LinkClientError(f"could not send: {exc}") from exc

        read = reader_for(connection)

        while True:
            try:
                payload = read_frame(read)
            except ProtocolError as exc:
                raise LinkClientError(f"bad frame from the computer: {exc}") from exc

            if payload is None:
                raise LinkClientError("the computer closed the connection")

            try:
                frame = parse_frame(payload)
            except ProtocolError as exc:
                raise LinkClientError(f"bad frame from the computer: {exc}") from exc

            if isinstance(frame, Event):
                self._events.append(frame)

                if self.on_event is not None:
                    self.on_event(frame)

                continue

            if isinstance(frame, Response):
                if frame.id != request_id:
                    raise LinkClientError(
                        f"response {frame.id} does not match request "
                        f"{request_id}"
                    )

                return frame

            raise LinkClientError("the computer sent a request, which is not expected")

    @property
    def events(self) -> tuple[Event, ...]:
        """Events received during the most recent call."""
        return tuple(self._events)

    def ping(self) -> bool:
        """Hold the session open, and confirm it is still alive."""

        try:
            return self.call("ping").ok
        except LinkClientError:
            return False


def main() -> None:
    """The client has no demo of its own; the server's exercises it."""

    print("Run the server demo, which drives this client:")
    print("    python -m core.link_server")


if __name__ == "__main__":
    main()
