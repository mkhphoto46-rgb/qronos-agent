from __future__ import annotations

import socket
import ssl
import threading
import unittest

from core.link_capability import LinkOp
from core.link_client import LinkClient, LinkClientError
from core.link_devices import SECRET_BYTES, new_secret
from core.link_pairing import PairingPayload
from core.link_protocol import Event, Request, Response, encode_frame
from core.link_transport import server_context
from tests.fixtures.link_harness import ServerHarness, ok_handler


DEVICE = "0123456789abcdef"
SECRET = bytes(range(SECRET_BYTES))


def payload() -> PairingPayload:
    return PairingPayload(
        host="192.168.1.10",
        port=47_711,
        device_id=DEVICE,
        secret=SECRET,
        expires_at=1_800_000_120.0,
    )


class ScriptedServer:
    """
    A PSK endpoint that sends exactly what a test tells it to.

    The real server never sends a mismatched response or a request frame, so
    those paths in the client need something that will.
    """

    def __init__(self, script: list[bytes]) -> None:
        self.script = script
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.received: list[bytes] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            raw, _ = self.listener.accept()
        except OSError:
            return

        context, _ = server_context(lambda identity: SECRET)

        try:
            with context.wrap_socket(raw, server_side=True) as tls:
                self.received.append(tls.recv(4096))

                for blob in self.script:
                    tls.sendall(blob)
        except (ssl.SSLError, OSError):
            pass
        finally:
            self.close()

    def close(self) -> None:
        try:
            self.listener.close()
        except OSError:
            pass

    def client(self) -> LinkClient:
        return LinkClient(
            host="127.0.0.1",
            port=self.port,
            device_id=DEVICE,
            secret=SECRET,
            timeout=5.0,
        )


class TestConstruction(unittest.TestCase):
    def test_a_client_is_built_from_a_payload(self) -> None:
        client = LinkClient.from_payload(payload())

        self.assertEqual(client.host, "192.168.1.10")
        self.assertEqual(client.port, 47_711)
        self.assertEqual(client.device_id, DEVICE)
        self.assertEqual(client.secret, SECRET)

    def test_a_client_is_built_straight_from_a_scanned_uri(self) -> None:
        client = LinkClient.from_uri(payload().to_uri())

        self.assertEqual(client.device_id, DEVICE)
        self.assertEqual(client.secret, SECRET)

    def test_the_host_and_port_can_be_overridden(self) -> None:
        client = LinkClient.from_payload(payload(), host="10.0.0.1", port=9)

        self.assertEqual(client.host, "10.0.0.1")
        self.assertEqual(client.port, 9)

    def test_the_key_never_appears_in_the_repr(self) -> None:
        text = repr(LinkClient.from_payload(payload()))

        self.assertNotIn(SECRET.hex(), text)
        self.assertNotIn(str(SECRET), text)
        self.assertIn(DEVICE, text)

    def test_a_bad_uri_does_not_produce_a_client(self) -> None:
        from core.link_pairing import PairingPayloadError

        with self.assertRaises(PairingPayloadError):
            LinkClient.from_uri("https://example.com/pair")


class TestConnectionErrors(unittest.TestCase):
    def test_calling_before_connecting_is_an_error(self) -> None:
        client = LinkClient.from_payload(payload())

        with self.assertRaises(LinkClientError):
            client.call("ping")

    def test_an_unreachable_host_is_reported_clearly(self) -> None:
        # Port 1 on loopback, where nothing listens.
        client = LinkClient(
            host="127.0.0.1", port=1, device_id=DEVICE, secret=SECRET,
            timeout=2.0,
        )

        with self.assertRaises(LinkClientError) as caught:
            client.connect()

        self.assertIn("cannot reach", str(caught.exception))

    def test_connecting_twice_is_an_error(self) -> None:
        harness = ServerHarness(handlers={LinkOp.PING: ok_handler})

        try:
            client = harness.pair()
            client.connect()

            with self.assertRaises(LinkClientError):
                client.connect()

            client.close()
        finally:
            harness.close()

    def test_closing_an_unconnected_client_is_harmless(self) -> None:
        LinkClient.from_payload(payload()).close()

    def test_closing_twice_is_harmless(self) -> None:
        harness = ServerHarness(handlers={LinkOp.PING: ok_handler})

        try:
            client = harness.pair()
            client.connect()
            client.close()
            client.close()
        finally:
            harness.close()

    def test_a_refused_device_reports_a_refusal_not_a_crash(self) -> None:
        harness = ServerHarness(handlers={LinkOp.PING: ok_handler})

        try:
            client = harness.client_for("ffffffffffffffff", new_secret())

            with self.assertRaises(LinkClientError) as caught:
                client.connect()

            self.assertIn("refused", str(caught.exception))
        finally:
            harness.close()


class TestState(unittest.TestCase):
    def test_connected_reflects_reality(self) -> None:
        harness = ServerHarness(handlers={LinkOp.PING: ok_handler})

        try:
            client = harness.pair()
            self.assertFalse(client.connected)

            client.connect()
            self.assertTrue(client.connected)

            client.close()
            self.assertFalse(client.connected)
        finally:
            harness.close()

    def test_the_tls_summary_is_honest_before_connecting(self) -> None:
        self.assertEqual(
            LinkClient.from_payload(payload()).tls_summary(), "not connected"
        )

    def test_the_tls_summary_names_the_negotiated_suite(self) -> None:
        harness = ServerHarness(handlers={LinkOp.PING: ok_handler})

        try:
            with harness.pair() as client:
                summary = client.tls_summary()

            self.assertIn("TLSv1.3", summary)
            self.assertIn("TLS_", summary)
        finally:
            harness.close()

    def test_request_ids_increase(self) -> None:
        harness = ServerHarness(handlers={LinkOp.PING: ok_handler})

        try:
            with harness.pair() as client:
                first = client.call("ping")
                second = client.call("ping")

            self.assertEqual(second.id, first.id + 1)
        finally:
            harness.close()

    def test_ping_reports_false_on_a_broken_session(self) -> None:
        # A convenience for the phone's keepalive, which should not have to
        # catch anything.
        harness = ServerHarness(handlers={LinkOp.PING: ok_handler})

        try:
            client = harness.pair()
            client.connect()
            self.assertTrue(client.ping())

            client._connection.close()  # type: ignore[union-attr]

            self.assertFalse(client.ping())
        finally:
            harness.close()


class TestScriptedResponses(unittest.TestCase):
    def test_a_mismatched_response_id_is_an_error(self) -> None:
        # Silently accepting it would let one request's answer be reported as
        # another's.
        server = ScriptedServer([encode_frame(Response.success(999, "wrong"))])
        client = server.client()
        client.connect()

        with self.assertRaises(LinkClientError) as caught:
            client.call("ping")

        client.close()

        self.assertIn("does not match", str(caught.exception))

    def test_a_request_frame_from_the_computer_is_an_error(self) -> None:
        server = ScriptedServer([encode_frame(Request(id=1, op="ping"))])
        client = server.client()
        client.connect()

        with self.assertRaises(LinkClientError):
            client.call("ping")

        client.close()

    def test_a_malformed_frame_is_an_error(self) -> None:
        server = ScriptedServer([b"\x00\x00\x00\x05{bad}"])
        client = server.client()
        client.connect()

        with self.assertRaises(LinkClientError) as caught:
            client.call("ping")

        client.close()

        self.assertIn("bad frame", str(caught.exception))

    def test_a_silent_hang_up_is_an_error(self) -> None:
        server = ScriptedServer([])
        client = server.client()
        client.connect()

        with self.assertRaises(LinkClientError) as caught:
            client.call("ping")

        client.close()

        self.assertIn("closed the connection", str(caught.exception))

    def test_events_before_the_response_are_collected_not_returned(
        self,
    ) -> None:
        server = ScriptedServer(
            [
                encode_frame(Event(topic="searching")),
                encode_frame(Event(topic="reading", data={"done": 1})),
                encode_frame(Response.success(1, "answer")),
            ]
        )
        client = server.client()
        client.connect()

        response = client.call("search", query="x")
        client.close()

        self.assertEqual(response.result, "answer")
        self.assertEqual(
            [event.topic for event in client.events],
            ["searching", "reading"],
        )

    def test_the_request_actually_carries_its_parameters(self) -> None:
        server = ScriptedServer([encode_frame(Response.success(1, "ok"))])
        client = server.client()
        client.connect()
        client.call("search", query="قیمت دلار", limit=3)
        client.close()
        server.thread.join(timeout=5)

        sent = server.received[0]

        self.assertIn("قیمت دلار".encode("utf-8"), sent)
        self.assertIn(b'"limit":3', sent)


class TestEventCallback(unittest.TestCase):
    def test_the_callback_fires_in_arrival_order(self) -> None:
        seen: list[str] = []
        server = ScriptedServer(
            [
                encode_frame(Event(topic="one")),
                encode_frame(Event(topic="two")),
                encode_frame(Response.success(1, None)),
            ]
        )
        client = LinkClient(
            host="127.0.0.1",
            port=server.port,
            device_id=DEVICE,
            secret=SECRET,
            timeout=5.0,
            on_event=lambda event: seen.append(event.topic),
        )
        client.connect()
        client.call("ping")
        client.close()

        self.assertEqual(seen, ["one", "two"])

    def test_a_callback_that_raises_is_not_swallowed(self) -> None:
        # The phone app's own bug should be visible, not hidden behind a
        # protocol error.
        server = ScriptedServer(
            [
                encode_frame(Event(topic="one")),
                encode_frame(Response.success(1, None)),
            ]
        )

        def explode(event: Event) -> None:
            raise ValueError("app bug")

        client = LinkClient(
            host="127.0.0.1",
            port=server.port,
            device_id=DEVICE,
            secret=SECRET,
            timeout=5.0,
            on_event=explode,
        )
        client.connect()

        with self.assertRaises(ValueError):
            client.call("ping")

        client.close()


if __name__ == "__main__":
    unittest.main()
