from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from core.persian_text import contains_marker, is_mostly_persian, normalise


class PrivacyLevel(Enum):
    """
    How much context a search query is allowed to draw on.

    ``USER_ONLY``
        The query is built from the current utterance and nothing else.
        Runs automatically.

    ``CONVERSATION_REFERENT``
        A pronoun or reference in the utterance may be resolved from the
        current conversation. "مصرف برقشون" is meaningless without knowing
        what "their" refers to, so an absolute ban on context would break
        ordinary conversation. Permitted only for non-sensitive referents.

    ``PRIVATE_CONTEXT``
        The query needs something from memory, a file, or the user profile.
        Requires the final query to be shown and explicitly approved before
        anything leaves the machine.
    """

    USER_ONLY = "user_only"
    CONVERSATION_REFERENT = "conversation_referent"
    PRIVATE_CONTEXT = "private_context"


class QueryRejection(Enum):
    """Why a query was refused."""

    EMPTY = "empty"
    APPROVAL_REQUIRED = "approval_required"
    SENSITIVE_REFERENT = "sensitive_referent"
    CONTEXT_NOT_PERMITTED = "context_not_permitted"


class PrivacyGateError(RuntimeError):
    """Raised when a query cannot be built without breaking a privacy rule."""

    def __init__(
        self,
        rejection: QueryRejection,
        message: str,
    ) -> None:
        super().__init__(message)
        self.rejection = rejection


# Filler words that add nothing to a search and only dilute it. Removed by
# deterministic rule, never by a model: a model rewriting the query is exactly
# the path by which private context leaks in.
PERSIAN_FILLER = (
    "برام",
    "برای من",
    "لطفا",
    "لطفاً",
    "میشه",
    "می‌شه",
    "سرچ کن",
    "جستجو کن",
    "جست‌وجو کن",
    "پیدا کن",
    "ببین",
    "بگرد",
    "یه",
    "یک لحظه",
)

ENGLISH_FILLER = (
    "please",
    "search for",
    "search",
    "look up",
    "look for",
    "find me",
    "find",
    "google",
    "can you",
    "could you",
    "for me",
)

# Words whose presence means the utterance is asking for a web lookup. Kept
# deliberately small and deterministic; an ambiguous utterance is referred back
# to the caller rather than guessed at.
PERSIAN_SEARCH_MARKERS = (
    "سرچ",
    "جستجو",
    "جست‌وجو",
    "گوگل",
    "توی اینترنت",
    "در اینترنت",
    "آخرین",
    "جدیدترین",
    "قیمت",
    "اخبار",
    "خبر",
)

ENGLISH_SEARCH_MARKERS = (
    "search",
    "google",
    "look up",
    "on the web",
    "latest",
    "newest",
    "current price",
    "news",
)

# Referent words that signal the utterance depends on earlier conversation.
REFERENT_MARKERS = (
    "شون",
    "شان",
    "اینا",
    "اون‌ها",
    "اونها",
    "همون",
    "این یکی",
    "آن یکی",
    "their",
    "them",
    "those",
    "that one",
    "it",
)

# Topics where a referent must never be resolved automatically, because
# resolving it would pull something private into a query that leaves the
# machine. Matched against the conversation context, not the utterance.
SENSITIVE_CONTEXT_MARKERS = (
    "بیماری",
    "درمان",
    "دارو",
    "پزشک",
    "دکتر",
    "حساب بانکی",
    "کارت",
    "رمز",
    "پسورد",
    "آدرس",
    "کد ملی",
    "شماره تلفن",
    "حقوق",
    "درآمد",
    "بدهی",
    "وام",
    "diagnosis",
    "treatment",
    "medication",
    "prescription",
    "bank account",
    "password",
    "credit card",
    "national id",
    "salary",
    "income",
    "debt",
)


@dataclass(frozen=True)
class SearchQuery:
    """
    A query that has passed the privacy gate and may be sent.

    ``text`` is what leaves the machine. ``shown_to_user`` is True when the
    query had to be displayed and approved first, which the caller must have
    done before this object was created.
    """

    text: str
    level: PrivacyLevel
    is_persian: bool
    shown_to_user: bool = False
    referent_resolved_from: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def looks_like_search_request(utterance: str) -> bool:
    """
    Deterministic check for whether an utterance is asking for a web lookup.

    Returns False for anything ambiguous. The caller then asks the user rather
    than guessing, which keeps the decision out of a model's hands and stops
    Qronos searching the web for something the user meant locally.
    """
    text = normalise(utterance).lower()

    if not text:
        return False

    markers = PERSIAN_SEARCH_MARKERS + ENGLISH_SEARCH_MARKERS

    return contains_marker(text, markers)


def mentions_referent(utterance: str) -> bool:
    """
    True when the utterance appears to refer to something said earlier.

    Used to decide whether :class:`PrivacyLevel.CONVERSATION_REFERENT` is even
    relevant. A query with no referent never needs context, so it stays at
    ``USER_ONLY`` regardless of what the caller asked for.
    """
    text = normalise(utterance).lower()

    return contains_marker(text, REFERENT_MARKERS)


def context_is_sensitive(context: str) -> bool:
    """
    True when the conversation context touches a sensitive topic.

    Deliberately broad. A false positive costs one confirmation prompt; a false
    negative sends a private detail to a search engine.
    """
    text = normalise(context).lower()

    return contains_marker(text, SENSITIVE_CONTEXT_MARKERS)


def strip_filler(text: str) -> str:
    """
    Remove request wrapping so only the subject remains.

    "برام قیمت دلار رو سرچ کن" becomes "قیمت دلار". Purely a lookup and
    replace; no model involved.
    """
    result = text

    for phrase in sorted(
        PERSIAN_FILLER + ENGLISH_FILLER,
        key=len,
        reverse=True,
    ):
        result = re.sub(
            re.escape(phrase),
            " ",
            result,
            flags=re.IGNORECASE,
        )

    # Persian object marker and leftover connectives that only make sense
    # inside a request sentence.
    result = re.sub(r"\bرو\b|\bرا\b", " ", result)

    return re.sub(r"\s+", " ", result).strip(" ?.,!؟،")


def build_query(
    utterance: str,
    level: PrivacyLevel = PrivacyLevel.USER_ONLY,
    context: str | None = None,
    approved: bool = False,
) -> SearchQuery:
    """
    Build a search query under the privacy rules.

    The single rule this function exists to enforce:

    ``No hidden enrichment from private context.``

    Nothing from memory, a file or the user profile may reach the query unless
    the caller has shown the exact final text to the user and recorded their
    approval. Conversation context may resolve a referent, and only when the
    context is not sensitive.

    Fail-closed in every direction: no context supplied means the level drops
    to ``USER_ONLY``; a sensitive context is refused; ``PRIVATE_CONTEXT``
    without approval is refused.

    Raises :class:`PrivacyGateError` rather than silently degrading, so a caller
    cannot mistake a refusal for a successful build.
    """
    subject = strip_filler(normalise(utterance))

    if not subject:
        raise PrivacyGateError(
            QueryRejection.EMPTY,
            "The utterance contained nothing to search for.",
        )

    persian = is_mostly_persian(subject)

    # A level that needs context but has none collapses to USER_ONLY. This is
    # the fail-closed direction: less context, never more.
    effective_level = level

    if context is None or not context.strip():
        effective_level = PrivacyLevel.USER_ONLY

    if effective_level is PrivacyLevel.USER_ONLY:
        return SearchQuery(
            text=subject,
            level=PrivacyLevel.USER_ONLY,
            is_persian=persian,
        )

    assert context is not None  # narrowed by the check above

    if effective_level is PrivacyLevel.CONVERSATION_REFERENT:
        if context_is_sensitive(context):
            raise PrivacyGateError(
                QueryRejection.SENSITIVE_REFERENT,
                "The conversation context touches a sensitive topic, so a "
                "referent cannot be resolved automatically. Ask the user to "
                "approve an explicit query instead.",
            )

        if not mentions_referent(utterance):
            # Nothing in the utterance needs resolving, so no context is
            # added. Asking for the level does not force context in.
            return SearchQuery(
                text=subject,
                level=PrivacyLevel.USER_ONLY,
                is_persian=persian,
            )

        referents = _extract_referents(context)

        if not referents:
            return SearchQuery(
                text=subject,
                level=PrivacyLevel.USER_ONLY,
                is_persian=persian,
            )

        combined = " ".join((*referents, subject))

        return SearchQuery(
            text=collapse(combined),
            level=PrivacyLevel.CONVERSATION_REFERENT,
            is_persian=is_mostly_persian(combined),
            referent_resolved_from=referents,
        )

    # PRIVATE_CONTEXT
    if not approved:
        raise PrivacyGateError(
            QueryRejection.APPROVAL_REQUIRED,
            "This query would include private context. Show the exact query "
            "to the user and obtain explicit approval before sending it.",
        )

    combined = f"{context.strip()} {subject}"

    return SearchQuery(
        text=collapse(combined),
        level=PrivacyLevel.PRIVATE_CONTEXT,
        is_persian=is_mostly_persian(combined),
        shown_to_user=True,
    )


def collapse(text: str) -> str:
    """Normalise and collapse a composed query."""
    return re.sub(r"\s+", " ", normalise(text)).strip()


def _extract_referents(context: str) -> tuple[str, ...]:
    """
    Pull candidate subject terms out of the conversation context.

    Deliberately crude and deterministic: capitalised runs, alphanumeric model
    designations such as ``RTX 5070``, and quoted phrases. It is not trying to
    understand the sentence — a model doing that is the leak this whole module
    prevents. Anything it cannot recognise is simply not carried over.
    """
    normalised = normalise(context)

    candidates: list[str] = []

    # Model or product designations: letters followed by digits, e.g. RTX 5070.
    for match in re.finditer(
        r"\b[A-Za-z]{2,}[\s-]?\d{2,5}\b",
        normalised,
    ):
        candidates.append(match.group(0).strip())

    # Quoted phrases.
    for match in re.finditer(r'"([^"]{2,60})"', normalised):
        candidates.append(match.group(1).strip())

    # Deduplicate, preserve order, cap the number carried over so a long
    # conversation cannot smuggle a paragraph into the query.
    seen: set[str] = set()
    unique: list[str] = []

    for candidate in candidates:
        key = candidate.lower()

        if key in seen:
            continue

        seen.add(key)
        unique.append(candidate)

    return tuple(unique[:4])


def main() -> None:
    """Demonstrate the privacy gate."""
    print("=== Privacy Query Gate ===\n")

    query = build_query("برام قیمت دلار رو سرچ کن")
    print(f"level 1 : {query.text!r}  persian={query.is_persian}")

    query = build_query(
        "مصرف برقشون رو هم سرچ کن",
        level=PrivacyLevel.CONVERSATION_REFERENT,
        context="RTX 5070 و RTX 4070 رو مقایسه کن",
    )
    print(f"level 2 : {query.text!r}")
    print(f"          resolved from {query.referent_resolved_from}")

    try:
        build_query(
            "درمانش چیه",
            level=PrivacyLevel.CONVERSATION_REFERENT,
            context="بیماری من چیه؟ دکتر گفت ...",
        )
    except PrivacyGateError as exc:
        print(f"sensitive: refused ({exc.rejection.value})")

    try:
        build_query(
            "قیمتش چنده",
            level=PrivacyLevel.PRIVATE_CONTEXT,
            context="فایل بودجه من",
        )
    except PrivacyGateError as exc:
        print(f"level 3 : refused ({exc.rejection.value})")


if __name__ == "__main__":
    main()
