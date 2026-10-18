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
}


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
    refused = _admit_before_import(service)
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


def _admit_before_import(service: str) -> int | None:
    """core/startup_admission.py for a store-opening service; an exit code when start-up is refused."""
    policy = STORE_SERVICES.get(service)
    if policy is None:
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
