from __future__ import annotations

from typing import Optional

import requests

from core.brain_runtime import (
    BrainRuntime,
    BrainRuntimeModelStatus,
)


OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# Temporary compatibility alias.
# Existing tests and development code can keep importing
# OllamaModelStatus while Qronos moves to runtime-neutral types.
OllamaModelStatus = BrainRuntimeModelStatus


class OllamaController(BrainRuntime):
    """
    Development BrainRuntime adapter backed by the local Ollama HTTP API.

    Qronos higher-level code talks to the BrainRuntime interface instead of
    depending directly on Ollama. A bundled native runtime can replace this
    adapter in the production application later.
    """

    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
    ) -> None:
        self.base_url = base_url.rstrip("/")

    def health_check(self) -> bool:
        """Return True when the local Ollama API is reachable."""
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
        """Return currently loaded Ollama models."""
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

        models: list[BrainRuntimeModelStatus] = []

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
        """Unload one model from Ollama."""
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
        """Unload every currently running model."""
        for model in self.list_running_models():
            self.stop_model(
                model.name
            )

    def chat(
        self,
        model_name: str,
        prompt: str,
        think: bool = False,
        num_predict: Optional[int] = None,
        keep_alive: str = "5m",
    ) -> str:
        """Send one chat request through the Ollama development runtime."""

        options: dict[str, int] = {}

        if num_predict is not None:
            options["num_predict"] = num_predict

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "think": think,
            "stream": False,
            "keep_alive": keep_alive,
            "options": options,
        }

        try:
            response = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=600,
            )

            response.raise_for_status()

        except requests.RequestException as exc:
            raise RuntimeError(
                "Could not send request to model: "
                f"{model_name}"
            ) from exc

        data = response.json()

        return str(
            data.get(
                "message",
                {},
            ).get(
                "content",
                "",
            )
        )


if __name__ == "__main__":
    controller = OllamaController()

    if controller.health_check():
        print("Ollama API: OK")
    else:
        print("Ollama API: unavailable")