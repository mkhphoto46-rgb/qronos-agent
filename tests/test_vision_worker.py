"""
The worker that looks at things: what it answers, and what it refuses.

No model runs here. ``describe_fn`` and ``reason_fn`` are the seam
``WebResearchWorker`` established, and a fake on the other end of them is what
makes every branch reachable — including the ones a real model would almost
never take, which are the ones that matter.

The escalation rule gets its own section. "Vision describes, heavy reasons" is
a sentence until something can point at where the line is, and a model that
escalates every question costs ten gigabytes and several seconds to answer
"what is on my screen".
"""

from __future__ import annotations

import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from core.model_registry import get_model
from core.task_plan import PlanStep, TaskPlan
from core.task_router import TaskType
from core.vision_worker import (
    NO_MODEL_MESSAGE,
    NO_PICTURE_MESSAGE,
    SAW_NOTHING_MESSAGE,
    TEXT_ALLOWANCE_TOKENS,
    VisionWorker,
    needs_reasoning,
)
from core.workers import UnavailableReason, WorkerRegistry


def png_at(path: Path, width: int = 800, height: int = 600) -> Path:
    buffer = BytesIO()

    Image.new("RGB", (width, height), (40, 40, 40)).save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())

    return path


class VisionWorkerCase(unittest.TestCase):
    """Shared setup: a temporary picture and a recording fake."""

    def setUp(self) -> None:
        self.folder = TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.addCleanup(self.folder.cleanup)

        self.picture = png_at(self.root / "shot.png")

        self.described: list[tuple[str, tuple[str, ...]]] = []
        self.reasoned: list[tuple[str, str]] = []

    def describe(self, answer: str = "A dark window with an error in it."):
        def describe_fn(question, images):
            self.described.append((question, tuple(images)))

            return answer

        return describe_fn

    def reason(self, answer: str = "The disk is full."):
        def reason_fn(question, description):
            self.reasoned.append((question, description))

            return answer

        return reason_fn

    def step(self, description: str, images=None) -> PlanStep:
        return PlanStep(
            order=1,
            task_type=TaskType.VISION,
            description=description,
            images=(str(self.picture),) if images is None else images,
        )


class TestLooking(VisionWorkerCase):
    """The ordinary path."""

    def test_it_describes_what_it_was_given(self) -> None:
        worker = VisionWorker(describe_fn=self.describe())

        result = worker.execute(self.step("What is on my screen?"))

        self.assertTrue(result.success)
        self.assertEqual(result.output, "A dark window with an error in it.")

    def test_the_question_and_the_pictures_both_reach_the_model(self) -> None:
        worker = VisionWorker(describe_fn=self.describe())

        worker.execute(self.step("What is on my screen?"))

        question, images = self.described[0]

        self.assertEqual(question, "What is on my screen?")
        self.assertEqual(images, (str(self.picture),))

    def test_it_answers_for_the_vision_task_type(self) -> None:
        self.assertIs(VisionWorker.task_type, TaskType.VISION)

    def test_it_is_healthy_when_there_is_something_to_call(self) -> None:
        self.assertTrue(VisionWorker(describe_fn=self.describe()).health_check())

    def test_it_is_unhealthy_with_no_model_behind_it(self) -> None:
        self.assertFalse(VisionWorker().health_check())

    def test_the_health_check_calls_nothing(self) -> None:
        """
        It is asked whether a capability exists, including on paths where the
        answer is no and nothing should have happened.
        """
        worker = VisionWorker(describe_fn=self.describe())

        worker.health_check()

        self.assertEqual(self.described, [])

    def test_the_registry_reports_it_as_available(self) -> None:
        registry = WorkerRegistry()
        registry.register(VisionWorker(describe_fn=self.describe()))

        self.assertIsNone(registry.availability(TaskType.VISION))

    def test_without_it_the_registry_still_says_not_implemented(self) -> None:
        unavailable = WorkerRegistry().availability(TaskType.VISION)

        self.assertIsNotNone(unavailable)
        self.assertIs(unavailable.reason, UnavailableReason.NOT_IMPLEMENTED)


class TestEscalation(VisionWorkerCase):
    """
    Vision describes; heavy reasons.

    The interesting half is what does *not* escalate, because escalating
    everything is the failure mode that looks like it is working.
    """

    def test_a_description_question_stays_with_the_small_model(self) -> None:
        worker = VisionWorker(
            describe_fn=self.describe(),
            reason_fn=self.reason(),
        )

        result = worker.execute(self.step("What is on my screen?"))

        self.assertEqual(self.reasoned, [])
        self.assertEqual(result.output, "A dark window with an error in it.")

    def test_a_why_question_goes_to_the_heavy_brain(self) -> None:
        worker = VisionWorker(
            describe_fn=self.describe(),
            reason_fn=self.reason(),
        )

        result = worker.execute(self.step("Why did this fail?"))

        self.assertEqual(len(self.reasoned), 1)
        self.assertEqual(result.output, "The disk is full.")

    def test_the_heavy_brain_is_given_the_description_as_its_evidence(
        self,
    ) -> None:
        """It never sees the picture, so the description is all it has."""
        worker = VisionWorker(
            describe_fn=self.describe(),
            reason_fn=self.reason(),
        )

        worker.execute(self.step("Why did this fail?"))

        question, description = self.reasoned[0]

        self.assertEqual(question, "Why did this fail?")
        self.assertEqual(description, "A dark window with an error in it.")

    def test_with_no_heavy_brain_it_still_answers_with_what_it_saw(self) -> None:
        worker = VisionWorker(describe_fn=self.describe())

        result = worker.execute(self.step("Why did this fail?"))

        self.assertTrue(result.success)
        self.assertEqual(result.output, "A dark window with an error in it.")

    def test_a_heavy_brain_that_fails_does_not_lose_the_description(self) -> None:
        """
        The description is real and was produced. Dropping it because the
        second model fell over turns a partial answer into no answer.
        """

        def explodes(question, description):
            raise RuntimeError("The heavy brain did not load.")

        worker = VisionWorker(
            describe_fn=self.describe(),
            reason_fn=explodes,
        )

        result = worker.execute(self.step("Why did this fail?"))

        self.assertTrue(result.success)
        self.assertEqual(result.output, "A dark window with an error in it.")

    def test_a_heavy_brain_that_says_nothing_falls_back_to_the_description(
        self,
    ) -> None:
        worker = VisionWorker(
            describe_fn=self.describe(),
            reason_fn=self.reason(answer="   "),
        )

        result = worker.execute(self.step("Why did this fail?"))

        self.assertEqual(result.output, "A dark window with an error in it.")


class TestTheEscalationRule(unittest.TestCase):
    """Where the line is drawn, stated as a rule rather than a feeling."""

    def test_descriptions_do_not_escalate(self) -> None:
        for question in (
            "What is on my screen?",
            "Read this",
            "What does this window say?",
            "Transcribe the text",
            "چه چیزی روی صفحه است",
            "از صفحه بخوان",
        ):
            with self.subTest(question=question):
                self.assertFalse(needs_reasoning(question))

    def test_questions_that_want_thinking_do(self) -> None:
        for question in (
            "Why did this fail?",
            "Explain this error",
            "How should I fix this?",
            "Compare these two dialogs",
            "What does it mean?",
            "چرا این خطا آمده",
            "توضیح بده",
            "چطور درست کنم",
        ):
            with self.subTest(question=question):
                self.assertTrue(needs_reasoning(question))

    def test_a_word_inside_another_word_does_not_escalate(self) -> None:
        """
        ``why`` is inside ``whyever`` and ``plan`` is inside ``planet``. The
        router's word-boundary rule is used here for the same reason.
        """
        self.assertFalse(needs_reasoning("what planet is this a picture of"))


class TestRefusals(VisionWorkerCase):
    """Every way this can decline, said in words a person can act on."""

    def test_being_asked_to_look_at_nothing_is_refused(self) -> None:
        worker = VisionWorker(describe_fn=self.describe())

        result = worker.execute(self.step("What is this?", images=()))

        self.assertFalse(result.success)
        self.assertEqual(result.error, NO_PICTURE_MESSAGE)
        self.assertEqual(self.described, [])

    def test_no_model_is_refused_distinctly(self) -> None:
        result = VisionWorker().execute(self.step("What is this?"))

        self.assertFalse(result.success)
        self.assertEqual(result.error, NO_MODEL_MESSAGE)

    def test_a_missing_picture_is_refused_before_the_model_is_called(
        self,
    ) -> None:
        worker = VisionWorker(describe_fn=self.describe())

        result = worker.execute(
            self.step("What is this?", images=(str(self.root / "gone.png"),))
        )

        self.assertFalse(result.success)
        self.assertIn("gone.png", result.error)
        self.assertEqual(self.described, [])

    def test_a_file_that_is_not_a_picture_is_refused_differently(self) -> None:
        """
        "There is no file there" and "that is not a picture" send a user to
        two completely different places, so they are not the same message.
        """
        text = self.root / "notes.png"
        text.write_text("This is not a picture.", encoding="utf-8")

        worker = VisionWorker(describe_fn=self.describe())

        result = worker.execute(self.step("What is this?", images=(str(text),)))

        self.assertFalse(result.success)
        self.assertIn("not a PNG", result.error)

    def test_a_zero_byte_file_is_refused(self) -> None:
        empty = self.root / "empty.png"
        empty.touch()

        worker = VisionWorker(describe_fn=self.describe())

        result = worker.execute(self.step("What is this?", images=(str(empty),)))

        self.assertFalse(result.success)
        self.assertIn("empty", result.error)

    def test_a_model_that_fails_is_reported_rather_than_raised(self) -> None:
        def explodes(question, images):
            raise RuntimeError("Ollama is not running.")

        worker = VisionWorker(describe_fn=explodes)

        result = worker.execute(self.step("What is this?"))

        self.assertFalse(result.success)
        self.assertIn("Ollama is not running", result.error)

    def test_a_model_that_says_nothing_is_a_failure_not_a_blank_answer(
        self,
    ) -> None:
        """
        Returning success with an empty string reads to everything downstream
        as a picture of an empty room.
        """
        worker = VisionWorker(describe_fn=self.describe(answer="   \n  "))

        result = worker.execute(self.step("What is this?"))

        self.assertFalse(result.success)
        self.assertEqual(result.error, SAW_NOTHING_MESSAGE)


class TestTheContextBudget(VisionWorkerCase):
    """
    Too many pictures is refused, out loud.

    An overflowing context does not fail loudly — it silently loses the
    beginning of the request, and the beginning is the instruction saying what
    to do. So the arithmetic happens first, from the file headers, before
    anything is decoded or sent.
    """

    def make_pictures(self, count: int) -> tuple[str, ...]:
        return tuple(
            str(png_at(self.root / f"shot{index}.png", 1920, 1080))
            for index in range(count)
        )

    def test_one_picture_fits(self) -> None:
        worker = VisionWorker(describe_fn=self.describe())

        result = worker.execute(
            self.step("What is this?", images=self.make_pictures(1))
        )

        self.assertTrue(result.success)

    def test_too_many_pictures_are_refused_with_the_numbers(self) -> None:
        worker = VisionWorker(describe_fn=self.describe())

        result = worker.execute(
            self.step("What is this?", images=self.make_pictures(12))
        )

        self.assertFalse(result.success)
        self.assertIn("too much to look at", result.error)
        self.assertEqual(self.described, [])

    def test_the_budget_follows_the_declared_context(self) -> None:
        """
        Not a hardcoded count of pictures. A larger context means more of them
        fit, and the worker should say so without being edited.
        """
        roomy = VisionWorker(describe_fn=self.describe(), context_tokens=32_768)

        result = roomy.execute(
            self.step("What is this?", images=self.make_pictures(12))
        )

        self.assertTrue(result.success)

    def test_room_is_kept_for_the_question_and_the_answer(self) -> None:
        """
        A picture that fits the context exactly still does not fit, because
        the question and the reply have to go somewhere.
        """
        picture = self.make_pictures(1)
        exact = VisionWorker(
            describe_fn=self.describe(),
            # What a 1920x1080 image costs once shrunk to the size Qronos
            # sends: the measured floor, exactly.
            context_tokens=1_040,
        )

        result = exact.execute(self.step("What is this?", images=picture))

        self.assertFalse(result.success)
        self.assertGreater(TEXT_ALLOWANCE_TOKENS, 0)


class TestThePlanCarriesThePicture(unittest.TestCase):
    """
    A step used to be an order, a type and a sentence, with nowhere to put the
    thing the sentence refers to.
    """

    def test_a_step_carries_no_pictures_by_default(self) -> None:
        plan = TaskPlan(goal="Say hello")
        plan.add_step(TaskType.FAST, "Say hello")

        self.assertEqual(plan.steps[0].images, ())

    def test_a_step_can_carry_pictures(self) -> None:
        plan = TaskPlan(goal="Read the screen")
        plan.add_step(TaskType.VISION, "What is on my screen?", ("shot.png",))

        self.assertEqual(plan.steps[0].images, ("shot.png",))

    def test_one_path_given_as_a_bare_string_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            PlanStep(
                order=1,
                task_type=TaskType.VISION,
                description="Look",
                images="shot.png",
            )


class TestTheVisionModelIsDeclared(unittest.TestCase):
    """
    It is a model without being a ``TaskClass``.

    ``TaskClass`` answers "which brain reasons about this", and for vision the
    answer is none of them — it describes, and the heavy brain reasons about
    the description. But the model store still has to know to keep it.
    """

    def test_the_vision_model_is_in_the_roster(self) -> None:
        self.assertEqual(get_model("vision").name, "qwen3-vl:4b-instruct")

    def test_it_declares_a_context_rather_than_taking_the_default(self) -> None:
        # This model ships a 262,144 default and no num_ctx in its params,
        # which is the exact trap that once put a 2.3 GB model into 15.7 GB.
        self.assertEqual(get_model("vision").context_tokens, 4_096)

    def test_its_estimate_is_the_peak_during_generation(self) -> None:
        # Measured at +4,475 MiB while generating. The load-time figure is
        # much smaller and taking it is the mistake already made once on the
        # voice runtime.
        self.assertGreater(get_model("vision").estimated_vram_gb, 4.0)

    def test_the_declared_context_holds_a_picture_and_a_question(self) -> None:
        # About 1,080 tokens for a screenshot at the size Qronos sends.
        self.assertGreater(
            get_model("vision").context_tokens,
            1_100 + TEXT_ALLOWANCE_TOKENS,
        )

    def test_widening_task_class_was_not_needed(self) -> None:
        from core.model_manager import TaskClass

        self.assertEqual(
            {member.value for member in TaskClass},
            {"fast", "heavy"},
        )


if __name__ == "__main__":
    unittest.main()
