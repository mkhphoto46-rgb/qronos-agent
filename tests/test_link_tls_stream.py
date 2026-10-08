"""
The inner TLS session run over another stream, as Layer 2 needs it.

Over a plain socket pair here, so what is under test is the BIO plumbing and
the handshake, not the relay. The same class runs inside a real outer TLS
connection in the relay's own end-to-end tests.
"""

from __future__ import annotations

import socket
import ssl
import threading
import unittest

from core.link_transport import TlsStream, client_context, server_context


DEVICE = "0123456789abcdef"
KEY = bytes(range(32))


def pair_with_server(lookup, server_steps):
    """Run a TlsStream server over one end of a socket pair in a thread."""

    pc, phone = socket.socketpair()
    pc.settimeout(5)
    phone.settimeout(5)
    result: dict[str, object] = {}

    def run() -> None:
        context, seen = server_context(lookup)

        try:
            stream = TlsStream.server(context, pc)
        except (ssl.SSLError, OSError) as exc:
            result["error"] = exc
            result["identity"] = seen.identity
            pc.close()

            return

        result["identity"] = seen.identity

        try:
            server_steps(stream, result)
        finally:
            stream.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    return phone, thread, result


class TestHandshake(unittest.TestCase):
    def test_a_matching_key_negotiates_tls13_and_carries_data(self) -> None:
        def echo(stream, result):
            result["got"] = stream.recv(100)
            stream.sendall(b"pong " + result["got"])
            result["version"] = stream.version()

        phone, thread, result = pair_with_server(lambda ident: KEY, echo)

        with TlsStream.client(client_context(DEVICE, KEY), phone) as stream:
            stream.sendall("سلام".encode("utf-8"))
            reply = stream.recv(100)
            version = stream.version()

        thread.join(5)

        self.assertEqual(reply, "pong سلام".encode("utf-8"))
        self.assertEqual(version, "TLSv1.3")
        self.assertEqual(result["version"], "TLSv1.3")
        self.assertEqual(result["identity"], DEVICE)

    def test_a_wrong_key_fails_on_both_sides(self) -> None:
        phone, thread, result = pair_with_server(lambda ident: KEY, lambda s, r: None)

        with self.assertRaises((ssl.SSLError, OSError)):
            TlsStream.client(client_context(DEVICE, b"x" * 32), phone)

        phone.close()
        thread.join(5)

        self.assertIn("error", result)
        self.assertEqual(result["identity"], DEVICE)

    def test_an_unknown_identity_fails_and_is_still_seen(self) -> None:
        phone, thread, result = pair_with_server(lambda ident: None, lambda s, r: None)

        with self.assertRaises((ssl.SSLError, OSError)):
            TlsStream.client(client_context(DEVICE, KEY), phone)

        phone.close()
        thread.join(5)

        self.assertIn("error", result)
        self.assertEqual(result["identity"], DEVICE)

    def test_a_carrier_that_closes_mid_handshake_is_an_error_not_a_hang(self) -> None:
        pc, phone = socket.socketpair()
        pc.settimeout(5)
        phone.close()
        context, _ = server_context(lambda ident: KEY)

        with self.assertRaises((ssl.SSLError, OSError)):
            TlsStream.server(context, pc)

        pc.close()


class TestStream(unittest.TestCase):
    def test_a_large_message_survives_record_splitting(self) -> None:
        payload = bytes(range(256)) * 400  # 100 KiB, several TLS records

        def receive_all(stream, result):
            chunks = []

            while sum(map(len, chunks)) < len(payload):
                chunk = stream.recv(65536)

                if not chunk:
                    break

                chunks.append(chunk)

            result["got"] = b"".join(chunks)

        phone, thread, result = pair_with_server(lambda ident: KEY, receive_all)

        with TlsStream.client(client_context(DEVICE, KEY), phone) as stream:
            stream.sendall(payload)
            thread.join(5)

        self.assertEqual(result["got"], payload)

    def test_a_clean_close_reads_as_end_of_stream(self) -> None:
        def close_at_once(stream, result):
            pass  # the helper closes it, sending close_notify

        phone, thread, _ = pair_with_server(lambda ident: KEY, close_at_once)

        with TlsStream.client(client_context(DEVICE, KEY), phone) as stream:
            thread.join(5)
            self.assertEqual(stream.recv(10), b"")

    def test_close_sends_the_goodbye_before_closing_the_carrier(self) -> None:
        # unwrap() normally raises SSLWantReadError because the peer's
        # close_notify has not arrived; the alert must be flushed regardless.
        sent: list[int] = []

        class RecordingCarrier:
            def __init__(self, inner: socket.socket) -> None:
                self.inner = inner

            def recv(self, count: int) -> bytes:
                return self.inner.recv(count)

            def sendall(self, data: bytes) -> None:
                sent.append(len(data))
                self.inner.sendall(data)

            def settimeout(self, value) -> None:
                self.inner.settimeout(value)

            def shutdown(self, how: int) -> None:
                self.inner.shutdown(how)

            def close(self) -> None:
                self.inner.close()

        pc, phone = socket.socketpair()
        pc.settimeout(5)
        phone.settimeout(5)
        client: list[TlsStream] = []

        thread = threading.Thread(
            target=lambda: client.append(TlsStream.client(client_context(DEVICE, KEY), phone)),
            daemon=True,
        )
        thread.start()

        context, _ = server_context(lambda ident: KEY)
        stream = TlsStream.server(context, RecordingCarrier(pc))
        thread.join(5)
        before = len(sent)

        stream.close()

        self.assertGreater(len(sent), before)
        self.assertEqual(client[0].recv(10), b"")
        client[0].close()

    def test_a_carrier_cut_without_goodbye_reads_as_end_of_stream(self) -> None:
        def cut(stream, result):
            stream.abort()

        phone, thread, _ = pair_with_server(lambda ident: KEY, cut)

        with TlsStream.client(client_context(DEVICE, KEY), phone) as stream:
            thread.join(5)
            self.assertEqual(stream.recv(10), b"")

    def test_abort_wakes_a_blocked_reader_in_another_thread(self) -> None:
        released = threading.Event()

        def wait_for_release(stream, result):
            released.wait(5)

        phone, thread, _ = pair_with_server(lambda ident: KEY, wait_for_release)
        stream = TlsStream.client(client_context(DEVICE, KEY), phone)
        phone.settimeout(30)
        outcome: list[object] = []

        def read() -> None:
            try:
                outcome.append(stream.recv(10))
            except (OSError, ssl.SSLError) as exc:
                outcome.append(exc)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(0.3)
        self.assertTrue(reader.is_alive())

        stream.abort()
        reader.join(3)
        released.set()
        thread.join(5)

        self.assertFalse(reader.is_alive())


if __name__ == "__main__":
    unittest.main()
