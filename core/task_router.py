"""
Which kind of task a request is, decided before any model runs.

Two things about this module are easy to get wrong, and both were wrong here.

The first is language. Qronos is a Persian-first assistant, and speech
recognition is asked for Persian explicitly, so almost everything this router
sees is Persian. It matched English substrings only, which meant every Persian
request missed every keyword list and fell through to FAST. Persian markers sit
alongside the English ones now, and the text is normalised before matching so
that Arabic letter variants, Persian digits and the zero-width non-joiner do
not decide the answer.

The second is substring matching. ``profile`` contains ``file``, ``prototype``
contains ``type`` and ``cobweb`` contains ``web``, so a plain ``in`` test
misroutes ordinary English. :func:`core.persian_text.contains_marker` matches
ASCII markers on word boundaries and Persian markers as substrings — the right
rule for each, because Persian is written without the spacing that makes a
boundary meaningful. The web layer already routes queries this way; this module
now does the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.persian_text import contains_marker, normalise


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


def _markers(*values: str) -> tuple[str, ...]:
    """
    Normalise the markers the same way the request will be normalised.

    Persian is commonly written with a zero-width non-joiner inside a single
    word — ``وب‌سایت``, ``برنامه‌ریزی`` — and normalisation turns that into a
    space. A marker written the natural way would never match a normalised
    request unless it went through the same transformation, so it does, here,
    once, at import.
    """
    return tuple(normalise(value).lower() for value in values)


class TaskRouter:
    """Route a user request to the first appropriate Qronos task type."""

    COMPUTER_KEYWORDS = _markers(
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
        # Persian
        "باز کن",
        "ببند",
        "اجرا کن",
        "اجرا کردن",
        "کلیک",
        "تایپ",
        "فایل",
        "پوشه",
        "برنامه",
        "نرم‌افزار",
        "ویندوز",
        "کامپیوتر",
        "رایانه",
        "نصب کن",
    )

    BROWSER_KEYWORDS = _markers(
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
        # Persian
        "مرورگر",
        "وب‌سایت",
        "سایت",
        "اینترنت",
        "گوگل",
        "آنلاین",
        "برو به",
        "جست‌وجو در وب",
        "جستجو در وب",
        "جست‌وجو در اینترنت",
        "جستجو در اینترنت",
        "پیام بفرست",
    )

    VISION_KEYWORDS = _markers(
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
        # Persian
        "عکس",
        "تصویر",
        "اسکرین‌شات",
        "دوربین",
        "نگاه کن",
        "می‌بینی",
        "از صفحه بخوان",
    )

    HEAVY_KEYWORDS = _markers(
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
        # Persian
        "تحلیل",
        "بررسی عمیق",
        "عمیق",
        "استدلال",
        "پیچیده",
        "نقد",
        "ارزیابی",
        "مقایسه",
        "برنامه‌ریزی",
        "حل کن",
        "منطق",
        "معماری",
        "داستان",
        "فصل",
        "رمان",
        "بازنویسی",
        "بهبود",
        "جزئیات",
        "مفصل",
    )

    def route(self, user_input: str) -> RouteDecision:
        """Classify a request using deterministic rules."""
        text = normalise(user_input).lower()

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
        return contains_marker(text, keywords)


if __name__ == "__main__":
    router = TaskRouter()

    examples = [
        "Hello Qronos",
        "Analyze this chapter deeply",
        "Look at this screenshot",
        "Open Premiere",
        "Go to ChatGPT and send this message",
        "سلام کرونوس",
        "این فصل را عمیق تحلیل کن",
        "به این عکس نگاه کن",
        "پریمیر را باز کن",
        "برو به گوگل",
    ]

    for example in examples:
        decision = router.route(example)

        print(f"{example} -> {decision.task_type.value}")
