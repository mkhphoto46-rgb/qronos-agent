from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import BinaryIO

from core.config import CONFIG
from core.local_ipc import (
    local_host, local_port, local_timeout, process_owns_listener, request_bytes,
)
from core.speech_runtime import SpeechRuntime
from core.windows_sidecar_guard import (
    SidecarAlreadyRunning,
    WindowsSidecarGuard,
)


DEFAULT_WHISPER_SERVER_EXE = (
    CONFIG.paths.runtime_assets
    / "whisper"
    / "bin"
    / "Release"
    / "whisper-server.exe"
)

DEFAULT_WHISPER_MODEL = (
    CONFIG.paths.runtime_assets
    / "whisper"
    / "models"
    / "ggml-large-v3-turbo-q5_0.bin"
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8178
DEFAULT_STARTUP_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_BEST_OF = 5
DEFAULT_BEAM_SIZE = 5
WHISPER_SIDECAR_MUTEX = r"Local\Qronos.Whisper.Sidecar.v1"


class WhisperServerRuntime(SpeechRuntime):
    """
    Persistent local whisper.cpp speech-to-text runtime.

    The whisper.cpp model is loaded once into whisper-server.exe and reused
    across transcription requests. The server is bound to localhost only.

    Lifecycle is explicit:
    - prepare() starts the server if needed
    - transcribe_file() lazily prepares it
    - shutdown() releases the process and GPU resources

    Higher-level Qronos lifecycle policy decides when prepare/shutdown occur.
    """

    def __init__(
        self,
        executable_path: str | Path = DEFAULT_WHISPER_SERVER_EXE,
        model_path: str | Path = DEFAULT_WHISPER_MODEL,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        startup_timeout_seconds: float = (
            DEFAULT_STARTUP_TIMEOUT_SECONDS
        ),
        request_timeout_seconds: float = (
            DEFAULT_REQUEST_TIMEOUT_SECONDS
        ),
        best_of: int = DEFAULT_BEST_OF,
        beam_size: int = DEFAULT_BEAM_SIZE,
        log_dir: str | Path = CONFIG.paths.temp,
        sidecar_guard_factory=None,
    ) -> None:
        host = local_host(host)
        port = local_port(port)
        startup_timeout_seconds = local_timeout(startup_timeout_seconds)
        request_timeout_seconds = local_timeout(request_timeout_seconds)

        if best_of <= 0:
            raise ValueError(
                "best_of must be greater than zero."
            )

        if beam_size <= 0:
            raise ValueError(
                "beam_size must be greater than zero."
            )

        self.executable_path = Path(
            executable_path
        )

        self.model_path = Path(
            model_path
        )

        self.host = host
        self.port = port

        self.startup_timeout_seconds = (
            float(startup_timeout_seconds)
        )

        self.request_timeout_seconds = (
            float(request_timeout_seconds)
        )

        self.best_of = int(
            best_of
        )

        self.beam_size = int(
            beam_size
        )

        self.log_dir = Path(
            log_dir
        )

        self._sidecar_guard_factory = (
            sidecar_guard_factory
            if sidecar_guard_factory is not None
            else lambda: WindowsSidecarGuard.acquire(
                WHISPER_SIDECAR_MUTEX
            )
        )

        self._process: subprocess.Popen[bytes] | None = None
        self._sidecar_guard: WindowsSidecarGuard | None = None
        self._stdout_file: BinaryIO | None = None
        self._stderr_file: BinaryIO | None = None

        self._lifecycle_lock = (
            threading.RLock()
        )

        self._request_lock = (
            threading.Lock()
        )

    @property
    def inference_url(self) -> str:
        host = local_host(self.host)
        port = local_port(self.port)
        return f"http://{host}:{port}/inference"

    @property
    def is_running(self) -> bool:
        process = self._process

        return (
            process is not None
            and process.poll() is None
        )

    def health_check(self) -> bool:
        return (
            self.executable_path.is_file()
            and self.model_path.is_file()
        )

    def _server_is_ready(self) -> bool:
        if not self.is_running:
            return False

        if not process_owns_listener(self._process, self.port):
            return False
        try:
            with socket.create_connection(
                (
                    local_host(self.host),
                    local_port(self.port),
                ),
                timeout=0.25,
            ):
                return True

        except OSError:
            return False

    def _build_command(self) -> list[str]:
        return [
            str(self.executable_path),
            "-m",
            str(self.model_path),
            "-l",
            "fa",
            "-nt",
            "-fa",
            "-bo",
            str(self.best_of),
            "-bs",
            str(self.beam_size),
            "--host",
            local_host(self.host),
            "--port",
            str(local_port(self.port)),
        ]

    def prepare(self) -> None:
        with self._lifecycle_lock:
            if self._server_is_ready():
                return

            if not self.executable_path.is_file():
                raise FileNotFoundError(
                    "whisper-server.exe was not found: "
                    f"{self.executable_path}"
                )

            if not self.model_path.is_file():
                raise FileNotFoundError(
                    "Whisper model was not found: "
                    f"{self.model_path}"
                )

            self.shutdown()

            self.log_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            stdout_path = (
                self.log_dir
                / "qronos_whisper_server_stdout.log"
            )

            stderr_path = (
                self.log_dir
                / "qronos_whisper_server_stderr.log"
            )

            self._stdout_file = stdout_path.open(
                "wb"
            )

            self._stderr_file = stderr_path.open(
                "wb"
            )

            guard: WindowsSidecarGuard | None = None
            process = None

            try:
                guard = self._sidecar_guard_factory()
                process = subprocess.Popen(
                    self._build_command(),
                    stdout=self._stdout_file,
                    stderr=self._stderr_file,
                    stdin=subprocess.DEVNULL,
                )

                pid = int(
                    getattr(process, "pid", 0)
                    or 0
                )
                if pid <= 0:
                    raise RuntimeError(
                        "whisper-server.exe did not return a valid PID."
                    )

                guard.bind_process(pid, process_handle=getattr(process, "_handle", None))
                self._process = process
                self._sidecar_guard = guard

            except SidecarAlreadyRunning as exc:
                if guard is not None:
                    guard.close()
                self._close_logs()
                raise RuntimeError(
                    "Another Qronos process already owns the "
                    "persistent Whisper sidecar."
                ) from exc

            except OSError as exc:
                if guard is not None:
                    guard.close()
                self._close_logs()

                raise RuntimeError(
                    "Could not start whisper-server.exe."
                ) from exc

            except Exception:
                if process is not None and process.poll() is None:
                    try:
                        process.kill()
                        process.wait(timeout=3.0)
                    except Exception:
                        pass

                if guard is not None:
                    guard.close()

                self._process = None
                self._sidecar_guard = None
                self._close_logs()
                raise

            deadline = (
                time.monotonic()
                + self.startup_timeout_seconds
            )

            while time.monotonic() < deadline:
                process = self._process

                if (
                    process is None
                    or process.poll() is not None
                ):
                    exit_code = (
                        None
                        if process is None
                        else process.poll()
                    )

                    self.shutdown()

                    raise RuntimeError(
                        "Whisper server exited during startup "
                        f"with code {exit_code}."
                    )

                if self._server_is_ready():
                    return

                time.sleep(
                    0.05
                )

            self.shutdown()

            raise RuntimeError(
                "Whisper server did not become ready "
                f"within {self.startup_timeout_seconds:.1f}s."
            )

    @staticmethod
    def _build_multipart_body(
        audio_path: Path,
        language: str,
    ) -> tuple[bytes, str]:
        boundary = (
            "----QronosWhisperBoundary"
            + uuid.uuid4().hex
        )

        chunks: list[bytes] = []

        chunks.append(
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; '
                'name="file"; '
                f'filename="{audio_path.name}"\r\n'
                "Content-Type: audio/wav\r\n"
                "\r\n"
            ).encode(
                "utf-8"
            )
        )

        chunks.append(
            audio_path.read_bytes()
        )

        chunks.append(
            b"\r\n"
        )

        for name, value in (
            (
                "response_format",
                "json",
            ),
            (
                "language",
                language,
            ),
        ):
            chunks.append(
                (
                    f"--{boundary}\r\n"
                    "Content-Disposition: form-data; "
                    f'name="{name}"\r\n'
                    "\r\n"
                    f"{value}\r\n"
                ).encode(
                    "utf-8"
                )
            )

        chunks.append(
            (
                f"--{boundary}--\r\n"
            ).encode(
                "utf-8"
            )
        )

        return (
            b"".join(chunks),
            boundary,
        )

    def transcribe_file(
        self,
        audio_path: str | Path,
        language: str = "auto",
    ) -> str:
        source = Path(
            audio_path
        )

        if not source.is_file():
            raise FileNotFoundError(
                f"Audio file was not found: {source}"
            )

        language = (
            language or ""
        ).strip()

        if not language:
            raise ValueError(
                "language must not be empty."
            )

        self.prepare()

        body, boundary = (
            self._build_multipart_body(
                source,
                language,
            )
        )

        request = urllib.request.Request(
            self.inference_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": (
                    "multipart/form-data; "
                    f"boundary={boundary}"
                ),
            },
        )

        with self._request_lock:
            with self._lifecycle_lock:
                process = self._process
            if not process_owns_listener(process, self.port):
                raise RuntimeError("Whisper sidecar no longer owns its local endpoint.")
            try:
                raw = request_bytes(
                    request, timeout=self.request_timeout_seconds, owner=process,
                )
            except Exception:
                raise RuntimeError("Whisper server transcription request failed.") from None

        try:
            payload = json.loads(
                raw.decode(
                    "utf-8"
                )
            )

        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise RuntimeError(
                "Whisper server returned invalid UTF-8 JSON."
            ) from None

        if not isinstance(payload, dict):
            raise RuntimeError("Whisper server response must be a JSON object.")
        transcript = payload.get(
            "text"
        )

        if not isinstance(
            transcript,
            str,
        ):
            raise RuntimeError(
                "Whisper server response did not contain "
                "a text transcript."
            )

        return transcript.strip()

    def _close_logs(self) -> None:
        stdout_file = (
            self._stdout_file
        )

        stderr_file = (
            self._stderr_file
        )

        self._stdout_file = None
        self._stderr_file = None

        for log_file in (
            stdout_file,
            stderr_file,
        ):
            if log_file is None:
                continue

            try:
                log_file.close()

            except OSError:
                pass

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            process = self._process
            guard = self._sidecar_guard

            self._process = None
            self._sidecar_guard = None

            if process is not None:
                if process.poll() is None:
                    try:
                        process.terminate()
                        process.wait(
                            timeout=3.0
                        )

                    except subprocess.TimeoutExpired:
                        process.kill()

                        try:
                            process.wait(
                                timeout=3.0
                            )

                        except subprocess.TimeoutExpired:
                            pass

                    except OSError:
                        pass

            if guard is not None:
                guard.close()

            self._close_logs()

    def __enter__(
        self,
    ) -> "WhisperServerRuntime":
        self.prepare()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.shutdown()

    def __del__(
        self,
    ) -> None:
        try:
            self.shutdown()

        except Exception:
            pass
