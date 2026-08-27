from __future__ import annotations

import socket
import ssl
import threading
import unittest

from core.link_transport import (
    IDLE_TIMEOUT_SECONDS,
    MINIMUM_TLS,
    IdentitySeen,
    TlsPskUnavailable,
    _as_identity,
    client_context,
    psk_support_report,
    read_exactly,
    reader_for,
    require_psk_support,
    server_context,
)


DEVICE = "0123456789abcdef"
SECRET = bytes(range(32))
OTHER_SECRET = bytes(range(32, 64))


class Endpoint:
    """
    A real PSK server on loopback, used by one client and then closed.

    Real sockets rather than mocks because the properties under test are
    OpenSSL's, not this project's: whether a wrong key fails, whether the
    identity arrives, whether the negotiated version is what was asked for.
    """

    def __init__(self, lookup) -> None:
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.lookup = lookup
        self.seen: IdentitySeen | None = None
        self.result: dict[str, object] = {}
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            raw, _ = self.listener.accept()
        except OSError:
            return

        context, seen = server_context(self.lookup)
        self.seen = seen

        try:
            with context.wrap_socket(raw, server_side=True) as tls:
                self.result["ok"] = True
                self.result["version"] = tls.version()
                self.result["cipher"] = tls.cipher()[0]
                self.result["received"] = tls.recv(32)
                tls.sendall(b"pong")
        except (ssl.SSLError, OSError) as exc:
            self.result["ok"] = False
            self.result["error"] = type(exc).__name__
        finally:
            self.close()

    def close(self) -> None:
        try:
            self.listener.close()
        except OSError:
            pass

    def join(self, timeout: float = 5.0) -> None:
        self.thread.join(timeout=timeout)


def connect(port: int, device: str, secret: bytes) -> tuple[bool, str]:
    context = client_context(device, secret)

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw) as tls:
                tls.sendall(b"ping")
                tls.recv(16)

                return True, str(tls.version())
    except (ssl.SSLError, OSError) as exc:
        return False, type(exc).__name__


class TestSupportProbe(unittest.TestCase):
    def test_this_python_supports_psk(self) -> None:
        # If this fails, the link cannot run here at all and the fallback is
        # certificate-based mutual TLS.
        require_psk_support()

    def test_the_report_names_what_matters(self) -> None:
        report = psk_support_report()

        for expected in ("HAS_PSK", "HAS_TLSv1_3", "server_cb", "openssl"):
            with self.subTest(expected=expected):
                self.assertIn(expected, report)

    def test_a_missing_feature_names_the_fallback(self) -> None:
        # The message has to be actionable, because the person reading it will
        # be looking at a Windows machine that refuses to start the link.
        original = ssl.HAS_PSK

        try:
            ssl.HAS_PSK = False  # type: ignore[misc]

            with self.assertRaises(TlsPskUnavailable) as caught:
                require_psk_support()
        finally:
            ssl.HAS_PSK = original  # type: ignore[misc]

        message = str(caught.exception)

        self.assertIn("cryptography", message)
        self.assertIn("PSK", message)


class TestIdentityNormalisation(unittest.TestCase):
    def test_a_string_identity_passes_through(self) -> None:
        self.assertEqual(_as_identity("abc"), "abc")

    def test_a_bytes_identity_is_decoded(self) -> None:
        # Measured as str on this build. Accepting bytes costs one branch and
        # removes a silent failure on a build that differs.
        self.assertEqual(_as_identity(b"abc"), "abc")

    def test_undecodable_bytes_do_not_raise(self) -> None:
        self.assertIsInstance(_as_identity(b"\xff\xfe"), str)

    def test_none_becomes_empty(self) -> None:
        self.assertEqual(_as_identity(None), "")


class TestContexts(unittest.TestCase):
    def test_both_sides_require_tls_1_3(self) -> None:
        server, _ = server_context(lambda ident: SECRET)

        self.assertIs(server.minimum_version, MINIMUM_TLS)
        self.assertIs(client_context(DEVICE, SECRET).minimum_version, MINIMUM_TLS)

    def test_the_client_does_not_verify_certificates(self) -> None:
        # There are none. The pre-shared key is the mutual authenticator.
        context = client_context(DEVICE, SECRET)

        self.assertIs(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)

    def test_a_non_ascii_device_id_is_refused_by_the_client(self) -> None:
        with self.assertRaises(UnicodeEncodeError):
            client_context("دستگاه", SECRET)


class TestHandshake(unittest.TestCase):
    def test_the_right_key_connects_over_tls_1_3(self) -> None:
        endpoint = Endpoint(lambda ident: SECRET)

        ok, version = connect(endpoint.port, DEVICE, SECRET)
        endpoint.join()

        self.assertTrue(ok)
        self.assertEqual(version, "TLSv1.3")
        self.assertEqual(endpoint.result.get("version"), "TLSv1.3")

    def test_the_server_learns_which_device_connected(self) -> None:
        endpoint = Endpoint(lambda ident: SECRET)

        connect(endpoint.port, DEVICE, SECRET)
        endpoint.join()

        assert endpoint.seen is not None
        self.assertEqual(endpoint.seen.identity, DEVICE)
        self.assertTrue(endpoint.seen.resolved)

    def test_the_data_actually_crosses(self) -> None:
        endpoint = Endpoint(lambda ident: SECRET)

        connect(endpoint.port, DEVICE, SECRET)
        endpoint.join()

        self.assertEqual(endpoint.result.get("received"), b"ping")

    def test_a_wrong_key_fails_on_both_sides(self) -> None:
        endpoint = Endpoint(lambda ident: SECRET)

        ok, _ = connect(endpoint.port, DEVICE, OTHER_SECRET)
        endpoint.join()

        self.assertFalse(ok)
        self.assertIs(endpoint.result.get("ok"), False)

    def test_an_unknown_device_fails_but_is_still_named(self) -> None:
        # This is what lets a handshake from a revoked or invented device be
        # logged as an attempt by that name.
        endpoint = Endpoint(lambda ident: None)

        ok, _ = connect(endpoint.port, DEVICE, SECRET)
        endpoint.join()

        self.assertFalse(ok)
        assert endpoint.seen is not None
        self.assertEqual(endpoint.seen.identity, DEVICE)
        self.assertFalse(endpoint.seen.resolved)

    def test_a_lookup_that_raises_becomes_a_refusal(self) -> None:
        # An exception inside the callback would surface as an opaque TLS
        # error, so it is converted into a refusal instead.
        def explode(identity: str) -> bytes:
            raise RuntimeError("registry on fire")

        endpoint = Endpoint(explode)

        ok, _ = connect(endpoint.port, DEVICE, SECRET)
        endpoint.join()

        self.assertFalse(ok)

    def test_the_lookup_decides_per_device(self) -> None:
        keys = {DEVICE: SECRET, "ffffffffffffffff": OTHER_SECRET}

        endpoint = Endpoint(keys.get)
        ok, _ = connect(endpoint.port, "ffffffffffffffff", OTHER_SECRET)
        endpoint.join()

        self.assertTrue(ok)

        endpoint = Endpoint(keys.get)
        ok, _ = connect(endpoint.port, "ffffffffffffffff", SECRET)
        endpoint.join()

        self.assertFalse(ok)


class TestReadExactly(unittest.TestCase):
    def pair(self) -> tuple[socket.socket, socket.socket]:
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        return left, right

    def test_it_returns_what_was_asked_for(self) -> None:
        left, right = self.pair()
        right.sendall(b"0123456789")

        self.assertEqual(read_exactly(left, 4), b"0123")
        self.assertEqual(read_exactly(left, 6), b"456789")

    def test_it_gathers_across_several_arrivals(self) -> None:
        # recv is free to return less than asked for, which is why the frame
        # reader cannot use it directly.
        left, right = self.pair()

        def dribble() -> None:
            for chunk in (b"ab", b"cd", b"ef"):
                right.sendall(chunk)

        threading.Thread(target=dribble, daemon=True).start()

        self.assertEqual(read_exactly(left, 6), b"abcdef")

    def test_a_closed_peer_returns_short(self) -> None:
        # Short rather than raising, so the protocol layer can tell a clean
        # hang-up between frames from a truncated frame.
        left, right = self.pair()
        right.sendall(b"ab")
        right.close()

        self.assertEqual(read_exactly(left, 8), b"ab")

    def test_nothing_available_returns_empty(self) -> None:
        left, right = self.pair()
        right.close()

        self.assertEqual(read_exactly(left, 4), b"")

    def test_a_timeout_returns_what_arrived(self) -> None:
        left, right = self.pair()
        right.sendall(b"ab")
        left.settimeout(0.05)

        self.assertEqual(read_exactly(left, 8), b"ab")

    def test_reader_for_binds_to_one_socket(self) -> None:
        left, right = self.pair()
        right.sendall(b"hello")

        read = reader_for(left)

        self.assertEqual(read(5), b"hello")

    def test_asking_for_nothing_reads_nothing(self) -> None:
        left, _ = self.pair()

        self.assertEqual(read_exactly(left, 0), b"")


class TestTimeouts(unittest.TestCase):
    def test_the_idle_timeout_is_long_enough_for_a_conversation(self) -> None:
        # A phone holds a session open with ping; five minutes means a user can
        # pause mid-thought without the session dropping.
        self.assertGreaterEqual(IDLE_TIMEOUT_SECONDS, 60.0)


if __name__ == "__main__":
    unittest.main()
