from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.chatterbox_runtime import (
    ChatterboxRuntime,
    VoiceUnavailable,
)
from core.windows_sidecar_guard import SidecarAlreadyRunning


class FakeProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FakeGuard:
    def __init__(self, bind_error: Exception | None = None) -> None:
        self.bind_error = bind_error
        self.bound_pids: list[int] = []
        self.closed = False

    def bind_process(self, pid: int, *, process_handle=None) -> None:
        if self.bind_error is not None:
            raise self.bind_error
        self.bound_pids.append(pid)

    def close(self) -> None:
        self.closed = True


def build_runtime(
    tmp_path: Path,
    *,
    guard_factory,
) -> ChatterboxRuntime:
    executable = tmp_path / "crispasr.exe"
    voice_model = tmp_path / "voice.gguf"
    codec_model = tmp_path / "codec.gguf"

    executable.write_bytes(b"exe")
    voice_model.write_bytes(b"voice")
    codec_model.write_bytes(b"codec")

    runtime = ChatterboxRuntime(
        executable_path=executable,
        voice_model_path=voice_model,
        codec_model_path=codec_model,
        temp_dir=tmp_path / "temp",
        cache_dir=tmp_path / "cache",
        idle_seconds=600.0,
        startup_timeout=0.5,
        request_timeout=0.5,
        admission_check=lambda budget: None,
        sidecar_guard_factory=guard_factory,
    )

    # These tests target process ownership rather than the resource ledger.
    runtime._create_reservation_locked = lambda: None
    runtime._update_reservation_state_locked = lambda state: None
    runtime._fail_reservation_locked = lambda: None
    runtime._release_reservation_locked = lambda: None
    runtime._arm_idle_timer_locked = lambda: None
    runtime._cancel_idle_timer_locked = lambda: None
    runtime._wait_until_speakable = lambda process: None

    return runtime


class TestChatterboxSidecarOwnership(unittest.TestCase):
    def test_prewarm_binds_started_process_to_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            guard = FakeGuard()
            process = FakeProcess(pid=5151)

            runtime = build_runtime(
                tmp_path,
                guard_factory=lambda: guard,
            )
            runtime._launch_process = MagicMock(
                return_value=process
            )

            runtime.prewarm()

            self.assertEqual(
                guard.bound_pids,
                [5151],
            )
            self.assertFalse(guard.closed)
            self.assertEqual(
                runtime.process_id,
                5151,
            )

            runtime.release()

            self.assertTrue(process.terminated)
            self.assertTrue(guard.closed)

    def test_duplicate_owner_fails_before_process_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)

            def duplicate_guard():
                raise SidecarAlreadyRunning(
                    "already owned"
                )

            runtime = build_runtime(
                tmp_path,
                guard_factory=duplicate_guard,
            )
            launcher = MagicMock(
                return_value=FakeProcess()
            )
            runtime._launch_process = launcher

            with self.assertRaisesRegex(
                VoiceUnavailable,
                "already owns",
            ):
                runtime.prewarm()

            launcher.assert_not_called()

    def test_bind_failure_kills_process_and_releases_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            guard = FakeGuard(
                bind_error=RuntimeError("bind failed")
            )
            process = FakeProcess(pid=6161)

            runtime = build_runtime(
                tmp_path,
                guard_factory=lambda: guard,
            )
            runtime._launch_process = MagicMock(
                return_value=process
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "bind failed",
            ):
                runtime.prewarm()

            self.assertTrue(process.killed)
            self.assertTrue(guard.closed)

    def test_release_is_idempotent_after_guard_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            guard = FakeGuard()
            process = FakeProcess(pid=7171)

            runtime = build_runtime(
                tmp_path,
                guard_factory=lambda: guard,
            )
            runtime._launch_process = MagicMock(
                return_value=process
            )

            runtime.prewarm()
            runtime.release()
            runtime.release()

            self.assertTrue(guard.closed)
            self.assertIsNone(runtime.process_id)


if __name__ == "__main__":
    unittest.main()
