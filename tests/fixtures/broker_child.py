"""
A disposable, NON-elevated stand-in for the Action Broker process, run as a
normal child by tests/test_action_broker_session.py.

It runs the real ``core.action_broker.serve_once`` with the real channel,
verification and single-use journal; the only difference from production is
the action table: one test action that writes a marker file into a directory
the test owns. Nothing here is elevated, touches a device or user data.

Usage: python broker_child.py WORK_DIR --pipe ADDRESS --key HEX --runtime-pid PID
       [--tamper | --vanish | --spooler]

    --vanish   perform the action, then exit without answering (a lost answer)
    --spooler  use the PRODUCTION ``printer.spooler_restart`` spec exactly, with
               only its ``perform`` replaced by a marker write: the real
               Service Control Manager is never touched
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core import action_broker  # noqa: E402
from core.action_broker import ActionRegistry, ActionSpec, ConsumedApprovals  # noqa: E402
from security.permissions import ActionCategory  # noqa: E402


def registry(work: Path) -> ActionRegistry:
    def perform(params) -> str:
        (work / "markers").mkdir(exist_ok=True)
        (work / "markers" / f"{params['name']}.done").write_text("performed", encoding="utf-8")
        return f"marker {params['name']} written"

    def validate(params) -> None:
        if not params["name"].isalnum():
            raise action_broker.BrokerRefusal("bad_parameters", "Marker names are letters and digits.")

    return ActionRegistry([
        ActionSpec(
            kind="fixture.write_marker",
            category=ActionCategory.CREATE_OR_EDIT_FILE,
            fields={"name": str},
            target=lambda params: f"test marker {params['name']}",
            perform=perform,
            summary="Write a test marker file.",
            validate=validate,
        ),
    ])


def spooler_registry(work: Path) -> ActionRegistry:
    from dataclasses import replace

    def perform(params) -> str:
        (work / "markers").mkdir(exist_ok=True)
        (work / "markers" / "spooler.done").write_text("performed", encoding="utf-8")
        return "fixture: spooler restart recorded"

    return ActionRegistry([replace(action_broker.PRODUCTION_ACTIONS.get("printer.spooler_restart"), perform=perform)])


def main() -> int:
    work = Path(sys.argv[1])
    arguments = sys.argv[2:]
    flags = {flag for flag in ("--tamper", "--vanish", "--spooler") if flag in arguments}
    for flag in flags:
        arguments.remove(flag)
    tamper = "--tamper" in flags
    address, key, runtime_pid = action_broker.parse_arguments(arguments)

    if "--vanish" in flags:
        original_handle = action_broker.handle_request

        def vanish(message, *args, **kwargs):
            original_handle(message, *args, **kwargs)
            import os

            os._exit(5)  # acted, never answered

        action_broker.handle_request = vanish

    if tamper:
        # A broker that receives an approval but acts on different parameters
        # would be caught by the digest; this one instead rewrites what it was
        # sent before verifying, to show the digest check refuses it.
        original = action_broker.handle_request

        def tampered(message, *args, **kwargs):
            message["action"]["params"]["name"] = "other"
            return original(message, *args, **kwargs)

        action_broker.handle_request = tampered

    result = action_broker.serve_once(
        address, key, runtime_pid,
        registry=spooler_registry(work) if "--spooler" in flags else registry(work),
        consumed=ConsumedApprovals(work / "consumed"),
        audit=action_broker.jsonl_audit(work / "audit.jsonl"),
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
