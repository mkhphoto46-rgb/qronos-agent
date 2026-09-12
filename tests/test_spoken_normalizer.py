from __future__ import annotations

from core.spoken_normalizer import normalise_spoken_text


def test_isolated_four_without_period_gets_context() -> None:
    assert normalise_spoken_text("چهار") == "می‌شود چهار."


def test_isolated_four_with_period_gets_context() -> None:
    assert normalise_spoken_text("چهار.") == "می‌شود چهار."


def test_four_inside_sentence_is_not_rewritten() -> None:
    assert normalise_spoken_text("چهار گزینه داریم.") == "چهار گزینه داریم."


def test_sentence_containing_four_is_not_rewritten() -> None:
    assert normalise_spoken_text("جواب چهار است.") == "جواب چهار است."


def test_related_word_is_not_rewritten() -> None:
    assert normalise_spoken_text("چهاردهم") == "چهاردهم"


def test_whitespace_is_safe_and_conservative() -> None:
    assert normalise_spoken_text("  چهار.  ") == "می‌شود چهار."


def test_empty_text_stays_empty() -> None:
    assert normalise_spoken_text("   ") == ""
