"""WAN configuration in core.link_runtime: the validated relay request, its status and the per-device grant.

Fixture only: temp folders, an unstarted LinkRuntime (no socket, server or dial), a connector that only records,
and a PUBLIC certificate copied from the installed certifi bundle (no key, no CA of ours). Nothing is deployed and
no endpoint is a real relay.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

import certifi

from core.link_runtime import (
    LinkRuntime,
    LinkRuntimeSettings,
    LinkWanRefused,
    load_relay_endpoint,
    relay_endpoint_from_request,
)
from core.link_tunnel import RelayEndpoint

PUBLIC_CERT = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----\n",
                        Path(certifi.where()).read_text(encoding="ascii"), re.S).group(0)
NONCE = "n" * 24


class WanTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.dialled: list = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def runtime(self, **overrides) -> LinkRuntime:
        values = dict(link_enabled=True, remote_access_enabled=False, device_path=self.folder / "devices.json",
                      audit_path=self.folder / "audit.jsonl", relay_path=self.folder / "link_relay.json",
                      security_path=self.folder / "security.json", incident_path=self.folder / "incidents.jsonl")
        values.update(overrides)

        def connector(endpoint, timeout):
            self.dialled.append(endpoint)
            raise AssertionError("no dial in these fixtures")

        return LinkRuntime(LinkRuntimeSettings(**values), relay_connector=connector)

    def files(self) -> dict[str, bytes]:
        return {p.relative_to(self.folder).as_posix(): p.read_bytes() for p in self.folder.rglob("*") if p.is_file()}


class TestRelayRequest(WanTestCase):
    def test_a_valid_request_with_a_public_certificate_is_stored_content_addressed(self) -> None:
        endpoint = relay_endpoint_from_request(
            {"host": "Relay.Example.Test", "port": 8443, "serverName": "relay.example.test", "caPem": PUBLIC_CERT,
             "requestNonce": NONCE}, self.folder)
        self.assertEqual((endpoint.host, endpoint.port, endpoint.server_name), ("relay.example.test", 8443,
                                                                                "relay.example.test"))
        ca = Path(endpoint.ca_file)
        self.assertEqual(ca.parent, self.folder / "link_relay_ca")
        self.assertEqual(ca.read_text(encoding="ascii"), PUBLIC_CERT)
        self.assertRegex(ca.name, r"^[0-9a-f]{64}\.pem$")
        self.assertEqual(relay_endpoint_from_request({"host": "10.0.0.5", "port": 443}, self.folder).ca_file, None)

    def test_bad_payloads_are_refused_before_anything_is_written(self) -> None:
        good = {"host": "relay.example.test", "port": 443}
        bad = [
            "not a dict", {**good, "extra": 1}, {"port": 443}, {**good, "host": "https://relay.example.test"},
            {**good, "host": "relay.example.test/path"}, {**good, "host": "relay.example.test:443"},
            {**good, "host": "c:\\relay"}, {**good, "port": "443"}, {**good, "port": True}, {**good, "port": 0},
            {**good, "port": 65536}, {**good, "serverName": "bad name"}, {**good, "caPem": 5},
            {**good, "caPem": "not a pem"}, {**good, "caPem": "x" * (16 * 1024 + 1)},
            {**good, "caPem": PUBLIC_CERT + "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"},
            {**good, "caPem": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"},
            {**good, "caFile": "C:/ca.pem"},
        ]
        for data in bad:
            with self.subTest(data=str(data)[:60]):
                with self.assertRaises(LinkWanRefused) as refused:
                    relay_endpoint_from_request(data, self.folder)
                self.assertIn(refused.exception.code, {"invalid_request", "relay_invalid"})
        self.assertEqual(self.files(), {})


class TestRuntimeRelay(WanTestCase):
    def test_saving_while_global_remote_is_off_stores_it_without_dialling(self) -> None:
        runtime = self.runtime()
        runtime.configure_relay({"host": "relay.example.test", "port": 443, "caPem": PUBLIC_CERT}, self.folder)
        self.assertEqual(load_relay_endpoint(self.folder / "link_relay.json").host, "relay.example.test")
        self.assertEqual(self.dialled, [])
        status = runtime.status()
        self.assertEqual(status["remote_unavailable"], "remote_access_off")
        self.assertEqual(status["relay"], {"configured": True, "host": "relay.example.test", "port": 443,
                                           "tlsName": "relay.example.test", "pinnedCa": True})
        self.assertNotIn("BEGIN CERTIFICATE", json.dumps(status))

    def test_a_rejected_request_leaves_the_existing_relay_and_ca_untouched(self) -> None:
        runtime = self.runtime()
        runtime.configure_relay({"host": "relay.example.test", "port": 443, "caPem": PUBLIC_CERT}, self.folder)
        before = self.files()
        for data in ({"host": "https://evil.test", "port": 443}, {"host": "relay.example.test", "port": 443,
                                                                  "caPem": "not a pem"}):
            with self.assertRaises(LinkWanRefused):
                runtime.configure_relay(data, self.folder)
        self.assertEqual(self.files(), before)

    def test_clear_removes_the_relay_even_with_remote_off(self) -> None:
        runtime = self.runtime()
        runtime.configure_relay({"host": "relay.example.test", "port": 443}, self.folder)
        runtime.clear_relay()
        self.assertIsNone(load_relay_endpoint(self.folder / "link_relay.json"))
        self.assertEqual(runtime.status()["relay"], {"configured": False, "host": None, "port": None,
                                                     "tlsName": None, "pinnedCa": False})

    def test_an_unreadable_relay_file_reports_not_configured_without_raising(self) -> None:
        (self.folder / "link_relay.json").write_text("{", encoding="utf-8")
        self.assertFalse(self.runtime().status()["relay"]["configured"])


class TestRemoteGrant(WanTestCase):
    def test_a_grant_needs_the_global_opt_in_a_relay_and_admission(self) -> None:
        off = self.runtime()
        with self.assertRaises(LinkWanRefused) as refused:
            off.grant_remote("0123456789abcdef", 30)
        self.assertEqual(refused.exception.code, "remote_access_off")
        on = self.runtime(remote_access_enabled=True)
        with self.assertRaises(LinkWanRefused) as refused:
            on.grant_remote("0123456789abcdef", 30)
        self.assertIn(refused.exception.code, {"relay_not_configured", "security_monitor_not_ready"})

    def test_days_must_be_an_int_from_1_to_365(self) -> None:
        on = self.runtime(remote_access_enabled=True)
        for days in (0, 366, 1.5, "30", True, None):
            with self.subTest(days=days):
                with self.assertRaises(LinkWanRefused) as refused:
                    on.grant_remote("0123456789abcdef", days)
                self.assertEqual(refused.exception.code, "invalid_request")


if __name__ == "__main__":
    unittest.main()
