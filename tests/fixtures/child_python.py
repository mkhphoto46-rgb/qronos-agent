"""
How tests start a disposable Python child whose process id is the process
that actually runs.

On Windows a virtual environment's ``Scripts/python.exe`` is a launcher that
starts the real interpreter as *its* child, so the process id ``Popen``
returns is not the process that connects to a pipe — exactly what the
supervisor's and broker's peer checks are built to refuse. Tests therefore
start the base interpreter directly, with this environment's import path, the
way the frozen product's own executable runs without any launcher.
"""

from __future__ import annotations

import os
import sys


def child_python() -> tuple[str, dict]:
    executable = getattr(sys, "_base_executable", None) or sys.executable
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path and os.path.isdir(path))
    environment.pop("PYTHONHOME", None)
    return executable, environment
