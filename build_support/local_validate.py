"""Scoped local software checks, so validation does not depend on hosted CI.

Runs only what is named: explicit Python test files or node IDs under ``tests/``
and, optionally, the desktop node unit tests. Nothing is installed or downloaded:
the Python and node executables are supplied by the caller. Without a selection,
or with ``--dry-run``, it only prints the plan.

Every child gets live devices off (``QRONOS_RUN_LIVE_DEVICE_TESTS=0``), Ollama
auto-start off, no desktop wake bootstrap, and its profile, asset, data, temp and
cache folders inside a fresh workspace whose name contains ``pytest``. Commands
are argument lists (never a shell) with a timeout. The report records the source
head, the commands and their actual exit codes.

    python -m build_support.local_validate --python <python.exe> \\
        --workspace <...>/pytest-local-1 tests/test_x.py tests/test_y.py::test_z
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
MAX_TIMEOUT = 3600
#: Environment the runner removes from every child.
DROPPED = ("QRONOS_DESKTOP_WAKE_BOOTSTRAP", "QRONOS_PACKAGED_RUNTIME")


class SelectionError(ValueError):
    """A requested selection or tool is not allowed."""


def check_selection(item: str, root: Path = ROOT) -> str:
    """A test file or node ID under tests/: never a directory, option or path outside it."""
    text = item.replace("\\", "/")
    path, _, node = text.partition("::")
    parts = Path(path).parts
    if text.startswith("-") or Path(path).is_absolute() or ".." in parts or len(parts) < 2 or parts[0] != "tests":
        raise SelectionError(f"not a test path under tests/: {item!r}")
    if not (Path(path).name.startswith("test_") and path.endswith(".py")) or not (root / path).is_file():
        raise SelectionError(f"not an existing tests/**/test_*.py file: {item!r}")
    if "::" in text and not node:
        raise SelectionError(f"empty node ID: {item!r}")
    return text


def check_workspace(workspace: Path) -> Path:
    workspace = workspace.resolve()
    if "pytest" not in workspace.name.lower():
        raise SelectionError("the workspace folder name must contain 'pytest'")
    if workspace == ROOT or ROOT in workspace.parents:
        raise SelectionError("the workspace must be outside the repository")
    if workspace.exists():
        raise SelectionError("the workspace must not exist yet (each run starts fresh)")
    return workspace


def check_tool(value: str | None, name: str) -> Path:
    if not value:
        raise SelectionError(f"--{name} is required to execute (nothing is installed or looked up)")
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise SelectionError(f"--{name} must be an absolute path to an existing executable")
    return path


def child_environment(workspace: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    for name in DROPPED:
        environment.pop(name, None)
    folders = {
        "LOCALAPPDATA": "local", "APPDATA": "roaming", "USERPROFILE": "user", "HOME": "user",
        "TEMP": "temp", "TMP": "temp", "QRONOS_DATA_ROOT": "data", "QRONOS_ASSET_ROOT": "assets",
        "npm_config_cache": "npm-cache", "XDG_CACHE_HOME": "cache",
    }
    environment.update({name: str(workspace / folder) for name, folder in folders.items()})
    environment.update({"QRONOS_RUN_LIVE_DEVICE_TESTS": "0", "QRONOS_OLLAMA_AUTO_START": "0",
                        "PYTHONDONTWRITEBYTECODE": "1"})
    return environment


def plan(selections: Sequence[str], frontend: bool, python: str | None, node: str | None,
         workspace: Path) -> list[dict]:
    steps = []
    if selections:
        steps.append({"name": "python-tests", "cwd": str(ROOT), "argv": [
            python or "<--python>", "-m", "pytest", *selections, "-q", "-p", "no:cacheprovider",
            f"--basetemp={workspace / 'basetemp'}"]})
    if frontend:
        tests = sorted(p.relative_to(DESKTOP).as_posix() for p in (DESKTOP / "tests").glob("*.test.mjs"))
        steps.append({"name": "desktop-node-tests", "cwd": str(DESKTOP),
                      "argv": [node or "<--node>", "--test", *tests]})
    return steps


def source_state() -> dict:
    def git(*args: str) -> str:
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=30)
        return result.stdout.strip() if result.returncode == 0 else ""
    return {"head": git("rev-parse", "HEAD"), "contentChanged": git("diff", "--name-only", "HEAD").splitlines()}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m build_support.local_validate", description=__doc__.split("\n\n")[0])
    parser.add_argument("selections", nargs="*", help="test files or node IDs under tests/ (no directories)")
    parser.add_argument("--frontend", action="store_true", help="also run the desktop node unit tests")
    parser.add_argument("--python", help="absolute path of the Python that runs pytest")
    parser.add_argument("--node", help="absolute path of node.exe (with --frontend)")
    parser.add_argument("--workspace", type=Path, help="fresh folder outside the repo; its name must contain 'pytest'")
    parser.add_argument("--timeout", type=int, default=900, help=f"seconds per step (max {MAX_TIMEOUT})")
    parser.add_argument("--report", type=Path, help="also write the JSON report here")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    args = parser.parse_args(argv)

    try:
        selections = [check_selection(item) for item in args.selections]
        if not 1 <= args.timeout <= MAX_TIMEOUT:
            raise SelectionError(f"--timeout must be 1..{MAX_TIMEOUT}")
        execute = not args.dry_run and bool(selections or args.frontend)
        workspace = (check_workspace(args.workspace) if execute or args.workspace
                     else Path("<--workspace>/pytest-..."))
        if execute:
            python = str(check_tool(args.python, "python")) if selections else None
            node = str(check_tool(args.node, "node")) if args.frontend else None
            if args.frontend and not (DESKTOP / "node_modules").is_dir():
                raise SelectionError("desktop/node_modules is missing; nothing is installed here")
        else:
            python, node = args.python, args.node
    except SelectionError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2

    steps = plan(selections, args.frontend, python, node, workspace)
    report = {"source": source_state(), "workspace": str(workspace), "executed": execute,
              "environment": {"QRONOS_RUN_LIVE_DEVICE_TESTS": "0", "QRONOS_OLLAMA_AUTO_START": "0",
                              "removed": list(DROPPED), "redirectedInto": "workspace"},
              "steps": steps}
    if not steps:
        report["note"] = "no selection: nothing to run (there is no full-suite default)"
    exit_code = 0
    if execute:
        workspace.mkdir(parents=True)
        environment = child_environment(workspace)
        for step in steps:
            started = time.monotonic()
            try:
                step["exit"] = subprocess.run(step["argv"], cwd=step["cwd"], env=environment,
                                              timeout=args.timeout).returncode
            except subprocess.TimeoutExpired:
                step["exit"], step["timedOut"] = 124, True
            step["seconds"] = round(time.monotonic() - started, 2)
            exit_code = exit_code or step["exit"]
        report["exit"] = exit_code
    text = json.dumps(report, indent=1)
    print(text)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
