"""
The worker that looks at things.

``TaskType.VISION`` has existed in the router since the beginning and routed to
nothing. A request to look at something came back "Qronos cannot carry out
vision tasks yet" and, because ``execute_plan`` stops at the first failure,
took the rest of the plan down with it.

**One model sees, another thinks.** The 4B vision model is very good at turning
pixels into words — measured here at 0.000 character error on English, 0.850
word recall on Persian, and 0.92 overlap when asked to point at a button. It is
a 4B model, so it is not the thing to ask *why the build failed*. So this
worker asks it to describe, and when the question wants more than a
description, hands the description to the heavy brain as text. The heavy brain
never sees the picture and does not need to: by then the picture is words.

That hand-off is a **stated rule**, not a judgement call, because a model that
escalates everything is as useless as one that never does, and neither is
noticeable without something to point at. :func:`needs_reasoning` is that rule,
and it is tested in both languages.

Like :class:`core.web_worker.WebResearchWorker`, this reaches a brain through
an injected callable and never imports one. That is what keeps ``TaskClass``
at FAST and HEAVY: vision is a worker that *uses* a model, not a new kind of
model, and widening that enum would ask ``ModelManager`` to find a profile for
something that does not run on a brain.

Nothing here touches the machine. Capturing the screen does, and that lives
behind the permission gate in a later phase; this worker is given pictures, it
does not go and get them.
"""

from __future__ import annotations

from typing import Callable, Sequence

from core.brain_runtime import BrainMessage, BrainMessageRole, BrainRuntime
from core.model_registry import get_model
from core.persian_text import contains_marker, normalise
from core.task_plan import PlanStep
from core.task_router import TaskType
from core.vision_image import ImageUnusable, cost
from core.workers import TaskWorker, WorkerOutput


#: Room kept for the question, the system prompt and the answer, on top of
#: whatever the pictures cost. The declared context is 4,096 and a picture at
#: the size Qronos sends is about 1,080, so this is what decides how many
#: pictures fit — two comfortably, three at a squeeze, four not at all.
TEXT_ALLOWANCE_TOKENS = 768


#: Questions that want more than a description of what is there. Matched in
#: both languages with the router's normalise-then-marker rule, because a
#: Persian request to explain something must escalate as an English one does.
_REASONING_MARKERS = (
    "why",
    "explain",
    "cause",
    "diagnose",
    "fix",
    "how do i",
    "how should i",
    "should i",
    "what went wrong",
    "what does it mean",
    "compare",
    "decide",
    "recommend",
    "suggest",
    "plan",
    "analyse",
    "analyze",
    "چرا",
    "توضیح بده",
    "دلیل",
    "علت",
    "عیب",
    "درست کن",
    "چطور",
    "چگونه",
    "یعنی چه",
    "یعنی چی",
    "مقایسه",
    "پیشنهاد",
    "تحلیل",
    "بررسی کن",
)

_REASONING = tuple(normalise(marker).lower() for marker in _REASONING_MARKERS)


NO_PICTURE_MESSAGE = (
    "Qronos was asked to look at something but was not given anything to "
    "look at. Attach a picture, or ask it to read the screen."
)

NO_MODEL_MESSAGE = (
    "Qronos cannot look at pictures on this machine because the vision "
    "model is not available."
)

SAW_NOTHING_MESSAGE = (
    "Qronos looked at the picture and could not describe it."
)


def needs_reasoning(question: str) -> bool:
    """
    True when the question wants thinking rather than describing.

    Deterministic, and deliberately narrow. "What is on my screen" is a
    description and stays with the small model; "why did this fail" is not.
    The cost of escalating too eagerly is the heavy brain being loaded for a
    question that did not need it, which on this card is ten gigabytes and
    several seconds — so the rule errs towards not escalating, and the
    description is returned either way.
    """
    return contains_marker(normalise(question).lower(), _REASONING)


DESCRIBE_INSTRUCTION = (
    "Describe exactly what is visible in this image. Transcribe any text you "
    "can read, word for word, including error codes and numbers. Do not "
    "guess at anything you cannot see. Be specific and factual."
)


#: How a reading somebody else made is offered to the model.
#:
#: Every word of this is doing something. "May be wrong" and "reading order may
#: be scrambled" are true — measured — and saying so is what makes the model
#: reconcile the hint against the pixels rather than copy it. "At a higher
#: resolution than you can see" is the reason to consult it at all, and is also
#: true: the hint is read at full size and the picture is sent shrunk.
HINT_PREAMBLE = (
    "Another program read this image with optical character recognition and "
    "produced the text below. It read the image at a higher resolution than "
    "you can see, so it is worth checking against — especially for codes and "
    "numbers. Its reading order may be scrambled and it may contain mistakes, "
    "so trust your own eyes about what is where."
)


def with_hints(question: str, images: Sequence[object]) -> str:
    """
    The question, with anything already read off the pictures attached.

    Unchanged when there is nothing to attach, so a request with no hint is
    exactly the request Qronos made before hints existed.
    """
    hints = [
        getattr(image, "hint", "")
        for image in images
        if getattr(image, "hint", "")
    ]

    if not hints:
        return question

    body = "\n\n".join(hints)

    return (
        f"{HINT_PREAMBLE}\n\n"
        f"--- what the other program read ---\n{body}\n--- end ---\n\n"
        f"{question}"
    )


def brain_describe_fn(
    runtime: BrainRuntime,
    model_name: str | None = None,
    num_ctx: int | None = None,
) -> Callable[[str, Sequence[str]], str]:
    """
    A describe function backed by a real vision model.

    The counterpart of :func:`core.web_worker.brain_answer_fn`, and the only
    place in the vision path that knows a model runtime exists.

    ``think`` is off. This model's job is to report what is in front of it, and
    a thinking pass on that spends tokens deliberating about pixels it has
    already seen.
    """
    profile = get_model("vision")
    name = model_name or profile.name
    context = num_ctx if num_ctx is not None else profile.context_tokens

    def describe(question: str, images: Sequence[str]) -> str:
        asked = with_hints(
            f"{DESCRIBE_INSTRUCTION}\n\n{question}".strip(), images
        )

        answer = runtime.chat(
            model_name=name,
            messages=[
                BrainMessage(
                    role=BrainMessageRole.USER,
                    content=asked,
                    images=tuple(images),
                )
            ],
            think=False,
            num_predict=512,
            num_ctx=context,
            # Nothing stays on the card between turns. See ModelManager.
            keep_alive="0",
        )

        runtime.stop_model(name)

        return answer

    return describe


REASON_PREAMBLE = (
    "Qronos looked at an image and this is what it saw. You cannot see the "
    "image yourself, so treat this description as your only evidence and say "
    "so if it is not enough to answer."
)


def brain_reason_fn(
    runtime: BrainRuntime,
    model_name: str | None = None,
    num_ctx: int | None = None,
) -> Callable[[str, str], str]:
    """The heavy brain, reasoning over a description it did not produce."""
    profile = get_model("heavy")
    name = model_name or profile.name
    context = num_ctx if num_ctx is not None else profile.context_tokens

    def reason(question: str, description: str) -> str:
        answer = runtime.chat(
            model_name=name,
            messages=[
                BrainMessage(
                    role=BrainMessageRole.USER,
                    content=(
                        f"{REASON_PREAMBLE}\n\n"
                        f"What was seen:\n{description}\n\n"
                        f"The question:\n{question}"
                    ),
                )
            ],
            think=True,
            num_predict=512,
            num_ctx=context,
            keep_alive="0",
        )

        runtime.stop_model(name)

        return answer

    return reason


class VisionWorker(TaskWorker):
    """
    Answers a question about a picture.

    ``describe_fn`` turns pixels into words. ``reason_fn`` is optional: without
    it the worker still answers, it simply answers with the description rather
    than escalating. That is the same arrangement the web worker documents,
    and it is what lets every path here be exercised with no model present.
    """

    task_type = TaskType.VISION

    def __init__(
        self,
        describe_fn: Callable[[str, Sequence[str]], str] | None = None,
        reason_fn: Callable[[str, str], str] | None = None,
        context_tokens: int | None = None,
    ) -> None:
        self.describe_fn = describe_fn
        self.reason_fn = reason_fn
        self.context_tokens = (
            context_tokens
            if context_tokens is not None
            else get_model("vision").context_tokens
        )

    def health_check(self) -> bool:
        """
        True when a picture could be looked at right now.

        Not a model check. Asking Ollama whether the model is pulled would
        make "can you see" cost a request, and would say no on a machine that
        is merely slow to start — for a local-first assistant a normal state,
        not an error. What it does check is that there is something to call.
        """
        return self.describe_fn is not None

    def execute(self, step: PlanStep) -> WorkerOutput:
        if not step.images:
            return self._refuse(NO_PICTURE_MESSAGE)

        if self.describe_fn is None:
            return self._refuse(NO_MODEL_MESSAGE)

        try:
            budget = self._check_budget(step.images)
        except ImageUnusable as error:
            # Named specifically — "there is no file at that path" sends a user
            # somewhere completely different from "that is not a picture".
            return self._refuse(str(error))

        if budget is not None:
            return self._refuse(budget)

        try:
            description = self.describe_fn(step.description, step.images)
        except Exception as error:
            return self._refuse(str(error))

        description = (description or "").strip()

        if not description:
            # Rather than returning success with nothing in it, which reads to
            # everything downstream as a picture of an empty room.
            return self._refuse(SAW_NOTHING_MESSAGE)

        if not needs_reasoning(step.description) or self.reason_fn is None:
            return WorkerOutput(output=description)

        try:
            reasoned = self.reason_fn(step.description, description)
        except Exception:
            # The description is real and was produced. Losing it because the
            # second model failed would turn a partial answer into no answer.
            return WorkerOutput(output=description)

        reasoned = (reasoned or "").strip()

        return WorkerOutput(output=reasoned or description)

    def _check_budget(self, images: Sequence[str]) -> str | None:
        """
        None when the pictures fit, otherwise why they do not.

        Measured from the headers rather than by sending them and finding out:
        a request that overflows the context does not fail loudly, it silently
        loses the beginning of itself, and what gets lost first is the
        instruction telling the model what to do.
        """
        total = sum(cost(image) for image in images)

        # A hint is text and text costs context. Roughly four characters to a
        # token, which is close enough for a budget check: a full desktop reads
        # as about 550 words, or some 900 tokens, which is most of a picture
        # again and would silently overflow a 4,096-token context otherwise.
        total += sum(
            len(getattr(image, "hint", "")) // 4 for image in images
        )

        if total + TEXT_ALLOWANCE_TOKENS <= self.context_tokens:
            return None

        return (
            f"That is too much to look at in one go — {len(images)} "
            f"picture{'s' if len(images) != 1 else ''} would need about "
            f"{total} tokens and Qronos has room for about "
            f"{self.context_tokens - TEXT_ALLOWANCE_TOKENS}. Ask about fewer "
            "at a time."
        )

    def hinted(self, step: PlanStep) -> str:
        """The prompt this step would produce. Here so a test can read it."""
        return with_hints(step.description, step.images)

    @staticmethod
    def _refuse(message: str) -> WorkerOutput:
        return WorkerOutput(output="", success=False, error=message)


def build_vision_worker(runtime: BrainRuntime) -> VisionWorker:
    """
    The worker as production assembles it, on the runtime already in use.

    Both brains come from the same runtime rather than a new one, so the
    vision model and the heavy model queue behind each other on one card
    instead of racing for it.
    """
    return VisionWorker(
        describe_fn=brain_describe_fn(runtime),
        reason_fn=brain_reason_fn(runtime),
    )
