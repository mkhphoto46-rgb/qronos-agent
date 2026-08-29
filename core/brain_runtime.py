from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class BrainMessageRole(Enum):
    """
    Runtime-neutral chat message roles.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class BrainMessage:
    """
    One runtime-neutral conversation message.

    Higher-level Qronos code uses this structure instead of depending
    directly on the message format of Ollama or another model runtime.

    A message may carry pictures as well as words. They are held as
    **paths**, not as encoded bytes: this is a frozen dataclass that ends up
    in log lines and tracebacks, and a megabyte of base64 in a repr would
    make every one of those unreadable. Encoding is the runtime adapter's
    business, because the encoding is the runtime's requirement.
    """

    role: BrainMessageRole
    content: str
    images: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.content.strip() and not self.images:
            raise ValueError(
                "A brain message must carry something — either words or "
                "at least one picture."
            )

        if isinstance(self.images, str):
            raise TypeError(
                "images is a sequence of paths, not one path. A bare string "
                "would be read as one path per character."
            )


@dataclass(frozen=True)
class BrainRuntimeModelStatus:
    """
    Runtime-neutral information about a loaded brain model.
    """

    name: str
    size: str = ""
    processor: str = ""
    context: int = 0
    until: str = ""


class BrainRuntime(ABC):
    """
    Runtime-independent interface used by Qronos to talk to local brains.

    The MVP currently uses Ollama behind this interface. Production can
    later replace Ollama with a bundled native runtime without changing
    the Orchestrator or higher-level Qronos logic.

    chat() supports either:

        prompt:
            A legacy single-turn request.

        messages:
            Structured multi-turn conversation context.

    Structured messages take precedence when supplied.
    """

    @abstractmethod
    def health_check(self) -> bool:
        """
        Return True when the brain runtime is ready.
        """

    @abstractmethod
    def chat(
        self,
        model_name: str,
        prompt: str = "",
        messages: Sequence[BrainMessage] | None = None,
        think: bool = False,
        num_predict: int | None = None,
        num_ctx: int | None = None,
        keep_alive: str = "5m",
        response_format: dict | None = None,
    ) -> str:
        """
        Generate a response with the requested brain.

        messages should be used for multi-turn conversation.

        prompt remains available for single-turn callers and temporary
        backward compatibility while Qronos migrates to structured chat.
        """

    @abstractmethod
    def list_running_models(
        self,
    ) -> list[BrainRuntimeModelStatus]:
        """
        Return models currently loaded by the runtime.
        """

    @abstractmethod
    def stop_model(
        self,
        model_name: str,
    ) -> None:
        """
        Unload one model.
        """

    @abstractmethod
    def unload_all(self) -> None:
        """
        Unload every model currently managed by the runtime.
        """