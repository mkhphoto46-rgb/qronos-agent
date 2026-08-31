from __future__ import annotations

import unittest

from core.arithmetic_fast_path import (
    integer_to_persian,
    solve_simple_arithmetic,
)


class ArithmeticFastPathTests(unittest.TestCase):
    def test_persian_addition(self) -> None:
        answer = solve_simple_arithmetic(
            "دو به علاوه دو چند میشه؟"
        )

        self.assertIsNotNone(answer)
        assert answer is not None

        self.assertEqual(answer.result, 4)
        self.assertEqual(
            answer.spoken_text,
            "می‌شود چهار.",
        )

    def test_compact_persian_addition(self) -> None:
        answer = solve_simple_arithmetic(
            "دو بعلاوه دو چنده"
        )

        self.assertIsNotNone(answer)
        assert answer is not None

        self.assertEqual(answer.result, 4)
        self.assertEqual(
            answer.spoken_text,
            "می‌شود چهار.",
        )

    def test_real_stt_dobe_alavi_variant(self) -> None:
        answer = solve_simple_arithmetic(
            "دوبه علاوی دو چند میشه؟"
        )

        self.assertIsNotNone(answer)
        assert answer is not None

        self.assertEqual(answer.result, 4)
        self.assertEqual(
            answer.spoken_text,
            "می‌شود چهار.",
        )

    def test_ba_alavi_variant(self) -> None:
        answer = solve_simple_arithmetic(
            "دو بعلاوی دو چند میشه؟"
        )

        self.assertIsNotNone(answer)
        assert answer is not None

        self.assertEqual(answer.result, 4)

    def test_persian_digits(self) -> None:
        answer = solve_simple_arithmetic(
            "۲ + ۲ چند میشه؟"
        )

        self.assertIsNotNone(answer)
        assert answer is not None

        self.assertEqual(answer.result, 4)
        self.assertEqual(
            answer.spoken_text,
            "می‌شود چهار.",
        )

    def test_subtraction(self) -> None:
        answer = solve_simple_arithmetic(
            "پنج منهای سه چند میشه؟"
        )

        self.assertIsNotNone(answer)
        assert answer is not None

        self.assertEqual(answer.result, 2)
        self.assertEqual(
            answer.spoken_text,
            "می‌شود دو.",
        )

    def test_multiplication(self) -> None:
        answer = solve_simple_arithmetic(
            "شش ضربدر هفت چند میشه؟"
        )

        self.assertIsNotNone(answer)
        assert answer is not None

        self.assertEqual(answer.result, 42)
        self.assertEqual(
            answer.spoken_text,
            "می‌شود چهل و دو.",
        )

    def test_division(self) -> None:
        answer = solve_simple_arithmetic(
            "هشت تقسیم بر دو چند میشه؟"
        )

        self.assertIsNotNone(answer)
        assert answer is not None

        self.assertEqual(answer.result, 4)
        self.assertEqual(
            answer.spoken_text,
            "می‌شود چهار.",
        )

    def test_divide_by_zero(self) -> None:
        answer = solve_simple_arithmetic(
            "هشت تقسیم بر صفر چند میشه؟"
        )

        self.assertIsNotNone(answer)
        assert answer is not None

        self.assertEqual(
            answer.spoken_text,
            "تقسیم بر صفر تعریف نشده است.",
        )

    def test_non_arithmetic_does_not_match(self) -> None:
        answer = solve_simple_arithmetic(
            "اسم تو چیه؟"
        )

        self.assertIsNone(answer)

    def test_larger_persian_numbers(self) -> None:
        answer = solve_simple_arithmetic(
            "بیست و سه به علاوه نوزده چند میشه؟"
        )

        self.assertIsNotNone(answer)
        assert answer is not None

        self.assertEqual(answer.result, 42)
        self.assertEqual(
            answer.spoken_text,
            "می‌شود چهل و دو.",
        )

    def test_integer_to_persian(self) -> None:
        self.assertEqual(
            integer_to_persian(42),
            "چهل و دو",
        )


if __name__ == "__main__":
    unittest.main()