"""Ed25519 verification (core.ed25519) against RFC 8032 section 7.1 and strictness cases."""

from __future__ import annotations

import pytest

from core import ed25519

# RFC 8032 section 7.1, tests 1-3.
RFC8032 = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


# ----------------------------------------------------------- Ed25519


@pytest.mark.parametrize(("secret", "public", "message", "signature"), RFC8032)
def test_rfc8032_vectors(secret, public, message, signature):
    secret, public, message, signature = (bytes.fromhex(v) for v in (secret, public, message, signature))

    assert ed25519.public_key(secret) == public
    assert ed25519.sign(secret, message) == signature
    assert ed25519.verify(public, message, signature)
    assert not ed25519.verify(public, message + b"!", signature)
    assert not ed25519.verify(public, message, signature[:-1] + bytes([signature[-1] ^ 1]))


def test_a_malleated_signature_is_refused():
    secret, public, message, signature = (bytes.fromhex(v) for v in RFC8032[1])
    s = int.from_bytes(signature[32:], "little")

    assert not ed25519.verify(public, message, signature[:32] + int.to_bytes(s + ed25519.L, 32, "little"))


def test_malformed_keys_and_signatures():
    _, public, message, signature = (bytes.fromhex(v) for v in RFC8032[0])
    with pytest.raises(ed25519.SignatureError):
        ed25519.verify(public[:31], message, signature)
    not_a_point = (2).to_bytes(32, "little")  # y = 2 has no x on the curve
    with pytest.raises(ed25519.SignatureError):
        ed25519.verify(not_a_point, message, signature)
    assert not ed25519.verify(public, message, signature[:63])
    assert not ed25519.verify(public, message, not_a_point + signature[32:])


# ------------------------------------------- weak keys and small-order R (I6)

IDENTITY = bytes([1]) + bytes(31)  # y = 1, x = 0: the neutral element


def torsion_points() -> set[bytes]:
    """The eight small-order points, found from the curve itself: [L]P for points P."""
    found: set[bytes] = set()
    y = 2
    while len(found) < 8 and y < 10_000:
        point = ed25519._decompress(y.to_bytes(32, "little"))
        if point is not None:
            found.add(ed25519._compress(ed25519._multiply(ed25519.L, point)))
        y += 1
    assert len(found) == 8
    return found


TORSION = sorted(torsion_points())


def test_the_torsion_set_has_the_expected_orders():
    orders = sorted(next(n for n in (1, 2, 4, 8)
                         if ed25519._is_identity(ed25519._multiply(n, ed25519._decompress(t))))
                    for t in TORSION)
    assert orders == [1, 2, 4, 4, 8, 8, 8, 8] and IDENTITY in TORSION


@pytest.mark.parametrize("weak", TORSION)
def test_small_order_public_keys_are_refused(weak):
    with pytest.raises(ed25519.SignatureError, match="weak"):
        ed25519.check_public_key(weak)
    with pytest.raises(ed25519.SignatureError):
        ed25519.verify(weak, b"anything", weak + bytes(32))


def test_the_reproduced_identity_forgery_is_refused():
    """I6: A = identity, R = A, S = 0 satisfies [S]B == R + [k]A for every message."""
    forged = IDENTITY + bytes(32)
    with pytest.raises(ed25519.SignatureError):
        ed25519.verify(IDENTITY, b'{"version": "9.9.9"}', forged)


def test_a_mixed_order_key_is_refused():
    _, public, message, signature = (bytes.fromhex(v) for v in RFC8032[0])
    order_8 = next(t for t in TORSION if not ed25519._is_identity(ed25519._multiply(4, ed25519._decompress(t))))
    mixed = ed25519._compress(ed25519._add(ed25519._decompress(public), ed25519._decompress(order_8)))

    assert ed25519._decompress(mixed) is not None  # a valid curve point...
    with pytest.raises(ed25519.SignatureError, match="weak"):
        ed25519.check_public_key(mixed)            # ...but not in the prime-order subgroup


def test_a_non_canonical_encoding_of_a_point_is_refused():
    non_canonical_identity = (ed25519.P + 1).to_bytes(32, "little")  # y = p + 1, i.e. y = 1 unreduced
    with pytest.raises(ed25519.SignatureError, match="canonical"):
        ed25519.check_public_key(non_canonical_identity)


@pytest.mark.parametrize("small_r", TORSION)
def test_a_small_order_r_never_verifies(small_r):
    secret, public, message, _ = (bytes.fromhex(v) for v in RFC8032[0])
    for s in (0, 1, ed25519.L - 1):
        assert not ed25519.verify(public, message, small_r + s.to_bytes(32, "little"))


def test_rfc8032_public_keys_are_in_the_prime_order_subgroup():
    for _, public, _, _ in RFC8032:
        assert ed25519.check_public_key(bytes.fromhex(public))
