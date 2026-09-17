from __future__ import annotations

from typing import Optional, Sequence

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

    def health_check(self) -> bool:
        """
        Return True when the local Ollama API is reachable.
        """

        try:
            response = requests.get(
                f"{self.base_url}/api/version",
                timeout=3,
            )

            response.raise_for_status()

            return True

        except requests.RequestException:
            return False

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

        try:
            response = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=effective_timeout,
            )

            response.raise_for_status()

        except requests.Timeout as exc:
            raise TimeoutError(
                (
                    "Qronos Brain exceeded the "
                    f"{effective_timeout:g}-second "
                    f"safety deadline: {model_name}"
                )
            ) from exc

        except requests.HTTPError as exc:
            # A model that was never downloaded and a server that is not
            # running produce the same sentence otherwise, and they send a
            # person to two completely different places: one to `ollama pull`,
            # the other to check whether anything is running at all.
            if exc.response is not None and exc.response.status_code == 404:
                raise RuntimeError(
                    f"The model {model_name} is not installed on this "
                    "machine. Qronos downloads the models it needs; if this "
                    "one is missing, that download has not happened yet."
                ) from exc

            status = (
                exc.response.status_code
                if exc.response is not None
                else "unknown"
            )
            detail = _safe_http_error_detail(
                exc.response
            )
            suffix = (
                f": {detail}"
                if detail
                else ""
            )

            raise RuntimeError(
                "Could not send request to model: "
                f"{model_name} (Ollama HTTP {status}{suffix})"
            ) from exc

        except requests.RequestException as exc:
            detail = " ".join(
                str(exc).split()
            )[:600]
            suffix = (
                f": {detail}"
                if detail
                else ""
            )

            raise RuntimeError(
                "Could not send request to model: "
                f"{model_name} "
                f"({type(exc).__name__}{suffix})"
            ) from exc

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