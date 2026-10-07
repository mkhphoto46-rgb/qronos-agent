"""
The Smart Queue's three-way contract: TypeScript, Rust and Python.

The desktop invokes Tauri commands by name, Rust turns each into a bridge
command by name, and Python dispatches on that name. A typo at any hop fails
silently at runtime — the button simply does nothing — so every hop is read
from the source here and checked against the next.

The view the panel renders is checked the same way: every field the
TypeScript ``QueueTask`` type declares must be one Python actually sends.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.load_signal import LoadLevel
from core.runtime_bridge import QUEUE_COMMANDS
from tests.test_queued_conversation import Harness


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop" / "src"
TAURI = ROOT / "desktop" / "src-tauri" / "src"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def invoked_queue_commands() -> set[str]:
    names: set[str] = set()

    for source in (
        DESKTOP / "queueStore.ts",
        DESKTOP / "conversationStore.ts",
    ):
        names.update(
            re.findall(
                r"""(?:invoke|send)\(\s*"(queue_[a-z_]+)\"""",
                read(source),
            )
        )

    return names


def rust_queue_functions() -> dict[str, str]:
    """Every ``#[tauri::command] pub fn queue_*`` and its body."""
    source = read(TAURI / "runtime" / "mod.rs")
    found: dict[str, str] = {}

    for match in re.finditer(
        r"#\[tauri::command[^\]]*\]\s*pub fn (queue_[a-z_]+)\((.*?)\n\}",
        source,
        flags=re.DOTALL,
    ):
        found[match.group(1)] = match.group(2)

    return found


def test_every_queue_command_the_desktop_invokes_exists_in_rust() -> None:
    invoked = invoked_queue_commands()
    rust = rust_queue_functions()
    registered = set(re.findall(r"runtime::(queue_[a-z_]+)", read(TAURI / "lib.rs")))

    assert invoked >= {
        "queue_list",
        "queue_cancel",
        "queue_pause_task",
        "queue_resume_task",
        "queue_set_paused",
        "queue_override",
    }
    assert invoked <= set(rust), invoked - set(rust)
    assert invoked <= registered, invoked - registered


def test_every_rust_queue_command_reaches_a_python_handler() -> None:
    for name, body in rust_queue_functions().items():
        sent = re.findall(r'"command":\s*"([a-z_]+)"', body)

        assert sent, f"{name} sends no command"
        assert set(sent) <= set(QUEUE_COMMANDS), (name, sent)


def test_task_commands_carry_the_task_id_python_reads() -> None:
    rust = rust_queue_functions()

    for name in ("queue_cancel", "queue_pause_task", "queue_resume_task"):
        assert "task_id: String" in rust[name]
        assert '"taskId": cleaned' in rust[name]

    submit = rust["queue_submit"]
    assert "text: Option<String>" in submit
    assert "request_id: Option<String>" in submit
    assert 'payload["requestId"]' in submit


def test_the_panel_is_mounted_with_its_controls() -> None:
    app = read(DESKTOP / "App.tsx")
    drawer = read(DESKTOP / "components" / "QueueDrawer.tsx")

    assert "<QueueDrawer" in app
    assert "<SmartQueuePanel" in drawer

    for control in ("onCancel=", "onPause=", "onResume=", "onTogglePaused="):
        assert control in drawer

    # The emergency Pause stays runtime-owned and lives elsewhere: the queue
    # has its own hold, and neither stands in for the other.
    permissions = read(DESKTOP / "components" / "PermissionsView.tsx")
    assert "QueueDrawer" not in permissions
    assert "smartQueue" not in permissions


def test_the_stores_listen_for_events_python_actually_sends() -> None:
    bridge = read(ROOT / "core" / "runtime_bridge.py")
    stores = read(DESKTOP / "queueStore.ts") + read(DESKTOP / "conversationStore.ts")

    for event in (
        "queue_changed",
        "queue_conversation_update",
        "text_deferred",
        "queue_task_refused",
        "queue_cancel_refused",
    ):
        assert f'"{event}"' in stores, event
        assert f'"{event}"' in bridge, event


def test_every_task_field_the_panel_declares_is_one_python_sends(
    tmp_path,
    monkeypatch,
) -> None:
    panel = read(DESKTOP / "components" / "SmartQueuePanel.tsx")
    task_type = panel.split("export type QueueTask = {", 1)[1].split("};", 1)[0]
    declared = set(re.findall(r"^\s+([a-zA-Z]+)\??:", task_type, re.MULTILINE))

    harness = Harness(tmp_path, monkeypatch)
    harness.machine.level = LoadLevel.BUSY
    harness.submit("list or tuple?", "req-contract")
    (task,) = harness.scheduler.view().tasks

    assert declared, "could not read the QueueTask type"
    assert declared <= set(task), declared - set(task)
