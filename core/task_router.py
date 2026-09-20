"""
Which kind of task a request is, decided before any model runs.

Qronos is Persian-first, so routing markers exist in both Persian and English.
Input and markers are normalised through ``core.persian_text`` before matching.

ASCII markers use word-aware matching while Persian markers use the project's
Persian marker semantics. This prevents unrelated substring collisions such as
``profile`` -> ``file`` or ``prototype`` -> ``type``.

Precedence is deliberate:
- browser navigation first;
- compound vision before computer actions;
- computer actions before loose vision markers;
- heavy reasoning after action/vision routing;
- otherwise fast.

Heavy routing intentionally uses high-signal phrases for code review, stress
testing, edge-case analysis, debugging, optimization and project planning.
Generic words such as ``بررسی``, ``review``, ``داستان`` and ambiguous
``برنامه`` are not enough by themselves because they would over-route ordinary
requests to Heavy or Computer.
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
    """Normalise markers exactly as incoming requests are normalised."""
    return tuple(normalise(value).lower() for value in values)


class TaskRouter:
    """Route a user request to the first appropriate Qronos task type."""

    PAINT_NAMES = _markers(
        "paint",
        "microsoft paint",
        "mspaint",
        "پینت",
        "پاینت",
    )

    DRAW_MARKERS = _markers(
        "draw",
        "sketch",
        "بکش",
        "بکشید",
        "رسم کن",
        "نقاشی کن",
    )

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
        "نرم‌افزار",
        "ویندوز",
        "کامپیوتر",
        "رایانه",
        "نصب کن",
        "در پینت",
        "توی پینت",
        "داخل پینت",
        "in paint",
        "draw in paint",
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

    VISION_VERBS = _markers(
        "look at",
        "look on",
        "read",
        "see",
        "watch",
        "describe",
        "what is on",
        "what's on",
        "what is in",
        "what's in",
        "what do you see",
        "what can you see",
        "show me what",
        "check",
        "ocr",
        # Persian
        "نگاه کن",
        "ببین",
        "می‌بینی",
        "بخوان",
        "توصیف کن",
        "چه چیزی",
        "چی هست",
    )

    VISION_SUBJECTS = _markers(
        "screen",
        "screenshot",
        "display",
        "desktop",
        "monitor",
        "image",
        "picture",
        "photo",
        "camera",
        "webcam",
        "window",
        "frame",
        "this",
        # Persian
        "صفحه",
        "نمایشگر",
        "دسکتاپ",
        "عکس",
        "تصویر",
        "دوربین",
        "وب‌کم",
        "پنجره",
        "اسکرین‌شات",
        "این",
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
        "chapter",
        "novel",
        "rewrite",
        "improve",
        "worldbuilding",
        "consistency",
        "detailed",
        # High-signal technical reasoning
        "stress test",
        "stress-test",
        "code review",
        "review this code",
        "review the code",
        "edge case",
        "edge cases",
        "find bugs",
        "find the bug",
        "race condition",
        "race conditions",
        "performance bottleneck",
        "algorithm bottleneck",
        "crash",
        "crashes",
        "debug",
        "debugging",
        "optimize",
        "optimization",
        "root cause",
        "step-by-step plan",
        "step by step plan",
        "project plan",
        "roadmap",
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
        "فصل",
        "رمان",
        "بازنویسی",
        "بهبود",
        "جزئیات",
        "مفصل",
        # High-signal Persian technical reasoning
        "استرس تست",
        "تست استرس",
        "بررسی کد",
        "کد را بررسی",
        "کد رو بررسی",
        "ایج کیس",
        "حالت مرزی",
        "حالت‌های مرزی",
        "حالت های مرزی",
        "باگ پیدا کن",
        "باگ‌ها را پیدا کن",
        "باگ ها را پیدا کن",
        "شرایط رقابتی",
        "گلوگاه عملکرد",
        "گلوگاه الگوریتم",
        "کرش",
        "دیباگ",
        "عیب یابی",
        "عیب‌یابی",
        "بهینه کن",
        "بهینه سازی",
        "بهینه‌سازی",
        "علت اصلی",
        "ریشه خطا",
        "نقشه راه",
        "برای این پروژه یک برنامه",
        "برنامه برای این پروژه",
        "برنامه مرحله به مرحله",
        "بررسی کن چرا",
        "بررسی کن که چرا",
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

        if self._looks_like_vision(text):
            return RouteDecision(
                task_type=TaskType.VISION,
                reason=(
                    "The request asks Qronos to look at something and says "
                    "what."
                ),
            )

        if self._looks_like_paint_draw(text):
            return RouteDecision(
                task_type=TaskType.COMPUTER,
                reason="The request explicitly asks to draw in Microsoft Paint.",
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

    def _looks_like_paint_draw(self, text: str) -> bool:
        return self._contains_any(
            text, self.PAINT_NAMES
        ) and self._contains_any(text, self.DRAW_MARKERS)

    def _looks_like_vision(self, text: str) -> bool:
        return self._contains_any(
            text, self.VISION_VERBS
        ) and self._contains_any(text, self.VISION_SUBJECTS)

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return contains_marker(text, keywords)


if __name__ == "__main__":
    router = TaskRouter()

    examples = [
        "Hello Qronos",
        "Analyze this chapter deeply",
        "stress test this code and find edge cases",
        "review this code for bugs",
        "Look at this screenshot",
        "Open Premiere",
        "Go to ChatGPT and send this message",
        "سلام کرونوس",
        "این فصل را عمیق تحلیل کن",
        "کد را بررسی و استرس تست کن",
        "به این عکس نگاه کن",
        "پریمیر را باز کن",
        "برو به گوگل",
    ]

    for example in examples:
        decision = router.route(example)
        print(f"{example} -> {decision.task_type.value}")
