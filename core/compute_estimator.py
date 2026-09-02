from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.arithmetic_fast_path import solve_simple_arithmetic
from core.persian_text import contains_marker, normalise
from core.routing_input import RoutingInput


class ComputeLevel(str, Enum):
    NONE = "none"
    FAST = "fast"
    HEAVY_ECO = "heavy_eco"
    HEAVY_NORMAL = "heavy_normal"
    HEAVY_DEEP = "heavy_deep"


@dataclass(frozen=True)
class ComputeDecision:
    level: ComputeLevel
    score: int
    confidence: float
    factors: tuple[str, ...]


def _markers(*values: str) -> tuple[str, ...]:
    return tuple(
        normalise(value).lower()
        for value in values
    )


class ComputeEstimator:
    """
    Deterministic estimate of cognitive compute required by a request.

    This class deliberately does NOT choose a worker and does NOT import the
    Intent Gate. A short factual question can therefore be estimated as FAST
    compute even though the later Resolver may route it to Heavy because the
    capability requirement is knowledge-sensitive.
    """

    REASONING = _markers(
        "چرا", "علتش چیه", "علت چیست", "علت احتمالی", "علت", "تحلیل",
        "تحلیل کن", "استدلال", "منطق", "نقد", "ارزیابی", "بررسی",
        "مزایا و معایب", "چطور می شود", "چطور می‌شود", "چگونه می شود",
        "چگونه می‌شود", "مقیاس پذیر", "مقیاس‌پذیر", "چه تغییری باید",
        "reason", "reasoning", "why", "analyze", "analyse", "evaluate",
        "critique", "pros and cons", "scalable",
    )

    COMPARISON = _markers(
        "مقایسه", "مقایسه کن", "فرق", "تفاوت", "مزایا و معایب", "در مقابل",
        "compare", "comparison", "versus", "vs", "pros and cons",
    )

    DEEP_REASONING = _markers(
        "عمیق", "بررسی عمیق", "تحلیل عمیق", "استرس تست", "stress test",
        "موشکافانه", "جامع", "همه جوانب", "تمام جوانب", "deep", "deeply",
        "comprehensive", "thorough",
    )

    PLANNING_ARCHITECTURE = _markers(
        "معماری", "طراحی سیستم", "سیستم دیزاین", "برنامه ریزی",
        "برنامه‌ریزی", "نقشه راه", "roadmap", "architecture",
        "system design", "planning", "migration plan", "strategy",
    )

    CODE = _markers(
        "پایتون", "جاوااسکریپت", "تایپ اسکریپت", "تایپ‌اسکریپت",
        "python", "javascript", "typescript", "rust", "c++", "sql", "css",
        "html", "function", "class", "api", "regex", "pytest", "npm",
        "cargo", "source code",
    )

    DEBUG = _markers(
        "باگ", "ارور", "خطا", "کرش", "دیباگ", "ددلاک", "ریس کاندیشن",
        "تریس بک", "اکسپشن", "مشکل", "کار نمی کنه", "کار نمی‌کند",
        "اعمال نمی شود", "اعمال نمی‌شود", "render نمی شود", "render نمی‌شود",
        "compile نمی شود", "compile نمی‌شود", "match نمی کند", "match نمی‌کند",
        "bug", "error", "crash", "debug", "deadlock", "race condition",
        "traceback", "exception", "failing", "fails", "doesn't work",
        "does not work",
    )

    MULTI_STEP = _markers(
        "مرحله به مرحله", "قدم به قدم", "اول", "بعد", "سپس", "در ادامه",
        "همزمان", "هم زمان", "هم‌زمان", "به ترتیب", "step by step",
        "first", "then", "next", "after that", "simultaneously",
    )

    CONSTRAINTS = _markers(
        "فقط", "دقیقاً", "حداکثر", "حداقل", "نباید", "باید", "بدون",
        "شرط", "محدودیت", "در قالب", "با فرمت", "exactly", "only",
        "must", "must not", "without", "at most", "at least",
        "constraint", "format",
    )

    STRUCTURED_OUTPUT = _markers(
        "جدول", "لیست", "چک لیست", "چک‌لیست", "json", "yaml", "csv",
        "markdown", "جدول مقایسه", "table", "list", "checklist",
    )

    LONG_FORM = _markers(
        "مفصل", "با جزئیات", "جزئیات کامل", "کامل توضیح بده", "گزارش کامل",
        "مقاله", "فصل", "رمان", "long form", "long-form", "detailed",
        "in detail", "full report", "chapter", "novel",
    )

    SIMPLE_LANGUAGE = _markers(
        "سلام", "مرسی", "ممنون", "خوبی", "حالت چطوره", "کوتاه تر کن",
        "کوتاه‌تر کن", "دوستانه تر کن", "دوستانه‌تر کن", "ترجمه کن",
        "بازنویسی کن", "hello", "hi", "thanks", "rewrite", "rephrase",
        "translate", "shorten",
    )

    def estimate(
        self,
        user_input: str | RoutingInput,
    ) -> ComputeDecision:
        data = (
            user_input
            if isinstance(
                user_input,
                RoutingInput,
            )
            else RoutingInput.from_text(
                user_input
            )
        )
        text = data.lower_text

        if not text:
            return ComputeDecision(
                level=ComputeLevel.FAST,
                score=0,
                confidence=0.80,
                factors=("empty_input",),
            )

        if (
            solve_simple_arithmetic(
                data.raw_text
            )
            is not None
        ):
            return ComputeDecision(
                level=ComputeLevel.NONE,
                score=0,
                confidence=0.995,
                factors=("direct_arithmetic",),
            )

        score = 0
        factors: list[str] = []

        if data.word_count >= 160:
            score += 24
            factors.append(
                "very_long_input"
            )

        elif data.word_count >= 90:
            score += 16
            factors.append(
                "long_input"
            )

        elif data.word_count >= 50:
            score += 10
            factors.append(
                "medium_long_input"
            )

        elif data.word_count >= 28:
            score += 5
            factors.append(
                "moderate_input_length"
            )

        if self._has(
            text,
            self.REASONING,
        ):
            score += 11
            factors.append(
                "reasoning_requested"
            )

        if (
            "اگر" in data.words
            or "if" in data.words
        ):
            score += 11
            factors.append(
                "conditional_reasoning"
            )

        if self._has(
            text,
            self.COMPARISON,
        ):
            score += 8
            factors.append(
                "comparison_requested"
            )

        if self._has(
            text,
            self.DEEP_REASONING,
        ):
            score += 18
            factors.append(
                "deep_reasoning_requested"
            )

        if self._has(
            text,
            self.PLANNING_ARCHITECTURE,
        ):
            score += 15
            factors.append(
                "planning_or_architecture"
            )

        code = (
            self._has(
                text,
                self.CODE,
            )
            or "کد" in data.words
            or "code" in data.words
        )

        debug = self._has(
            text,
            self.DEBUG,
        )

        if code:
            score += 9
            factors.append(
                "code_context"
            )

        if debug:
            score += 11
            factors.append(
                "debugging_context"
            )

        if code and debug:
            score += 6
            factors.append(
                "code_debug_combination"
            )

        if self._has(
            text,
            self.MULTI_STEP,
        ):
            score += 9
            factors.append(
                "multi_step_request"
            )

        constraint_hits = (
            self._marker_hit_count(
                text,
                self.CONSTRAINTS,
            )
        )

        if constraint_hits:
            constraint_score = min(
                15,
                constraint_hits * 3,
            )
            score += constraint_score
            factors.append(
                f"constraints:{constraint_hits}"
            )

        if self._has(
            text,
            self.STRUCTURED_OUTPUT,
        ):
            score += 5
            factors.append(
                "structured_output"
            )

        if self._has(
            text,
            self.LONG_FORM,
        ):
            score += 9
            factors.append(
                "long_form_requested"
            )

        if (
            self._has(
                text,
                self.SIMPLE_LANGUAGE,
            )
            and score <= 5
        ):
            factors.append(
                "simple_language_task"
            )

        score = max(
            0,
            min(
                100,
                score,
            ),
        )

        level = (
            self._level_for_score(
                score
            )
        )

        confidence = (
            self._confidence(
                score,
                level,
                factors,
            )
        )

        if not factors:
            factors.append(
                "no_complexity_signal"
            )

        return ComputeDecision(
            level=level,
            score=score,
            confidence=confidence,
            factors=tuple(factors),
        )

    @staticmethod
    def _level_for_score(
        score: int,
    ) -> ComputeLevel:
        if score <= 7:
            return ComputeLevel.FAST

        if score <= 22:
            return ComputeLevel.HEAVY_ECO

        if score <= 44:
            return ComputeLevel.HEAVY_NORMAL

        return ComputeLevel.HEAVY_DEEP

    @staticmethod
    def _confidence(
        score: int,
        level: ComputeLevel,
        factors: list[str],
    ) -> float:
        boundaries = (
            8,
            22,
            44,
        )

        nearest = min(
            abs(
                score - boundary
            )
            for boundary in boundaries
        )

        if (
            level is ComputeLevel.FAST
            and not factors
        ):
            return 0.90

        if nearest <= 1:
            return 0.82

        if nearest <= 3:
            return 0.88

        if len(factors) >= 4:
            return 0.96

        if len(factors) >= 2:
            return 0.93

        return 0.90

    @staticmethod
    def _marker_hit_count(
        text: str,
        markers: tuple[str, ...],
    ) -> int:
        return sum(
            1
            for marker in markers
            if marker
            and ComputeEstimator._marker_present(
                text,
                marker,
            )
        )

    @staticmethod
    def _marker_present(
        text: str,
        marker: str,
    ) -> bool:
        return contains_marker(
            text,
            (marker,),
        )

    @staticmethod
    def _has(
        text: str,
        markers: tuple[str, ...],
    ) -> bool:
        return contains_marker(
            text,
            markers,
        )

