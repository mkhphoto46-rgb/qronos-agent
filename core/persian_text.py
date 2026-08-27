from __future__ import annotations

import re
import unicodedata


# Arabic and Persian share most of their alphabet, and text typed on different
# keyboards mixes the two. These pairs look identical to a reader but are
# different code points, so a search for one will not match the other.
ARABIC_TO_PERSIAN = {
    "ي": "ی",  # ARABIC YEH        -> FARSI YEH
    "ى": "ی",  # ALEF MAKSURA      -> FARSI YEH
    "ك": "ک",  # ARABIC KAF        -> KEHEH
    "ۀ": "ه",  # HEH WITH YEH ABOVE-> HEH
    "ة": "ه",  # TEH MARBUTA       -> HEH
}

# Digits. Persian and Arabic-Indic digits both appear in Persian text; search
# engines and numeric comparisons want ASCII.
PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
ASCII_DIGITS = "0123456789"

DIGIT_MAP = {
    **{ch: ASCII_DIGITS[i] for i, ch in enumerate(PERSIAN_DIGITS)},
    **{ch: ASCII_DIGITS[i] for i, ch in enumerate(ARABIC_DIGITS)},
}

# Punctuation that differs between Persian and ASCII keyboards.
PUNCTUATION_MAP = {
    "،": ",",   # ARABIC COMMA
    "؛": ";",   # ARABIC SEMICOLON
    "؟": "?",   # ARABIC QUESTION MARK
    "٫": ".",   # ARABIC DECIMAL SEPARATOR
    "٬": ",",   # ARABIC THOUSANDS SEPARATOR
    "«": '"',
    "»": '"',
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
}

# Marks that carry no meaning for matching: vowel diacritics, tatweel, and the
# bidirectional control characters that often survive a copy-paste.
DIACRITICS = "".join(
    chr(code) for code in range(0x064B, 0x0653)
) + "ـٰٕٓٔ"

INVISIBLE = (
    "‎"  # LEFT-TO-RIGHT MARK
    "‏"  # RIGHT-TO-LEFT MARK
    "‪‫‬‭‮"  # bidi embedding/override
    "⁦⁧⁨⁩"        # bidi isolates
    "﻿"  # BOM / zero width no-break space
)

ZERO_WIDTH_NON_JOINER = "‌"


def unify_letters(text: str) -> str:
    """Replace Arabic letter variants with their Persian equivalents."""
    return text.translate(str.maketrans(ARABIC_TO_PERSIAN))


def unify_digits(text: str) -> str:
    """Convert Persian and Arabic-Indic digits to ASCII digits."""
    return text.translate(str.maketrans(DIGIT_MAP))


def unify_punctuation(text: str) -> str:
    """Convert Persian and typographic punctuation to ASCII equivalents."""
    return text.translate(str.maketrans(PUNCTUATION_MAP))


def strip_diacritics(text: str) -> str:
    """
    Remove vowel marks and tatweel.

    Persian is normally written without diacritics, so text that carries them
    would fail to match the same words written plainly.
    """
    return text.translate({ord(ch): None for ch in DIACRITICS})


def strip_invisible(text: str) -> str:
    """
    Remove bidirectional control characters and byte-order marks.

    These are invisible but count as characters, so they break exact matching
    and can corrupt a URL-encoded query. The zero-width non-joiner is *not*
    removed here because it is meaningful in Persian; see
    :func:`collapse_zwnj`.
    """
    return text.translate({ord(ch): None for ch in INVISIBLE})


def collapse_zwnj(text: str, keep: bool = False) -> str:
    """
    Handle the zero-width non-joiner.

    ZWNJ is real Persian orthography — it separates the parts of a compound
    word such as ``می‌روم`` — so it is not noise. But writers are inconsistent
    about it, and a search for ``نرم‌افزار`` should also match ``نرم افزار``.

    ``keep=False`` replaces it with a space, which is what a search query
    wants: the engine then matches either spelling. ``keep=True`` preserves it,
    which is what display text wants.
    """
    if keep:
        return text

    return text.replace(ZERO_WIDTH_NON_JOINER, " ")


def collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace to a single space and trim the ends."""
    return re.sub(r"\s+", " ", text).strip()


def normalise(
    text: str,
    for_search: bool = True,
) -> str:
    """
    Normalise Persian text.

    ``for_search=True`` produces a form suitable for a search query or a cache
    key: Arabic variants unified, digits and punctuation converted to ASCII,
    diacritics and invisible marks removed, ZWNJ turned into a space, and
    whitespace collapsed.

    ``for_search=False`` keeps the zero-width non-joiner, so the result is
    still correct Persian for display, while still fixing letter variants and
    stripping invisible controls.

    Unicode NFC is applied first so that decomposed forms compare equal to
    composed ones.
    """
    if not text:
        return ""

    result = unicodedata.normalize("NFC", text)
    result = strip_invisible(result)
    result = unify_letters(result)
    result = unify_digits(result)

    if for_search:
        result = unify_punctuation(result)
        result = strip_diacritics(result)

    result = collapse_zwnj(result, keep=not for_search)

    return collapse_whitespace(result)


def contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    """
    True when any marker appears in the text as a whole word.

    Plain substring matching is wrong for short ASCII markers: ``api`` is
    inside ``capital``, ``now`` is inside ``known``, ``fix`` is inside
    ``prefix`` and ``rate`` is inside ``accurate``. Every one of those would
    misclassify a query.

    ASCII markers are therefore matched on word boundaries. Persian markers use
    substring matching, because Persian is written without the spacing that
    makes boundaries meaningful — ``قیمت`` legitimately appears joined to
    neighbouring words.

    ``text`` is expected to be already normalised and lowercased.
    """
    for marker in markers:
        if not marker:
            continue

        if marker.isascii():
            if re.search(
                rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])",
                text,
            ):
                return True
        elif marker in text:
            return True

    return False


def contains_persian(text: str) -> bool:
    """
    True when the text contains at least one Persian or Arabic letter.

    Used to route a query to Persian-capable sources. Digits and punctuation
    are ignored, because "iPhone 15" is not a Persian query merely because a
    Persian keyboard produced the digits.
    """
    return any(
        "؀" <= ch <= "ۿ" or "ﭐ" <= ch <= "﷿"
        for ch in text
    )


def is_mostly_persian(text: str, threshold: float = 0.5) -> bool:
    """
    True when Persian letters make up at least ``threshold`` of the letters.

    A mixed sentence such as «کد Python رو توضیح بده» is mostly Persian even
    though it contains an English word, and should be treated as a Persian
    query.
    """
    letters = [ch for ch in text if ch.isalpha()]

    if not letters:
        return False

    persian = sum(
        1
        for ch in letters
        if "؀" <= ch <= "ۿ" or "ﭐ" <= ch <= "﷿"
    )

    return (persian / len(letters)) >= threshold


def main() -> None:
    """Show the normaliser on a few awkward inputs."""
    samples = [
        "نرم‌افزار مديريت فايل",      # Arabic yeh and kaf mixed in
        "قيمت دلار ۱۴۰۵",             # Arabic yeh + Persian digits
        "مُحَمَّد",                      # diacritics
        "  چند   فاصله  ",            # whitespace
        "code Python رو توضیح بده",   # mixed script
    ]

    print("=== Persian normaliser ===")

    for sample in samples:
        print(f"in :  {sample!r}")
        print(f"out:  {normalise(sample)!r}")
        print(
            f"      persian={contains_persian(sample)} "
            f"mostly={is_mostly_persian(sample)}"
        )
        print()


if __name__ == "__main__":
    main()
