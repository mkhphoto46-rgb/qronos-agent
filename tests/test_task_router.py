from __future__ import annotations

import unittest

from core.task_router import TaskRouter, TaskType


class TestTaskRouter(unittest.TestCase):
    def setUp(self) -> None:
        self.router = TaskRouter()

    def test_empty_input_defaults_to_fast(self) -> None:
        result = self.router.route("")
        self.assertEqual(result.task_type, TaskType.FAST)

    def test_simple_message_goes_to_fast(self) -> None:
        result = self.router.route("Hello Qronos")
        self.assertEqual(result.task_type, TaskType.FAST)

    def test_complex_analysis_goes_to_heavy(self) -> None:
        result = self.router.route(
            "Analyze this chapter deeply and find logical inconsistencies"
        )
        self.assertEqual(result.task_type, TaskType.HEAVY)

    def test_image_request_goes_to_vision(self) -> None:
        result = self.router.route(
            "Look at this screenshot and tell me what you see"
        )
        self.assertEqual(result.task_type, TaskType.VISION)

    def test_computer_request_goes_to_computer(self) -> None:
        result = self.router.route("Open Premiere")
        self.assertEqual(result.task_type, TaskType.COMPUTER)

    def test_browser_request_goes_to_browser(self) -> None:
        result = self.router.route(
            "Go to ChatGPT and send this message"
        )
        self.assertEqual(result.task_type, TaskType.BROWSER)

    def test_case_is_ignored(self) -> None:
        result = self.router.route("OPEN PREMIERE")
        self.assertEqual(result.task_type, TaskType.COMPUTER)


class TestPersianIsRouted(unittest.TestCase):
    """
    The language the router actually receives.

    Speech recognition is called with ``language="fa"``, so the transcript
    reaching this module is Persian. Before Persian markers existed, every one
    of these fell through all four lists into FAST — and it was invisible,
    because FAST is the only branch with an implementation, so the wrong route
    and the only possible route were the same one.
    """

    def setUp(self) -> None:
        self.router = TaskRouter()

    def test_a_greeting_is_fast(self) -> None:
        self.assertEqual(
            self.router.route("سلام کرونوس").task_type,
            TaskType.FAST,
        )

    def test_a_question_with_no_marker_is_fast(self) -> None:
        self.assertEqual(
            self.router.route("ساعت چند است؟").task_type,
            TaskType.FAST,
        )

    def test_deep_analysis_is_heavy(self) -> None:
        self.assertEqual(
            self.router.route("این فصل را عمیق تحلیل کن").task_type,
            TaskType.HEAVY,
        )

    def test_looking_at_a_photo_is_vision(self) -> None:
        self.assertEqual(
            self.router.route("به این عکس نگاه کن").task_type,
            TaskType.VISION,
        )

    def test_opening_an_application_is_computer(self) -> None:
        self.assertEqual(
            self.router.route("پریمیر را باز کن").task_type,
            TaskType.COMPUTER,
        )

    def test_going_to_a_site_is_browser(self) -> None:
        self.assertEqual(
            self.router.route("برو به گوگل").task_type,
            TaskType.BROWSER,
        )

    def test_the_zero_width_non_joiner_does_not_hide_a_marker(self) -> None:
        # اسکرین‌شات is one word joined by a zero-width non-joiner, which
        # normalisation turns into a space. A marker stored in its raw form
        # would never match the normalised text, so both sides go through the
        # same normalisation.
        self.assertEqual(
            self.router.route("یک اسکرین‌شات بگیر").task_type,
            TaskType.VISION,
        )

    def test_arabic_letter_variants_route_the_same_way(self) -> None:
        # A phone keyboard may produce Arabic yeh and kaf where a Persian one
        # produces the Persian forms. They look identical and are different
        # code points, so without normalisation the same sentence typed on two
        # keyboards would route two different ways.
        arabic_kaf = "پريمير را باز كن"
        persian_kaf = "پریمیر را باز کن"

        self.assertEqual(
            self.router.route(arabic_kaf).task_type,
            self.router.route(persian_kaf).task_type,
        )


class TestSubstringsDoNotMisroute(unittest.TestCase):
    """
    Regressions for the matching rule.

    Plain ``in`` matching put every one of these in the wrong place, because a
    short marker sits inside a longer, unrelated word. Word-boundary matching
    for ASCII markers is what fixes it, and these fail again the moment
    somebody replaces it with a substring test.
    """

    def setUp(self) -> None:
        self.router = TaskRouter()

    def test_profile_is_not_a_file_operation(self) -> None:
        self.assertEqual(
            self.router.route("update my profile").task_type,
            TaskType.FAST,
        )

    def test_prototype_is_not_a_typing_operation(self) -> None:
        self.assertEqual(
            self.router.route("build a prototype").task_type,
            TaskType.FAST,
        )

    def test_cobweb_is_not_a_browser_request(self) -> None:
        self.assertEqual(
            self.router.route("clean the cobweb").task_type,
            TaskType.FAST,
        )

    def test_a_whole_word_marker_still_matches(self) -> None:
        # The other half of the rule. Tightening the match must not stop the
        # markers working.
        for request, expected in (
            ("open the file", TaskType.COMPUTER),
            ("type this for me", TaskType.COMPUTER),
            ("search the web for it", TaskType.BROWSER),
        ):
            with self.subTest(request=request):
                self.assertEqual(
                    self.router.route(request).task_type,
                    expected,
                )


class TestPrecedenceIsStable(unittest.TestCase):
    def setUp(self) -> None:
        self.router = TaskRouter()

    def test_browser_wins_over_computer(self) -> None:
        # "open the website" names both a computer verb and a browser noun.
        # Browser is checked first, in both languages, so the answer does not
        # depend on which language the request arrived in.
        self.assertEqual(
            self.router.route("open the website").task_type,
            TaskType.BROWSER,
        )
        self.assertEqual(
            self.router.route("وب‌سایت را باز کن").task_type,
            TaskType.BROWSER,
        )


class TestMarkersAreNormalised(unittest.TestCase):
    def test_no_marker_carries_a_zero_width_non_joiner(self) -> None:
        # A marker containing one could never match normalised text. Checking
        # the tables directly catches a marker added later in its raw form,
        # which would otherwise look fine and simply never fire.
        for name in (
            "COMPUTER_KEYWORDS",
            "BROWSER_KEYWORDS",
            "VISION_KEYWORDS",
            "HEAVY_KEYWORDS",
        ):
            for marker in getattr(TaskRouter, name):
                with self.subTest(table=name, marker=marker):
                    self.assertNotIn("‌", marker)

    def test_no_marker_is_empty_or_padded(self) -> None:
        for name in (
            "COMPUTER_KEYWORDS",
            "BROWSER_KEYWORDS",
            "VISION_KEYWORDS",
            "HEAVY_KEYWORDS",
        ):
            for marker in getattr(TaskRouter, name):
                with self.subTest(table=name, marker=marker):
                    self.assertTrue(marker)
                    self.assertEqual(marker, marker.strip())

    def test_every_category_has_persian_markers(self) -> None:
        # The defect this module was fixed for. A category with English
        # markers only is unreachable for the language Qronos listens in.
        for name in (
            "COMPUTER_KEYWORDS",
            "BROWSER_KEYWORDS",
            "VISION_KEYWORDS",
            "HEAVY_KEYWORDS",
        ):
            persian = [
                marker
                for marker in getattr(TaskRouter, name)
                if not marker.isascii()
            ]

            with self.subTest(table=name):
                self.assertTrue(persian, f"{name} has no Persian markers")


if __name__ == "__main__":
    unittest.main()