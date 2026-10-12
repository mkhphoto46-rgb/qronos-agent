"""
A start-up probe child (base interpreter, -B; tests/test_startup_admission_resolution.py). Test fixture only.

Runs ONE entry exactly as the product does, then reports:
- ``frozen SERVICE [ARGS]``: ``build_support.runtime_entry.main`` (the frozen dispatch);
- ``source MODULE [ARGS]``: ``runpy.run_module(MODULE, run_name="__main__")``, i.e. ``python -m MODULE``.

Before anything of Qronos runs, and only in this process:
- the known-folder Local AppData of core.startup_admission is a PRIVATE folder (KNOWN);
- the update fence is a PRIVATE mutex name (FENCE) for core.store_admission AND core.update_service, so no
  production mutex name is ever opened;
- in source mode, core.config.CONFIG is a private checkout under CHECKOUT (so the module's own default data stays
  in tmp; store overrides come from the environment as in a real run);
- an audit hook records every file open under WATCH, whether the start-up admission was held at that moment, and
  which service modules were imported.
Writes OUT (JSON). Touches only tmp paths. Usage:
    startup_entry_child.py TARGET WATCH FENCE KNOWN CHECKOUT OUT MODE NAME [ARGS...]
"""
import json
import os
import runpy
import sys
from pathlib import Path

target, watch, fence, known, checkout, out, mode, name = sys.argv[1:9]
arguments = sys.argv[9:]
sys.path.insert(0, target)
watch_root = os.path.normcase(os.path.abspath(watch))  # the spelling the opens use (an alias stays an alias)
events = []
SERVICE_MODULES = ("core.settings_cli", "core.update_cli", "core.library_cli", "core.memory_cli",
                   "core.permissions_cli", "core.runtime_bridge")


def hook(event, args):
    if event != "open" or not args or isinstance(args[0], int) or args[0] is None:
        return
    try:
        path = os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
    except Exception:
        return
    if path == watch_root or path.startswith(watch_root + os.sep):
        admission = sys.modules.get("core.startup_admission")
        events.append({"path": path[len(watch_root):].lstrip(os.sep),
                       "admitted": bool(admission and "admission" in admission._held),  # no lock: enter holds it
                       "serviceImported": [m for m in SERVICE_MODULES if m in sys.modules]})


from core import startup_admission, store_admission  # noqa: E402
from core import update_service  # noqa: E402

startup_admission._known_local_appdata = lambda: Path(known)
store_admission.UPDATE_IN_PROGRESS_MUTEX = fence
store_admission.update_in_progress.__defaults__ = (fence,)
store_admission.admit_writer.__defaults__ = (fence,)
update_service.update_in_progress.__defaults__ = (fence,)
if mode == "source":
    from core import config

    config.CONFIG = config.QronosConfig(paths=config.resolve_paths(root=Path(checkout), packaged=False, environ={}))
sys.addaudithook(hook)
code = None
try:
    if mode == "frozen":
        from build_support import runtime_entry

        code = runtime_entry.main([name, *arguments])
    else:
        sys.argv = [name, *arguments]
        runpy.run_module(name, run_name="__main__", alter_sys=True)
except SystemExit as stop:
    code = stop.code
result = {"code": code, "events": events, "imported": [m for m in SERVICE_MODULES if m in sys.modules],
          "admittedAtEnd": bool(startup_admission._held.get("admission") is not None),
          "binding": startup_admission._held.get("binding")}
with open(out, "w", encoding="utf-8") as stream:
    json.dump(result, stream, default=str)
