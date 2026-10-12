"""Owned, inert process trees for the sidecar whole-tree proof tests. No Qronos module is imported.

Usage: contained_tree_child.py <mode> <private directory>. Every process writes its own pid file and ends by itself
on the "go" file in that directory (or after its own bound). Nothing here opens a port, a store, a model or a
service. Modes:
  wait-go           write <role>.pid ("child" by default) and wait for "go".
  spawn-first       FIRST start a grandchild ("runner"), then write child.pid and wait for "go".
  exit-leaving-runner  start a "runner" that outlives this process, then exit at once (a dead server, a live runner).
  nested            start a "middle" that starts a "runner"; all wait for "go".
  try-breakaway     ask Windows for a child outside the Job; write breakaway.txt = "refused" or "escaped:<pid>".
  serve             stands in for "ollama serve": starts a "runner", then waits for "go" (or is ended).
"""

import os
import subprocess
import sys
import time
from pathlib import Path

mode, directory = sys.argv[1], Path(sys.argv[2])
role = sys.argv[3] if len(sys.argv) > 3 else "child"
BOUND = 30.0
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


def write(name: str, text: str) -> None:
    temporary = directory / (name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, directory / name)


def wait_go() -> None:
    deadline = time.monotonic() + BOUND
    while time.monotonic() < deadline and not (directory / "go").exists():
        time.sleep(0.02)


def start(child_mode: str, child_role: str, flags: int = 0) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-B", __file__, child_mode, str(directory), child_role],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags | CREATE_NO_WINDOW)  # no console host joins the tree


if mode == "wait-go":
    write(f"{role}.pid", str(os.getpid()))
    wait_go()
elif mode == "spawn-first":
    start("wait-go", "runner")
    write("child.pid", str(os.getpid()))
    wait_go()
elif mode == "exit-leaving-runner":
    start("wait-go", "runner")
    deadline = time.monotonic() + BOUND
    while not (directory / "runner.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    write("child.pid", str(os.getpid()))
elif mode == "nested":
    start("spawn-runner", "middle")
    write("child.pid", str(os.getpid()))
    wait_go()
elif mode == "spawn-runner":
    start("wait-go", "runner")
    write(f"{role}.pid", str(os.getpid()))
    wait_go()
elif mode == "try-breakaway":
    try:
        escaped = start("wait-go", "escaped", CREATE_BREAKAWAY_FROM_JOB)
        write("breakaway.txt", f"escaped:{escaped.pid}")
    except OSError:
        write("breakaway.txt", "refused")
    write("child.pid", str(os.getpid()))
    wait_go()
elif mode == "serve":
    start("wait-go", "runner")
    write("child.pid", str(os.getpid()))
    wait_go()
else:
    raise SystemExit(f"unknown mode {mode}")
