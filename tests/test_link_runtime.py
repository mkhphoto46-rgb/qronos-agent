"""
The link's lifecycle helper: what the runtime and desktop would call.

Disposable files in a temporary directory, loopback only, the fake relay for
Layer 2. Never the user's own devices.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.link_client import LinkClient, LinkClientError
from core.link_devices import DeviceStatus
from core.link_runtime import (
    LinkRuntime,
    LinkRuntimeSettings,
    LinkServices,
    load_relay_endpoint,
    save_relay_endpoint,
)
from core.link_tunnel import RelayConfigError, RelayEndpoint
from tests.fixtures.link_fake_relay import FakeRelay
from tests.fixtures.link_harness import CLIENT_TIMEOUT, wait_for
from tests.fixtures.link_remote_rig import FAST_TUNNEL


class Chat:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        return "پاسخ"


class RuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def settings(self, **overrides) -> LinkRuntimeSettings:
        values = dict(
            link_enabled=True,
            remote_access_enabled=False,
            port=0,
            bind_host="127.0.0.1",
            device_path=self.folder / "link_devices.json",
            audit_path=self.folder / "link_audit.jsonl",
            relay_path=self.folder / "link_relay.json",
            security_path=self.folder / "link_security.json",
            incident_path=self.folder / "link_incidents.jsonl",
            pairing_host="127.0.0.1",
        )
        values.update(overrides)

        return LinkRuntimeSettings(**values)

    def runtime(self, **overrides) -> LinkRuntime:
        self.chat = Chat()
        runtime = LinkRuntime(
            self.settings(**overrides),
            services=LinkServices(remote_chat=self.chat),
            relay_connector=FakeRelay.connector,
            tunnel_settings=FAST_TUNNEL,
        )
        self.addCleanup(runtime.stop)

        return runtime

    def pair(self, runtime: LinkRuntime) -> tuple[LinkClient, str]:
        uri = runtime.open_pairing("گوشی")
        client = LinkClient.from_uri(uri, timeout=CLIENT_TIMEOUT)
        client.connect()
        self.addCleanup(client.close)
        self.assertTrue(client.call("ping").ok)

        return client, client.device_id


class TestSwitches(RuntimeTestCase):
    def test_nothing_starts_when_the_link_is_off(self) -> None:
        runtime = self.runtime(link_enabled=False)

        self.assertFalse(runtime.start())
        self.assertFalse(runtime.running)
        self.assertIsNone(runtime.server)

    def test_the_defaults_mirror_the_config_switches_which_are_off(self) -> None:
        settings = LinkRuntimeSettings.from_config()

        self.assertFalse(settings.link_enabled)
        self.assertFalse(settings.remote_access_enabled)

    def test_local_only_when_remote_access_is_off(self) -> None:
        save_relay_endpoint(self.folder / "link_relay.json", RelayEndpoint(host="127.0.0.1", port=9))
        runtime = self.runtime()
        runtime.start()

        self.assertTrue(runtime.running)
        self.assertFalse(runtime.remote_active)
        self.assertEqual(runtime.status()["remote_unavailable"], "remote_access_off")

    def test_remote_access_without_a_relay_stays_local(self) -> None:
        runtime = self.runtime(remote_access_enabled=True)
        runtime.start()

        self.assertFalse(runtime.remote_active)
        self.assertEqual(runtime.status()["remote_unavailable"], "no_relay_configured")

    def test_a_damaged_relay_file_leaves_the_local_link_running(self) -> None:
        (self.folder / "link_relay.json").write_text("{", encoding="utf-8")
        runtime = self.runtime(remote_access_enabled=True)

        self.assertTrue(runtime.start())
        self.assertFalse(runtime.remote_active)
        self.assertEqual(runtime.status()["remote_unavailable"], "relay_settings_unreadable")


class TestLocalLifecycle(RuntimeTestCase):
    def test_pair_list_and_revoke(self) -> None:
        runtime = self.runtime()
        runtime.start()
        client, device_id = self.pair(runtime)

        listed = runtime.devices()
        self.assertEqual([d.device_id for d in listed], [device_id])
        self.assertEqual(listed[0].status, "active")
        self.assertEqual(listed[0].name, "گوشی")

        # The desktop's view of a device never carries its key.
        rendered = json.dumps([d.to_json() for d in listed], ensure_ascii=False)
        self.assertNotIn(client.secret.hex(), rendered)
        self.assertNotIn("secret", rendered)

        runtime.revoke(device_id)

        with self.assertRaises(LinkClientError):
            client.call("ping")

        self.assertEqual(runtime.devices()[0].status, DeviceStatus.REVOKED.value)

    def test_a_question_on_the_local_network_is_answer_only_by_default(self) -> None:
        runtime = self.runtime()
        runtime.start()
        client, _ = self.pair(runtime)

        response = client.call("ask", question="سلام")

        self.assertEqual(response.result["answer"], "پاسخ")
        self.assertEqual(response.result["performed_actions"], [])

    def test_stop_ends_sessions_and_closes_the_window(self) -> None:
        runtime = self.runtime()
        runtime.start()
        client, _ = self.pair(runtime)
        runtime.open_pairing("second")

        runtime.stop()

        self.assertFalse(runtime.running)

        with self.assertRaises(LinkClientError):
            client.call("ping")

        # The unfinished second pairing left no pending device behind.
        self.assertEqual(len(runtime.devices()), 1)


class TestRemoteLifecycle(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.relay = FakeRelay()
        self.addCleanup(self.relay.close)
        save_relay_endpoint(self.folder / "link_relay.json", self.relay.endpoint)

    def test_remote_access_end_to_end_through_the_runtime(self) -> None:
        runtime = self.runtime(remote_access_enabled=True)
        runtime.start()
        self.assertTrue(runtime.remote_active)

        local, device_id = self.pair(runtime)

        # Before the opt-in, the phone is told nothing about the relay.
        self.assertNotIn("remote", local.call("ping").result)

        runtime.set_remote_access(device_id, True, days=7)
        told = local.call("ping").result["remote"]

        self.assertEqual(told["relay"], {"host": "127.0.0.1", "port": self.relay.port})
        self.assertTrue(wait_for(lambda: runtime.status()["parked_devices"] == 1))

        remote = LinkClient(
            host="unused",
            port=1,
            device_id=device_id,
            secret=local.secret,
            timeout=CLIENT_TIMEOUT,
            relay=RelayEndpoint(**told["relay"]),
            relay_connector=FakeRelay.connector,
        )

        with remote:
            response = remote.call("ask", question="سلام")

        self.assertEqual(response.result["answer"], "پاسخ")

        runtime.set_remote_access(device_id, False)

        self.assertTrue(wait_for(lambda: runtime.status()["parked_devices"] == 0))
        self.assertTrue(local.call("ping").ok)

    def test_removing_the_relay_turns_remote_access_off(self) -> None:
        runtime = self.runtime(remote_access_enabled=True)
        runtime.start()
        self.assertTrue(runtime.remote_active)

        runtime.set_relay_endpoint(None)

        self.assertTrue(runtime.running)
        self.assertFalse(runtime.remote_active)


class TestRelaySettingsFile(RuntimeTestCase):
    def test_round_trip(self) -> None:
        path = self.folder / "link_relay.json"
        endpoint = RelayEndpoint(host="relay.example.org", port=8443, server_name="relay.example.org")

        save_relay_endpoint(path, endpoint)

        self.assertEqual(load_relay_endpoint(path), endpoint)

    def test_no_file_means_no_relay(self) -> None:
        self.assertIsNone(load_relay_endpoint(self.folder / "absent.json"))

    def test_a_damaged_file_is_an_error_not_an_absence(self) -> None:
        path = self.folder / "link_relay.json"
        path.write_text("{", encoding="utf-8")

        with self.assertRaises(RelayConfigError):
            load_relay_endpoint(path)

    def test_an_invalid_relay_in_the_file_is_refused(self) -> None:
        path = self.folder / "link_relay.json"
        path.write_text(
            json.dumps({"version": 1, "relay": {"host": "https://relay.example.org/x"}}),
            encoding="utf-8",
        )

        with self.assertRaises(RelayConfigError):
            load_relay_endpoint(path)


if __name__ == "__main__":
    unittest.main()
