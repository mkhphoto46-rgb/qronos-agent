"""
Bounded startup status hooks for the runtime bridge.

A few services owned by other parts of Qronos need to report a status once
the bridge is ready. The first is the update service (A8, Installer-owned),
which reports whether a verified update is waiting. They must never hold up
or break chat, wake or Device Link, whatever state they are in — including
an unconfigured update trust store.

So a hook here is a closed record (event type, module, function), not code
from a caller. All hooks run on one daemon thread after ``runtime_ready``.
Each is called with a small read-only context, must return a JSON object of
at most ``MAX_STATUS_BYTES`` within ``HOOK_TIMEOUT_SECONDS``, and becomes one
event of its own type with status ``ready``. A hook that raises, returns
something else, or runs out of time becomes the same event with status
``unavailable`` and only the failure's type; the bridge carries on. A hook
that never returns is abandoned on its daemon thread.

The context holds ``appVersion``: the running product version, which the
desktop passes from its own package version (``tauri.conf.json``) as
``QRONOS_APP_VERSION``. It is the empty string when absent or malformed;
there is no second hard-coded version here.

``STARTUP_STATUS_HOOKS`` is empty until a reviewed service exists to add.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

HOOK_TIMEOUT_SECONDS = 10.0
MAX_STATUS_BYTES = 64 * 1024
_VERSION = re.compile(r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$")
_EVENT_TYPE = re.compile(r"^[a-z][a-z_]{2,39}$")
_MODULE = re.compile(r"^core\.[a-z][a-z0-9_]{0,63}$")
_FUNCTION = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
#: Event types a hook may never use: they mean something to the desktop already.
_RESERVED = frozenset({
    "runtime_ready", "runtime_stopping", "runtime_error", "runtime_warning", "runtime_log",
    "runtime_pong", "supervisor_state", "link_pairing",
})

Emit = Callable[[str, str, str], None]


@dataclass(frozen=True)
class StartupStatusHook:
    event_type: str
    module: str
    function: str

    def __post_init__(self) -> None:
        if not _EVENT_TYPE.fullmatch(self.event_type) or self.event_type in _RESERVED:
            raise ValueError(f"Invalid hook event type {self.event_type!r}.")
        if not _MODULE.fullmatch(self.module) or not _FUNCTION.fullmatch(self.function):
            raise ValueError("A hook names one function of one core module.")


#: Reviewed hooks only. Installer's update check is added here once
#: core.update_service exists in an accepted commit:
#:     StartupStatusHook("update_status", "core.update_service", "startup_check")
STARTUP_STATUS_HOOKS: tuple[StartupStatusHook, ...] = ()


def running_app_version(environ: Mapping[str, str] = os.environ) -> str:
    """The desktop's package version, or "" when it is absent or malformed."""
    value = environ.get("QRONOS_APP_VERSION", "")
    return value if _VERSION.fullmatch(value) else ""


def _status(hook: StartupStatusHook, context: Mapping[str, str], timeout: float) -> tuple[str, str]:
    outcome: dict[str, object] = {}

    def call() -> None:
        try:
            function = getattr(importlib.import_module(hook.module), hook.function)
            outcome["value"] = function(dict(context))
        except BaseException as error:  # noqa: BLE001 - reported by type only
            outcome["error"] = type(error).__name__

    worker = threading.Thread(target=call, name=f"qronos-hook-{hook.event_type}", daemon=True)
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        return "unavailable", json.dumps({"reason": "timeout"})
    if "error" in outcome:
        return "unavailable", json.dumps({"reason": str(outcome["error"])[:80]})

    value = outcome.get("value")
    if not isinstance(value, dict):
        return "unavailable", json.dumps({"reason": "not_an_object"})
    try:
        text = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "unavailable", json.dumps({"reason": "not_json"})
    if len(text) > MAX_STATUS_BYTES:
        return "unavailable", json.dumps({"reason": "too_large"})
    return "ready", text


def run_startup_status_hooks(
    emit: Emit,
    hooks: Sequence[StartupStatusHook] = STARTUP_STATUS_HOOKS,
    context: Mapping[str, str] | None = None,
    timeout: float = HOOK_TIMEOUT_SECONDS,
) -> threading.Thread | None:
    """Start the hooks on one daemon thread; returns it, or None when there are none."""
    hooks = tuple(hooks)
    if not hooks:
        return None
    context = dict(context if context is not None else {"appVersion": running_app_version()})

    def run_all() -> None:
        for hook in hooks:
            status, message = _status(hook, context, timeout)
            try:
                emit(hook.event_type, status, message)
            except Exception:
                pass

    thread = threading.Thread(target=run_all, name="qronos-startup-hooks", daemon=True)
    thread.start()
    return thread
