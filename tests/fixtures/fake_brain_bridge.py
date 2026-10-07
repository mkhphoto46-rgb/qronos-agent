"""
The real bridge process, with a scripted brain and a machine that is free.

Started by ``tests/test_queue_restart_process.py`` as a child process, so that
restarts and crashes are real: a real ``main()``, real stdin/stdout, real files,
and a real kill. Only two things are replaced, both before ``main()`` runs and
neither through any switch in production code:

    The brain. ``Orchestrator`` becomes a script that answers instantly, or
    hangs until the process is killed — the only honest way to be "mid-run"
    when the crash comes. No model is loaded and no model server is contacted.

    The load monitor. It reports a free machine, so whether work starts does
    not depend on how busy the computer running the suite happens to be.

Behaviour is chosen by environment variables, set only by the test:

    QRONOS_TEST_BRAIN_MODE     "answer" (default) or "hang"
    QRONOS_TEST_BRAIN_MARKER   a file created when the brain is first asked
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import core.runtime_bridge as runtime_bridge
from core.load_signal import LoadLevel, LoadSample, LoadSnapshot
from core.orchestrator import StepResult
from core.resource_policy import ResourceDecision


class _NoWorkers:
    def close(self) -> None:
        return None


class ScriptedBrain:
    """Stands in for the Orchestrator. Records nothing; answers or hangs."""

    def __init__(self, *args, **kwargs) -> None:
        self.workers = _NoWorkers()

    def execute_plan(self, plan, conversation_messages=None, priority=None):
        marker = os.environ.get("QRONOS_TEST_BRAIN_MARKER", "").strip()

        if marker:
            Path(marker).write_text(
                f"{priority.value if priority is not None else 'none'}\n",
                encoding="utf-8",
            )

        if os.environ.get("QRONOS_TEST_BRAIN_MODE", "answer") == "hang":
            while True:
                time.sleep(0.5)

        step = plan.steps[0]

        return [
            StepResult(
                order=1,
                success=True,
                output=f"Scripted answer to: {step.description}",
            )
        ]

    def release_brains_for_voice_phase(self) -> bool:
        return True


class FreeMachine:
    """A load monitor whose verdict is always "free"."""

    def __init__(self) -> None:
        self.clock = time.time

    def _sample(self) -> LoadSample:
        return LoadSample(
            at=time.time(),
            decision=ResourceDecision.ALLOW,
            cpu_percent=5.0,
            ram_percent=30.0,
            vram_free_mb=15_000,
            gpu_temperature_c=40,
        )

    def prime(self) -> None:
        return None

    def observe(self) -> LoadSample:
        return self._sample()

    def snapshot(self) -> LoadSnapshot:
        return LoadSnapshot(
            level=LoadLevel.FREE,
            since=0.0,
            latest=self._sample(),
            sample_count=30,
            loaded_fraction=0.0,
        )


runtime_bridge.Orchestrator = ScriptedBrain  # type: ignore[misc]
runtime_bridge.build_sustained_load_monitor = FreeMachine  # type: ignore


if __name__ == "__main__":
    raise SystemExit(runtime_bridge.main())
