"""
Bounded startup status hooks for the runtime bridge.

A service owned by another part of Qronos may need to report a status once
the bridge is ready. It must never hold up or break chat or wake, whatever
state it is in.

So a hook here is a closed record — an event type and a function defined in
this module that imports its service by a literal, lazy import — never a
module name computed at run time. All hooks run on one daemon thread after
``runtime_ready``.
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

READ-ONLY ONLY. A hook runs after the runtime has opened its settings,
history and stores, and a timeout merely abandons a thread that is still
running: it cancels nothing. So a hook may only read and report. Work that
must finish before any store opens belongs in ``core/startup_admission.py``,
never in a status hook.

``STARTUP_STATUS_HOOKS`` is empty until a reviewed read-only service exists.
"""

from __future__ import annotations

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
#: Event types a hook may never use: they mean something to the desktop already.
_RESERVED = frozenset({
    "runtime_ready", "runtime_stopping", "runtime_error", "runtime_warning", "runtime_log",
    "runtime_pong", "supervisor_state",
})

Emit = Callable[[str, str, str], None]
StatusCheck = Callable[[dict], object]


@dataclass(frozen=True)
class StartupStatusHook:
    event_type: str
    check: StatusCheck

    def __post_init__(self) -> None:
        if not _EVENT_TYPE.fullmatch(self.event_type) or self.event_type in _RESERVED:
            raise ValueError(f"Invalid hook event type {self.event_type!r}.")
        if not callable(self.check):
            raise ValueError("A hook's check must be a function.")


#: Reviewed READ-ONLY hooks only. A status reader is added as a function in
#: this module with a literal, lazy import (so a missing or failing service
#: only makes its event "unavailable"):
#:
#:     def _example_status(context: dict) -> object:
#:         from core.example_service import read_status  # reads; changes nothing
#:         return read_status(context)
#:
#:     STARTUP_STATUS_HOOKS = (StartupStatusHook("example_status", _example_status),)
#:
#: A mutating startup step must never be registered here; see the module doc.
STARTUP_STATUS_HOOKS: tuple[StartupStatusHook, ...] = ()


def running_app_version(environ: Mapping[str, str] = os.environ) -> str:
    """The desktop's package version, or "" when it is absent or malformed."""
    value = environ.get("QRONOS_APP_VERSION", "")
    return value if _VERSION.fullmatch(value) else ""


def _status(hook: StartupStatusHook, context: Mapping[str, str], timeout: float) -> tuple[str, str]:
    outcome: dict[str, object] = {}

    def call() -> None:
        try:
            outcome["value"] = hook.check(dict(context))
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
