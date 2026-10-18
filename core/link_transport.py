"""
The encrypted channel between the phone and the PC.

TLS 1.3 with an external pre-shared key. Pairing produces a shared secret, and
TLS can authenticate a connection from that secret directly — no certificates,
no certificate authority, no pinning code to get wrong. The work is done by
OpenSSL; this module only wires it up.

Measured on macOS with Python 3.14.7 and OpenSSL 3.6.3 before the design was
settled:

    handshake            TLSv1.3, TLS_CHACHA20_POLY1305_SHA256
    wrong key            handshake fails on both sides
    unknown identity     handshake fails, and the server still learns which
                         identity was attempted, so it can be logged
    forward secrecy      yes — the ServerHello carries a key_share with group
                         X25519MLKEM768, so the exchange is psk_dhe_ke and a
                         recorded session stays unreadable even if the pairing
                         key later leaks

Two things about the Python API that are easy to get wrong, both found by
testing rather than by reading:

    The server callback receives the identity as ``str``, not ``bytes``. A
    bytes-keyed lookup fails silently and every handshake is refused with no
    obvious cause. ``_as_identity`` normalises both, so a Windows build that
    differs cannot reintroduce it.

    The callback receives no handle for the connection it belongs to, so one
    shared context cannot tell two simultaneous handshakes apart. Each
    connection gets its own context whose callback closes over its own state.

Whether the OpenSSL that CPython links on Windows supports PSK at all is
unverified. ``require_psk_support`` fails loudly at startup rather than leaving
it to surface as an unexplained handshake failure later.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from typing import Callable


# TLS 1.3 only. Everything below it either lacks the PSK modes we rely on or
# brings cipher suites there is no reason to accept.
MINIMUM_TLS = ssl.TLSVersion.TLSv1_3

# Restricts the pre-1.3 suite list to PSK. Redundant while the minimum version
# is 1.3, and kept so lowering that minimum could not silently permit an
# unauthenticated suite.
PSK_CIPHERS = "PSK"

# A handshake that has not finished in this long is not going to.
HANDSHAKE_TIMEOUT_SECONDS = 10.0

# A session with nothing on it for this long is closed. The phone sends a
# ``ping`` to hold one open deliberately.
IDLE_TIMEOUT_SECONDS = 300.0

SecretLookup = Callable[[str], bytes | None]


class TlsPskUnavailable(Exception):
    """This Python cannot do TLS-PSK, so the link cannot start."""


@dataclass
class IdentitySeen:
    """
    Which device the handshake claimed to be.

    Mutable and deliberately per-connection: it is the only channel by which
    the PSK callback can report back, because it is handed no connection of its
    own.

    Set even when the identity is unknown, so a handshake from a revoked or
    made-up device can still be logged as an attempt by that name.
    """

    identity: str | None = None
    resolved: bool = False


def psk_support_report() -> str:
    """A one-line description of what this Python can do, for diagnostics."""

    return (
        f"HAS_PSK={getattr(ssl, 'HAS_PSK', False)} "
        f"HAS_TLSv1_3={ssl.HAS_TLSv1_3} "
        f"server_cb={hasattr(ssl.SSLContext, 'set_psk_server_callback')} "
        f"client_cb={hasattr(ssl.SSLContext, 'set_psk_client_callback')} "
        f"openssl={ssl.OPENSSL_VERSION}"
    )


def require_psk_support() -> None:
    """
    Refuse to start unless this Python can actually do the handshake.

    Called before the listening socket is opened. The alternative — discovering
    it during the first handshake — produces an error on the phone and nothing
    useful on the PC.
    """

    missing: list[str] = []

    if not getattr(ssl, "HAS_PSK", False):
        missing.append("OpenSSL built without PSK support")

    if not ssl.HAS_TLSv1_3:
        missing.append("no TLS 1.3")

    for name in ("set_psk_server_callback", "set_psk_client_callback"):
        if not hasattr(ssl.SSLContext, name):
            missing.append(f"ssl.SSLContext.{name} is absent")

    if missing:
        raise TlsPskUnavailable(
            "The device link needs TLS 1.3 with pre-shared keys, and this "
            "Python cannot provide it: "
            + "; ".join(missing)
            + ". "
            + psk_support_report()
            + ". The fallback is mutual TLS with self-signed certificates, "
            "which needs the 'cryptography' package."
        )


def _as_identity(raw: object) -> str:
    """
    Normalise whatever the callback was handed into a string.

    Measured as ``str`` here. Accepting ``bytes`` too costs one branch and
    removes a whole class of silent failure on a build that differs.
    """

    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")

    return str(raw) if raw is not None else ""


def server_context(
    lookup: SecretLookup,
) -> tuple[ssl.SSLContext, IdentitySeen]:
    """
    A context for exactly one incoming connection.

    ``lookup`` is given the identity the peer claims and returns that device's
    key, or ``None`` to refuse. Returning ``None`` for a revoked device is what
    makes revocation take effect during the handshake rather than after it.

    The callback never raises. An exception inside it would surface as an
    opaque TLS error, so a failing lookup is converted into a refusal, which is
    the safe direction.
    """

    seen = IdentitySeen()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = MINIMUM_TLS

    try:
        context.set_ciphers(PSK_CIPHERS)
    except ssl.SSLError:  # pragma: no cover - build dependent
        # A build with no pre-1.3 PSK suites at all. TLS 1.3 is unaffected.
        pass

    def callback(identity: object) -> bytes:
        seen.identity = _as_identity(identity)

        try:
            secret = lookup(seen.identity)
        except Exception:
            secret = None

        seen.resolved = secret is not None

        # An empty key fails the handshake. Returning None would raise a
        # TypeError inside OpenSSL's callback instead.
        return secret if secret is not None else b""

    context.set_psk_server_callback(callback)

    return context, seen


def client_context(device_id: str, secret: bytes) -> ssl.SSLContext:
    """
    A context for the phone side.

    No certificate verification, because there is no certificate: the PSK is
    the mutual authenticator. A server that does not hold the same key cannot
    complete the handshake, so there is nothing for ``verify_mode`` to add.
    """

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = MINIMUM_TLS
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    try:
        context.set_ciphers(PSK_CIPHERS)
    except ssl.SSLError:  # pragma: no cover - build dependent
        pass

    identity = device_id.encode("ascii", errors="strict")

    context.set_psk_client_callback(lambda hint: (identity, secret))

    return context


def read_exactly(connection: socket.socket, count: int) -> bytes:
    """
    Read exactly ``count`` bytes, or fewer only at end of stream.

    ``recv`` is free to return less than asked for, so the frame reader cannot
    use it directly. Returning short instead of raising lets the protocol layer
    tell a clean hang-up between frames apart from a truncated frame.
    """

    chunks: list[bytes] = []
    remaining = count

    while remaining > 0:
        try:
            chunk = connection.recv(remaining)
        except (TimeoutError, socket.timeout):
            break
        except (OSError, ssl.SSLError):
            break

        if not chunk:
            break

        chunks.append(chunk)
        remaining -= len(chunk)

    return b"".join(chunks)


def reader_for(connection: socket.socket) -> Callable[[int], bytes]:
    """A ``read_exactly`` bound to one socket, for the protocol layer."""

    def read(count: int) -> bytes:
        return read_exactly(connection, count)

    return read


def main() -> None:
    """Report what this Python can do, then prove it on loopback."""

    print(psk_support_report())

    try:
        require_psk_support()
        print("PSK support: present")
    except TlsPskUnavailable as exc:
        print("PSK support: MISSING")
        print(exc)

        return

    import threading

    secret = b"k" * 32
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    result: dict[str, object] = {}

    def serve() -> None:
        raw, _ = listener.accept()
        context, seen = server_context(lambda ident: secret)

        with context.wrap_socket(raw, server_side=True) as tls:
            result["identity"] = seen.identity
            result["version"] = tls.version()
            result["cipher"] = tls.cipher()[0]
            tls.recv(16)

        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    context = client_context("00112233445566aa", secret)

    with socket.create_connection(("127.0.0.1", port)) as raw:
        with context.wrap_socket(raw) as tls:
            tls.sendall(b"hello")

    thread.join(timeout=5)

    print("server saw identity:", result.get("identity"))
    print("negotiated         :", result.get("version"), result.get("cipher"))


if __name__ == "__main__":
    main()
