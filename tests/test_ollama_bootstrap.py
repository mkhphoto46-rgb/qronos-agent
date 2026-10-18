from __future__ import annotations

from unittest.mock import Mock, patch
import pytest

from core.model_manager import ModelManager
from core.ollama_controller import OLLAMA_AUTO_START_ENV, OllamaController


def test_ensure_ready_does_not_spawn_when_server_is_healthy():
    runtime = OllamaController()
    with patch.object(runtime, "_probe", return_value=True), patch(
        "core.ollama_controller.subprocess.Popen"
    ) as popen:
        assert runtime.ensure_ready() is True
        popen.assert_not_called()


def test_ensure_ready_starts_local_server_and_waits_for_health(monkeypatch):
    # The start below is faked; auto-start is set here so the test does not depend on the parent environment.
    monkeypatch.setenv(OLLAMA_AUTO_START_ENV, "1")
    runtime = OllamaController()
    probes = iter((False, False, False, True))
    with patch.object(runtime, "_probe", side_effect=lambda *_: next(probes)), patch.object(
        runtime, "_resolve_local_executable", return_value=r"C:\Ollama\ollama.exe"
    ), patch.object(runtime, "_start_local_server") as start, patch(
        "core.ollama_controller.time.sleep", return_value=None
    ) as sleep:
        assert runtime.ensure_ready(timeout_seconds=2.0) is True
        start.assert_called_once_with(r"C:\Ollama\ollama.exe")
        sleep.assert_called()


def test_nonlocal_runtime_is_rejected_before_probe_or_autostart():
    with patch("core.ollama_controller.requests.get") as probe, patch.object(
        OllamaController, "_start_local_server",
    ) as start:
        with pytest.raises(ValueError, match="loopback"):
            OllamaController("http://192.0.2.10:11434")
        probe.assert_not_called()
        start.assert_not_called()


def test_model_manager_uses_runtime_bootstrap_when_available():
    runtime = Mock()
    runtime.ensure_ready.return_value = True
    manager = ModelManager(runtime=runtime)

    assert manager.ensure_runtime_ready(timeout_seconds=7.0) is True
    runtime.ensure_ready.assert_called_once_with(
        auto_start=True,
        timeout_seconds=7.0,
    )
