from __future__ import annotations

import json
import unittest

from core.web_answer import (
    ANSWER_SCHEMA,
    DISCLAIMER_EN,
    DISCLAIMER_FA,
    NO_ANSWER_FA,
    AnswerRejection,
    build_prompt,
    extract_citations,
    validate_response,
)
from core.web_evidence import (
    FENCE_OPEN,
    EvidenceItem,
    EvidenceKind,
    EvidencePackage,
)


def package(count: int = 2) -> EvidencePackage:
    return EvidencePackage(
        query="test query",
        items=tuple(
            EvidenceItem(
                ordinal=index + 1,
                kind=EvidenceKind.SNIPPET,
                title=f"title {index + 1}",
                url=f"https://source{index + 1}.example.com/page",
                text=f"evidence text {index + 1}",
            )
            for index in range(count)
        ),
    )


def response(**overrides: object) -> str:
    payload: dict[str, object] = {
        "answered": True,
        "answer": "The answer is forty two [1].",
        "claims": [
            {"statement": "It is forty two", "citations": ["[1]"]},
        ],
    }
    payload.update(overrides)

    return json.dumps(payload, ensure_ascii=False)


class TestSchema(unittest.TestCase):
    def test_schema_requires_the_load_bearing_fields(self) -> None:
        # Grammar-constrained decoding enforces this at the runtime layer, so
        # the schema is the contract rather than a suggestion in a prompt.
        self.assertEqual(
            set(ANSWER_SCHEMA["required"]),  # type: ignore[arg-type]
            {"answered", "answer", "claims"},
        )

    def test_a_claim_must_carry_at_least_one_citation(self) -> None:
        claims = ANSWER_SCHEMA["properties"]["claims"]  # type: ignore[index]
        citations = claims["items"]["properties"]["citations"]  # type: ignore[index]

        self.assertEqual(citations["minItems"], 1)


class TestExtractCitations(unittest.TestCase):
    def test_finds_bracket_labels(self) -> None:
        self.assertEqual(
            extract_citations("a [1] b [12] c"),
            ("[1]", "[12]"),
        )

    def test_ignores_other_brackets(self) -> None:
        self.assertEqual(extract_citations("[a] [x1] plain"), ())

    def test_empty_text_yields_nothing(self) -> None:
        self.assertEqual(extract_citations(""), ())


class TestBuildPrompt(unittest.TestCase):
    def test_rules_come_before_the_untrusted_block(self) -> None:
        # Nothing inside the block can then appear to have replaced them.
        prompt = build_prompt(package(), "what is it?")

        self.assertLess(
            prompt.index("You answer questions"),
            prompt.index(FENCE_OPEN),
        )

    def test_valid_labels_are_stated(self) -> None:
        prompt = build_prompt(package(3), "q")

        self.assertIn("[1], [2], [3]", prompt)

    def test_the_question_comes_last(self) -> None:
        prompt = build_prompt(package(), "what is it?")

        self.assertTrue(prompt.rstrip().endswith("what is it?"))

    def test_persian_rules_are_used_for_a_persian_question(self) -> None:
        prompt = build_prompt(package(), "این چیه؟", persian=True)

        self.assertIn("شواهد", prompt)

    def test_an_empty_package_states_no_valid_labels(self) -> None:
        prompt = build_prompt(EvidencePackage(query="q"), "q")

        self.assertIn("none", prompt)


class TestAcceptance(unittest.TestCase):
    def test_a_well_formed_cited_answer_is_accepted(self) -> None:
        answer = validate_response(response(), package())

        self.assertTrue(answer.ok)
        self.assertIsNone(answer.rejection)
        self.assertEqual(len(answer.claims), 1)

    def test_a_dict_is_accepted_as_well_as_a_string(self) -> None:
        answer = validate_response(json.loads(response()), package())

        self.assertTrue(answer.ok)

    def test_only_cited_sources_appear_in_the_urls(self) -> None:
        # Listing everything read would imply the answer rested on sources it
        # never used.
        answer = validate_response(response(), package(3))

        self.assertEqual(
            answer.cited_urls,
            ("https://source1.example.com/page",),
        )

    def test_multiple_citations_are_all_recorded(self) -> None:
        answer = validate_response(
            response(
                claims=[
                    {"statement": "a", "citations": ["[1]", "[2]"]},
                ]
            ),
            package(2),
        )

        self.assertEqual(len(answer.cited_urls), 2)

    def test_inference_is_carried_through(self) -> None:
        answer = validate_response(
            response(inference="Taken together this suggests X."),
            package(),
        )

        self.assertIn("suggests X", answer.inference)

    def test_disagreement_is_carried_through(self) -> None:
        answer = validate_response(
            response(sources_disagree=True),
            package(),
        )

        self.assertTrue(answer.sources_disagree)

    def test_citations_in_the_prose_are_allowed_when_valid(self) -> None:
        answer = validate_response(
            response(answer="Both agree [1] [2]."),
            package(2),
        )

        self.assertTrue(answer.ok)


class TestRefusals(unittest.TestCase):
    def test_a_fabricated_citation_is_refused(self) -> None:
        # The clearest signature of an invented source.
        answer = validate_response(
            response(claims=[{"statement": "x", "citations": ["[7]"]}]),
            package(2),
        )

        self.assertFalse(answer.ok)
        self.assertIs(answer.rejection, AnswerRejection.FABRICATED_CITATION)

    def test_a_fabricated_citation_in_the_prose_is_refused(self) -> None:
        # A model can cite in the answer text while leaving the claims clean.
        answer = validate_response(
            response(answer="It is so [9]."),
            package(2),
        )

        self.assertIs(answer.rejection, AnswerRejection.FABRICATED_CITATION)

    def test_an_uncited_claim_is_refused(self) -> None:
        answer = validate_response(
            response(claims=[{"statement": "x", "citations": []}]),
            package(),
        )

        self.assertIs(answer.rejection, AnswerRejection.UNCITED_CLAIM)

    def test_claiming_to_answer_with_no_claims_is_refused(self) -> None:
        answer = validate_response(response(claims=[]), package())

        self.assertIs(answer.rejection, AnswerRejection.UNCITED_CLAIM)

    def test_invalid_json_is_refused(self) -> None:
        answer = validate_response("{ not json", package())

        self.assertIs(answer.rejection, AnswerRejection.MALFORMED)

    def test_a_non_object_response_is_refused(self) -> None:
        answer = validate_response("[1, 2, 3]", package())

        self.assertIs(answer.rejection, AnswerRejection.MALFORMED)

    def test_a_non_list_claims_field_is_refused(self) -> None:
        answer = validate_response(response(claims="nope"), package())

        self.assertIs(answer.rejection, AnswerRejection.MALFORMED)

    def test_a_malformed_claim_entry_is_refused(self) -> None:
        answer = validate_response(response(claims=["just a string"]), package())

        self.assertIs(answer.rejection, AnswerRejection.MALFORMED)

    def test_empty_output_is_refused(self) -> None:
        answer = validate_response("   ", package())

        self.assertIs(answer.rejection, AnswerRejection.EMPTY)

    def test_an_empty_evidence_package_can_only_refuse(self) -> None:
        # Answering anyway would mean answering from the model's own knowledge
        # while appearing to have read the web.
        answer = validate_response(response(), EvidencePackage(query="q"))

        self.assertIs(answer.rejection, AnswerRejection.NO_EVIDENCE)

    def test_a_refusal_never_returns_the_unvalidated_text(self) -> None:
        # An answer citing an invented source is worse than no answer, because
        # the citation makes it look checked.
        answer = validate_response(
            response(
                answer="Very confident wrong thing [7].",
                claims=[{"statement": "x", "citations": ["[7]"]}],
            ),
            package(),
            persian=True,
        )

        self.assertNotIn("Very confident", answer.text)
        self.assertEqual(answer.text, NO_ANSWER_FA)

    def test_a_refusal_explains_itself(self) -> None:
        answer = validate_response(
            response(claims=[{"statement": "x", "citations": ["[7]"]}]),
            package(),
        )

        self.assertIn("[7]", answer.detail)

    def test_a_refusal_shows_no_disclaimer(self) -> None:
        # There is nothing to disclaim if nothing was answered.
        answer = validate_response("{ bad", package())

        self.assertEqual(answer.disclaimer, "")


class TestHonestNoAnswer(unittest.TestCase):
    def test_answered_false_is_a_valid_outcome_not_a_failure(self) -> None:
        answer = validate_response(
            response(answered=False, answer="I could not find it."),
            package(),
        )

        self.assertFalse(answer.answered)
        self.assertIsNone(answer.rejection)

    def test_a_persian_refusal_uses_persian_wording(self) -> None:
        answer = validate_response(
            response(answered=False, answer=""),
            package(),
            persian=True,
        )

        self.assertEqual(answer.text, NO_ANSWER_FA)

    def test_no_claims_are_required_when_not_answering(self) -> None:
        answer = validate_response(
            response(answered=False, answer="no idea", claims=[]),
            package(),
        )

        self.assertIsNone(answer.rejection)


class TestRendering(unittest.TestCase):
    def test_a_persian_answer_carries_the_persian_disclaimer(self) -> None:
        answer = validate_response(response(), package(), persian=True)

        self.assertIn(DISCLAIMER_FA, answer.render())

    def test_an_english_answer_carries_the_english_disclaimer(self) -> None:
        answer = validate_response(response(), package())

        self.assertIn(DISCLAIMER_EN, answer.render())

    def test_inference_is_rendered_after_the_answer(self) -> None:
        answer = validate_response(
            response(inference="My reading is X."),
            package(),
        )

        rendered = answer.render()

        self.assertLess(
            rendered.index("forty two"),
            rendered.index("My reading is X."),
        )

    def test_provenance_is_appended_when_supplied(self) -> None:
        answer = validate_response(response(), package())

        rendered = answer.render(provenance="خوانده شد از 1 منبع")

        self.assertIn("خوانده شد", rendered)

    def test_the_disclaimer_can_be_suppressed_for_a_repeat_answer(self) -> None:
        # Said once per session, not per answer, or it becomes noise.
        answer = validate_response(response(), package())
        quiet = type(answer)(
            answered=answer.answered,
            text=answer.text,
            claims=answer.claims,
            is_persian=answer.is_persian,
            show_disclaimer=False,
        )

        self.assertNotIn(DISCLAIMER_EN, quiet.render())


if __name__ == "__main__":
    unittest.main()
