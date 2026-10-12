"""Owned synthetic pipe peers for Rust IPC tests; no Qronos services imported."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

mode, marker = sys.argv[1], Path(sys.argv[2])
marker.write_text(str(os.getpid()), encoding="ascii")


def start_descendant() -> None:
    descendant = marker.with_suffix(".descendant")
    subprocess.Popen(
        [sys.executable, __file__, "hold", str(descendant)],
        stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
    )
    deadline = time.monotonic() + 2
    while not descendant.exists() and time.monotonic() < deadline:
        time.sleep(0.01)


if mode == "hold":
    deadline = time.monotonic() + 10
    probed = False
    while time.monotonic() < deadline and not marker.with_suffix(".stop").exists():
        if not probed and marker.with_suffix(".probe").exists():
            closed = []
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.write("fixture-probe")
                    stream.flush()
                    closed.append(False)
                except OSError:
                    closed.append(True)
            try:
                sys.stdin.buffer.read()
                closed.append(True)
            except OSError:
                closed.append(True)
            observed = marker.with_suffix(".probe-result")
            temporary = observed.with_suffix(".tmp")
            temporary.write_text(json.dumps(closed))
            temporary.replace(observed)
            probed = True
        time.sleep(0.01)
    raise SystemExit(0)
if mode in {"hang_input", "inherited_blocked_input"}:
    if mode == "inherited_blocked_input":
        start_descendant()
    time.sleep(10)
    raise SystemExit(0)
if mode == "duplex":
    # Emit pipe-sized output BEFORE consuming pipe-sized input. Sequential
    # write-then-read clients deadlock; concurrent async drains must succeed.
    sys.stderr.write("x" * 100_000)
    sys.stderr.flush()
    sys.stdout.write('{"ok":true,"padding":"' + "x" * 100_000 + '",')
    sys.stdout.flush()
if mode == "slow_reader":
    time.sleep(0.15)
    parts = []
    while chunk := sys.stdin.buffer.read(4096):
        parts.append(chunk)
        time.sleep(0.002)
    raw = b"".join(parts)
else:
    raw = sys.stdin.buffer.read()
if mode == "duplex":
    sys.stdout.write('"inputBytes":' + str(len(raw)) + "}\n")
elif mode == "slow_reader":
    print(json.dumps({"ok": True, "inputBytes": len(raw), "dataMatches": json.loads(raw).get("data") == "x" * 200_000}))
elif mode in {"stdout_flood", "stderr_flood"}:
    stream = sys.stdout if mode == "stdout_flood" else sys.stderr
    while True:
        stream.write("PRIVATE_FIXTURE" * 8192)
        stream.flush()
elif mode == "hang_body":
    sys.stdout.write('{"ok":')
    sys.stdout.flush()
    time.sleep(10)
elif mode in {"inherited", "inherited_truncated"}:
    start_descendant()
    if mode == "inherited_truncated":
        sys.stdout.write('{"ok":true}')
        sys.stdout.flush()
    else:
        print(json.dumps({"ok": True}))
elif mode == "nonzero":
    print(json.dumps({"ok": True, "error": "PRIVATE_FIXTURE"}))
    sys.stderr.write("PRIVATE_FIXTURE")
    raise SystemExit(7)
elif mode == "malformed":
    print("PRIVATE_FIXTURE invalid JSON")
elif mode == "multi_frame":
    print(json.dumps({"ok": True}))
    sys.stdout.flush()
    time.sleep(0.03)
    print(json.dumps({"ok": True, "secret": "PRIVATE_FIXTURE"}))
elif mode == "truncated":
    sys.stdout.write('{"ok":true}')
elif mode == "wrong_shape":
    print(json.dumps({"ok": "PRIVATE_FIXTURE"}))
elif mode == "refused":
    print(json.dumps({"ok": False, "error": "PRIVATE_FIXTURE"}))
elif mode == "doctor":
    print(json.dumps({"schemaVersion": 1, "features": {"padding": "x" * 100_000}, "modelServer": {}}))
elif mode == "serialize":
    counter = Path(sys.argv[3])
    value = int(counter.read_text()) if counter.exists() else 0
    time.sleep(0.1)
    counter.write_text(str(value + 1))
    print(json.dumps({"ok": True}))
else:
    print(json.dumps({"ok": True, "inputBytes": len(raw)}))
