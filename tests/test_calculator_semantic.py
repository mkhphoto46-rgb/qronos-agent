from __future__ import annotations

import unittest

from core.calculator_semantic import (
    CalculatorSemanticParseError,
    parse_calculator_semantic_command,
)


class CalculatorSemanticTests(unittest.TestCase):
    def test_persian_multiply(self) -> None:
        plan = parse_calculator_semantic_command(
            "\u0645\u0627\u0634\u06cc\u0646 \u062d\u0633\u0627\u0628 \u062f\u0648 \u0631\u0648 \u0636\u0631\u0628\u062f\u0631 \u0686\u0647\u0627\u0631 \u06a9\u0646"
        )

        self.assertEqual(
            plan.result_text,
            "8",
        )

        self.assertEqual(
            plan.expected_display,
            "Display is 8",
        )

    def test_persian_plus(self) -> None:
        plan = parse_calculator_semantic_command(
            "\u0645\u0627\u0634\u06cc\u0646 \u062d\u0633\u0627\u0628 \u062f\u0648 \u0631\u0648 \u0628\u0647 \u0639\u0644\u0627\u0648\u0647 \u0686\u0647\u0627\u0631 \u06a9\u0646"
        )

        self.assertEqual(
            plan.result_text,
            "6",
        )

    def test_requires_explicit_calculator_target(self) -> None:
        with self.assertRaises(
            CalculatorSemanticParseError
        ):
            parse_calculator_semantic_command(
                "\u062f\u0648 \u0636\u0631\u0628\u062f\u0631 \u0686\u0647\u0627\u0631"
            )


if __name__ == "__main__":
    unittest.main()
