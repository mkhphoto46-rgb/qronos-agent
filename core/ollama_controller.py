from __future__ import annotations

from typing import Optional, Sequence

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from core import vision_image
from core.brain_runtime import (
    BrainMessage,
    BrainRuntime,
    BrainRuntimeModelStatus,
)


OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# Interactive local inference must never hold Qronos indefinitely.
#
# This remains the conservative runtime default for callers that do not supply
# an explicit role-aware deadline. Orchestrator supplies longer bounded
# deadlines for Heavy Brain cold-load and deeper-reasoning requests.
CHAT_TIMEOUT_SECONDS = 90
OLLAMA_AUTO_START_ENV = "QRONOS_OLLAMA_AUTO_START"
OLLAMA_EXECUTABLE_ENV = "QRONOS_OLLAMA_EXECUTABLE"
OLLAMA_START_TIMEOUT_SECONDS = 12.0
OLLAMA_START_POLL_SECONDS = 0.25

_BOOTSTRAP_LOCK = threading.Lock()
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off"})


# Temporary compatibility alias.
# Existing tests and development code can keep importing
# OllamaModelStatus while Qronos moves to runtime-neutral types.
OllamaModelStatus = BrainRuntimeModelStatus


def _safe_http_error_detail(
    response,
    *,
    limit: int = 600,
) -> str:
    """Return bounded local Ollama error detail for diagnostics."""
    if response is None:
        return ""

    detail = ""

    try:
        data = response.json()
        if isinstance(data, dict):
            detail = str(
                data.get("error", "")
                or ""
            ).strip()
    except Exception:
        detail = ""

    if not detail:
        try:
            detail = str(
                response.text
                or ""
            ).strip()
        except Exception:
            detail = ""

    detail = " ".join(
        detail.split()
    )

    return detail[:limit]


class OllamaController(BrainRuntime):
    """
    Development BrainRuntime adapter backed by the local Ollama HTTP API.

    Qronos higher-level code talks to the BrainRuntime interface instead
    of depending directly on Ollama. A bundled native runtime can replace
    this adapter in the production application later.
    """

    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
    ) -> None:
        self.base_url = base_url.rstrip("/")

    def _probe(self, timeout_seconds: float = 3.0) -> bool:
        try:
            response = requests.get(
                f"{self.base_url}/api/version",
                timeout=max(0.1, float(timeout_seconds)),
            )
            response.raise_for_status()
            return True
        except requests.RequestException:
            return False

    def health_check(self) -> bool:
        """Return True when the configured Ollama API is reachable."""
        return self._probe(3.0)

    def ensure_ready(
        self,
        *,
        auto_start: bool = True,
        timeout_seconds: float = OLLAMA_START_TIMEOUT_SECONDS,
    ) -> bool:
        """
        Ensure the local Ollama service is reachable, starting it if needed.

        Qronos never shells out and never starts a server for a non-local URL.
        Startup is process-serialized so simultaneous first-use requests do not
        race several ``ollama serve`` children after a Windows reboot.
        """
        if self._probe(0.5):
            return True
        if not auto_start or not self._auto_start_enabled():
            return False
        if not self._is_local_base_url():
            return False

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with _BOOTSTRAP_LOCK:
            if self._probe(0.5):
                return True

            executable = self._resolve_local_executable()
            if executable is None:
                return False

            try:
                self._start_local_server(executable)
            except OSError:
                return False

            while time.monotonic() < deadline:
                if self._probe(0.5):
                    return True
                time.sleep(OLLAMA_START_POLL_SECONDS)

        return self._probe(0.5)

    @staticmethod
    def _auto_start_enabled() -> bool:
        raw = os.environ.get(OLLAMA_AUTO_START_ENV, "1").strip().lower()
        return raw not in _FALSE_ENV_VALUES

    def _is_local_base_url(self) -> bool:
        host = (urlparse(self.base_url).hostname or "").lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    @staticmethod
    def _resolve_local_executable() -> str | None:
        configured = os.environ.get(OLLAMA_EXECUTABLE_ENV, "").strip()
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_file():
                return str(candidate.resolve())
            return None

        discovered = shutil.which("ollama")
        if discovered:
            return str(Path(discovered).resolve())

        if os.name == "nt":
            roots = (
                os.environ.get("LOCALAPPDATA", ""),
                os.environ.get("PROGRAMFILES", ""),
            )
            for root in roots:
                if not root:
                    continue
                candidate = Path(root) / "Programs" / "Ollama" / "ollama.exe"
                if candidate.is_file():
                    return str(candidate.resolve())
                candidate = Path(root) / "Ollama" / "ollama.exe"
                if candidate.is_file():
                    return str(candidate.resolve())

        return None

    @staticmethod
    def _start_local_server(executable: str) -> None:
        kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            kwargs["start_new_session"] = True

        subprocess.Popen(
            [executable, "serve"],
            **kwargs,
        )

    def list_running_models(
        self,
    ) -> list[BrainRuntimeModelStatus]:
        """
        Return currently loaded Ollama models.
        """

        try:
            response = requests.get(
                f"{self.base_url}/api/ps",
                timeout=3,
            )

            response.raise_for_status()

        except requests.RequestException as exc:
            raise RuntimeError(
                "Ollama API is unavailable."
            ) from exc

        data = response.json()

        models: list[
            BrainRuntimeModelStatus
        ] = []

        for model in data.get(
            "models",
            [],
        ):
            models.append(
                BrainRuntimeModelStatus(
                    name=str(
                        model.get(
                            "name",
                            "",
                        )
                    ),
                    size=str(
                        model.get(
                            "size",
                            "",
                        )
                    ),
                    processor=str(
                        model.get(
                            "processor",
                            "",
                        )
                    ),
                    context=int(
                        model.get(
                            "context_length",
                            0,
                        )
                        or 0
                    ),
                    until=str(
                        model.get(
                            "expires_at",
                            "",
                        )
                    ),
                )
            )

        return models

    def stop_model(
        self,
        model_name: str,
    ) -> None:
        """
        Unload one model from Ollama.
        """

        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": model_name,
                    "prompt": "",
                    "keep_alive": 0,
                    "stream": False,
                },
                timeout=10,
            )

            response.raise_for_status()

        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not stop model: {model_name}"
            ) from exc

    def unload_all(self) -> None:
        """
        Unload every currently running model.
        """

        for model in self.list_running_models():
            self.stop_model(
                model.name
            )

    @staticmethod
    def _build_messages(
        prompt: str,
        messages: Sequence[BrainMessage] | None,
    ) -> list[dict]:
        """
        Convert runtime-neutral Qronos messages to Ollama messages.

        Structured conversation messages take precedence over the legacy
        single prompt.

        A message carrying pictures gains an ``images`` field holding them
        base64-encoded, which is what this API wants. A message carrying none
        does not gain the field at all â€” so every request Qronos already
        sends goes out byte-identical, and a test says so.
        """

        if messages is not None:
            if not messages:
                raise ValueError(
                    "messages must not be empty."
                )

            built: list[dict] = []

            for message in messages:
                entry: dict = {
                    "role": message.role.value,
                    "content": message.content,
                }

                if message.images:
                    entry["images"] = [
                        vision_image.as_prepared(image).base64
                        for image in message.images
                    ]

                built.append(entry)

            return built

        cleaned_prompt = prompt.strip()

        if not cleaned_prompt:
            raise ValueError(
                "Either prompt or messages must be provided."
            )

        return [
            {
                "role": "user",
                "content": cleaned_prompt,
            }
        ]

    def chat(
        self,
        model_name: str,
        prompt: str = "",
        messages: Sequence[BrainMessage] | None = None,
        think: bool | str = False,
        num_predict: Optional[int] = None,
        num_ctx: Optional[int] = None,
        keep_alive: str = "5m",
        response_format: Optional[dict] = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """
        Send one chat request through the Ollama development runtime.

        Multi-turn conversations use structured BrainMessage objects.
        Legacy single-turn callers can continue passing prompt.
        """

        effective_timeout = (
            float(CHAT_TIMEOUT_SECONDS)
            if timeout_seconds is None
            else float(timeout_seconds)
        )

        if effective_timeout <= 0:
            raise ValueError(
                "timeout_seconds must be greater than zero."
            )

        if isinstance(think, str):
            cleaned_think = think.strip().lower()
            if cleaned_think not in {"low", "medium", "high"}:
                raise ValueError(
                    "GPT-OSS reasoning level must be low, medium, or high."
                )
            think_value: bool | str = cleaned_think
        elif isinstance(think, bool):
            think_value = think
        else:
            raise TypeError(
                "think must be a bool or one of: low, medium, high."
            )

        options: dict[str, int] = {}

        if num_predict is not None:
            options["num_predict"] = (
                num_predict
            )

        # Without this the server picks its own context, which for these
        # models is 262144 tokens and costs more than ten gigabytes of key
        # and value cache â€” for a 2.3 GB model. Left unset, Qronos fills the
        # card and then refuses its own next request.
        if num_ctx is not None:
            options["num_ctx"] = num_ctx

        ollama_messages = (
            self._build_messages(
                prompt=prompt,
                messages=messages,
            )
        )

        payload: dict = {
            "model": model_name,
            "messages": ollama_messages,
            "think": think_value,
            "stream": False,
            "keep_alive": keep_alive,
            "options": options,
        }

        # Structured output. A caller that needs a particular shape back
        # can constrain generation to it rather than describing it in the
        # prompt and hoping â€” which is how the web research layer came to
        # discard every answer it produced.
        if response_format is not None:
            payload["format"] = response_format

        response = None

        for attempt in range(2):
            try:
                response = requests.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                    timeout=effective_timeout,
                )

                response.raise_for_status()
                break

            except requests.ConnectionError as exc:
                if (
                    attempt == 0
                    and self.ensure_ready(
                        auto_start=True,
                    )
                ):
                    continue

                raise RuntimeError(
                    "Qronos local Brain is unavailable."
                ) from exc

            except requests.Timeout as exc:
                raise TimeoutError(
                    (
                        "Qronos Brain exceeded the "
                        f"{effective_timeout:g}-second "
                        "safety deadline."
                    )
                ) from exc

            except requests.HTTPError as exc:
                if (
                    exc.response is not None
                    and exc.response.status_code == 404
                ):
                    raise RuntimeError(
                        "Qronos local Brain model is not installed."
                    ) from exc

                status = (
                    exc.response.status_code
                    if exc.response is not None
                    else "unknown"
                )

                raise RuntimeError(
                    "Qronos local Brain request failed "
                    f"(HTTP {status})."
                ) from exc

            except requests.RequestException as exc:
                raise RuntimeError(
                    "Qronos local Brain request failed."
                ) from exc

        if response is None:
            raise RuntimeError(
                "Qronos local Brain is unavailable."
            )

        data = response.json()
        message = data.get("message", {})

        if not isinstance(message, dict):
            message = {}

        content = str(
            message.get(
                "content",
                "",
            )
            or ""
        )

        thinking = str(
            message.get(
                "thinking",
                "",
            )
            or ""
        )

        if (
            not content.strip()
            and thinking.strip()
            and str(data.get("done_reason", "")) == "length"
        ):
            raise RuntimeError(
                "GPT-OSS used the full generation budget for reasoning "
                "before producing a final answer. Increase the live-test "
                "budget or use a lower reasoning level."
            )

        return content


if __name__ == "__main__":
    controller = OllamaController()

    if controller.health_check():
        print(
            "Ollama API: OK"
        )

    else:
        print(
            "Ollama API: unavailable"
        )