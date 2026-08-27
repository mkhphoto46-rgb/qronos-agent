from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from core.persian_text import (
    ZERO_WIDTH_NON_JOINER,
    is_mostly_persian,
    normalise,
)


# trafilatura strips the zero-width non-joiner, which silently corrupts Persian
# orthography: «فایل‌ها» comes back as «فایلها» and «می‌توانید» as «میتوانید».
# Both are misspelled, and if that text is later spoken or shown to the user it
# is wrong rather than merely untidy.
#
# The fix is to swap the ZWNJ for a placeholder across the extraction call and
# swap it back afterwards. It has to be ASCII letters: private-use code points
# are stripped by the same pass that removes the ZWNJ, so a "nicer" sentinel
# does not survive. The string is deliberately absurd so it cannot collide with
# real page text.
ZWNJ_SENTINEL = "qQzWnJzQq"


# Markers of a page that loaded but has nothing to read. Detected before the
# extractor runs, because an extractor handed a consent wall returns the
# consent text as though it were the article.
CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "enable javascript and cookies",
    "cf-browser-verification",
    "cf_chl_opt",
    "attention required",
    "ddos protection by",
)

CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "are you a robot",
    "are you human",
    "verify you are human",
    "unusual traffic",
)

CONSENT_MARKERS = (
    "accept all cookies",
    "we value your privacy",
    "manage your cookie",
    "cookie preferences",
    "consent to the use of cookies",
)

LOGIN_MARKERS = (
    "sign in to continue",
    "log in to continue",
    "create a free account to read",
    "subscribe to read",
    "members only",
)


class PageProblem:
    """Named reasons a page could not be read, as plain strings."""

    CHALLENGE = "challenge"
    CAPTCHA = "captcha"
    CONSENT_WALL = "consent_wall"
    LOGIN_WALL = "login_wall"
    EMPTY = "empty"


@dataclass(frozen=True)
class ExtractedPage:
    """Clean text pulled out of one HTML page."""

    url: str
    title: str
    text: str
    word_count: int
    is_persian: bool
    truncated: bool = False
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem and bool(self.text.strip())

    @property
    def is_substantial(self) -> bool:
        """
        Enough text to be worth giving to a model.

        Below roughly a hundred words a page is usually navigation, a stub, or
        a wall, and feeding it to a 4B model wastes context that better sources
        could use.
        """
        return self.ok and self.word_count >= 100


class Extractor(Protocol):
    """
    Turns HTML into readable text.

    An interface rather than a direct call, so the extraction library can be
    replaced without touching anything else — the same rule the rest of Qronos
    follows for external libraries.
    """

    def extract(self, html: str, url: str = "") -> ExtractedPage:
        ...


# Below this many extracted words, a page is treated as having produced nothing
# and is diagnosed. Above it, whatever markers the HTML contains are irrelevant:
# the content came out, so the page is readable.
DIAGNOSE_BELOW_WORDS = 40


def detect_problem(html: str) -> str:
    """
    Explain why a page produced no readable content.

    **Only meaningful for a page that extracted to nothing.** An earlier version
    ran this before extraction and refused Persian Wikipedia, because MediaWiki's
    configuration contains the word "captcha" in the page head. Cooling off a
    legitimate encyclopedia for six hours over a substring is exactly the
    failure this ordering prevents.

    So the real signal is "nothing came out"; these markers only say what kind
    of wall it was. A long article that happens to discuss CAPTCHAs never
    reaches this function.

    Only the start of the document is examined, since a challenge page states
    its business immediately.
    """
    head = html[:6_000].lower()

    if any(marker in head for marker in CAPTCHA_MARKERS):
        return PageProblem.CAPTCHA

    if any(marker in head for marker in CHALLENGE_MARKERS):
        return PageProblem.CHALLENGE

    if any(marker in head for marker in LOGIN_MARKERS):
        return PageProblem.LOGIN_WALL

    if any(marker in head for marker in CONSENT_MARKERS):
        return PageProblem.CONSENT_WALL

    return ""


def count_words(text: str) -> int:
    """
    Count words in a way that works for Persian as well as English.

    Splitting on whitespace is adequate for both once the text has been
    normalised, and it avoids pulling in a tokeniser for a rough size measure.
    """
    return len([token for token in re.split(r"\s+", text.strip()) if token])


def truncate_words(text: str, max_words: int) -> tuple[str, bool]:
    """
    Trim to a word budget, reporting whether anything was cut.

    Reported rather than silent, because a model given a truncated article
    should be told it is truncated instead of concluding the article simply
    ended.
    """
    tokens = [token for token in re.split(r"(\s+)", text) if token]
    words = 0
    kept: list[str] = []

    for token in tokens:
        if not token.isspace():
            if words >= max_words:
                return "".join(kept).strip(), True

            words += 1

        kept.append(token)

    return "".join(kept).strip(), False


class TrafilaturaExtractor:
    """
    Extraction backed by trafilatura.

    Chosen because it is Python-first, outputs Markdown so heading structure
    survives, and measures around 90% content recall at above 91% precision on
    published benchmarks — which matters when the reader is a 4B model with a
    small context.

    ``max_words`` bounds what any single page can contribute. Three pages of two
    thousand words each is already a lot for a small model; without a cap one
    long article would crowd out every other source.
    """

    def __init__(self, max_words: int = 2_000) -> None:
        self.max_words = max_words

    def extract(self, html: str, url: str = "") -> ExtractedPage:
        """
        Pull the readable text out of a page.

        Extraction runs **first**, and a wall is diagnosed only if nothing
        substantial came out. Diagnosing first was a defect: it refused pages
        whose markup merely mentioned a CAPTCHA.
        """
        try:
            import trafilatura
        except ImportError:  # pragma: no cover - dependency is declared
            return ExtractedPage(
                url=url,
                title="",
                text="",
                word_count=0,
                is_persian=False,
                problem=PageProblem.EMPTY,
            )

        # Only pages that actually contain a ZWNJ pay for the substitution, so
        # English pages are untouched.
        protected = ZERO_WIDTH_NON_JOINER in html

        source = (
            html.replace(ZERO_WIDTH_NON_JOINER, ZWNJ_SENTINEL)
            if protected
            else html
        )

        extracted = trafilatura.extract(
            source,
            url=url or None,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )

        if extracted and protected:
            extracted = extracted.replace(
                ZWNJ_SENTINEL,
                ZERO_WIDTH_NON_JOINER,
            )

        title = self._extract_title(html)

        # Persian display text keeps its zero-width non-joiners: the model is
        # reading prose here, not matching a search key.
        cleaned = (
            normalise(extracted, for_search=False)
            if extracted
            else ""
        )
        text, truncated = truncate_words(cleaned, self.max_words)
        words = count_words(text)

        if words < DIAGNOSE_BELOW_WORDS:
            # Nothing usable came out. Now — and only now — work out whether
            # this was a wall or simply an empty page.
            return ExtractedPage(
                url=url,
                title=title,
                text=text,
                word_count=words,
                is_persian=is_mostly_persian(text),
                truncated=truncated,
                problem=detect_problem(html) or PageProblem.EMPTY,
            )

        return ExtractedPage(
            url=url,
            title=title,
            text=text,
            word_count=words,
            is_persian=is_mostly_persian(text),
            truncated=truncated,
        )

    @staticmethod
    def _extract_title(html: str) -> str:
        match = re.search(
            r"<title[^>]*>(.*?)</title>",
            html,
            re.S | re.I,
        )

        if match is None:
            return ""

        import html as html_module

        raw = re.sub(r"<[^>]+>", "", match.group(1))

        return normalise(
            html_module.unescape(raw),
            for_search=False,
        )


def main() -> None:
    """Extract from a small hand-written page."""
    sample = """<html><head><title>نمونه صفحه</title></head>
    <body>
      <nav>منو و لینک‌های ناوبری که باید حذف شود</nav>
      <article>
        <h1>سازماندهی فایل‌ها</h1>
        <p>برای مرتب کردن فایل‌ها می‌توانید از پوشه‌بندی موضوعی استفاده کنید.</p>
        <p>ابزارهایی مانند Everything و Total Commander هم کمک می‌کنند.</p>
      </article>
      <footer>تمام حقوق محفوظ است</footer>
    </body></html>"""

    page = TrafilaturaExtractor().extract(sample, url="https://example.ir/x")

    print("=== extraction ===")
    print(f"title    : {page.title}")
    print(f"words    : {page.word_count}")
    print(f"persian  : {page.is_persian}")
    print(f"ok       : {page.ok}")
    print(f"problem  : {page.problem or 'none'}")
    print()
    print(page.text)

    print()
    print("=== a Cloudflare interstitial ===")

    blocked = TrafilaturaExtractor().extract(
        "<html><head><title>Just a moment...</title></head>"
        "<body>Checking your browser before accessing.</body></html>",
        url="https://walled.example.com",
    )

    print(f"problem  : {blocked.problem}")
    print(f"ok       : {blocked.ok}")


if __name__ == "__main__":
    main()
