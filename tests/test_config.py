from __future__ import annotations

import unittest
from pathlib import Path

from core.config import CONFIG


class TestQronosConfig(unittest.TestCase):
    def test_project_root_is_correct(self) -> None:
        expected = Path(__file__).resolve().parents[1]
        self.assertEqual(CONFIG.paths.root.resolve(), expected.resolve())

    def test_security_defaults_are_disabled(self) -> None:
        self.assertFalse(CONFIG.security.camera_enabled)
        self.assertFalse(CONFIG.security.microphone_enabled)
        self.assertFalse(CONFIG.security.link_enabled)
        self.assertFalse(CONFIG.security.remote_access_enabled)
        self.assertFalse(CONFIG.security.external_ai_enabled)

    def test_the_device_link_is_off_by_default(self) -> None:
        # Both layers. Nothing in the link starts by itself, and Layer 2 needs
        # this plus a per-device opt-in.
        self.assertFalse(CONFIG.security.link_enabled)
        self.assertFalse(CONFIG.security.remote_access_enabled)

    def test_destructive_actions_require_approval(self) -> None:
        self.assertTrue(
            CONFIG.security.destructive_actions_require_approval
        )


if __name__ == "__main__":
    unittest.main()