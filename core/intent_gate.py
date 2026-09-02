from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from core.arithmetic_fast_path import solve_simple_arithmetic
from core.persian_text import contains_marker, normalise
from core.routing_input import RoutingInput


class IntentType(str, Enum):
    DIRECT_DETERMINISTIC = "direct_deterministic"
    CASUAL_LANGUAGE = "casual_language"
    LANGUAGE_TRANSFORM = "language_transform"
    CREATIVE_LANGUAGE = "creative_language"
    KNOWLEDGE_STABLE = "knowledge_stable"
    KNOWLEDGE_CURRENT = "knowledge_current"
    REASONING = "reasoning"
    CODE = "code"
    DEVICE_ACTION = "device_action"
    BROWSER_ACTION = "browser_action"
    WEB_RESEARCH = "web_research"
    VISION_ANALYSIS = "vision_analysis"
    LOCAL_STATE = "local_state"
    IMAGE_GENERATION = "image_generation"
    UNKNOWN = "unknown"


class AccuracyRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class IntentDecision:
    primary_intent: IntentType
    required_intents: tuple[IntentType, ...]
    confidence: float
    accuracy_risk: AccuracyRisk
    signals: tuple[str, ...]

    def has(self, intent: IntentType) -> bool:
        return intent in self.required_intents


def _markers(*values: str) -> tuple[str, ...]:
    return tuple(normalise(value).lower() for value in values)


class IntentGate:
    """Deterministic, Persian-first, multi-label capability classifier."""

    GREETINGS = _markers(
        "سلام", "درود", "صبح بخیر", "شب بخیر", "خوبی", "حالت چطوره",
        "حالت خوبه", "چه خبر", "hello", "hi", "hey", "good morning",
        "good night",
    )
    THANKS = _markers(
        "مرسی", "ممنون", "متشکرم", "دمت گرم", "thanks", "thank you"
    )
    PERSONAL_STATE = _markers(
        "خسته ام", "خسته‌ام", "حوصله ندارم", "حالم گرفته", "حالم خوب نیست",
        "ناراحتم", "خوشحالم", "استرس دارم", "i am tired", "i'm tired",
    )
    ASSISTANT_IDENTITY = _markers(
        "اسمت چیه", "اسم تو چیه", "تو کی هستی", "تو چی هستی",
        "who are you", "what is your name",
    )

    LANGUAGE_TRANSFORM = _markers(
        "بازنویسی کن", "بازنویسی", "کوتاه تر کن", "کوتاه‌تر کن", "کوتاه کن",
        "خلاصه کن", "خلاصه اش کن", "خلاصه‌اش کن",
        "دوستانه تر کن", "دوستانه‌تر کن",
        "رسمی تر کن", "رسمی‌تر کن",
        "روان تر کن", "روان‌تر کن", "روان تر بنویس", "روان‌تر بنویس",
        "حرفه ای تر کن", "حرفه‌ای‌تر کن", "حرفه ای ترش کن", "حرفه‌ای‌ترش کن",
        "مودبانه تر کن", "مودبانه‌تر کن", "محترمانه تر کن", "محترمانه‌تر کن",
        "بهترش کن", "ویرایش کن", "اصلاح کن", "ترجمه کن", "ترجمه",
        "rewrite", "rephrase", "shorten", "summarize", "summarise",
        "translate", "edit this", "improve this sentence", "make it more professional",
        "make it friendlier", "make it more polite",
    )

    CREATIVE_SUBJECTS = _markers(
        "داستان", "شعر", "کپشن", "متن خلاق", "دیالوگ", "سناریو", "جوک",
        "داستانک", "شعار", "اسلوگان", "story", "poem", "caption", "dialogue",
        "script idea", "joke", "slogan",
    )
    CREATIVE_OUTPUT_UNITS = _markers(
        "جمله", "کلمه", "اسم", "نام", "ایده", "عنوان", "تیتر",
        "sentence", "word", "words", "name", "names", "idea", "ideas", "title",
    )
    CREATIVE_VERBS = _markers(
        "بنویس", "بگو", "بساز", "پیشنهاد بده", "ایده بده",
        "write", "create", "make", "suggest", "propose",
    )

    REASONING = _markers(
        "تحلیل کن", "تحلیل", "استدلال", "مقایسه کن", "مقایسه", "نقد کن", "نقد",
        "ارزیابی کن", "ارزیابی", "استرس تست", "مزایا و معایب", "علتش چیه",
        "علت چیست", "علت احتمالی", "چرا", "چطور ممکنه", "چگونه ممکن است",
        "چطور می شود", "چطور می‌شود", "چگونه می شود", "چگونه می‌شود",
        "راه حل", "راه‌حل", "نقشه راه", "مقیاس پذیر", "مقیاس‌پذیر",
        "چه تغییری باید", "بررسی عمیق", "stress test", "deep analysis",
        "analyze", "analyse", "reason", "reasoning", "compare", "critique",
        "evaluate", "why", "pros and cons", "roadmap", "scalable",
    )

    CODE_TECH = _markers(
        "python", "javascript", "typescript", "rust", "c++", "sql", "css", "html",
        "پایتون", "جاوااسکریپت", "تایپ اسکریپت", "تایپ‌اسکریپت",
    )
    CODE_CONTEXT = _markers(
        "برنامه نویسی", "برنامه‌نویسی", "اسکریپت", "source code",
        "traceback", "stack trace", "regex", "git", "github", "api", "function",
        "class", "pytest", "npm", "cargo", "ریس کاندیشن", "ددلاک", "تریس بک",
        "اکسپشن", "تابع", "code",
    )
    CODE_PROBLEM = _markers(
        "باگ", "ارور", "خطا", "کرش", "دیباگ", "مشکل", "کار نمی کنه", "کار نمی‌کند",
        "اعمال نمی شود", "اعمال نمی‌شود", "render نمی شود", "render نمی‌شود",
        "compile نمی شود", "compile نمی‌شود", "match نمی کند", "match نمی‌کند",
        "کند است", "bug", "error", "crash", "debug", "deadlock", "race condition",
        "fails", "failing", "doesn't work", "does not work", "won't compile",
    )
    CODE_ACTION = _markers(
        "دیباگ کن", "بهینه کن", "رفکتور کن", "refactor", "optimize", "debug this",
        "find the bug", "fix this code",
    )

    DEVICE_VERBS = _markers(
        "باز کن", "ببند", "اجرا کن", "شروع کن", "متوقف کن", "کلیک کن", "تایپ کن",
        "نصب کن", "حذف کن", "پاک کن", "جابجا کن", "جابه جا کن", "جابه‌جا کن",
        "تغییر نام بده", "روشن کن", "خاموش کن", "کم کن", "زیاد کن", "open", "close",
        "launch", "run", "click", "type", "install", "uninstall", "delete", "remove",
        "rename", "turn on", "turn off",
    )
    DEVICE_TARGETS = _markers(
        "فایل", "پوشه", "برنامه", "نرم افزار", "نرم‌افزار", "پنجره", "عکس", "تصویر",
        "صدا", "ولوم", "میکروفون", "دوربین", "ویندوز", "کامپیوتر", "سیستم", "پریمیر",
        "فتوشاپ", "بلوتوث", "وای فای", "وای‌فای", "wifi", "wi-fi", "bluetooth",
        "premiere", "photoshop", "file", "folder", "app", "application",
        "window", "volume", "microphone", "camera", "windows", "computer",
    )

    BROWSER_VERBS = _markers(
        "برو به", "باز کن", "بازش کن", "open", "go to", "navigate to"
    )
    BROWSER_TARGETS = _markers(
        "مرورگر", "وب سایت", "وب‌سایت", "سایت", "گوگل", "کروم", "فایرفاکس",
        "ویکی پدیا", "ویکی‌پدیا", "wikipedia", "chatgpt", "claude", "gemini",
        "perplexity", "browser", "website", "chrome", "firefox",
    )

    WEB_RESEARCH = _markers(
        "در وب جستجو کن", "در وب جست و جو کن", "در اینترنت جستجو کن",
        "تو اینترنت جستجو کن", "در وب بررسی کن", "در اینترنت بررسی کن",
        "تو اینترنت بررسی کن", "در اینترنت تحقیق کن", "تو اینترنت تحقیق کن",
        "وب رو بگرد", "وب را بگرد", "سرچ کن", "search the web", "search online",
        "look it up online", "research online",
    )
    WEB_LOCATIONS = _markers("وب", "اینترنت", "online", "web")
    WEB_RESEARCH_VERBS = _markers(
        "جستجو کن", "جست و جو کن", "بررسی کن", "تحقیق کن", "بگرد",
        "search", "research", "look up", "check",
    )

    VISION_VERBS = _markers(
        "نگاه کن", "ببین", "می بینی", "می‌بینی", "بخوان", "توصیف کن",
        "از صفحه بخوان", "بررسی کن", "چک کن", "look at", "look on", "see",
        "read", "watch", "describe", "what do you see", "check", "inspect", "ocr",
    )
    VISION_SUBJECTS = _markers(
        "صفحه", "نمایشگر", "دسکتاپ", "اسکرین", "اسکرین شات", "اسکرین‌شات",
        "عکس", "تصویر", "دوربین", "وب کم", "وب‌کم", "پنجره", "screen", "display",
        "desktop", "screenshot", "image", "picture", "photo", "camera", "webcam",
        "window",
    )

    IMAGE_GEN_ACTIONS = _markers(
        "بساز", "تولید کن", "طراحی کن", "generate", "create", "make"
    )
    IMAGE_GEN_SUBJECTS = _markers(
        "عکس", "تصویر", "image", "picture", "photo"
    )

    LOCAL_STATE = _markers(
        "ساعت چند است", "ساعت چنده", "الان ساعت چنده", "تاریخ امروز",
        "امروز چندمه", "امروز چه تاریخی است", "امروز چه تاریخیه",
        "باتری چقدره", "باتری چند درصده", "درصد باتری", "مصرف cpu",
        "مصرف gpu", "مصرف ram", "حافظه آزاد", "فضای دیسک",
        "چقدر ram آزاده", "ram چقدر آزاده", "چقدر رم آزاده", "رم چقدر آزاده",
        "what time is it", "current time", "today's date", "battery percentage",
        "cpu usage", "gpu usage", "ram usage", "free memory", "disk space",
    )
    LOCAL_RESOURCE_SUBJECTS = _markers(
        "ram", "رم", "cpu", "gpu", "باتری", "حافظه", "فضای دیسک", "disk",
    )
    LOCAL_RESOURCE_QUERIES = _markers(
        "چقدره", "چقدر", "آزاده", "آزاد است", "مصرف", "درصد",
        "usage", "free", "available",
    )

    CURRENT_MARKERS = _markers(
        "آخرین", "جدیدترین", "فعلی", "در حال حاضر", "همین الان", "الان",
        "امروز", "امشب", "current", "currently", "latest", "newest",
        "today", "tonight", "right now",
    )
    VOLATILE_SUBJECTS = _markers(
        "قیمت", "هوا", "آب و هوا", "خبر", "اخبار", "بورس", "بازار",
        "نتیجه بازی", "امتیاز بازی", "weather", "price", "stock price",
        "news", "score", "market",
    )
    FACTUAL_QUERIES = _markers(
        "چیست", "چیه", "کیست", "کیه", "کی بود", "چه کسی بود", "کجاست",
        "چنده", "چند است", "چه کسی", "چه فرقی", "چه تفاوتی", "فرق",
        "تعریف", "درسته", "درست است", "آیا", "چه شرکتیه", "چه شرکتی است",
        "what is", "who is", "who was", "where is", "how many",
        "difference between", "is it true", "what company",
    )
    CURRENT_QUERIES = _markers(
        "چطوره", "چطور است", "چنده", "چند است", "چیست", "چیه", "کیه",
        "کجاست", "how is", "what is", "who is", "where is",
    )
    STABLE_SUBJECTS = _markers(
        "پایتخت", "کشور", "سیاره", "ماه طبیعی", "عنصر شیمیایی", "دمای جوش",
        "نقطه جوش", "تاریخچه", "تعریف", "capital of", "planet",
        "chemical element", "boiling point", "history of",
    )
    FACTUAL_SHAPE_RE = re.compile(
        r"(?:^|\s)چند\s+.+?(?:داریم|وجود دارد|وجود داره)(?:\s|\?|$)"
    )

    DESTRUCTIVE = _markers(
        "حذف کن", "پاک کن", "فرمت کن", "delete", "remove", "erase", "format"
    )
    VAGUE_REFERENCES = _markers(
        "اون", "اونو", "اون رو", "این رو", "همونو", "that", "that one", "it"
    )
    CONCRETE_TARGETS = _markers(
        "فایل", "پوشه", "عکس", "تصویر", "برنامه", "پنجره", "پیام", "ایمیل",
        "file", "folder", "photo", "image", "app", "window", "message", "email",
    )

    URL_RE = re.compile(
        r"(?:https?://|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|ai|ir)\b)",
        re.I,
    )

    PRIMARY_PRIORITY = (
        IntentType.DIRECT_DETERMINISTIC,
        IntentType.UNKNOWN,
        IntentType.DEVICE_ACTION,
        IntentType.BROWSER_ACTION,
        IntentType.IMAGE_GENERATION,
        IntentType.VISION_ANALYSIS,
        IntentType.WEB_RESEARCH,
        IntentType.LOCAL_STATE,
        IntentType.KNOWLEDGE_CURRENT,
        IntentType.CODE,
        IntentType.REASONING,
        IntentType.KNOWLEDGE_STABLE,
        IntentType.LANGUAGE_TRANSFORM,
        IntentType.CREATIVE_LANGUAGE,
        IntentType.CASUAL_LANGUAGE,
    )

    def classify(
        self,
        user_input: str | RoutingInput,
    ) -> IntentDecision:
        data = (
            user_input
            if isinstance(user_input, RoutingInput)
            else RoutingInput.from_text(user_input)
        )
        text = data.lower_text

        if not text:
            return self._decision(
                {IntentType.UNKNOWN},
                ["empty_input"],
                confidence=0.35,
                risk=AccuracyRisk.LOW,
            )

        if self._ambiguous_destructive(text):
            return self._decision(
                {IntentType.UNKNOWN},
                ["ambiguous_destructive_action"],
                confidence=0.99,
                risk=AccuracyRisk.HIGH,
            )

        intents: set[IntentType] = set()
        signals: list[str] = []

        self._add(
            intents,
            signals,
            solve_simple_arithmetic(data.raw_text) is not None,
            IntentType.DIRECT_DETERMINISTIC,
            "arithmetic_fast_path_match",
        )
        self._add(
            intents,
            signals,
            self._image_generation(text),
            IntentType.IMAGE_GENERATION,
            "image_generation_subject_plus_action",
        )
        self._add(
            intents,
            signals,
            self._vision(text),
            IntentType.VISION_ANALYSIS,
            "vision_verb_plus_subject",
        )

        browser_action = self._browser_action(text)
        self._add(
            intents,
            signals,
            browser_action,
            IntentType.BROWSER_ACTION,
            "browser_action_verb_plus_target",
        )
        self._add(
            intents,
            signals,
            not browser_action and self._device_action(text),
            IntentType.DEVICE_ACTION,
            "device_action_verb_plus_target",
        )

        self._add(
            intents,
            signals,
            self._local_state(text),
            IntentType.LOCAL_STATE,
            "local_state_query",
        )
        self._add(
            intents,
            signals,
            self._web_research(text),
            IntentType.WEB_RESEARCH,
            "explicit_web_research",
        )
        self._add(
            intents,
            signals,
            self._current_knowledge(text, intents),
            IntentType.KNOWLEDGE_CURRENT,
            "volatile_or_current_information",
        )

        reasoning = self._reasoning(data)

        self._add(
            intents,
            signals,
            self._code(data, reasoning),
            IntentType.CODE,
            "code_or_debug_context",
        )
        self._add(
            intents,
            signals,
            reasoning,
            IntentType.REASONING,
            "reasoning_marker",
        )
        self._add(
            intents,
            signals,
            self._has(text, self.LANGUAGE_TRANSFORM),
            IntentType.LANGUAGE_TRANSFORM,
            "language_transform_marker",
        )
        self._add(
            intents,
            signals,
            self._creative(text),
            IntentType.CREATIVE_LANGUAGE,
            "creative_language_request",
        )
        self._add(
            intents,
            signals,
            self._stable_knowledge(text, intents),
            IntentType.KNOWLEDGE_STABLE,
            "stable_knowledge_query",
        )
        self._add(
            intents,
            signals,
            self._casual(text),
            IntentType.CASUAL_LANGUAGE,
            "casual_language_marker",
        )

        if not intents:
            intents.add(IntentType.UNKNOWN)
            signals.append("no_confident_capability_match")

        return self._decision(
            intents,
            signals,
        )

    def _decision(
        self,
        intents: set[IntentType],
        signals: list[str],
        *,
        confidence: float | None = None,
        risk: AccuracyRisk | None = None,
    ) -> IntentDecision:
        ordered = tuple(
            intent
            for intent in self.PRIMARY_PRIORITY
            if intent in intents
        )
        primary = ordered[0]

        return IntentDecision(
            primary_intent=primary,
            required_intents=ordered,
            confidence=(
                self._confidence(
                    primary,
                    intents,
                    signals,
                )
                if confidence is None
                else confidence
            ),
            accuracy_risk=(
                self._risk(intents)
                if risk is None
                else risk
            ),
            signals=tuple(signals),
        )

    @staticmethod
    def _add(
        intents: set[IntentType],
        signals: list[str],
        condition: bool,
        intent: IntentType,
        signal: str,
    ) -> None:
        if condition:
            intents.add(intent)
            signals.append(signal)

    def _vision(self, text: str) -> bool:
        return (
            self._has(text, self.VISION_VERBS)
            and self._has(text, self.VISION_SUBJECTS)
        )

    def _image_generation(self, text: str) -> bool:
        return (
            self._has(text, self.IMAGE_GEN_ACTIONS)
            and self._has(text, self.IMAGE_GEN_SUBJECTS)
        )

    def _browser_action(self, text: str) -> bool:
        target = (
            self._has(text, self.BROWSER_TARGETS)
            or bool(self.URL_RE.search(text))
        )
        return (
            target
            and self._has(text, self.BROWSER_VERBS)
        )

    def _device_action(self, text: str) -> bool:
        return (
            self._has(text, self.DEVICE_VERBS)
            and self._has(text, self.DEVICE_TARGETS)
        )

    def _local_state(self, text: str) -> bool:
        if self._has(text, self.LOCAL_STATE):
            return True

        return (
            self._has(text, self.LOCAL_RESOURCE_SUBJECTS)
            and self._has(text, self.LOCAL_RESOURCE_QUERIES)
        )

    def _web_research(self, text: str) -> bool:
        if self._has(text, self.WEB_RESEARCH):
            return True

        return (
            self._has(text, self.WEB_LOCATIONS)
            and self._has(text, self.WEB_RESEARCH_VERBS)
        )

    def _current_knowledge(
        self,
        text: str,
        intents: set[IntentType],
    ) -> bool:
        if (
            IntentType.LOCAL_STATE in intents
            or IntentType.WEB_RESEARCH in intents
        ):
            return False

        has_current = self._has(
            text,
            self.CURRENT_MARKERS,
        )
        has_volatile = self._has(
            text,
            self.VOLATILE_SUBJECTS,
        )
        has_query = (
            self._has(text, self.FACTUAL_QUERIES)
            or self._has(text, self.CURRENT_QUERIES)
        )

        if has_volatile and (
            has_current
            or has_query
        ):
            return True

        if has_current and (
            has_query
            or "?" in text
        ):
            return True

        return False

    def _reasoning(
        self,
        data: RoutingInput,
    ) -> bool:
        return (
            self._has(
                data.lower_text,
                self.REASONING,
            )
            or "اگر" in data.words
            or "if" in data.words
        )

    def _code(
        self,
        data: RoutingInput,
        reasoning: bool,
    ) -> bool:
        text = data.lower_text

        tech = self._has(
            text,
            self.CODE_TECH,
        )

        explicit_code_word = (
            "کد" in data.words
            or "code" in data.words
        )

        context = (
            explicit_code_word
            or self._has(
                text,
                self.CODE_CONTEXT,
            )
        )

        problem_or_action = (
            self._has(
                text,
                self.CODE_PROBLEM,
            )
            or self._has(
                text,
                self.CODE_ACTION,
            )
        )

        return (
            (
                context
                and (
                    problem_or_action
                    or reasoning
                )
            )
            or (
                tech
                and problem_or_action
            )
        )

    def _creative(self, text: str) -> bool:
        has_verb = self._has(
            text,
            self.CREATIVE_VERBS,
        )

        if not has_verb:
            return False

        return (
            self._has(
                text,
                self.CREATIVE_SUBJECTS,
            )
            or self._has(
                text,
                self.CREATIVE_OUTPUT_UNITS,
            )
        )

    def _stable_knowledge(
        self,
        text: str,
        intents: set[IntentType],
    ) -> bool:
        if (
            IntentType.KNOWLEDGE_CURRENT in intents
            or IntentType.LOCAL_STATE in intents
        ):
            return False

        if self._has(
            text,
            self.ASSISTANT_IDENTITY,
        ):
            return False

        return (
            self._has(
                text,
                self.FACTUAL_QUERIES,
            )
            or self._has(
                text,
                self.STABLE_SUBJECTS,
            )
            or bool(
                self.FACTUAL_SHAPE_RE.search(text)
            )
        )

    def _casual(self, text: str) -> bool:
        return any(
            self._has(
                text,
                markers,
            )
            for markers in (
                self.GREETINGS,
                self.THANKS,
                self.PERSONAL_STATE,
                self.ASSISTANT_IDENTITY,
            )
        )

    def _ambiguous_destructive(
        self,
        text: str,
    ) -> bool:
        return (
            self._has(
                text,
                self.DESTRUCTIVE,
            )
            and self._has(
                text,
                self.VAGUE_REFERENCES,
            )
            and not self._has(
                text,
                self.CONCRETE_TARGETS,
            )
        )

    @staticmethod
    def _risk(
        intents: set[IntentType],
    ) -> AccuracyRisk:
        if intents & {
            IntentType.KNOWLEDGE_CURRENT,
            IntentType.WEB_RESEARCH,
            IntentType.CODE,
            IntentType.REASONING,
            IntentType.UNKNOWN,
        }:
            return AccuracyRisk.HIGH

        if intents & {
            IntentType.KNOWLEDGE_STABLE,
            IntentType.VISION_ANALYSIS,
            IntentType.DEVICE_ACTION,
            IntentType.BROWSER_ACTION,
            IntentType.IMAGE_GENERATION,
        }:
            return AccuracyRisk.MEDIUM

        return AccuracyRisk.LOW

    @staticmethod
    def _confidence(
        primary: IntentType,
        intents: set[IntentType],
        signals: list[str],
    ) -> float:
        if primary is IntentType.DIRECT_DETERMINISTIC:
            return 0.995

        if primary is IntentType.UNKNOWN:
            return 0.35

        strong = {
            "vision_verb_plus_subject",
            "browser_action_verb_plus_target",
            "device_action_verb_plus_target",
            "image_generation_subject_plus_action",
            "explicit_web_research",
            "local_state_query",
        }

        if any(
            signal in strong
            for signal in signals
        ):
            base = 0.97

        elif intents & {
            IntentType.KNOWLEDGE_CURRENT,
            IntentType.KNOWLEDGE_STABLE,
            IntentType.CODE,
            IntentType.REASONING,
        }:
            base = 0.90

        else:
            base = 0.94

        if len(intents) > 1:
            base -= min(
                0.06,
                0.015 * (
                    len(intents) - 1
                ),
            )

        return round(
            max(
                0.50,
                min(
                    0.995,
                    base,
                ),
            ),
            3,
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

