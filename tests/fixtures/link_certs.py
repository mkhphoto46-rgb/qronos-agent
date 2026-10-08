"""
Throwaway certificates for the relay's outer TLS, made fresh for each test run.

The outer connection to a relay is ordinary certificate TLS, so testing it
needs a certificate authority and a server certificate. Committing a private
key, even a test one, is the kind of thing secret scanners exist to stop, and
the project has no certificate library: ``cryptography`` is deliberately not a
dependency. So this module builds both in pure Python — RSA key generation,
DER encoding and PKCS #1 v1.5 signing — which is a few hundred lines because
the formats are fixed and small.

The certificates satisfy OpenSSL's strict verification, which Python 3.13
turns on in ``create_default_context``: a CA with basic constraints, key usage
and a subject key identifier, and a leaf with an authority key identifier,
server-auth extended key usage and subject alternative names.

Nothing here is used outside the tests. The keys live in a temporary directory
for the life of the test session and are never written anywhere else.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


# ------------------------------------------------------------------- RSA

_SMALL_PRIMES = tuple(
    n for n in range(3, 2000, 2) if all(n % d for d in range(3, int(n ** 0.5) + 1, 2))
)


def _probably_prime(n: int, rounds: int = 40) -> bool:
    if n < 2:
        return False

    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p

    d, r = n - 1, 0

    while d % 2 == 0:
        d //= 2
        r += 1

    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)

        if x in (1, n - 1):
            continue

        for _ in range(r - 1):
            x = pow(x, 2, n)

            if x == n - 1:
                break
        else:
            return False

    return True


def _prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1

        if _probably_prime(candidate):
            return candidate


@dataclass(frozen=True)
class RsaKey:
    n: int
    e: int
    d: int
    p: int
    q: int

    @property
    def size(self) -> int:
        return (self.n.bit_length() + 7) // 8

    def sign_sha256(self, message: bytes) -> bytes:
        """PKCS #1 v1.5 with SHA-256, as X.509 signatures use."""

        digest_info = bytes.fromhex(
            "3031300d060960864801650304020105000420"
        ) + hashlib.sha256(message).digest()
        padding = b"\xff" * (self.size - len(digest_info) - 3)
        block = b"\x00\x01" + padding + b"\x00" + digest_info

        signature = pow(int.from_bytes(block, "big"), self.d, self.n)

        return signature.to_bytes(self.size, "big")


def generate_rsa(bits: int = 2048) -> RsaKey:
    e = 65537

    while True:
        p = _prime(bits // 2)
        q = _prime(bits // 2)

        if p == q:
            continue

        phi = (p - 1) * (q - 1)

        try:
            d = pow(e, -1, phi)
        except ValueError:
            continue

        n = p * q

        if n.bit_length() == bits:
            return RsaKey(n=n, e=e, d=d, p=p, q=q)


# ------------------------------------------------------------------- DER


def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])

    body = n.to_bytes((n.bit_length() + 7) // 8, "big")

    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _length(len(body)) + body


def _integer(value: int) -> bytes:
    body = value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big")

    return _tlv(0x02, body)


def _sequence(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _set(*items: bytes) -> bytes:
    return _tlv(0x31, b"".join(items))


def _oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    body = bytearray([40 * parts[0] + parts[1]])

    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7

        while part:
            chunk.append(0x80 | (part & 0x7F))
            part >>= 7

        body.extend(reversed(chunk))

    return _tlv(0x06, bytes(body))


def _null() -> bytes:
    return b"\x05\x00"


def _bit_string(body: bytes, unused: int = 0) -> bytes:
    return _tlv(0x03, bytes([unused]) + body)


def _octet_string(body: bytes) -> bytes:
    return _tlv(0x04, body)


def _utf8(text: str) -> bytes:
    return _tlv(0x0C, text.encode("utf-8"))


def _utc_time(moment: dt.datetime) -> bytes:
    return _tlv(0x17, moment.strftime("%y%m%d%H%M%SZ").encode("ascii"))


def _explicit(number: int, body: bytes) -> bytes:
    return _tlv(0xA0 | number, body)


def _boolean(value: bool) -> bytes:
    return _tlv(0x01, b"\xff" if value else b"\x00")


SHA256_WITH_RSA = _sequence(_oid("1.2.840.113549.1.1.11"), _null())
RSA_ENCRYPTION = _sequence(_oid("1.2.840.113549.1.1.1"), _null())


def _name(common_name: str) -> bytes:
    return _sequence(_set(_sequence(_oid("2.5.4.3"), _utf8(common_name))))


def _public_key_bits(key: RsaKey) -> bytes:
    return _sequence(_integer(key.n), _integer(key.e))


def _spki(key: RsaKey) -> bytes:
    return _sequence(RSA_ENCRYPTION, _bit_string(_public_key_bits(key)))


def _key_id(key: RsaKey) -> bytes:
    return hashlib.sha1(_public_key_bits(key)).digest()


def _extension(oid: str, value: bytes, critical: bool = False) -> bytes:
    parts = [_oid(oid)]

    if critical:
        parts.append(_boolean(True))

    parts.append(_octet_string(value))

    return _sequence(*parts)


def _certificate(
    subject: str,
    subject_key: RsaKey,
    issuer: str,
    issuer_key: RsaKey,
    extensions: list[bytes],
) -> bytes:
    now = dt.datetime.now(dt.timezone.utc)
    tbs = _sequence(
        _explicit(0, _integer(2)),
        _integer(secrets.randbits(63) | 1),
        SHA256_WITH_RSA,
        _name(issuer),
        _sequence(
            _utc_time(now - dt.timedelta(hours=1)),
            _utc_time(now + dt.timedelta(days=2)),
        ),
        _name(subject),
        _spki(subject_key),
        _explicit(3, _sequence(*extensions)),
    )

    return _sequence(tbs, SHA256_WITH_RSA, _bit_string(issuer_key.sign_sha256(tbs)))


def _pem(label: str, der: bytes) -> str:
    body = base64.encodebytes(der).decode("ascii").replace("\n", "")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]

    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def _private_key_der(key: RsaKey) -> bytes:
    return _sequence(
        _integer(0),
        _integer(key.n),
        _integer(key.e),
        _integer(key.d),
        _integer(key.p),
        _integer(key.q),
        _integer(key.d % (key.p - 1)),
        _integer(key.d % (key.q - 1)),
        _integer(pow(key.q, -1, key.p)),
    )


# ------------------------------------------------------------- the bundle


RELAY_NAME = "relay.test"


@dataclass(frozen=True)
class RelayCertificates:
    """Paths to a throwaway CA and a relay certificate signed by it."""

    ca_file: Path
    cert_file: Path
    key_file: Path
    server_name: str = RELAY_NAME


@lru_cache(maxsize=1)
def _material() -> tuple[str, str, str]:
    """Generated once per test session; RSA in pure Python takes a moment."""

    ca_key = generate_rsa()
    leaf_key = generate_rsa()

    ca = _certificate(
        "Qronos throwaway test CA",
        ca_key,
        "Qronos throwaway test CA",
        ca_key,
        [
            _extension("2.5.29.19", _sequence(_boolean(True)), critical=True),
            # keyCertSign and cRLSign.
            _extension("2.5.29.15", _bit_string(b"\x06", unused=1), critical=True),
            _extension("2.5.29.14", _octet_string(_key_id(ca_key))),
        ],
    )

    leaf = _certificate(
        RELAY_NAME,
        leaf_key,
        "Qronos throwaway test CA",
        ca_key,
        [
            _extension("2.5.29.19", _sequence()),
            # digitalSignature and keyEncipherment.
            _extension("2.5.29.15", _bit_string(b"\xa0", unused=5), critical=True),
            _extension("2.5.29.37", _sequence(_oid("1.3.6.1.5.5.7.3.1"))),
            _extension(
                "2.5.29.17",
                _sequence(
                    _tlv(0x82, RELAY_NAME.encode("ascii")),
                    _tlv(0x87, ipaddress.ip_address("127.0.0.1").packed),
                ),
            ),
            _extension("2.5.29.14", _octet_string(_key_id(leaf_key))),
            _extension(
                "2.5.29.35", _sequence(_tlv(0x80, _key_id(ca_key)))
            ),
        ],
    )

    return (
        _pem("CERTIFICATE", ca),
        _pem("CERTIFICATE", leaf),
        _pem("RSA PRIVATE KEY", _private_key_der(leaf_key)),
    )


def relay_certificates(directory: Path) -> RelayCertificates:
    """Write the throwaway CA, certificate and key into ``directory``."""

    ca_pem, leaf_pem, key_pem = _material()
    directory.mkdir(parents=True, exist_ok=True)

    bundle = RelayCertificates(
        ca_file=directory / "test-ca.pem",
        cert_file=directory / "relay-cert.pem",
        key_file=directory / "relay-key.pem",
    )

    bundle.ca_file.write_text(ca_pem, encoding="ascii")
    bundle.cert_file.write_text(leaf_pem, encoding="ascii")
    bundle.key_file.write_text(key_pem, encoding="ascii")

    return bundle
