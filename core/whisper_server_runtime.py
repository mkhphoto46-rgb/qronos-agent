from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import BinaryIO

from core.config import CONFIG
from core.speech_runtime import SpeechRuntime


DEFAULT_WHISPER_SERVER_EXE = (
    CONFIG.paths.root
    / "runtime"
    / "whisper"
    / "bin"
    / "Release"
    / "whisper-server.exe"
)

DEFAULT_WHISPER_MODEL = (
    CONFIG.paths.root
    / "runtime"
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
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError(
                "WhisperServerRuntime must bind to 127.0.0.1."
            )

        if not 1 <= port <= 65535:
            raise ValueError(
                "port must be between 1 and 65535."
            )

        if startup_timeout_seconds <= 0:
            raise ValueError(
                "startup_timeout_seconds must be greater than zero."
            )

        if request_timeout_seconds <= 0:
            raise ValueError(
                "request_timeout_seconds must be greater than zero."
            )

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

        self._process: subprocess.Popen[bytes] | None = None
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
        return (
            f"http://{self.host}:"
            f"{self.port}/inference"
        )

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

        try:
            with socket.create_connection(
                (
                    self.host,
                    self.port,
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
            self.host,
            "--port",
            str(self.port),
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

            try:
                self._process = subprocess.Popen(
                    self._build_command(),
                    stdout=self._stdout_file,
                    stderr=self._stderr_file,
                    stdin=subprocess.DEVNULL,
                )

            except OSError as exc:
                self._close_logs()

                raise RuntimeError(
                    "Could not start whisper-server.exe."
                ) from exc

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
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.request_timeout_seconds,
                ) as response:
                    raw = response.read()

            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
            ) as exc:
                raise RuntimeError(
                    "Whisper server transcription request failed."
                ) from exc

        try:
            payload = json.loads(
                raw.decode(
                    "utf-8"
                )
            )

        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise RuntimeError(
                "Whisper server returned invalid UTF-8 JSON."
            ) from exc

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

            self._process = None

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
