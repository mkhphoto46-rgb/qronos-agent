from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests


OLLAMA_BASE_URL = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class OllamaModelStatus:
    """Information about a currently loaded Ollama model."""

    name: str
    size: str
    processor: str
    context: int
    until: str


class OllamaController:
    """Control local Ollama models through the local HTTP API."""

    def __init__(self, base_url: str = OLLAMA_BASE_URL) -> None:
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

    def list_running_models(self) -> list[OllamaModelStatus]:
        """Return currently loaded Ollama models."""
        try:
            response = requests.get(
                f"{self.base_url}/api/ps",
                timeout=3,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError("Ollama API is unavailable.") from exc

        data = response.json()
        models: list[OllamaModelStatus] = []

        for model in data.get("models", []):
            models.append(
                OllamaModelStatus(
                    name=str(model.get("name", "")),
                    size=str(model.get("size", "")),
                    processor=str(model.get("processor", "")),
                    context=int(model.get("context_length", 0) or 0),
                    until=str(model.get("expires_at", "")),
                )
            )

        return models

    def stop_model(self, model_name: str) -> None:
        """Unload a model from Ollama."""
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
        """Unload all currently running models."""
        for model in self.list_running_models():
            self.stop_model(model.name)

    def chat(
        self,
        model_name: str,
        prompt: str,
        think: bool = False,
        num_predict: Optional[int] = None,
        keep_alive: str = "5m",
    ) -> str:
        """Send a single chat request to a local Ollama model."""
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
                f"Could not send request to model: {model_name}"
            ) from exc

        data = response.json()

        return str(
            data.get("message", {}).get("content", "")
        )


if __name__ == "__main__":
    controller = OllamaController()

    if controller.health_check():
        print("Ollama API: OK")
    else:
        print("Ollama API: unavailable")