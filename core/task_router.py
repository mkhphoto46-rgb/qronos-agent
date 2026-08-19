from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskType(Enum):
    FAST = "fast"
    HEAVY = "heavy"
    VISION = "vision"
    COMPUTER = "computer"
    BROWSER = "browser"


@dataclass(frozen=True)
class RouteDecision:
    task_type: TaskType
    reason: str


class TaskRouter:
    """Route a user request to the first appropriate Qronos task type."""

    COMPUTER_KEYWORDS = (
        "open",
        "close",
        "start",
        "launch",
        "run",
        "click",
        "type",
        "premiere",
        "photoshop",
        "file",
        "folder",
        "windows",
        "computer",
        "app",
        "application",
    )

    BROWSER_KEYWORDS = (
        "browser",
        "website",
        "web",
        "chatgpt",
        "claude",
        "gemini",
        "perplexity",
        "search the web",
        "go to",
        "send a message",
        "send this message",
        "online",
    )

    VISION_KEYWORDS = (
        "image",
        "photo",
        "picture",
        "screenshot",
        "camera",
        "what do you see",
        "look at",
        "analyze this image",
        "read this image",
        "ocr",
        "video frame",
    )

    HEAVY_KEYWORDS = (
        "deep",
        "deeply",
        "analyze",
        "analysis",
        "reason",
        "reasoning",
        "complex",
        "critique",
        "criticize",
        "evaluate",
        "compare",
        "plan",
        "planning",
        "solve",
        "logic",
        "architecture",
        "story",
        "chapter",
        "novel",
        "rewrite",
        "improve",
        "worldbuilding",
        "consistency",
        "detailed",
    )

    def route(self, user_input: str) -> RouteDecision:
        """Classify a request using deterministic rules."""
        text = user_input.strip().lower()

        if not text:
            return RouteDecision(
                task_type=TaskType.FAST,
                reason="Empty input defaults to fast handling.",
            )

        if self._contains_any(text, self.BROWSER_KEYWORDS):
            return RouteDecision(
                task_type=TaskType.BROWSER,
                reason="The request appears to require browser interaction.",
            )

        if self._contains_any(text, self.COMPUTER_KEYWORDS):
            return RouteDecision(
                task_type=TaskType.COMPUTER,
                reason="The request appears to require computer interaction.",
            )

        if self._contains_any(text, self.VISION_KEYWORDS):
            return RouteDecision(
                task_type=TaskType.VISION,
                reason="The request appears to require image or visual analysis.",
            )

        if self._contains_any(text, self.HEAVY_KEYWORDS):
            return RouteDecision(
                task_type=TaskType.HEAVY,
                reason="The request appears to require deeper reasoning or extended work.",
            )

        return RouteDecision(
            task_type=TaskType.FAST,
            reason="The request appears suitable for fast handling.",
        )

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text for keyword in keywords)


if __name__ == "__main__":
    router = TaskRouter()

    examples = [
        "Hello Qronos",
        "Analyze this chapter deeply",
        "Look at this screenshot",
        "Open Premiere",
        "Go to ChatGPT and send this message",
    ]

    for example in examples:
        decision = router.route(example)
        print(f"{example} -> {decision.task_type.value}")