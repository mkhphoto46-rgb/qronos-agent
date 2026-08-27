from __future__ import annotations

import unittest

from core.web_query import (
    PrivacyGateError,
    PrivacyLevel,
    QueryRejection,
    build_query,
    context_is_sensitive,
    looks_like_search_request,
    mentions_referent,
    strip_filler,
)


class TestIntentDetection(unittest.TestCase):
    def test_persian_search_request_is_recognised(self) -> None:
        self.assertTrue(looks_like_search_request("برام قیمت دلار رو سرچ کن"))

    def test_english_search_request_is_recognised(self) -> None:
        self.assertTrue(looks_like_search_request("search for python dataclass"))

    def test_news_and_latest_count_as_search(self) -> None:
        self.assertTrue(looks_like_search_request("آخرین اخبار ایران"))
        self.assertTrue(looks_like_search_request("latest news"))

    def test_an_ordinary_statement_is_not_a_search(self) -> None:
        # Anything ambiguous returns False so the caller asks rather than
        # guessing. Searching the web for something meant locally is worse
        # than one clarifying question.
        self.assertFalse(looks_like_search_request("سلام حالت چطوره"))
        self.assertFalse(looks_like_search_request("open premiere"))

    def test_empty_input_is_not_a_search(self) -> None:
        self.assertFalse(looks_like_search_request("   "))


class TestStripFiller(unittest.TestCase):
    def test_persian_request_wrapping_is_removed(self) -> None:
        self.assertEqual(
            strip_filler("برام قیمت دلار رو سرچ کن"),
            "قیمت دلار",
        )

    def test_english_request_wrapping_is_removed(self) -> None:
        self.assertEqual(
            strip_filler("please search for python dataclass"),
            "python dataclass",
        )

    def test_trailing_punctuation_is_trimmed(self) -> None:
        self.assertEqual(strip_filler("python dataclass?"), "python dataclass")

    def test_subject_only_input_is_untouched(self) -> None:
        self.assertEqual(strip_filler("قیمت طلا"), "قیمت طلا")


class TestReferentDetection(unittest.TestCase):
    def test_persian_possessive_suffix_is_a_referent(self) -> None:
        self.assertTrue(mentions_referent("مصرف برقشون رو هم سرچ کن"))

    def test_english_pronoun_is_a_referent(self) -> None:
        self.assertTrue(mentions_referent("search their power consumption"))

    def test_a_self_contained_question_has_no_referent(self) -> None:
        self.assertFalse(mentions_referent("قیمت دلار امروز"))


class TestSensitiveContext(unittest.TestCase):
    def test_medical_context_is_sensitive(self) -> None:
        self.assertTrue(context_is_sensitive("دکتر گفت بیماری من چیه"))

    def test_financial_context_is_sensitive(self) -> None:
        self.assertTrue(context_is_sensitive("حساب بانکی و بدهی من"))

    def test_credentials_are_sensitive(self) -> None:
        self.assertTrue(context_is_sensitive("my password is stored here"))

    def test_hardware_comparison_is_not_sensitive(self) -> None:
        self.assertFalse(
            context_is_sensitive("RTX 5070 و RTX 4070 رو مقایسه کن")
        )


class TestLevelOne(unittest.TestCase):
    def test_query_is_built_from_the_utterance_alone(self) -> None:
        query = build_query("برام قیمت دلار رو سرچ کن")

        self.assertEqual(query.text, "قیمت دلار")
        self.assertIs(query.level, PrivacyLevel.USER_ONLY)
        self.assertTrue(query.is_persian)
        self.assertFalse(query.shown_to_user)

    def test_english_query(self) -> None:
        query = build_query("search for python dataclass")

        self.assertEqual(query.text, "python dataclass")
        self.assertFalse(query.is_persian)

    def test_empty_utterance_is_refused(self) -> None:
        with self.assertRaises(PrivacyGateError) as caught:
            build_query("   ")

        self.assertIs(caught.exception.rejection, QueryRejection.EMPTY)

    def test_filler_only_utterance_is_refused(self) -> None:
        # "just search for me" has no subject, so there is nothing to send.
        with self.assertRaises(PrivacyGateError) as caught:
            build_query("لطفا برام سرچ کن")

        self.assertIs(caught.exception.rejection, QueryRejection.EMPTY)

    def test_no_context_ever_appears_at_level_one(self) -> None:
        # The rule this module exists for: nothing private reaches the query.
        query = build_query(
            "قیمت دلار",
            level=PrivacyLevel.USER_ONLY,
            context="RTX 5070 و بیماری من",
        )

        self.assertNotIn("RTX", query.text)
        self.assertNotIn("بیماری", query.text)


class TestLevelTwo(unittest.TestCase):
    def test_referent_is_resolved_from_conversation(self) -> None:
        # "their power consumption" is meaningless without the referent, so an
        # absolute ban on context would break ordinary conversation.
        query = build_query(
            "مصرف برقشون رو هم سرچ کن",
            level=PrivacyLevel.CONVERSATION_REFERENT,
            context="RTX 5070 و RTX 4070 رو مقایسه کن",
        )

        self.assertIn("RTX 5070", query.text)
        self.assertIn("RTX 4070", query.text)
        self.assertIs(query.level, PrivacyLevel.CONVERSATION_REFERENT)
        self.assertEqual(
            query.referent_resolved_from,
            ("RTX 5070", "RTX 4070"),
        )

    def test_sensitive_context_is_refused(self) -> None:
        # The leak this whole design prevents: three words from the user, a
        # medical record out to a search engine.
        with self.assertRaises(PrivacyGateError) as caught:
            build_query(
                "درمانش چیه",
                level=PrivacyLevel.CONVERSATION_REFERENT,
                context="دکتر گفت بیماری من فلان است",
            )

        self.assertIs(
            caught.exception.rejection,
            QueryRejection.SENSITIVE_REFERENT,
        )

    def test_no_referent_in_utterance_means_no_context_is_added(self) -> None:
        # Asking for the level does not force context in. A self-contained
        # question stays self-contained.
        query = build_query(
            "قیمت دلار امروز",
            level=PrivacyLevel.CONVERSATION_REFERENT,
            context="RTX 5070 و RTX 4070",
        )

        self.assertIs(query.level, PrivacyLevel.USER_ONLY)
        self.assertNotIn("RTX", query.text)

    def test_unrecognisable_context_carries_nothing_over(self) -> None:
        query = build_query(
            "مصرف برقشون چقدره",
            level=PrivacyLevel.CONVERSATION_REFERENT,
            context="یه چیزی گفتم که هیچ اسم مشخصی نداشت",
        )

        self.assertIs(query.level, PrivacyLevel.USER_ONLY)

    def test_number_of_carried_referents_is_capped(self) -> None:
        # A long conversation must not be able to smuggle a paragraph into a
        # query.
        context = " ".join(f"GPU {1000 + i}" for i in range(20))

        query = build_query(
            "مصرف برقشون",
            level=PrivacyLevel.CONVERSATION_REFERENT,
            context=context,
        )

        self.assertLessEqual(len(query.referent_resolved_from), 4)

    def test_quoted_phrases_are_eligible_referents(self) -> None:
        query = build_query(
            "قیمتشون چنده",
            level=PrivacyLevel.CONVERSATION_REFERENT,
            context='درباره "Sony WH-1000XM5" حرف زدیم',
        )

        self.assertTrue(query.referent_resolved_from)


class TestLevelThree(unittest.TestCase):
    def test_private_context_without_approval_is_refused(self) -> None:
        with self.assertRaises(PrivacyGateError) as caught:
            build_query(
                "قیمتش چنده",
                level=PrivacyLevel.PRIVATE_CONTEXT,
                context="فایل بودجه شخصی من",
            )

        self.assertIs(
            caught.exception.rejection,
            QueryRejection.APPROVAL_REQUIRED,
        )

    def test_approved_private_context_is_allowed_and_marked(self) -> None:
        query = build_query(
            "قیمتش چنده",
            level=PrivacyLevel.PRIVATE_CONTEXT,
            context="Sony WH-1000XM5",
            approved=True,
        )

        self.assertIs(query.level, PrivacyLevel.PRIVATE_CONTEXT)
        self.assertTrue(query.shown_to_user)
        self.assertIn("Sony", query.text)

    def test_approval_flag_alone_does_not_bypass_the_empty_check(self) -> None:
        with self.assertRaises(PrivacyGateError):
            build_query(
                "لطفا",
                level=PrivacyLevel.PRIVATE_CONTEXT,
                context="anything",
                approved=True,
            )


class TestFailClosed(unittest.TestCase):
    def test_a_level_needing_context_collapses_without_it(self) -> None:
        # Fail-closed direction: less context, never more.
        for level in (
            PrivacyLevel.CONVERSATION_REFERENT,
            PrivacyLevel.PRIVATE_CONTEXT,
        ):
            with self.subTest(level=level.value):
                query = build_query("قیمت دلار", level=level)

                self.assertIs(query.level, PrivacyLevel.USER_ONLY)

    def test_blank_context_is_treated_as_no_context(self) -> None:
        query = build_query(
            "قیمت دلار",
            level=PrivacyLevel.PRIVATE_CONTEXT,
            context="   ",
        )

        self.assertIs(query.level, PrivacyLevel.USER_ONLY)

    def test_refusal_raises_rather_than_degrading_silently(self) -> None:
        # A caller must not be able to mistake a refusal for a built query.
        with self.assertRaises(PrivacyGateError):
            build_query(
                "درمانش چیه",
                level=PrivacyLevel.CONVERSATION_REFERENT,
                context="بیماری من",
            )

    def test_query_text_is_normalised(self) -> None:
        # So two spellings of the same request hit the same cache entry.
        first = build_query("سرچ کن نرم‌افزار مديريت")
        second = build_query("سرچ کن نرم افزار مدیریت")

        self.assertEqual(first.text, second.text)


if __name__ == "__main__":
    unittest.main()
