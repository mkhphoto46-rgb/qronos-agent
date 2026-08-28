"""
The caller the web research layer never had.

Fifteen tested modules — search, fetch, extraction, evidence, provenance,
caching, rate limiting, the privacy gate — sit in ``core/web_*.py`` with, until
now, no importer outside their own package and the test suite. They work. They
were simply never connected to anything a user could reach.

This is the connection, and it covers **read-only research only**. That
distinction is the whole reason this module has a long docstring:

    Qronos routes "search the web for X", "go to Y" and "send this message"
    all to ``BROWSER``. The first is a lookup. The last two change something on
    somebody else's computer. Answering a question from the web needs no
    permission beyond the privacy gate the query already passes; sending a
    message on the user's behalf needs a confirmation and an audit entry.

    So this worker answers questions and **refuses interactive requests
    explicitly**, rather than doing the lookup half and quietly ignoring the
    rest. A user who says "go to the bank site and pay the bill" must not get a
    summary of the bank's home page and the impression something happened.

Interactive browser control stays unbuilt. When it lands it goes through
:mod:`security.gate` like every other action, and this worker hands off to it.
"""

from __future__ import annotations

from typing import Callable

from core.brain_runtime import BrainRuntime
from core.persian_text import contains_marker, normalise
from core.web_answer import ANSWER_SCHEMA
from core.task_plan import PlanStep
from core.task_router import TaskType
from core.web_query import PrivacyGateError, build_query
from core.web_research import ResearchEvent, WebResearch
from core.workers import TaskWorker, WorkerOutput


# Requests that change something rather than read it. Matched in both
# languages, using the same normalise-then-marker rule the router uses, because
# a Persian request to send a message must be refused as clearly as an English
# one.
_INTERACTIVE_MARKERS = (
    "send a message",
    "send this message",
    "send an email",
    "log in",
    "sign in",
    "buy",
    "purchase",
    "pay",
    "book a",
    "submit",
    "fill in",
    "پیام بفرست",
    "ایمیل بفرست",
    "وارد شو",
    "لاگین",
    "بخر",
    "خرید کن",
    "پرداخت کن",
    "رزرو کن",
    "ثبت کن",
    "پر کن",
)

_INTERACTIVE = tuple(
    normalise(marker).lower() for marker in _INTERACTIVE_MARKERS
)


REFUSAL_MESSAGE = (
    "Qronos can look this up on the web, but it cannot act on a website — "
    "sending, buying, signing in or filling anything in. That needs browser "
    "control, which is not built yet."
)


def is_interactive_request(text: str) -> bool:
    """
    True when the request wants something done, not something found.

    Deterministic and deliberately cautious. The cost of a false positive is a
    lookup that gets refused and can be rephrased; the cost of a false negative
    is Qronos appearing to have acted on a website when it did not.
    """
    return contains_marker(normalise(text).lower(), _INTERACTIVE)


def brain_answer_fn(
    runtime: BrainRuntime,
    model_name: str,
    num_ctx: int | None = None,
) -> Callable[[str], str]:
    """
    An answer function that returns the shape the validator requires.

    The evidence prompt asks for a JSON object; this constrains generation
    to :data:`core.web_answer.ANSWER_SCHEMA` as well, so the reply is that
    object rather than a well-meant approximation of one.

    Without the constraint the model answered in cited prose — correctly,
    and usefully — and ``validate_response`` rejected all of it as
    MALFORMED, so every lookup ended in "I could not find a clear answer".
    """

    def answer(prompt: str) -> str:
        return runtime.chat(
            model_name=model_name,
            prompt=prompt,
            num_predict=600,
            num_ctx=num_ctx,
            response_format=ANSWER_SCHEMA,
        )

    return answer

class WebResearchWorker(TaskWorker):
    """
    Answers a question from the web.

    Registered for ``BROWSER``, which is the task type the router already sends
    web lookups to. It does not make browser control available: see the module
    docstring, and :meth:`execute`, which refuses interactive requests.
    """

    task_type = TaskType.BROWSER

    def __init__(
        self,
        research: WebResearch | None = None,
        answer_fn: Callable[[str], str] | None = None,
        on_event: Callable[[ResearchEvent], None] | None = None,
    ) -> None:
        # ``answer_fn`` is the call into a brain. Without it the pipeline runs
        # and stops at the prompt, which is how the search and evidence layers
        # are exercised with no model present — the same arrangement
        # WebResearch itself documents.
        self.research = research or WebResearch(
            answer_fn=answer_fn,
            on_event=on_event,
        )

    def health_check(self) -> bool:
        """
        True when a lookup could be attempted.

        Not a network check. Reaching out to prove the internet works would
        make asking whether a capability exists cost a request, and would fail
        on a machine that is offline right now but will not be in a minute —
        which for a local-first assistant is a normal state, not an error.
        """
        return self.research is not None

    def execute(self, step: PlanStep) -> WorkerOutput:
        request = step.description

        if is_interactive_request(request):
            return WorkerOutput(
                output="",
                success=False,
                error=REFUSAL_MESSAGE,
            )

        try:
            query = build_query(request)
        except PrivacyGateError as error:
            # The privacy gate refused to build a query. Its reason is the
            # honest answer; inventing a looser query would defeat the gate.
            return WorkerOutput(
                output="",
                success=False,
                error=str(error),
            )

        try:
            result = self.research.research(query)
        except Exception as error:
            return WorkerOutput(
                output="",
                success=False,
                error=str(error),
            )

        if not result.ok:
            # A pipeline that ran and produced nothing usable. Its own
            # description says where it stopped, which is more useful than a
            # generic failure.
            return WorkerOutput(
                output="",
                success=False,
                error=result.render(),
            )

        return WorkerOutput(output=result.render())
