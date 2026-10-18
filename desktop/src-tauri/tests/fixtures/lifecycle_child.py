"""Owned, inert process trees for the desktop runtime Job and stop tests; no Qronos module is imported.

Usage: lifecycle_child.py <mode> <private directory>. Every process writes its pid file and ends by itself:
on the "go" file in the directory, or within its own bound. Nothing here touches a real runtime, store,
installer, mutex or service.

Modes:
  wait-go            write child.pid, wait for "go".
  tree-immediate     FIRST start a grandchild (wait-go as grandchild.pid), then wait for "go".
  runtime-clean      a stand-in supervisor: its grandchild follows it (ends at its stdin EOF), and it ends on a
                     {"command": "shutdown"} line or at stdin EOF.
  runtime-lingering  the same, but its grandchild ignores it and waits for "go".
  runtime-deaf       never reads its input; waits for "go" (a stop must use the backstop).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

mode, directory = sys.argv[1], Path(sys.argv[2])
BOUND = 30.0


def write(name: str, text: str) -> None:
    temporary = directory / (name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, directory / name)


def wait_go(bound: float = BOUND) -> None:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline and not (directory / "go").exists():
        time.sleep(0.02)


def start(child_mode: str, **options) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-B", __file__, child_mode, str(directory)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)


def serve_until_shutdown() -> None:
    for line in sys.stdin:
        try:
            if json.loads(line).get("command") == "shutdown":
                return
        except ValueError:
            continue


if mode == "wait-go":
    write("child.pid", str(os.getpid()))
    wait_go()
elif mode == "grandchild":
    write("grandchild.pid", str(os.getpid()))
    wait_go()
elif mode == "follower":
    write("grandchild.pid", str(os.getpid()))
    sys.stdin.read()  # EOF when the parent (its only writer) ends
elif mode == "tree-immediate":
    start("grandchild", stdin=subprocess.DEVNULL)
    write("child.pid", str(os.getpid()))
    wait_go()
elif mode in ("runtime-clean", "runtime-lingering"):
    if mode == "runtime-clean":
        follower = start("follower", stdin=subprocess.PIPE)
    else:
        start("grandchild", stdin=subprocess.DEVNULL)
    write("child.pid", str(os.getpid()))
    serve_until_shutdown()
elif mode == "runtime-deaf":
    write("child.pid", str(os.getpid()))
    wait_go()
else:
    raise SystemExit(f"unknown mode {mode}")
