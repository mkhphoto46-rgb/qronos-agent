from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PermissionsUiV2Stage2ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.view = (ROOT / "desktop" / "src" / "components" / "PermissionsView.tsx").read_text(
            encoding="utf-8"
        )
        self.css = (ROOT / "desktop" / "src" / "components" / "PermissionsView.css").read_text(
            encoding="utf-8"
        )
        self.app = (ROOT / "desktop" / "src" / "App.tsx").read_text(
            encoding="utf-8"
        )

    def test_exact_nine_user_permission_metadata_entries_exist(self) -> None:
        block = self.view.split("const USER_PERMISSION_META", 1)[1].split(
            "const USER_GROUP_LABELS", 1
        )[0]
        ids = re.findall(r"^\s{2}([a-z]+): \{$", block, flags=re.MULTILINE)
        self.assertEqual(
            ids,
            [
                "files",
                "apps",
                "browser",
                "microphone",
                "camera",
                "screen",
                "clipboard",
                "automation",
                "system",
            ],
        )

    def test_catalog_uses_user_permissions_not_internal_33_categories(self) -> None:
        self.assertIn("userPermissions.filter(", self.view)
        self.assertIn("selectedPermissionId", self.view)
        self.assertNotIn("selectedCategory", self.view)

    def test_policy_selector_calls_real_backend_store_mutation(self) -> None:
        self.assertIn("setPermissionPolicy(selected.id, policy)", self.view)
        self.assertIn("setPermissionCustomRule(selected.id, category, policy)", self.view)
        self.assertIn("resetPermission(selected.id)", self.view)

    def test_pause_actions_is_owned_by_runtime_and_separate_from_queue_hold(
        self,
    ) -> None:
        """
        Two controls, two commands, two states - and neither stands in for
        the other.

        Pause Actions (PermissionsView) pauses all of Qronos through
        ``runtime_set_paused``. The Smart Queue's Hold, mounted in the app
        through QueueDrawer, holds only queued work through
        ``queue_set_paused``. Each is checked at the place it is wired, not
        by a string being absent from App.tsx, and the backend is checked to
        keep the two states independent.
        """
        src = ROOT / "desktop" / "src"
        drawer = (src / "components" / "QueueDrawer.tsx").read_text(
            encoding="utf-8"
        )
        panel = (src / "components" / "SmartQueuePanel.tsx").read_text(
            encoding="utf-8"
        )
        store = (src / "queueStore.ts").read_text(encoding="utf-8")
        rust = (
            ROOT / "desktop" / "src-tauri" / "src" / "runtime" / "mod.rs"
        ).read_text(encoding="utf-8")

        # Pause Actions: PermissionsView's control drives the runtime pause.
        self.assertIn("onToggleRuntimePaused(!runtimePaused)", self.view)
        self.assertIn("aria-pressed={runtimePaused}", self.view)
        self.assertIn("runtimePaused={", self.app)
        self.assertRegex(
            self.app,
            r"onToggleRuntimePaused=\{\s*setRuntimePause\s*\}",
        )
        runtime_pause = self.app.split("const setRuntimePause", 1)[1].split(
            "[],", 1
        )[0]
        self.assertIn('"runtime_set_paused"', runtime_pause)
        self.assertNotIn("queue_set_paused", runtime_pause)
        self.assertIn("setRuntimePaused(", runtime_pause)
        for queue_wiring in (
            "queue_set_paused",
            "QueueDrawer",
            "SmartQueue",
            "smartQueue",
            "queueStore",
        ):
            self.assertNotIn(queue_wiring, self.view)

        # Queue Hold: the panel is mounted, and its Hold reaches only the
        # queue's own command.
        self.assertIn("<QueueDrawer", self.app)
        self.assertIn("<SmartQueuePanel", drawer)
        self.assertRegex(
            drawer,
            r"onTogglePaused=\{\(held\) => void setQueueHeld\(held\)\}",
        )
        hold = store.split("export function setQueueHeld", 1)[1].split("}", 1)[0]
        self.assertIn('"queue_set_paused"', hold)
        self.assertNotIn("runtime_set_paused", store)
        self.assertNotIn("runtime_set_paused", drawer)
        # The panel shows Qronos's pause but offers no control for it.
        self.assertIn("snapshot?.runtimePaused", panel)
        self.assertNotIn("runtime_set_paused", panel)
        self.assertNotIn("onToggleRuntimePaused", panel)

        # Rust keeps them as two commands with two payloads.
        for command in ("queue_set_paused", "runtime_set_paused"):
            body = rust.split(f"pub fn {command}(", 1)[1].split("\n}", 1)[0]
            self.assertIn(f'"command": "{command}"', body)

        # And the backend keeps two independent states.
        from core.load_signal import LoadLevel, LoadSnapshot
        from core.queue_scheduler import QueueScheduler
        from core.resource_governor import ResourceGovernor
        from core.safe_queue import SafeQueue

        class Free:
            def observe(self):
                return None

            def snapshot(self):
                return LoadSnapshot(LoadLevel.FREE, 0.0, None, 0, 0.0)

        scheduler = QueueScheduler(
            queue=SafeQueue(),
            governor=ResourceGovernor(),
            monitor=Free(),  # type: ignore[arg-type]
        )
        scheduler.set_runtime_paused(True)
        self.assertFalse(scheduler.view().paused)
        self.assertTrue(scheduler.view().runtime_paused)

        scheduler.set_paused(True)
        scheduler.set_runtime_paused(False)
        self.assertTrue(scheduler.view().paused)
        self.assertFalse(scheduler.view().runtime_paused)

    def test_security_activity_is_top_level_and_has_no_manual_refresh(self) -> None:
        activity = self.view.split(
            '<aside className="permissions-activity-panel">', 1
        )[1]
        self.assertTrue(
            activity.lstrip().startswith(
                '<header className="permissions-security-activity-head">'
            )
        )
        self.assertNotIn("permissions-audit-refresh", self.view)
        self.assertNotIn("REFRESH AUDIT", self.view)
        self.assertIn("window.setInterval(", self.view)

    def test_catalog_is_smaller_and_detail_column_is_flexible(self) -> None:
        layout_blocks = re.findall(
            r"\.permissions-layout\s*\{(.*?)\}",
            self.css,
            flags=re.DOTALL,
        )
        self.assertTrue(
            any(
                "276px" in block
                and "470px" in block
                and "248px" in block
                for block in layout_blocks
            )
        )
        self.assertIn("min-height: 50px !important;", self.css)
        self.assertIn("width: 34px !important;", self.css)
        self.assertIn("font-size: 11.25px !important;", self.css)

    def test_screen_permission_is_present_and_truthfully_marked_ready(self) -> None:
        screen_block = self.view.split("screen: {", 1)[1].split("clipboard: {", 1)[0]
        self.assertIn('availability: "READY"', screen_block)
        self.assertIn("READ_SCREEN", screen_block)
        self.assertIn("Effective Resolver", screen_block)

    def test_camera_is_not_pretended_available(self) -> None:
        camera_block = self.view.split("camera: {", 1)[1].split("screen: {", 1)[0]
        self.assertIn('availability: "UNAVAILABLE"', camera_block)

    def test_policy_truth_english_alignment_is_ltr_and_left(self) -> None:
        self.assertIn(".permissions-ltr", self.css)
        self.assertIn("direction: ltr !important;", self.css)
        self.assertIn("text-align: left !important;", self.css)
        self.assertIn(".permissions-policy-truth .permissions-action-row", self.css)

    def test_old_read_only_baseline_copy_is_removed_from_view(self) -> None:
        self.assertNotIn("Security baseline is read-only in Permissions V1.", self.view)
        self.assertNotIn("READ-ONLY", self.view)


if __name__ == "__main__":
    unittest.main()
