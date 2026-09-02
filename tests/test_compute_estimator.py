from __future__ import annotations

import unittest

from core.compute_estimator import ComputeEstimator, ComputeLevel
from core.routing_input import RoutingInput


class ComputeEstimatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.estimator = ComputeEstimator()

    def level(self, text: str) -> ComputeLevel:
        return self.estimator.estimate(text).level

    def test_empty_input_is_low_compute(self) -> None:
        result = self.estimator.estimate("")
        self.assertEqual(result.level, ComputeLevel.FAST)
        self.assertEqual(result.score, 0)

    def test_simple_arithmetic_needs_no_brain_compute(self) -> None:
        result = self.estimator.estimate("دو به علاوه دو چند میشه؟")
        self.assertEqual(result.level, ComputeLevel.NONE)
        self.assertIn("direct_arithmetic", result.factors)

    def test_adversarial_arithmetic_explanation_is_not_direct(self) -> None:
        result = self.estimator.estimate("چرا دو به علاوه دو همیشه چهار میشه؟")
        self.assertNotEqual(result.level, ComputeLevel.NONE)

    def test_greeting_is_fast(self) -> None:
        self.assertEqual(self.level("سلام کرونوس"), ComputeLevel.FAST)

    def test_simple_rewrite_is_fast(self) -> None:
        self.assertEqual(
            self.level("این جمله رو دوستانه‌تر کن"),
            ComputeLevel.FAST,
        )

    def test_short_stable_fact_is_raw_fast_compute(self) -> None:
        # Capability routing is Intent Gate / Resolver work. The estimator only
        # says this is computationally cheap.
        self.assertEqual(
            self.level("پایتخت کانادا چیه؟"),
            ComputeLevel.FAST,
        )

    def test_short_current_fact_is_raw_fast_compute(self) -> None:
        self.assertEqual(
            self.level("قیمت امروز دلار چنده؟"),
            ComputeLevel.FAST,
        )

    def test_simple_why_question_is_heavy_eco(self) -> None:
        self.assertEqual(
            self.level("چرا آسمان آبی است؟"),
            ComputeLevel.HEAVY_ECO,
        )

    def test_comparison_is_heavy_eco(self) -> None:
        self.assertEqual(
            self.level("RAM و VRAM چه تفاوتی دارند؟"),
            ComputeLevel.HEAVY_ECO,
        )

    def test_code_debug_is_heavy_normal(self) -> None:
        result = self.estimator.estimate(
            "این کد Python چرا deadlock میده؟ خطاش رو دیباگ کن"
        )
        self.assertEqual(result.level, ComputeLevel.HEAVY_NORMAL)
        self.assertIn("code_debug_combination", result.factors)

    def test_architecture_stress_test_is_heavy_deep(self) -> None:
        result = self.estimator.estimate(
            "این معماری سیستم را عمیق استرس تست کن، محدودیت‌های latency و VRAM "
            "را مقایسه کن و مرحله به مرحله یک راه حل جامع پیشنهاد بده"
        )
        self.assertEqual(result.level, ComputeLevel.HEAVY_DEEP)

    def test_exact_five_words_does_not_over_escalate(self) -> None:
        self.assertEqual(
            self.level("دقیقاً پنج کلمه درباره باران بنویس"),
            ComputeLevel.FAST,
        )

    def test_structured_short_output_is_not_deep(self) -> None:
        result = self.estimator.estimate(
            "تفاوت RAM و VRAM را در یک جدول کوتاه بگو"
        )
        self.assertIn(
            result.level,
            {ComputeLevel.HEAVY_ECO, ComputeLevel.HEAVY_NORMAL},
        )
        self.assertNotEqual(result.level, ComputeLevel.HEAVY_DEEP)

    def test_long_input_adds_compute_pressure(self) -> None:
        short = self.estimator.estimate("این متن را بررسی کن")
        long_text = " ".join(["این متن را بررسی کن"] * 60)
        long = self.estimator.estimate(long_text)
        self.assertGreater(long.score, short.score)

    def test_more_constraints_do_not_reduce_score(self) -> None:
        simple = self.estimator.estimate("این موضوع را تحلیل کن")
        constrained = self.estimator.estimate(
            "این موضوع را تحلیل کن و فقط سه گزینه بده، بدون تکرار، "
            "حداکثر دو جمله برای هر گزینه و با فرمت جدول"
        )
        self.assertGreater(constrained.score, simple.score)

    def test_routing_input_can_be_reused_without_renormalising(self) -> None:
        data = RoutingInput.from_text("این کد Python چرا crash می‌کنه؟")
        first = self.estimator.estimate(data)
        second = self.estimator.estimate(data)
        self.assertEqual(first, second)

    def test_persian_and_arabic_keyboard_variants_match(self) -> None:
        persian = self.estimator.estimate("این کد چرا خطا میده؟")
        arabic = self.estimator.estimate("اين كد چرا خطا ميده؟")
        self.assertEqual(persian.level, arabic.level)
        self.assertEqual(persian.score, arabic.score)

    def test_decision_fields_are_bounded(self) -> None:
        result = self.estimator.estimate("سلام")
        self.assertGreaterEqual(result.score, 0)
        self.assertLessEqual(result.score, 100)
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertLessEqual(result.confidence, 1.0)
        self.assertTrue(result.factors)


class ComputeEstimatorStressRegressionTests(unittest.TestCase):
    """Compute regressions promoted from the adversarial corpus."""

    def setUp(self) -> None:
        self.estimator = ComputeEstimator()

    def test_reasoning_phrasings_do_not_undercompute(self) -> None:
        for text in (
            "علت احتمالی این مشکل چیست؟",
            "اگر منابع نصف شود چه تغییری باید بدهیم؟",
            "چطور می‌شود این سیستم را مقیاس‌پذیر کرد؟",
        ):
            with self.subTest(text=text):
                result = self.estimator.estimate(text)
                self.assertIn(
                    result.level,
                    {
                        ComputeLevel.HEAVY_ECO,
                        ComputeLevel.HEAVY_NORMAL,
                        ComputeLevel.HEAVY_DEEP,
                    },
                    msg=(text, result),
                )

    def test_conditional_if_is_a_word_not_a_persian_substring(self) -> None:
        result = self.estimator.estimate("این پاراگراف رو خلاصه کن")
        self.assertEqual(result.level, ComputeLevel.FAST)
        self.assertNotIn("conditional_reasoning", result.factors)

    def test_creative_paragraph_is_not_false_conditional(self) -> None:
        result = self.estimator.estimate(
            "یک داستان ترسناک یک پاراگرافی بساز"
        )
        self.assertEqual(result.level, ComputeLevel.FAST)
        self.assertNotIn("conditional_reasoning", result.factors)

    def test_kodam_does_not_trigger_persian_code_substring(self) -> None:
        result = self.estimator.estimate(
            "بین این دو گزینه کدام منطقی‌تر است و چرا؟"
        )
        self.assertEqual(result.level, ComputeLevel.HEAVY_ECO)
        self.assertNotIn("code_context", result.factors)

    def test_css_html_debugging_gets_code_compute(self) -> None:
        for text in (
            "این CSS چرا اعمال نمی‌شود؟",
            "این HTML چرا درست render نمی‌شود؟",
        ):
            with self.subTest(text=text):
                result = self.estimator.estimate(text)
                self.assertIn("code_context", result.factors, msg=(text, result))
                self.assertIn("debugging_context", result.factors, msg=(text, result))


if __name__ == "__main__":
    unittest.main()

