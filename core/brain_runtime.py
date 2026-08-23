from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class BrainRuntimeModelStatus:
    """Runtime-neutral information about a loaded brain model."""

    name: str
    size: str = ""
    processor: str = ""
    context: int = 0
    until: str = ""


class BrainRuntime(ABC):
    """
    Runtime-independent interface used by Qronos to talk to local brains.

    The MVP currently uses Ollama behind this interface. Production can later
    replace Ollama with a bundled native runtime without changing the
    Orchestrator or higher-level Qronos logic.
    """

    @abstractmethod
    def health_check(self) -> bool:
        """Return True when the brain runtime is ready."""

    @abstractmethod
    def chat(
        self,
        model_name: str,
        prompt: str,
        think: bool = False,
        num_predict: int | None = None,
        keep_alive: str = "5m",
    ) -> str:
        """Generate a response with the requested brain."""

    @abstractmethod
    def list_running_models(
        self,
    ) -> list[BrainRuntimeModelStatus]:
        """Return models currently loaded by the runtime."""

    @abstractmethod
    def stop_model(
        self,
        model_name: str,
    ) -> None:
        """Unload one model."""

    @abstractmethod
    def unload_all(self) -> None:
        """Unload every model currently managed by the runtime."""