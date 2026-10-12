"""Exercise the actual frozen executable using a disposable, isolated profile."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any


def smoke(executable: Path, profile: Path) -> dict[str, Any]:
    executable = executable.resolve(strict=True)
    profile = profile.resolve()
    if profile.exists():
        raise ValueError("The smoke profile must be a new empty directory.")
    if profile == executable.parent or executable.parent in profile.parents:
        raise ValueError("The smoke profile must be outside the runtime bundle.")
    profile.mkdir(parents=True)
    environment = dict(os.environ)
    # Match the desktop-owned version contract, including the CI smoke run.
    desktop_config = Path(__file__).resolve().parents[1] / "desktop" / "src-tauri" / "tauri.conf.json"
    product_version = json.loads(desktop_config.read_text(encoding="utf-8"))["version"]
    environment.update({
        "QRONOS_APP_VERSION": product_version,
        "QRONOS_PACKAGED_RUNTIME": "1",
        "QRONOS_DATA_ROOT": str(profile / "Qronos"),
        "QRONOS_ASSET_ROOT": str(profile / "assets"),
        "LOCALAPPDATA": str(profile / "local"),
        "APPDATA": str(profile / "roaming"),
        "USERPROFILE": str(profile / "user"),
        "HOME": str(profile / "user"),
        "QRONOS_SETTINGS_PATH": str(profile / "Qronos/settings.json"),
        "QRONOS_CONVERSATION_HISTORY_PATH": str(profile / "Qronos/history.sqlite3"),
        "QRONOS_PERMISSION_POLICY_PATH": str(profile / "Qronos/permissions.json"),
        "QRONOS_PERMISSION_AUDIT_PATH": str(profile / "Qronos/audit.jsonl"),
        "QRONOS_APPROVAL_SECRET_PATH": str(profile / "Qronos/approval-secret.sqlite3"),
        "QRONOS_QUEUE_STATE_PATH": str(profile / "Qronos/queue-state.json"),
        "QRONOS_MEMORY_RUNTIME": "0",
        "QRONOS_DESKTOP_WAKE_BOOTSTRAP": "0",
        "PYTHON": str(profile / "no-python-here"),
        "PYTHONPATH": str(profile / "no-source-here"),
    })

    def invoke(arguments: list[str], *, body: str = "{}", expected: int = 0) -> Any:
        result = subprocess.run([str(executable), *arguments], input=body,
                                cwd=profile, env=environment, capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
        if result.returncode != expected:
            raise RuntimeError(f"Frozen service {arguments[0]} exited with {result.returncode}; expected {expected}.")
        return json.loads(result.stdout)

    imports = invoke(["--self-check"])
    if imports.get("packaged") is not True or imports.get("ok") is not True:
        raise RuntimeError("Frozen runtime import check failed.")
    checked: list[str] = []
    for service, operation in (("settings", "snapshot"), ("permissions", "snapshot"),
                               ("library", "snapshot"), ("memory", "status")):
        result = invoke([service, operation])
        if result.get("ok") is not True:
            raise RuntimeError(f"Frozen service {service} failed its structured response check.")
        checked.append(service)
    doctor = invoke(["doctor"])
    if not doctor.get("packaged") or doctor.get("dataRoot") != str(profile / "Qronos"):
        raise RuntimeError("Frozen setup diagnostics used the wrong profile.")
    if invoke(["-c", "raise SystemExit(0)"], expected=2).get("ok") is not False:
        raise RuntimeError("Frozen backend accepted arbitrary Python execution.")
    invalid = invoke(["settings", "unknown-operation"], expected=1)
    if invalid.get("ok") is not False:
        raise RuntimeError("Frozen backend did not preserve structured CLI errors.")
    bridge = subprocess.run([str(executable), "bridge"],
                            input='{"command":"ping"}\n{"command":"shutdown"}\n',
                            cwd=profile, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=60)
    if bridge.returncode:
        raise RuntimeError(f"Frozen bridge exited with {bridge.returncode}.")
    events = [json.loads(line) for line in bridge.stdout.splitlines() if line.strip()]
    types = {event.get("eventType") for event in events}
    if not {"runtime_ready", "runtime_pong", "runtime_stopping"} <= types:
        raise RuntimeError("Frozen bridge startup/ping/shutdown contract failed.")
    return {"ok": True, "services": checked, "bridge": "startup/ping/shutdown passed",
            "profile": str(profile), "missingAssets": doctor["features"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(smoke(arguments.executable, arguments.profile), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
