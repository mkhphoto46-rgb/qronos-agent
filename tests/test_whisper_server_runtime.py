from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.whisper_server_runtime import (
    WhisperServerRuntime,
)


class FakeProcess:
    def __init__(
        self,
    ) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(
        self,
    ):
        return self.returncode

    def terminate(
        self,
    ) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(
        self,
    ) -> None:
        self.killed = True
        self.returncode = -9

    def wait(
        self,
        timeout=None,
    ):
        return self.returncode


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, object],
    ) -> None:
        self._raw = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode(
            "utf-8"
        )

    def __enter__(
        self,
    ):
        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ):
        return None

    def read(
        self,
    ) -> bytes:
        return self._raw


def build_runtime(
    tmp_path: Path,
) -> WhisperServerRuntime:
    executable = (
        tmp_path
        / "whisper-server.exe"
    )

    model = (
        tmp_path
        / "model.bin"
    )

    executable.write_bytes(
        b"server"
    )

    model.write_bytes(
        b"model"
    )

    return WhisperServerRuntime(
        executable_path=executable,
        model_path=model,
        log_dir=tmp_path,
        startup_timeout_seconds=0.5,
        request_timeout_seconds=0.5,
    )


def test_health_check_requires_server_and_model(
    tmp_path: Path,
) -> None:
    runtime = WhisperServerRuntime(
        executable_path=(
            tmp_path
            / "missing.exe"
        ),
        model_path=(
            tmp_path
            / "missing.bin"
        ),
        log_dir=tmp_path,
    )

    assert runtime.health_check() is False


def test_rejects_non_localhost_binding(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="127.0.0.1",
    ):
        WhisperServerRuntime(
            executable_path=(
                tmp_path
                / "server.exe"
            ),
            model_path=(
                tmp_path
                / "model.bin"
            ),
            host="0.0.0.0",
        )


def test_command_preserves_decoder_parity(
    tmp_path: Path,
) -> None:
    runtime = build_runtime(
        tmp_path
    )

    command = (
        runtime._build_command()
    )

    assert "-bo" in command
    assert command[
        command.index("-bo") + 1
    ] == "5"

    assert "-bs" in command
    assert command[
        command.index("-bs") + 1
    ] == "5"

    assert "--host" in command
    assert command[
        command.index("--host") + 1
    ] == "127.0.0.1"


def test_prepare_starts_server_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_runtime(
        tmp_path
    )

    fake_process = FakeProcess()

    popen = MagicMock(
        return_value=fake_process
    )

    monkeypatch.setattr(
        "core.whisper_server_runtime.subprocess.Popen",
        popen,
    )

    readiness = iter(
        (
            False,
            True,
        )
    )

    monkeypatch.setattr(
        runtime,
        "_server_is_ready",
        lambda: next(
            readiness,
            True,
        ),
    )

    runtime.prepare()

    assert popen.call_count == 1

    runtime.prepare()

    assert popen.call_count == 1

    runtime.shutdown()


def test_transcribe_file_decodes_utf8_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_runtime(
        tmp_path
    )

    audio = (
        tmp_path
        / "voice.wav"
    )

    audio.write_bytes(
        b"RIFF-test"
    )

    monkeypatch.setattr(
        runtime,
        "prepare",
        lambda: None,
    )

    monkeypatch.setattr(
        "core.whisper_server_runtime.urllib.request.urlopen",
        lambda *args, **kwargs: FakeResponse(
            {
                "text": (
                    "دو به علاوه دو چند می شه؟"
                )
            }
        ),
    )

    transcript = (
        runtime.transcribe_file(
            audio,
            language="fa",
        )
    )

    assert transcript == (
        "دو به علاوه دو چند می شه؟"
    )


def test_transcribe_file_rejects_missing_audio(
    tmp_path: Path,
) -> None:
    runtime = build_runtime(
        tmp_path
    )

    with pytest.raises(
        FileNotFoundError,
    ):
        runtime.transcribe_file(
            tmp_path
            / "missing.wav"
        )


def test_shutdown_terminates_running_process(
    tmp_path: Path,
) -> None:
    runtime = build_runtime(
        tmp_path
    )

    fake_process = FakeProcess()

    runtime._process = fake_process

    runtime._stdout_file = io.BytesIO()
    runtime._stderr_file = io.BytesIO()

    runtime.shutdown()

    assert (
        fake_process.terminated
        is True
    )

    assert runtime.is_running is False
