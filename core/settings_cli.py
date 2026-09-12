from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audio_input import canonical_input_device_id, list_input_devices
from core.audio_output import canonical_output_device_id, list_output_devices
from core.settings_service import (
    DEFAULT_DEFINITIONS,
    DEFAULT_SETTINGS_PATH,
    SCHEMA_VERSION,
    SettingDefinition,
    SettingsService,
)


def _payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}

    parsed = json.loads(raw)

    if not isinstance(parsed, dict):
        raise ValueError("Settings payload must be a JSON object.")

    return parsed


def _settings_path() -> Path:
    override = os.environ.get("QRONOS_SETTINGS_PATH", "").strip()
    return Path(override) if override else DEFAULT_SETTINGS_PATH


def _value_type(definition: SettingDefinition) -> str:
    if isinstance(definition.default, bool):
        return "boolean"

    if isinstance(definition.default, (int, float)):
        return "number"

    return "string"


def _snapshot(
    service: SettingsService,
    problems: list[str],
) -> dict[str, Any]:
    values = service.all_values()

    definitions = [
        {
            "key": definition.key,
            "description": definition.description,
            "valueType": _value_type(definition),
            "default": definition.default,
            "minimum": definition.minimum,
            "maximum": definition.maximum,
            "choices": (
                list(definition.choices)
                if definition.choices is not None
                else None
            ),
            "isDefault": service.is_default(definition.key),
        }
        for definition in service.definitions()
    ]

    return {
        "ok": True,
        "schemaVersion": SCHEMA_VERSION,
        "storePath": (
            str(service.path)
            if service.path is not None
            else ""
        ),
        "writeBlocked": service.write_blocked_reason is not None,
        "writeBlockedReason": service.write_blocked_reason or "",
        "values": values,
        "definitions": definitions,
        "problems": list(problems),
    }


def _payload_for_operation(
    operation: str,
) -> dict[str, Any]:
    """
    Snapshot/reset_all are payloadless operations.

    Do not read stdin for them: in an interactive shell stdin is the console,
    and sys.stdin.read() would wait for EOF forever. Mutating/keyed operations
    still read their JSON body from the pipe opened by the Tauri command.
    """
    if operation in {
        "snapshot",
        "reset_all",
        "audio_devices",
        "audio_output_devices",
    }:
        return {}

    return _payload()


def main() -> int:
    if len(sys.argv) < 2:
        raise ValueError("Settings operation is required.")

    operation = sys.argv[1].strip().lower()
    payload = _payload_for_operation(operation)

    problems: list[str] = []
    service = SettingsService(
        DEFAULT_DEFINITIONS,
        path=_settings_path(),
        on_problem=problems.append,
    )

    if operation == "snapshot":
        result = _snapshot(service, problems)

    elif operation == "audio_devices":
        selected_id = str(
            service.get("voice.input_device_id") or ""
        ).strip()

        try:
            canonical_selected_id = (
                canonical_input_device_id(
                    selected_id
                )
            )
        except LookupError:
            canonical_selected_id = ""

        result = {
            "ok": True,
            "devices": [
                device.to_public_dict()
                for device in list_input_devices()
            ],
            "selectedDeviceId": canonical_selected_id,
            "selectionRemapped": bool(
                selected_id
                and canonical_selected_id
                and selected_id != canonical_selected_id
            ),
        }

    elif operation == "audio_output_devices":
        selected_id = str(
            service.get("voice.output_device_id") or ""
        ).strip()

        try:
            canonical_selected_id = (
                canonical_output_device_id(
                    selected_id
                )
            )
        except LookupError:
            canonical_selected_id = ""

        result = {
            "ok": True,
            "devices": [
                device.to_public_dict()
                for device in list_output_devices()
            ],
            "selectedDeviceId": canonical_selected_id,
            "selectionRemapped": bool(
                selected_id
                and canonical_selected_id
                and selected_id != canonical_selected_id
            ),
        }

    elif operation == "get":
        key = str(payload.get("key", "")).strip()
        if not key:
            raise ValueError("Settings get requires key.")

        result = {
            "ok": True,
            "key": key,
            "value": service.get(key),
            **_snapshot(service, problems),
        }

    elif operation == "set":
        key = str(payload.get("key", "")).strip()
        if not key:
            raise ValueError("Settings set requires key.")

        if "value" not in payload:
            raise ValueError("Settings set requires value.")

        service.set(key, payload["value"])
        result = _snapshot(service, problems)

    elif operation == "update":
        values = payload.get("values")
        if not isinstance(values, dict):
            raise ValueError("Settings update requires values object.")

        service.update(values)
        result = _snapshot(service, problems)

    elif operation == "reset":
        key = str(payload.get("key", "")).strip()
        if not key:
            raise ValueError("Settings reset requires key.")

        service.reset(key)
        result = _snapshot(service, problems)

    elif operation == "reset_all":
        service.reset_all()
        result = _snapshot(service, problems)

    else:
        raise ValueError(
            f"Unsupported settings operation: {operation}"
        )

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "errorType": type(exc).__name__,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        raise SystemExit(1)
