"""Closed entry point for the bundled Python backend.

Unlike shipping a general Python interpreter, this executable exposes only
the reviewed Qronos services. It does not accept scripts, expressions, or
arbitrary module names from the desktop.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from typing import Sequence


PROTOCOL_VERSION = 1
SERVICES = {
    "bridge": "core.runtime_bridge",
    "settings": "core.settings_cli",
    "library": "core.library_cli",
    "memory": "core.memory_cli",
    "permissions": "core.permissions_cli",
    "audio": "core.audio_output_cli",
    "doctor": "core.runtime_preflight",
    # Lifecycle only (`supervisor --desktop`): starts the bridge, relays the
    # desktop's pipes to it and keeps it alive; no audio, UI or dialogs.
    "supervisor": "core.supervisor",
    # The Installer's offline update service (core/update_cli.py). Its launch
    # stays refused (launch_unavailable) until the hand-over path is accepted.
    "update": "core.update_cli",
}

#: Services that open Qronos's persistent stores. Each one runs the start-up
#: order of core/startup_admission.py BEFORE its module is imported, so no
#: import-time code can open a store first: the bridge also owns its singleton.
#: The supervisor opens no store (it owns its own singleton in run_desktop);
#: audio (playback) and doctor (read-only diagnostics) open none either.
STORE_SERVICES = {
    "bridge": {"instance": True},
    "settings": {},
    "library": {},
    "memory": {},
    "permissions": {},
    "update": {},
}
#: The one update operation that must NOT take a shared admission: launching
#: takes the EXCLUSIVE admission itself (core.update_service.LaunchHold), and a
#: process cannot hold both. With the default quiescence it is refused anyway.
UPDATE_EXCLUSIVE_OPERATIONS = frozenset({"launch"})
#: Services that open no store and own no singleton of their own. They still run this executable, which an
#: update's installer replaces, so while the update fence exists (an update launch or its installer is alive)
#: they refuse BEFORE their module is imported. The store services are refused by their writer admission, the
#: bridge and supervisor by their instance ownership, the sidecars by their guards (each checks the same fence).
FENCED_SERVICES = frozenset({"audio", "doctor"})


def _prevent_asio_selection() -> None:
    """Keep the frozen backend on the bundled non-ASIO PortAudio build.

    sounddevice loads its ASIO-enabled DLL when SD_ENABLE_ASIO is set. That
    build is not shipped or reviewed, so the inherited variable is dropped from
    this process only, before any service can import sounddevice. Source mode
    and the user's environment settings are left alone.
    """
    if getattr(sys, "frozen", False):
        os.environ.pop("SD_ENABLE_ASIO", None)


def main(argv: Sequence[str] | None = None) -> int:
    _prevent_asio_selection()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--version"]:
        print(json.dumps({"name": "qronos-runtime", "protocolVersion": PROTOCOL_VERSION}))
        return 0
    if arguments == ["--self-check"]:
        # Import every exposed service without running it. No microphone,
        # model, application, or persistent user state is opened here.
        for module in SERVICES.values():
            importlib.import_module(module)
        from core.config import CONFIG
        print(json.dumps({
            "ok": True,
            "protocolVersion": PROTOCOL_VERSION,
            "packaged": CONFIG.paths.packaged,
            "services": sorted(SERVICES),
        }))
        return 0
    if not arguments or arguments[0] not in SERVICES:
        print(json.dumps({"ok": False, "error": "Unknown Qronos backend service."}))
        return 2
    service = arguments.pop(0)
    refused = _refuse_during_update(service)
    if refused is not None:
        return refused
    refused = _admit_before_import(service, arguments)
    if refused is not None:
        return refused
    sys.argv = [service, *arguments]
    try:
        return int(importlib.import_module(SERVICES[service]).main())
    except Exception as error:
        # The source CLIs normally provide this contract in their __main__
        # blocks. Frozen dispatch calls main directly, so preserve it here.
        print(json.dumps({"ok": False, "error": str(error), "errorType": type(error).__name__}, ensure_ascii=False))
        return 1


def _refuse_during_update(service: str) -> int | None:
    """While the update fence exists, a FENCED service refuses before import (typed update_in_progress).

    An unknown fence answer refuses too (ownership_unavailable): nothing continues on an unknown fence. Returns the
    exit code after printing the one refusal JSON, or None to continue.
    """
    if service not in FENCED_SERVICES:
        return None
    from core import startup_admission
    from core.instance_ownership import update_fence_exists

    try:
        live = bool(update_fence_exists())
        refused = startup_admission.StartupRefused("update_in_progress") if live else None
    except Exception:  # noqa: BLE001 - an unknown fence is never "no update"
        refused = startup_admission.StartupRefused("ownership_unavailable",
                                                   "The update fence could not be checked; nothing was started.")
    if refused is None:
        return None
    print(json.dumps(startup_admission.refusal(refused), ensure_ascii=False), flush=True)
    return startup_admission.STARTUP_REFUSED_EXIT


def _admit_before_import(service: str, arguments: list) -> int | None:
    """core/startup_admission.py for a store-opening service; an exit code when start-up is refused."""
    policy = STORE_SERVICES.get(service)
    if policy is None:
        return None
    if service == "update" and arguments[:1] and arguments[0].strip().lower() in UPDATE_EXCLUSIVE_OPERATIONS:
        return None
    from core import startup_admission

    try:
        startup_admission.enter(service, instance=bool(policy.get("instance")))
    except startup_admission.StartupRefused as refused:
        if service == "bridge":
            # The bridge's own event shape, relayed to the desktop by the supervisor.
            print(json.dumps({"eventType": "runtime_error", "status": "error",
                              "message": f"{refused} ({refused.code})"}, ensure_ascii=True), flush=True)
        else:
            print(json.dumps(startup_admission.refusal(refused), ensure_ascii=False), flush=True)
        return startup_admission.STARTUP_REFUSED_EXIT
    return None


if __name__ == "__main__":
    raise SystemExit(main())
