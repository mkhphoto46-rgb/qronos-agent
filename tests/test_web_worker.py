from __future__ import annotations

import unittest

from core.orchestrator import Orchestrator
from core.task_plan import PlanStep, TaskPlan
from core.task_router import TaskType
from core.web_answer import (
    ANSWER_SCHEMA,
    SYSTEM_PROMPT_EN,
    SYSTEM_PROMPT_FA,
)
from core.web_worker import (
    brain_answer_fn,
    REFUSAL_MESSAGE,
    WebResearchWorker,
    is_interactive_request,
)
from core.workers import WorkerRegistry


class FakeAnswer:
    def __init__(self, ok: bool, text: str) -> None:
        self.ok = ok
        self._text = text

    def render(self) -> str:
        return self._text


class FakeResult:
    def __init__(self, ok: bool, text: str) -> None:
        self.ok = ok
        self._text = text

    def render(self) -> str:
        return self._text


class FakeResearch:
    """Stands in for the pipeline. The pipeline has its own 15 test modules."""

    def __init__(
        self,
        result: FakeResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or FakeResult(True, "پاسخ از وب")
        self.error = error
        self.queries: list[object] = []

    def research(self, query, force_fetch: bool = False) -> FakeResult:
        self.queries.append(query)

        if self.error is not None:
            raise self.error

        return self.result


def worker(**kwargs) -> WebResearchWorker:
    return WebResearchWorker(research=FakeResearch(**kwargs))


def a_step(description: str) -> PlanStep:
    plan = TaskPlan(goal=description)
    plan.add_step(TaskType.BROWSER, description)

    return plan.steps[0]


class TestLookupsWork(unittest.TestCase):
    def test_a_question_is_answered(self) -> None:
        result = worker().execute(a_step("what is the capital of Norway"))

        self.assertTrue(result.success)
        self.assertEqual(result.output, "پاسخ از وب")

    def test_a_persian_question_is_answered(self) -> None:
        result = worker().execute(a_step("پایتخت نروژ کجاست"))

        self.assertTrue(result.success)

    def test_the_query_reaches_the_pipeline(self) -> None:
        research = FakeResearch()
        WebResearchWorker(research=research).execute(
            a_step("what is the capital of Norway")
        )

        self.assertEqual(len(research.queries), 1)

    def test_a_pipeline_that_produced_nothing_reports_why(self) -> None:
        instance = WebResearchWorker(
            research=FakeResearch(
                result=FakeResult(False, "no usable sources were found")
            )
        )

        result = instance.execute(a_step("something obscure"))

        self.assertFalse(result.success)
        self.assertIn("no usable sources", result.error)

    def test_a_raising_pipeline_does_not_escape(self) -> None:
        instance = WebResearchWorker(
            research=FakeResearch(error=OSError("the network is down"))
        )

        result = instance.execute(a_step("what is the capital of Norway"))

        self.assertFalse(result.success)
        self.assertIn("network is down", result.error)


class TestInteractiveRequestsAreRefused(unittest.TestCase):
    """
    The distinction this worker exists to make.

    "Search the web for X", "go to Y" and "send this message" all route to
    BROWSER. The first is a lookup; the last changes something on somebody
    else's computer. Doing the lookup half of an interactive request and
    saying nothing about the rest would leave the user believing something
    happened.
    """

    def test_sending_a_message_is_refused(self) -> None:
        result = worker().execute(
            a_step("go to the site and send this message to my brother")
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, REFUSAL_MESSAGE)

    def test_the_pipeline_is_not_even_reached(self) -> None:
        # A refusal that still performed the search would leak the request to a
        # search engine on the way to saying no.
        research = FakeResearch()
        WebResearchWorker(research=research).execute(
            a_step("log in to my bank")
        )

        self.assertEqual(research.queries, [])

    def test_persian_interactive_requests_are_refused_too(self) -> None:
        for request in (
            "برو به سایت و پیام بفرست",
            "این را بخر",
            "وارد شو به حساب من",
            "قبض را پرداخت کن",
        ):
            with self.subTest(request=request):
                self.assertTrue(is_interactive_request(request))

    def test_english_interactive_requests_are_recognised(self) -> None:
        for request in (
            "send an email to my brother",
            "sign in to my account",
            "buy this for me",
            "book a table for two",
            "fill in the form",
        ):
            with self.subTest(request=request):
                self.assertTrue(is_interactive_request(request))

    def test_a_plain_lookup_is_not_treated_as_interactive(self) -> None:
        for request in (
            "what is the capital of Norway",
            "search the web for train times",
            "قیمت دلار چند است",
            "برو به ویکی‌پدیا و بگو چه نوشته",
        ):
            with self.subTest(request=request):
                self.assertFalse(is_interactive_request(request))


class TestThroughTheOrchestrator(unittest.TestCase):
    def test_registering_the_worker_makes_browser_tasks_run(self) -> None:
        # The end of the chain the fifteen web modules never had: a router
        # decision reaching an orchestrator step reaching the pipeline.
        registry = WorkerRegistry()
        registry.register(worker())

        plan = TaskPlan(goal="lookup")
        plan.add_step(TaskType.BROWSER, "what is the capital of Norway")

        result = Orchestrator(workers=registry).execute_plan(plan)[0]

        self.assertTrue(result.success)
        self.assertEqual(result.output, "پاسخ از وب")

    def test_without_the_worker_browser_is_still_unavailable(self) -> None:
        plan = TaskPlan(goal="lookup")
        plan.add_step(TaskType.BROWSER, "what is the capital of Norway")

        result = Orchestrator().execute_plan(plan)[0]

        self.assertIsNotNone(result.unavailable)

    def test_an_interactive_request_fails_the_step_rather_than_the_run(
        self,
    ) -> None:
        registry = WorkerRegistry()
        registry.register(worker())

        plan = TaskPlan(goal="interactive")
        plan.add_step(TaskType.BROWSER, "send this message to my brother")

        result = Orchestrator(workers=registry).execute_plan(plan)[0]

        self.assertFalse(result.success)
        # Not "unavailable": the worker exists and answered. It declined.
        self.assertIsNone(result.unavailable)


class TestHealthCheck(unittest.TestCase):
    def test_the_health_check_does_not_reach_the_network(self) -> None:
        # Asking whether a capability exists must not cost a request, and an
        # offline machine is a normal state for a local-first assistant, not an
        # error.
        research = FakeResearch()

        self.assertTrue(WebResearchWorker(research=research).health_check())
        self.assertEqual(research.queries, [])


if __name__ == "__main__":
    unittest.main()


class TestTheAnswerFormatIsEnforced(unittest.TestCase):
    """
    The prompt and the validator have to agree, and they did not.

    `validate_response` calls `json.loads` and requires an object shaped
    `{"answered", "answer", "claims": [{"statement", "citations"}]}`. The
    prompt described those fields in English prose and never used the word
    JSON, so a model doing exactly as asked produced cited prose that was
    rejected as MALFORMED — and every real lookup ended in "I could not find
    a clear answer", however good the search had been.

    Two halves to the fix, and both are checked here: the prompt states the
    contract, and a runtime that supports structured output is given the
    schema so the shape is enforced rather than requested.
    """

    def test_both_prompts_ask_for_json(self) -> None:
        for name, prompt in (
            ("english", SYSTEM_PROMPT_EN),
            ("persian", SYSTEM_PROMPT_FA),
        ):
            with self.subTest(prompt=name):
                self.assertIn("JSON", prompt)

    def test_both_prompts_name_every_required_field(self) -> None:
        for name, prompt in (
            ("english", SYSTEM_PROMPT_EN),
            ("persian", SYSTEM_PROMPT_FA),
        ):
            for field in ("answered", "answer", "claims", "citations"):
                with self.subTest(prompt=name, field=field):
                    self.assertIn(field, prompt)

    def test_the_answer_function_constrains_the_reply(self) -> None:
        captured: dict = {}

        class RecordingRuntime:
            def chat(self, **kwargs):
                captured.update(kwargs)
                return "{}"

        brain_answer_fn(RecordingRuntime(), "some-model", num_ctx=8192)("p")

        self.assertIs(captured["response_format"], ANSWER_SCHEMA)
        self.assertEqual(captured["num_ctx"], 8192)

    def test_the_schema_matches_what_the_validator_requires(self) -> None:
        # If these drift apart the model is constrained to the wrong shape,
        # which fails exactly as silently as no constraint at all.
        self.assertEqual(
            set(ANSWER_SCHEMA["required"]),
            {"answered", "answer", "claims"},
        )
        claim = ANSWER_SCHEMA["properties"]["claims"]["items"]
        self.assertEqual(set(claim["required"]), {"statement", "citations"})
