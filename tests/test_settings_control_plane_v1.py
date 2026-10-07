from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.json_store import JsonStore
from core.settings_service import (
    DEFAULT_DEFINITIONS,
    InvalidSettingValue,
    SCHEMA_VERSION,
    SettingsService,
    SettingsWriteBlocked,
)


ROOT = Path(__file__).resolve().parent.parent


class SettingsControlPlaneV1Tests(unittest.TestCase):
    def test_payloadless_operations_never_read_console_stdin(self) -> None:
        from core import settings_cli

        with patch.object(
            settings_cli,
            "_payload",
            side_effect=AssertionError(
                "payloadless operation attempted to read stdin"
            ),
        ):
            self.assertEqual(
                settings_cli._payload_for_operation("snapshot"),
                {},
            )
            self.assertEqual(
                settings_cli._payload_for_operation("reset_all"),
                {},
            )

    def test_payload_operations_still_read_piped_json(self) -> None:
        from core import settings_cli

        with patch.object(
            settings_cli,
            "_payload",
            return_value={
                "key": "voice.language",
                "value": "en",
            },
        ) as payload_reader:
            payload = settings_cli._payload_for_operation("set")

        payload_reader.assert_called_once_with()
        self.assertEqual(
            payload["key"],
            "voice.language",
        )

    def test_write_is_persisted_before_in_memory_commit(self) -> None:
        service = SettingsService(
            DEFAULT_DEFINITIONS,
            path=None,
        )
        original = service.get("voice.language")

        class FailingStore:
            path = None

            @staticmethod
            def save(_payload: object) -> None:
                raise OSError("simulated disk failure")

        service._store = FailingStore()  # type: ignore[attr-defined]

        with self.assertRaises(OSError):
            service.set(
                "voice.language",
                "en",
            )

        self.assertEqual(
            service.get("voice.language"),
            original,
        )

    def test_unknown_same_schema_keys_survive_known_setting_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"

            JsonStore(
                path,
                version=SCHEMA_VERSION,
            ).save(
                {
                    "voice.language": "fa",
                    "future.setting": "keep-me",
                }
            )

            service = SettingsService(
                DEFAULT_DEFINITIONS,
                path=path,
            )
            service.set(
                "voice.language",
                "en",
            )

            payload = JsonStore(
                path,
                version=SCHEMA_VERSION,
            ).load_payload()

            self.assertEqual(
                payload["voice.language"],
                "en",
            )
            self.assertEqual(
                payload["future.setting"],
                "keep-me",
            )

    def test_invalid_batch_is_all_or_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            service = SettingsService(
                DEFAULT_DEFINITIONS,
                path=path,
            )

            with self.assertRaises(InvalidSettingValue):
                service.update(
                    {
                        "voice.language": "en",
                        "voice.silence_timeout_seconds": 99.0,
                    }
                )

            self.assertEqual(
                service.get("voice.language"),
                "fa",
            )
            self.assertEqual(
                service.get("voice.silence_timeout_seconds"),
                2.0,
            )
            self.assertFalse(path.exists())

    def test_corrupt_store_fails_open_for_reads_but_blocks_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            path.write_text(
                "{not-json",
                encoding="utf-8",
            )
            problems: list[str] = []

            service = SettingsService(
                DEFAULT_DEFINITIONS,
                path=path,
                on_problem=problems.append,
            )

            self.assertEqual(
                service.get("voice.language"),
                "fa",
            )
            self.assertTrue(problems)
            self.assertIsNotNone(
                service.write_blocked_reason
            )

            with self.assertRaises(SettingsWriteBlocked):
                service.set(
                    "voice.language",
                    "en",
                )

            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "{not-json",
            )

    def test_newer_schema_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            original = {
                "schemaVersion": SCHEMA_VERSION + 1,
                "payload": {
                    "future.setting": "newer",
                },
            }
            path.write_text(
                json.dumps(original),
                encoding="utf-8",
            )

            service = SettingsService(
                DEFAULT_DEFINITIONS,
                path=path,
            )

            with self.assertRaises(SettingsWriteBlocked):
                service.reset_all()

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                original,
            )

    def test_tauri_settings_command_is_registered(self) -> None:
        runtime_source = (
            ROOT
            / "desktop"
            / "src-tauri"
            / "src"
            / "runtime"
            / "mod.rs"
        ).read_text(encoding="utf-8")
        lib_source = (
            ROOT
            / "desktop"
            / "src-tauri"
            / "src"
            / "lib.rs"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "pub async fn settings_command(",
            runtime_source,
        )
        self.assertIn(
            "Service::Settings",
            runtime_source,
        )
        self.assertIn(
            "settings_command: Arc<tokio::sync::Mutex<()>>",
            runtime_source,
        )
        self.assertIn(
            "runtime::settings_command,",
            lib_source,
        )
        self.assertIn("Some(state.settings_command.clone())", runtime_source)
        ipc_source = (ROOT / "desktop/src-tauri/src/runtime/service_ipc.rs").read_text(encoding="utf-8")
        self.assertIn("gate.lock_owned()", ipc_source)
        self.assertIn("let _guard = guard;", ipc_source)

    def test_frontend_store_is_backend_first_and_serialized(self) -> None:
        source = (
            ROOT
            / "desktop"
            / "src"
            / "settingsStore.ts"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '"settings_command"',
            source,
        )
        self.assertIn(
            '"set",',
            source,
        )
        self.assertIn(
            '"update",',
            source,
        )
        self.assertIn(
            '"reset",',
            source,
        )
        self.assertIn(
            'command("reset_all")',
            source,
        )
        self.assertIn(
            "let operationTail: Promise<void>",
            source,
        )
        self.assertNotIn(
            "localStorage",
            source,
        )
        self.assertNotIn(
            "sessionStorage",
            source,
        )


if __name__ == "__main__":
    unittest.main()
