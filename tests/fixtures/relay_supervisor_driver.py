"""
Runs ``core.supervisor_relay.run_desktop`` as its own process, with the
disposable bridge fixture and a test-only Job Object and instance names, so a
test can kill the supervisor outright and check what happens to its bridge.

Arguments: the fixture mode, then a unique name suffix for the mutexes.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.supervisor import SupervisorPolicy  # noqa: E402
from core.supervisor_relay import exit_process, run_desktop  # noqa: E402
from core.windows_sidecar_guard import WindowsSidecarGuard  # noqa: E402
from tests.fixtures.child_python import child_python  # noqa: E402

CHILD = Path(__file__).with_name("relay_bridge_child.py")


class _NoGuard:
    def release(self) -> None:
        pass


def main() -> int:
    mode, suffix = sys.argv[1], sys.argv[2]
    executable, environment = child_python()
    policy = SupervisorPolicy(
        heartbeat_interval_seconds=0.2, heartbeat_timeout_seconds=3.0, connect_timeout_seconds=20.0,
        restart_backoff_initial_seconds=0.1, restart_backoff_max_seconds=0.2, max_restarts=2,
        restart_window_seconds=60.0, stable_after_seconds=60.0, shutdown_grace_seconds=2.0,
        terminate_grace_seconds=1.0, command_loop_warn_seconds=1.0,
    )
    return run_desktop(
        command=[executable, str(CHILD), mode],
        environment=environment,
        policy=policy,
        job_factory=lambda: WindowsSidecarGuard.acquire(f"Local\\QronosTestRelayJob-{suffix}"),
        instance_guard=_NoGuard,
    )


if __name__ == "__main__":
    exit_process(main())
