from __future__ import annotations

import unittest

from core.intent_gate import AccuracyRisk, IntentGate, IntentType
from core.routing_input import RoutingInput


class IntentGateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = IntentGate()

    def assert_route(
        self,
        text: str,
        primary: IntentType,
        required: tuple[IntentType, ...] = (),
        forbidden: tuple[IntentType, ...] = (),
    ) -> None:
        decision = self.gate.classify(text)
        self.assertIs(
            decision.primary_intent,
            primary,
            msg=(text, decision),
        )

        for intent in required:
            self.assertIn(
                intent,
                decision.required_intents,
                msg=(text, decision),
            )

        for intent in forbidden:
            self.assertNotIn(
                intent,
                decision.required_intents,
                msg=(text, decision),
            )

    def test_empty_is_unknown(self) -> None:
        decision = self.gate.classify("")
        self.assertIs(decision.primary_intent, IntentType.UNKNOWN)

    def test_greeting_is_casual(self) -> None:
        self.assert_route(
            "سلام کرونوس",
            IntentType.CASUAL_LANGUAGE,
        )

    def test_personal_state_is_casual_not_current_knowledge(self) -> None:
        self.assert_route(
            "امروز خیلی خسته‌ام و حوصله ندارم",
            IntentType.CASUAL_LANGUAGE,
            forbidden=(IntentType.KNOWLEDGE_CURRENT,),
        )

    def test_assistant_identity_is_casual(self) -> None:
        self.assert_route(
            "اسمت چیه؟",
            IntentType.CASUAL_LANGUAGE,
            forbidden=(IntentType.KNOWLEDGE_STABLE,),
        )

    def test_simple_rewrite_is_language_transform(self) -> None:
        self.assert_route(
            "این جمله رو دوستانه‌تر کن",
            IntentType.LANGUAGE_TRANSFORM,
        )

    def test_translation_is_language_transform(self) -> None:
        self.assert_route(
            "این جمله رو به انگلیسی ترجمه کن",
            IntentType.LANGUAGE_TRANSFORM,
        )

    def test_short_story_is_creative_language(self) -> None:
        self.assert_route(
            "یه داستان کوتاه سه جمله‌ای بگو",
            IntentType.CREATIVE_LANGUAGE,
        )

    def test_stable_fact_is_knowledge(self) -> None:
        self.assert_route(
            "پایتخت کانادا چیه؟",
            IntentType.KNOWLEDGE_STABLE,
        )

    def test_current_price_is_current_knowledge(self) -> None:
        self.assert_route(
            "قیمت امروز دلار چنده؟",
            IntentType.KNOWLEDGE_CURRENT,
            forbidden=(IntentType.KNOWLEDGE_STABLE,),
        )

    def test_latest_version_is_current_knowledge(self) -> None:
        self.assert_route(
            "آخرین نسخه Blender چیه؟",
            IntentType.KNOWLEDGE_CURRENT,
        )

    def test_weather_is_current_knowledge(self) -> None:
        self.assert_route(
            "هوا چطوره؟",
            IntentType.KNOWLEDGE_CURRENT,
        )

    def test_explicit_web_research(self) -> None:
        self.assert_route(
            "این موضوع رو در وب بررسی کن",
            IntentType.WEB_RESEARCH,
        )

    def test_google_company_question_is_not_browser_action(self) -> None:
        self.assert_route(
            "گوگل چه شرکتیه؟",
            IntentType.KNOWLEDGE_STABLE,
            forbidden=(IntentType.BROWSER_ACTION,),
        )

    def test_google_open_is_browser_action(self) -> None:
        self.assert_route(
            "گوگل رو باز کن",
            IntentType.BROWSER_ACTION,
        )

    def test_url_navigation_is_browser_action(self) -> None:
        self.assert_route(
            "برو به https://example.com",
            IntentType.BROWSER_ACTION,
        )

    def test_premiere_open_is_device_action(self) -> None:
        self.assert_route(
            "پریمیر رو باز کن",
            IntentType.DEVICE_ACTION,
        )

    def test_file_open_is_device_action(self) -> None:
        self.assert_route(
            "این فایل رو باز کن",
            IntentType.DEVICE_ACTION,
        )

    def test_file_summary_is_not_device_action(self) -> None:
        self.assert_route(
            "این فایل رو خلاصه کن",
            IntentType.LANGUAGE_TRANSFORM,
            forbidden=(IntentType.DEVICE_ACTION,),
        )

    def test_photo_open_is_device_action_not_vision(self) -> None:
        self.assert_route(
            "این عکس رو باز کن",
            IntentType.DEVICE_ACTION,
            forbidden=(IntentType.VISION_ANALYSIS,),
        )

    def test_photo_look_is_vision(self) -> None:
        self.assert_route(
            "به این عکس نگاه کن",
            IntentType.VISION_ANALYSIS,
        )

    def test_screen_error_analysis_is_multilabel(self) -> None:
        self.assert_route(
            "این ارور روی صفحه رو ببین و تحلیل کن چرا اتفاق افتاده",
            IntentType.VISION_ANALYSIS,
            required=(
                IntentType.VISION_ANALYSIS,
                IntentType.REASONING,
            ),
        )

    def test_image_generation_is_not_vision(self) -> None:
        self.assert_route(
            "یک تصویر از یک شهر آینده بساز",
            IntentType.IMAGE_GENERATION,
            forbidden=(IntentType.VISION_ANALYSIS,),
        )

    def test_arithmetic_is_direct(self) -> None:
        self.assert_route(
            "دو به علاوه دو چند میشه؟",
            IntentType.DIRECT_DETERMINISTIC,
        )

    def test_arithmetic_explanation_is_not_direct(self) -> None:
        decision = self.gate.classify(
            "چرا دو به علاوه دو همیشه چهار میشه؟"
        )
        self.assertNotIn(
            IntentType.DIRECT_DETERMINISTIC,
            decision.required_intents,
        )
        self.assertIn(
            IntentType.REASONING,
            decision.required_intents,
        )

    def test_local_time_is_local_state(self) -> None:
        self.assert_route(
            "ساعت چند است؟",
            IntentType.LOCAL_STATE,
            forbidden=(IntentType.KNOWLEDGE_STABLE,),
        )

    def test_battery_is_local_state(self) -> None:
        self.assert_route(
            "باتری چند درصده؟",
            IntentType.LOCAL_STATE,
        )

    def test_code_debug_is_code_and_reasoning(self) -> None:
        self.assert_route(
            "این کد Python چرا deadlock میده؟ تحلیلش کن",
            IntentType.CODE,
            required=(IntentType.CODE, IntentType.REASONING),
        )

    def test_ram_vram_difference_is_stable_knowledge(self) -> None:
        decision = self.gate.classify("RAM و VRAM چه فرقی دارن؟")
        self.assertIn(
            IntentType.KNOWLEDGE_STABLE,
            decision.required_intents,
        )

    def test_false_premise_is_knowledge(self) -> None:
        self.assert_route(
            "زمین دو ماه طبیعی دارد، درسته؟",
            IntentType.KNOWLEDGE_STABLE,
        )

    def test_ambiguous_destructive_action_is_unknown_high_risk(self) -> None:
        decision = self.gate.classify("اون رو پاک کن")
        self.assertIs(decision.primary_intent, IntentType.UNKNOWN)
        self.assertIs(decision.accuracy_risk, AccuracyRisk.HIGH)
        self.assertIn(
            "ambiguous_destructive_action",
            decision.signals,
        )

    def test_concrete_delete_is_device_action(self) -> None:
        self.assert_route(
            "این فایل رو پاک کن",
            IntentType.DEVICE_ACTION,
        )

    def test_mixed_greeting_and_fact_keeps_fact(self) -> None:
        decision = self.gate.classify("سلام، پایتخت ژاپن چیه؟")
        self.assertIs(
            decision.primary_intent,
            IntentType.KNOWLEDGE_STABLE,
        )
        self.assertIn(
            IntentType.CASUAL_LANGUAGE,
            decision.required_intents,
        )

    def test_persian_arabic_letter_variants_match(self) -> None:
        a = self.gate.classify("پريمير را باز كن")
        b = self.gate.classify("پریمیر را باز کن")
        self.assertEqual(a.primary_intent, b.primary_intent)

    def test_shared_routing_input_produces_same_decision(self) -> None:
        raw = "به این عکس نگاه کن"
        prepared = RoutingInput.from_text(raw)
        self.assertEqual(
            self.gate.classify(raw),
            self.gate.classify(prepared),
        )


class IntentGateStressRegressionTests(unittest.TestCase):
    """Regressions promoted from the 210-case adversarial corpus."""

    def setUp(self) -> None:
        self.gate = IntentGate()

    def assert_primary(self, text: str, expected: IntentType) -> None:
        decision = self.gate.classify(text)
        self.assertIs(decision.primary_intent, expected, msg=(text, decision))

    def test_language_transform_variants(self) -> None:
        for text in (
            "این رو روان‌تر بنویس",
            "لحنش رو حرفه‌ای‌تر کن",
            "این کپشن رو کوتاه کن",
            "این پیام رو مودبانه‌تر کن",
        ):
            with self.subTest(text=text):
                self.assert_primary(text, IntentType.LANGUAGE_TRANSFORM)

    def test_generic_creative_generation_variants(self) -> None:
        for text in (
            "سه اسم برای یک پادکست پیشنهاد بده",
            "پنج ایده برای اسم برند پیشنهاد بده",
            "دقیقاً پنج کلمه درباره باران بنویس",
            "دو جمله درباره امید بنویس",
            "یک جمله انگیزشی بساز",
        ):
            with self.subTest(text=text):
                self.assert_primary(text, IntentType.CREATIVE_LANGUAGE)

    def test_stable_knowledge_shapes(self) -> None:
        for text in (
            "نقطه جوش آب چند درجه است؟",
            "آلبرت اینشتین کی بود؟",
            "چند قاره روی زمین داریم؟",
            "Python چیست؟",
        ):
            with self.subTest(text=text):
                self.assert_primary(text, IntentType.KNOWLEDGE_STABLE)

    def test_python_definition_is_not_a_code_task(self) -> None:
        decision = self.gate.classify("Python چیست؟")
        self.assertNotIn(IntentType.CODE, decision.required_intents)

    def test_current_knowledge_variants(self) -> None:
        for text in (
            "بورس امروز چطور بوده؟",
            "latest version of Blender?",
            "weather today in Tehran?",
        ):
            with self.subTest(text=text):
                self.assert_primary(text, IntentType.KNOWLEDGE_CURRENT)

    def test_local_date_beats_current_knowledge(self) -> None:
        for text in (
            "امروز چه تاریخی است؟",
            "تاریخ امروز چیه؟",
        ):
            with self.subTest(text=text):
                decision = self.gate.classify(text)
                self.assertIs(decision.primary_intent, IntentType.LOCAL_STATE)
                self.assertNotIn(
                    IntentType.KNOWLEDGE_CURRENT,
                    decision.required_intents,
                )

    def test_reasoning_variants(self) -> None:
        for text in (
            "مزایا و معایب این معماری را بگو",
            "علت احتمالی این مشکل چیست؟",
            "برای این پروژه یک نقشه راه پیشنهاد بده",
            "اگر منابع نصف شود چه تغییری باید بدهیم؟",
            "چطور می‌شود این سیستم را مقیاس‌پذیر کرد؟",
        ):
            with self.subTest(text=text):
                self.assert_primary(text, IntentType.REASONING)

    def test_code_context_variants(self) -> None:
        for text in (
            "این traceback رو تحلیل کن",
            "این API چرا 500 میده؟",
            "این تست pytest چرا fail میشه؟",
            "این کد Rust چرا compile نمیشه؟",
            "این CSS چرا اعمال نمی‌شود؟",
            "این HTML چرا درست render نمی‌شود؟",
        ):
            with self.subTest(text=text):
                self.assert_primary(text, IntentType.CODE)

    def test_persian_short_code_marker_does_not_match_inside_kodam(self) -> None:
        decision = self.gate.classify(
            "بین این دو گزینه کدام منطقی‌تر است و چرا؟"
        )
        self.assertIs(decision.primary_intent, IntentType.REASONING)
        self.assertNotIn(IntentType.CODE, decision.required_intents)

    def test_device_aliases(self) -> None:
        for text in (
            "بلوتوث رو روشن کن",
            "وای فای رو خاموش کن",
        ):
            with self.subTest(text=text):
                self.assert_primary(text, IntentType.DEVICE_ACTION)

    def test_browser_alias(self) -> None:
        self.assert_primary("ویکی‌پدیا رو باز کن", IntentType.BROWSER_ACTION)

    def test_web_research_variants(self) -> None:
        for text in (
            "تو اینترنت بررسی کن این خبر درسته یا نه",
            "research this online",
            "در اینترنت درباره این موضوع تحقیق کن",
        ):
            with self.subTest(text=text):
                self.assert_primary(text, IntentType.WEB_RESEARCH)

    def test_vision_aliases(self) -> None:
        for text in (
            "این اسکرین‌شات رو بررسی کن",
            "اسکرین رو نگاه کن",
        ):
            with self.subTest(text=text):
                self.assert_primary(text, IntentType.VISION_ANALYSIS)

    def test_local_resource_query(self) -> None:
        self.assert_primary("چقدر RAM آزاده؟", IntentType.LOCAL_STATE)

    def test_paragraph_does_not_trigger_conditional_reasoning_substring(self) -> None:
        decision = self.gate.classify("این پاراگراف رو خلاصه کن")
        self.assertIs(decision.primary_intent, IntentType.LANGUAGE_TRANSFORM)
        self.assertNotIn(IntentType.REASONING, decision.required_intents)

    def test_creative_paragraph_does_not_trigger_conditional_reasoning(self) -> None:
        decision = self.gate.classify(
            "یک داستان ترسناک یک پاراگرافی بساز"
        )
        self.assertIs(decision.primary_intent, IntentType.CREATIVE_LANGUAGE)
        self.assertNotIn(IntentType.REASONING, decision.required_intents)


if __name__ == "__main__":
    unittest.main()

