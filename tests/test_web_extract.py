from __future__ import annotations

import unittest

from core.persian_text import ZERO_WIDTH_NON_JOINER
from core.web_extract import (
    DIAGNOSE_BELOW_WORDS,
    ZWNJ_SENTINEL,
    ExtractedPage,
    PageProblem,
    TrafilaturaExtractor,
    count_words,
    detect_problem,
    truncate_words,
)


def article(body: str, title: str = "نمونه") -> str:
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><nav>منو</nav><article>{body}</article>"
        f"<footer>حقوق</footer></body></html>"
    )


def long_persian(words: int = 120) -> str:
    sentence = "این یک جمله نمونه برای تست استخراج متن فارسی است "

    return "<p>" + (sentence * ((words // 9) + 1)) + "</p>"


def long_english(words: int = 120) -> str:
    sentence = "This is a sample sentence used to test text extraction here "

    return "<p>" + (sentence * ((words // 10) + 1)) + "</p>"


class TestCountWords(unittest.TestCase):
    def test_counts_english(self) -> None:
        self.assertEqual(count_words("one two three"), 3)

    def test_counts_persian(self) -> None:
        self.assertEqual(count_words("یک دو سه چهار"), 4)

    def test_empty_is_zero(self) -> None:
        self.assertEqual(count_words("   "), 0)

    def test_collapses_repeated_whitespace(self) -> None:
        self.assertEqual(count_words("a\n\n  b\tc"), 3)


class TestTruncateWords(unittest.TestCase):
    def test_short_text_is_untouched(self) -> None:
        text, cut = truncate_words("one two three", 10)

        self.assertEqual(text, "one two three")
        self.assertFalse(cut)

    def test_long_text_is_cut_and_reported(self) -> None:
        # Reported rather than silent: a model given a truncated article should
        # be told, not left to conclude the article simply ended.
        text, cut = truncate_words("one two three four five", 3)

        self.assertEqual(count_words(text), 3)
        self.assertTrue(cut)

    def test_persian_text_is_cut_correctly(self) -> None:
        text, cut = truncate_words("یک دو سه چهار پنج", 2)

        self.assertEqual(count_words(text), 2)
        self.assertTrue(cut)

    def test_exact_budget_is_not_reported_as_cut(self) -> None:
        text, cut = truncate_words("one two three", 3)

        self.assertFalse(cut)
        self.assertEqual(count_words(text), 3)


class TestDetectProblem(unittest.TestCase):
    def test_cloudflare_interstitial(self) -> None:
        self.assertEqual(
            detect_problem("<title>Just a moment...</title>"),
            PageProblem.CHALLENGE,
        )

    def test_captcha_page(self) -> None:
        self.assertEqual(
            detect_problem("<p>Please verify you are human</p>"),
            PageProblem.CAPTCHA,
        )

    def test_paywall(self) -> None:
        self.assertEqual(
            detect_problem("<p>Subscribe to read the rest</p>"),
            PageProblem.LOGIN_WALL,
        )

    def test_consent_wall(self) -> None:
        self.assertEqual(
            detect_problem("<p>Accept all cookies to continue</p>"),
            PageProblem.CONSENT_WALL,
        )

    def test_ordinary_page_has_no_problem(self) -> None:
        self.assertEqual(detect_problem("<p>hello world</p>"), "")

    def test_only_the_start_of_the_document_is_examined(self) -> None:
        # A challenge page states its business immediately.
        padded = "<p>" + ("x " * 5_000) + " captcha</p>"

        self.assertEqual(detect_problem(padded), "")


class TestExtraction(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = TrafilaturaExtractor()

    def test_extracts_persian_article_text(self) -> None:
        page = self.extractor.extract(
            article(long_persian()),
            url="https://example.ir/x",
        )

        self.assertTrue(page.ok)
        self.assertTrue(page.is_persian)
        self.assertGreater(page.word_count, 40)

    def test_extracts_english_article_text(self) -> None:
        page = self.extractor.extract(
            article(long_english(), title="Sample"),
            url="https://example.com/x",
        )

        self.assertTrue(page.ok)
        self.assertFalse(page.is_persian)

    def test_navigation_and_footer_are_dropped(self) -> None:
        page = self.extractor.extract(
            article(long_persian()),
            url="https://example.ir/x",
        )

        self.assertNotIn("منو", page.text)
        self.assertNotIn("حقوق", page.text)

    def test_title_is_extracted_and_decoded(self) -> None:
        page = self.extractor.extract(
            article(long_english(), title="A &amp; B"),
            url="https://example.com/x",
        )

        self.assertEqual(page.title, "A & B")

    def test_missing_title_is_empty_not_an_error(self) -> None:
        page = self.extractor.extract(
            f"<html><body><article>{long_english()}</article></body></html>",
            url="https://example.com/x",
        )

        self.assertEqual(page.title, "")
        self.assertTrue(page.ok)

    def test_url_is_carried_through(self) -> None:
        page = self.extractor.extract(
            article(long_english()),
            url="https://example.com/page",
        )

        self.assertEqual(page.url, "https://example.com/page")


class TestZwnjPreservation(unittest.TestCase):
    """
    The extraction library strips the zero-width non-joiner, which turns
    «فایل‌ها» into «فایلها» — misspelled Persian. For a Persian-first product
    that is a real defect, not a cosmetic one.
    """

    def setUp(self) -> None:
        self.extractor = TrafilaturaExtractor()

        joined = (
            f"فایل{ZERO_WIDTH_NON_JOINER}ها را "
            f"می{ZERO_WIDTH_NON_JOINER}توانید در "
            f"پوشه{ZERO_WIDTH_NON_JOINER}بندی موضوعی مرتب کنید و این متن "
            "باید به اندازه کافی طولانی باشد تا استخراج شود و کوتاه به "
            "حساب نیاید چون متن کوتاه تشخیص داده نمی‌شود "
        )

        self.html = article("<p>" + (joined * 4) + "</p>")

    def test_zwnj_survives_extraction(self) -> None:
        page = self.extractor.extract(self.html, url="https://example.ir/x")

        self.assertTrue(page.ok)
        self.assertIn(ZERO_WIDTH_NON_JOINER, page.text)

    def test_the_sentinel_never_leaks_into_the_output(self) -> None:
        page = self.extractor.extract(self.html, url="https://example.ir/x")

        self.assertNotIn(ZWNJ_SENTINEL, page.text)

    def test_the_compound_word_is_spelled_correctly(self) -> None:
        page = self.extractor.extract(self.html, url="https://example.ir/x")

        self.assertIn(f"فایل{ZERO_WIDTH_NON_JOINER}ها", page.text)
        self.assertNotIn("فایلها", page.text)

    def test_english_pages_are_unaffected(self) -> None:
        page = self.extractor.extract(
            article(long_english()),
            url="https://example.com/x",
        )

        self.assertTrue(page.ok)
        self.assertNotIn(ZWNJ_SENTINEL, page.text)


class TestDiagnosisOrdering(unittest.TestCase):
    """
    Extraction runs before diagnosis. Diagnosing first refused Persian
    Wikipedia, because MediaWiki's page head contains the word "captcha" —
    cooling off a legitimate encyclopedia over a substring.
    """

    def setUp(self) -> None:
        self.extractor = TrafilaturaExtractor()

    def test_a_real_article_mentioning_captcha_is_still_read(self) -> None:
        html = (
            "<html><head><title>x</title>"
            '<script>var config = {"captcha": true};</script></head>'
            f"<body><article>{long_english()}</article></body></html>"
        )

        page = self.extractor.extract(html, url="https://example.com/x")

        self.assertTrue(page.ok)
        self.assertEqual(page.problem, "")

    def test_an_article_about_captchas_is_still_read(self) -> None:
        body = (
            "<p>"
            + (
                "A captcha is a challenge used to tell humans from bots and "
                "this article explains how they work in some detail here "
                * 6
            )
            + "</p>"
        )

        page = self.extractor.extract(
            article(body, title="On CAPTCHAs"),
            url="https://example.com/x",
        )

        self.assertTrue(page.ok)

    def test_a_wall_with_no_content_is_diagnosed(self) -> None:
        page = self.extractor.extract(
            "<html><head><title>Just a moment...</title></head>"
            "<body>Checking your browser before accessing.</body></html>",
            url="https://walled.example.com",
        )

        self.assertFalse(page.ok)
        self.assertEqual(page.problem, PageProblem.CHALLENGE)

    def test_an_empty_page_with_no_marker_is_reported_as_empty(self) -> None:
        page = self.extractor.extract(
            "<html><head><title>x</title></head><body></body></html>",
            url="https://example.com/x",
        )

        self.assertEqual(page.problem, PageProblem.EMPTY)

    def test_the_diagnosis_threshold_is_the_word_count(self) -> None:
        page = self.extractor.extract(
            article("<p>سه کلمه کوتاه</p>"),
            url="https://example.ir/x",
        )

        self.assertLess(page.word_count, DIAGNOSE_BELOW_WORDS)
        self.assertTrue(page.problem)


class TestSubstantiality(unittest.TestCase):
    def test_a_long_page_is_substantial(self) -> None:
        page = TrafilaturaExtractor().extract(
            article(long_english(200)),
            url="https://example.com/x",
        )

        self.assertTrue(page.is_substantial)

    def test_a_thin_page_is_not_substantial(self) -> None:
        page = ExtractedPage(
            url="https://example.com/x",
            title="t",
            text="a few words only",
            word_count=4,
            is_persian=False,
        )

        self.assertTrue(page.ok)
        self.assertFalse(page.is_substantial)


class TestWordCap(unittest.TestCase):
    def test_a_long_page_is_capped_and_flagged(self) -> None:
        # Without a cap, one long article would crowd out every other source
        # in a small model's context.
        extractor = TrafilaturaExtractor(max_words=60)

        page = extractor.extract(
            article(long_english(500)),
            url="https://example.com/x",
        )

        self.assertTrue(page.truncated)
        self.assertLessEqual(page.word_count, 60)

    def test_a_short_page_is_not_flagged(self) -> None:
        extractor = TrafilaturaExtractor(max_words=5_000)

        page = extractor.extract(
            article(long_english(120)),
            url="https://example.com/x",
        )

        self.assertFalse(page.truncated)


if __name__ == "__main__":
    unittest.main()
