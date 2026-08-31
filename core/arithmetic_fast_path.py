from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import re


@dataclass(frozen=True)
class ArithmeticAnswer:
    left: int
    operator: str
    right: int
    result: Fraction
    spoken_text: str


_PERSIAN_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

_UNITS = {
    "صفر": 0,
    "یک": 1,
    "يه": 1,
    "یه": 1,
    "دو": 2,
    "سه": 3,
    "چهار": 4,
    "پنج": 5,
    "شش": 6,
    "هفت": 7,
    "هشت": 8,
    "نه": 9,
}

_TEENS = {
    "ده": 10,
    "یازده": 11,
    "دوازده": 12,
    "سیزده": 13,
    "چهارده": 14,
    "پانزده": 15,
    "شانزده": 16,
    "هفده": 17,
    "هجده": 18,
    "نوزده": 19,
}

_TENS = {
    "بیست": 20,
    "سی": 30,
    "چهل": 40,
    "پنجاه": 50,
    "شصت": 60,
    "هفتاد": 70,
    "هشتاد": 80,
    "نود": 90,
}

_HUNDREDS = {
    "صد": 100,
    "یکصد": 100,
    "دویست": 200,
    "سیصد": 300,
    "چهارصد": 400,
    "پانصد": 500,
    "ششصد": 600,
    "هفتصد": 700,
    "هشتصد": 800,
    "نهصد": 900,
}

_SCALES = {
    "هزار": 1_000,
    "میلیون": 1_000_000,
}

_OPERATOR_PATTERNS = (
    ("divide", re.compile(r"\s+(?:تقسیم\s+بر|تقسیمبر)\s+")),
    ("multiply", re.compile(r"\s+(?:ضرب\s+در|ضربدر)\s+")),
    ("plus", re.compile(r"\s+(?:به\s+علاوه|بعلاوه)\s+")),
    ("minus", re.compile(r"\s+(?:منهای|منها)\s+")),
    ("plus", re.compile(r"\s*\+\s*")),
    ("minus", re.compile(r"\s*-\s*")),
    ("multiply", re.compile(r"\s*[×*]\s*")),
    ("divide", re.compile(r"\s*[÷/]\s*")),
)

_TRAILING_QUESTION = re.compile(
    r"""
    (?:
        \s*
        (?:
            چند\s*(?:می\s*شه|میشه|می\s*شود|می‌شود)
            |
            چی\s*(?:می\s*شه|میشه|می\s*شود|می‌شود)
            |
            چند\s*است
            |
            چنده
            |
            می\s*شه
            |
            میشه
            |
            می\s*شود
            |
            می‌شود
            |
            است
        )
        \s*
    )$
    """,
    re.VERBOSE,
)

_LEADING_NOISE = re.compile(
    r"""
    ^
    \s*
    (?:
        لطفا\s+
        |
        لطفاً\s+
        |
        حساب\s+کن\s+
        |
        محاسبه\s+کن\s+
        |
        بگو\s+
        |
        جواب\s+
        |
        حاصل\s+
    )*
    """,
    re.VERBOSE,
)


def _normalise(text: str) -> str:
    value = text.translate(_PERSIAN_DIGITS)

    value = (
        value.replace("ي", "ی")
        .replace("ى", "ی")
        .replace("ك", "ک")
        .replace("\u200c", " ")
        .replace("\u200f", " ")
        .replace("\ufeff", "")
    )

    value = re.sub(r"[؟?!،,؛;]", " ", value)

    # Common Persian STT spacing/pronunciation variants.
    value = re.sub(r"\bدوبه\b", "دو به", value)
    value = re.sub(r"\bسهبه\b", "سه به", value)
    value = re.sub(r"\bچهاربه\b", "چهار به", value)

    value = value.replace("علاوی", "علاوه")
    value = value.replace("بعلاوی", "بعلاوه")
    value = value.replace("علاوه ی", "علاوه")

    value = re.sub(r"\s+", " ", value)

    return value.strip()


def _clean_operand(text: str, *, is_left: bool) -> str:
    value = text.strip()

    if is_left:
        value = _LEADING_NOISE.sub("", value)
    else:
        value = _TRAILING_QUESTION.sub("", value)

    value = value.strip(" .")

    return value


def _parse_integer(text: str) -> int | None:
    value = text.strip()

    if not value:
        return None

    if re.fullmatch(r"[+-]?\d+", value):
        try:
            return int(value)
        except ValueError:
            return None

    negative = False

    if value.startswith("منفی "):
        negative = True
        value = value[len("منفی "):].strip()

    tokens = [
        token
        for token in value.split()
        if token and token != "و"
    ]

    if not tokens:
        return None

    total = 0
    current = 0

    for token in tokens:
        if token in _UNITS:
            current += _UNITS[token]
            continue

        if token in _TEENS:
            current += _TEENS[token]
            continue

        if token in _TENS:
            current += _TENS[token]
            continue

        if token in _HUNDREDS:
            current += _HUNDREDS[token]
            continue

        if token in _SCALES:
            scale = _SCALES[token]

            if current == 0:
                current = 1

            if scale == 1_000:
                total += current * scale
                current = 0
            elif scale == 1_000_000:
                total = (total + current) * scale
                current = 0

            continue

        return None

    result = total + current

    if negative:
        result = -result

    return result


def _under_thousand_to_persian(number: int) -> str:
    if number < 0 or number >= 1000:
        raise ValueError("number must be between 0 and 999")

    if number == 0:
        return "صفر"

    parts: list[str] = []

    hundreds = (number // 100) * 100

    if hundreds:
        reverse_hundreds = {
            value: key
            for key, value in _HUNDREDS.items()
            if key != "یکصد"
        }
        parts.append(reverse_hundreds[hundreds])
        number %= 100

    if 10 <= number <= 19:
        reverse_teens = {
            value: key
            for key, value in _TEENS.items()
        }
        parts.append(reverse_teens[number])
        number = 0
    else:
        tens = (number // 10) * 10

        if tens:
            reverse_tens = {
                value: key
                for key, value in _TENS.items()
            }
            parts.append(reverse_tens[tens])
            number %= 10

        if number:
            reverse_units = {
                value: key
                for key, value in _UNITS.items()
                if key not in {"يه", "یه"}
            }
            parts.append(reverse_units[number])

    return " و ".join(parts)


def integer_to_persian(number: int) -> str:
    if number == 0:
        return "صفر"

    if number < 0:
        return "منفی " + integer_to_persian(-number)

    if number >= 1_000_000_000:
        return str(number)

    parts: list[str] = []

    millions = number // 1_000_000

    if millions:
        parts.append(
            integer_to_persian(millions)
            + " میلیون"
        )
        number %= 1_000_000

    thousands = number // 1_000

    if thousands:
        parts.append(
            integer_to_persian(thousands)
            + " هزار"
        )
        number %= 1_000

    if number:
        parts.append(
            _under_thousand_to_persian(number)
        )

    return " و ".join(parts)


def _fraction_to_spoken(value: Fraction) -> str:
    if value.denominator == 1:
        return integer_to_persian(value.numerator)

    numerator = integer_to_persian(value.numerator)
    denominator = integer_to_persian(value.denominator)

    return f"{numerator} تقسیم بر {denominator}"


def solve_simple_arithmetic(
    user_input: str,
) -> ArithmeticAnswer | None:
    text = _normalise(user_input)

    if not text:
        return None

    for operator, pattern in _OPERATOR_PATTERNS:
        match = pattern.search(text)

        if match is None:
            continue

        left_text = _clean_operand(
            text[: match.start()],
            is_left=True,
        )

        right_text = _clean_operand(
            text[match.end():],
            is_left=False,
        )

        left = _parse_integer(left_text)
        right = _parse_integer(right_text)

        if left is None or right is None:
            return None

        left_spoken = integer_to_persian(left)
        right_spoken = integer_to_persian(right)

        if operator == "plus":
            result = Fraction(left + right, 1)
            spoken = (
                f"می‌شود {_fraction_to_spoken(result)}."
            )

        elif operator == "minus":
            result = Fraction(left - right, 1)
            spoken = (
                f"می‌شود {_fraction_to_spoken(result)}."
            )

        elif operator == "multiply":
            result = Fraction(left * right, 1)
            spoken = (
                f"می‌شود {_fraction_to_spoken(result)}."
            )

        elif operator == "divide":
            if right == 0:
                return ArithmeticAnswer(
                    left=left,
                    operator=operator,
                    right=right,
                    result=Fraction(0, 1),
                    spoken_text=(
                        "تقسیم بر صفر تعریف نشده است."
                    ),
                )

            result = Fraction(left, right)

            spoken = (
                f"می‌شود {_fraction_to_spoken(result)}."
            )

        else:
            return None

        return ArithmeticAnswer(
            left=left,
            operator=operator,
            right=right,
            result=result,
            spoken_text=spoken,
        )

    return None