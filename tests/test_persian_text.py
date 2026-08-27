from __future__ import annotations

import unittest

from core.persian_text import (
    ZERO_WIDTH_NON_JOINER,
    collapse_whitespace,
    collapse_zwnj,
    contains_persian,
    is_mostly_persian,
    normalise,
    strip_diacritics,
    strip_invisible,
    unify_digits,
    unify_letters,
    unify_punctuation,
)


class TestUnifyLetters(unittest.TestCase):
    def test_arabic_yeh_becomes_persian_yeh(self) -> None:
        # These look identical on screen but are different code points, so a
        # search for one would not match the other.
        self.assertEqual(unify_letters("مديريت"), "مدیریت")

    def test_arabic_kaf_becomes_keheh(self) -> None:
        self.assertEqual(unify_letters("كتاب"), "کتاب")

    def test_alef_maksura_becomes_yeh(self) -> None:
        self.assertEqual(unify_letters("علی"), "علی")
        self.assertEqual(unify_letters("على"), "علی")

    def test_teh_marbuta_becomes_heh(self) -> None:
        self.assertEqual(unify_letters("مدرسة"), "مدرسه")

    def test_already_persian_text_is_unchanged(self) -> None:
        self.assertEqual(unify_letters("کتاب مدیریت"), "کتاب مدیریت")


class TestUnifyDigits(unittest.TestCase):
    def test_persian_digits_become_ascii(self) -> None:
        self.assertEqual(unify_digits("۱۴۰۵"), "1405")

    def test_arabic_indic_digits_become_ascii(self) -> None:
        self.assertEqual(unify_digits("١٢٣"), "123")

    def test_mixed_digits_in_text(self) -> None:
        self.assertEqual(unify_digits("سال ۱۴۰۵ بود"), "سال 1405 بود")

    def test_ascii_digits_are_unchanged(self) -> None:
        self.assertEqual(unify_digits("2026"), "2026")


class TestUnifyPunctuation(unittest.TestCase):
    def test_arabic_comma_and_question_mark(self) -> None:
        self.assertEqual(unify_punctuation("الف، ب؟"), "الف, ب?")

    def test_guillemets_become_quotes(self) -> None:
        self.assertEqual(unify_punctuation("«نقل»"), '"نقل"')

    def test_typographic_dashes_become_hyphen(self) -> None:
        self.assertEqual(unify_punctuation("a—b–c"), "a-b-c")


class TestStripMarks(unittest.TestCase):
    def test_diacritics_are_removed(self) -> None:
        # Persian is normally written without vowel marks, so text carrying
        # them would fail to match the same word written plainly.
        self.assertEqual(strip_diacritics("مُحَمَّد"), "محمد")

    def test_bidi_controls_are_removed(self) -> None:
        # Invisible but real characters. They break exact matching and can
        # corrupt a percent-encoded query.
        self.assertEqual(strip_invisible("سلام‎‏"), "سلام")

    def test_byte_order_mark_is_removed(self) -> None:
        self.assertEqual(strip_invisible("﻿سلام"), "سلام")

    def test_zwnj_is_not_treated_as_invisible_noise(self) -> None:
        # ZWNJ is real orthography, not junk, so strip_invisible leaves it.
        text = f"می{ZERO_WIDTH_NON_JOINER}روم"

        self.assertIn(ZERO_WIDTH_NON_JOINER, strip_invisible(text))


class TestZwnj(unittest.TestCase):
    def test_becomes_a_space_for_search(self) -> None:
        # A search for «نرم‌افزار» should also match «نرم افزار».
        text = f"نرم{ZERO_WIDTH_NON_JOINER}افزار"

        self.assertEqual(collapse_zwnj(text), "نرم افزار")

    def test_is_preserved_when_asked(self) -> None:
        text = f"نرم{ZERO_WIDTH_NON_JOINER}افزار"

        self.assertEqual(collapse_zwnj(text, keep=True), text)


class TestCollapseWhitespace(unittest.TestCase):
    def test_runs_become_single_spaces(self) -> None:
        self.assertEqual(collapse_whitespace("a   b\t\nc"), "a b c")

    def test_ends_are_trimmed(self) -> None:
        self.assertEqual(collapse_whitespace("  x  "), "x")


class TestNormalise(unittest.TestCase):
    def test_empty_input(self) -> None:
        self.assertEqual(normalise(""), "")

    def test_full_pipeline_for_search(self) -> None:
        self.assertEqual(
            normalise("نرم‌افزار مديريت فايل"),
            "نرم افزار مدیریت فایل",
        )

    def test_digits_and_letters_together(self) -> None:
        self.assertEqual(normalise("قيمت دلار ۱۴۰۵"), "قیمت دلار 1405")

    def test_display_mode_keeps_zwnj(self) -> None:
        result = normalise(
            f"نرم{ZERO_WIDTH_NON_JOINER}افزار",
            for_search=False,
        )

        self.assertIn(ZERO_WIDTH_NON_JOINER, result)

    def test_display_mode_still_fixes_letter_variants(self) -> None:
        self.assertEqual(
            normalise("مديريت", for_search=False),
            "مدیریت",
        )

    def test_two_spellings_normalise_to_the_same_string(self) -> None:
        # This is what makes the cache key work: two spellings of the same
        # query must not cost two searches out of a budget of two.
        with_zwnj = normalise(f"نرم{ZERO_WIDTH_NON_JOINER}افزار")
        with_space = normalise("نرم افزار")

        self.assertEqual(with_zwnj, with_space)

    def test_arabic_and_persian_spelling_normalise_equal(self) -> None:
        self.assertEqual(normalise("مديريت فايل"), normalise("مدیریت فایل"))

    def test_decomposed_and_composed_forms_compare_equal(self) -> None:
        # NFC is applied first, so a decomposed form does not read as
        # different text.
        self.assertEqual(
            normalise("اً"),
            normalise(normalise("اً")),
        )

    def test_is_idempotent(self) -> None:
        once = normalise("نرم‌افزار مديريت ۱۴۰۵")

        self.assertEqual(normalise(once), once)


class TestScriptDetection(unittest.TestCase):
    def test_persian_text_is_detected(self) -> None:
        self.assertTrue(contains_persian("سلام"))

    def test_english_text_is_not(self) -> None:
        self.assertFalse(contains_persian("hello world"))

    def test_digits_alone_do_not_count_as_persian(self) -> None:
        # "iPhone 15" typed on a Persian keyboard is not a Persian query.
        self.assertFalse(contains_persian("2026"))

    def test_mixed_sentence_is_mostly_persian(self) -> None:
        self.assertTrue(is_mostly_persian("کد Python رو توضیح بده"))

    def test_mostly_english_sentence_is_not(self) -> None:
        self.assertFalse(
            is_mostly_persian("explain this Python dataclass code سلام")
        )

    def test_no_letters_is_not_persian(self) -> None:
        self.assertFalse(is_mostly_persian("123 456"))
        self.assertFalse(is_mostly_persian(""))

    def test_threshold_is_configurable(self) -> None:
        text = "کد Python"

        self.assertTrue(is_mostly_persian(text, threshold=0.2))
        self.assertFalse(is_mostly_persian(text, threshold=0.95))


if __name__ == "__main__":
    unittest.main()
