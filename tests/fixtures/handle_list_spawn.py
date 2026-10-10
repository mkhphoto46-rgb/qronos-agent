"""A test-only helper: start a detached child that inherits exactly the given handles. Test fixture only.

Moved here unchanged from core/update_service.py (Media21 A21-1): no product code calls it. The product starts
an update installer only through update_service.fenced_launcher, which hands over the fence by DuplicateHandle.
tests/test_update_service.py uses this to prove that an inherited copy of the update mutex keeps the exclusion
alive after the launching process lets go.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from core.local_update import UpdateError


def spawn_inheriting(argv: list[str], handles: tuple[int, ...], cwd: str | None = None) -> Any:
    """Start a detached process that inherits exactly ``handles`` (plus null standard handles).

    The handles are made inheritable only for this call and restricted with the
    process-thread attribute handle list, so nothing else of this process leaks.
    """
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    startup = None
    if handles:
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": list(handles)}
        for handle in handles:
            os.set_handle_inheritable(handle, True)
    try:
        return subprocess.Popen(argv, cwd=cwd, close_fds=True, creationflags=flags, startupinfo=startup,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        raise UpdateError("launch_failed", "Windows could not start the update installer.") from error
    finally:
        for handle in handles:
            try:
                os.set_handle_inheritable(handle, False)
            except OSError:
                pass
