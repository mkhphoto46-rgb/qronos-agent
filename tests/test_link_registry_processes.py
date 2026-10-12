"""
The device registry shared between processes.

The link service holds a registry; whatever the user revokes a phone from may
hold another, in another process, on the same file. These tests run real
separate Python processes against a disposable registry file in a temporary
directory — never the user's own devices — and check the two things that
matter: a revocation made elsewhere reaches this server, and two processes
changing the file at once do not undo each other.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.link_audit import AuditEvent, AuditLog
from core.link_capability import LinkOp
from core.link_client import LinkClient, LinkClientError
from core.link_devices import DeviceRegistry, DeviceStatus, DeviceStoreCorrupt
from core.link_server import LinkServer, LinkSettings, SessionRule
from tests.fixtures.link_harness import CLIENT_TIMEOUT, ok_handler, wait_for


ROOT = Path(__file__).resolve().parent.parent


def in_another_process(script: str, *args: str) -> subprocess.Popen:
    """Run ``script`` in a fresh interpreter with the repository importable."""

    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(process: subprocess.Popen, timeout: float = 60) -> None:
    out, err = process.communicate(timeout=timeout)

    if process.returncode != 0:
        raise AssertionError(f"helper process failed:\n{out}\n{err}")


REVOKE = """
    import sys
    from core.link_devices import DeviceRegistry
    DeviceRegistry(sys.argv[1]).revoke(sys.argv[2])
"""

RENAME_MANY = """
    import sys
    from core.link_devices import DeviceRegistry
    path, device_id, label, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    registry = DeviceRegistry(path)
    for n in range(count):
        registry.rename(device_id, f"{label}-{n}")
"""


class SharedFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "link_devices.json"

    def server(self, registry: DeviceRegistry) -> tuple[LinkServer, AuditLog]:
        audit = AuditLog(path=None)
        server = LinkServer(
            registry=registry,
            audit=audit,
            handlers={LinkOp.PING: ok_handler, LinkOp.STATUS: ok_handler},
            settings=LinkSettings(host="127.0.0.1", port=0, failure_pause=0.0),
        )
        server.start()
        self.addCleanup(server.stop, 2.0)

        return server, audit

    def client(self, server: LinkServer, record) -> LinkClient:
        return LinkClient(
            host="127.0.0.1",
            port=server.address[1],
            device_id=record.device_id,
            secret=record.secret,
            timeout=CLIENT_TIMEOUT,
        )


class TestRevocationFromAnotherProcess(SharedFileTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = DeviceRegistry(self.path)
        record = self.registry.create("phone")
        self.record = self.registry.activate(record.device_id)
        self.server_, self.audit = self.server(self.registry)

    def test_new_handshakes_are_refused(self) -> None:
        with self.client(self.server_, self.record) as phone:
            self.assertTrue(phone.call("ping").ok)

        finish(in_another_process(REVOKE, str(self.path), self.record.device_id))

        with self.assertRaises(LinkClientError):
            with self.client(self.server_, self.record) as phone:
                phone.call("ping")

        self.assertIs(self.registry.get(self.record.device_id).status, DeviceStatus.REVOKED)

    def test_an_open_session_ends_at_its_next_request(self) -> None:
        phone = self.client(self.server_, self.record)
        phone.connect()
        self.addCleanup(phone.close)
        self.assertTrue(phone.call("status").ok)

        finish(in_another_process(REVOKE, str(self.path), self.record.device_id))

        with self.assertRaises(LinkClientError):
            phone.call("status")

        self.assertTrue(
            wait_for(
                lambda: [
                    e.reason for e in self.audit.records() if e.event is AuditEvent.SESSION_ENDED
                ]
                == [SessionRule.REVOKED.value]
            )
        )

    def test_a_device_file_made_unreadable_elsewhere_fails_closed(self) -> None:
        phone = self.client(self.server_, self.record)
        phone.connect()
        self.addCleanup(phone.close)
        self.assertTrue(phone.call("status").ok)

        self.path.write_text("{ not json", encoding="utf-8")

        with self.assertRaises(LinkClientError):
            phone.call("status")

        with self.assertRaises(LinkClientError):
            with self.client(self.server_, self.record) as again:
                again.call("ping")

        with self.assertRaises(DeviceStoreCorrupt):
            self.registry.get(self.record.device_id)


class TestConcurrentChanges(SharedFileTestCase):
    def test_two_processes_changing_different_devices_lose_nothing(self) -> None:
        registry = DeviceRegistry(self.path)
        first = registry.create("first")
        second = registry.create("second")
        count = "8"

        workers = [
            in_another_process(RENAME_MANY, str(self.path), first.device_id, "a", count),
            in_another_process(RENAME_MANY, str(self.path), second.device_id, "b", count),
        ]

        for worker in workers:
            finish(worker)

        fresh = DeviceRegistry(self.path)

        self.assertEqual(fresh.get(first.device_id).name, "a-7")
        self.assertEqual(fresh.get(second.device_id).name, "b-7")

    def test_a_change_here_does_not_undo_a_revocation_made_elsewhere(self) -> None:
        # The lost-update case that matters: this process still holds the
        # device as active in memory when another process revokes it, then
        # makes an unrelated change of its own.
        registry = DeviceRegistry(self.path)
        phone = registry.activate(registry.create("phone").device_id)
        other = registry.create("other")

        finish(in_another_process(REVOKE, str(self.path), phone.device_id))

        registry.rename(other.device_id, "renamed")

        fresh = DeviceRegistry(self.path)

        self.assertIs(fresh.get(phone.device_id).status, DeviceStatus.REVOKED)
        self.assertEqual(fresh.get(other.device_id).name, "renamed")

    def test_a_deleted_file_reads_as_no_devices(self) -> None:
        registry = DeviceRegistry(self.path)
        record = registry.activate(registry.create("phone").device_id)

        self.path.unlink()

        self.assertIsNone(registry.secret_for(record.device_id))
        self.assertEqual(len(registry), 0)


if __name__ == "__main__":
    unittest.main()
