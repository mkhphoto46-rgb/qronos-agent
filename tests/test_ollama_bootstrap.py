from __future__ import annotations

from unittest.mock import Mock, patch

from core.model_manager import ModelManager
from core.ollama_controller import OllamaController


def test_ensure_ready_does_not_spawn_when_server_is_healthy():
    runtime = OllamaController()
    with patch.object(runtime, "_probe", return_value=True), patch(
        "core.ollama_controller.subprocess.Popen"
    ) as popen:
        assert runtime.ensure_ready() is True
        popen.assert_not_called()


def test_ensure_ready_starts_local_server_and_waits_for_health():
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


def test_ensure_ready_never_autostarts_nonlocal_runtime():
    runtime = OllamaController("http://192.0.2.10:11434")
    with patch.object(runtime, "_probe", return_value=False), patch.object(
        runtime, "_start_local_server"
    ) as start:
        assert runtime.ensure_ready(timeout_seconds=0.0) is False
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
