from __future__ import annotations

import unittest

from core.task_router import TaskRouter, TaskType


class TestTaskRouter(unittest.TestCase):
    def setUp(self) -> None:
        self.router = TaskRouter()

    def test_empty_input_defaults_to_fast(self) -> None:
        self.assertEqual(self.router.route("").task_type, TaskType.FAST)

    def test_simple_message_goes_to_fast(self) -> None:
        self.assertEqual(
            self.router.route("Hello Qronos").task_type,
            TaskType.FAST,
        )

    def test_complex_analysis_goes_to_heavy(self) -> None:
        self.assertEqual(
            self.router.route(
                "Analyze this chapter deeply and find logical inconsistencies"
            ).task_type,
            TaskType.HEAVY,
        )

    def test_image_request_goes_to_vision(self) -> None:
        self.assertEqual(
            self.router.route(
                "Look at this screenshot and tell me what you see"
            ).task_type,
            TaskType.VISION,
        )

    def test_computer_request_goes_to_computer(self) -> None:
        self.assertEqual(
            self.router.route("Open Premiere").task_type,
            TaskType.COMPUTER,
        )

    def test_browser_request_goes_to_browser(self) -> None:
        self.assertEqual(
            self.router.route(
                "Go to ChatGPT and send this message"
            ).task_type,
            TaskType.BROWSER,
        )

    def test_case_is_ignored(self) -> None:
        self.assertEqual(
            self.router.route("OPEN PREMIERE").task_type,
            TaskType.COMPUTER,
        )


class TestPersianIsRouted(unittest.TestCase):
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

    def test_calculator_arithmetic_interaction_is_computer(self) -> None:
        self.assertEqual(
            self.router.route(
                "??? ???????? ?? ?? ?? ????? ???? ??"
            ).task_type,
            TaskType.COMPUTER,
        )

    def test_going_to_a_site_is_browser(self) -> None:
        self.assertEqual(
            self.router.route("برو به گوگل").task_type,
            TaskType.BROWSER,
        )

    def test_the_zero_width_non_joiner_does_not_hide_a_marker(self) -> None:
        self.assertEqual(
            self.router.route("یک اسکرین‌شات بگیر").task_type,
            TaskType.VISION,
        )

    def test_arabic_letter_variants_route_the_same_way(self) -> None:
        arabic_kaf = "پريمير را باز كن"
        persian_kaf = "پریمیر را باز کن"

        self.assertEqual(
            self.router.route(arabic_kaf).task_type,
            self.router.route(persian_kaf).task_type,
        )


class TestHeavyTechnicalReasoning(unittest.TestCase):
    def setUp(self) -> None:
        self.router = TaskRouter()

    def assert_heavy(self, text: str) -> None:
        self.assertEqual(
            self.router.route(text).task_type,
            TaskType.HEAVY,
            text,
        )

    def test_persian_code_stress_test_is_heavy(self) -> None:
        self.assert_heavy("کد را بررسی و استرس تست کن")

    def test_persian_edge_cases_are_heavy(self) -> None:
        self.assert_heavy("این کد را برای edge case ها بررسی کن")

    def test_persian_why_algorithm_is_slow_is_heavy(self) -> None:
        self.assert_heavy("بررسی کن چرا این الگوریتم کند است")

    def test_english_stress_test_is_heavy(self) -> None:
        self.assert_heavy("stress test this code and find edge cases")

    def test_english_code_review_is_heavy(self) -> None:
        self.assert_heavy("review this code for bugs")

    def test_existing_architecture_review_stays_heavy(self) -> None:
        self.assert_heavy("review this architecture for race conditions")

    def test_existing_persian_architecture_critique_stays_heavy(self) -> None:
        self.assert_heavy("این معماری را نقد کن")

    def test_existing_persian_comparison_stays_heavy(self) -> None:
        self.assert_heavy("این دو راه حل را مقایسه کن")


class TestHeavyTechnicalReasoningDoesNotStealSimpleWork(unittest.TestCase):
    def setUp(self) -> None:
        self.router = TaskRouter()

    def test_generic_review_word_does_not_force_heavy(self) -> None:
        self.assertEqual(
            self.router.route("review my email").task_type,
            TaskType.FAST,
        )

    def test_generic_persian_check_does_not_force_heavy(self) -> None:
        self.assertEqual(
            self.router.route("این مورد را بررسی کن").task_type,
            TaskType.FAST,
        )

    def test_opening_a_file_stays_computer(self) -> None:
        self.assertEqual(
            self.router.route("این فایل را باز کن").task_type,
            TaskType.COMPUTER,
        )

    def test_running_code_stays_computer(self) -> None:
        self.assertEqual(
            self.router.route("این کد را اجرا کن").task_type,
            TaskType.COMPUTER,
        )

    def test_greeting_stays_fast(self) -> None:
        self.assertEqual(
            self.router.route("سلام کرونوس").task_type,
            TaskType.FAST,
        )


class TestSubstringsDoNotMisroute(unittest.TestCase):
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

    def test_both_halves_of_the_vision_rule_have_persian_markers(self) -> None:
        for name in ("VISION_VERBS", "VISION_SUBJECTS"):
            persian = [
                marker
                for marker in getattr(TaskRouter, name)
                if not marker.isascii()
            ]

            with self.subTest(table=name):
                self.assertTrue(persian, f"{name} has no Persian markers")


class TestVisionReachesVision(unittest.TestCase):
    def setUp(self) -> None:
        self.router = TaskRouter()

    def route(self, text: str) -> TaskType:
        return self.router.route(text).task_type

    def test_reading_a_screenshot_that_mentions_a_file_is_vision(self) -> None:
        self.assertEqual(
            self.route("read the file name in this screenshot"),
            TaskType.VISION,
        )

    def test_asking_what_is_on_the_screen_is_vision(self) -> None:
        self.assertEqual(
            self.route("what is on my screen"),
            TaskType.VISION,
        )

    def test_looking_at_a_window_is_vision(self) -> None:
        self.assertEqual(
            self.route("look at this window and tell me the error"),
            TaskType.VISION,
        )

    def test_describing_a_picture_is_vision(self) -> None:
        self.assertEqual(
            self.route("describe this picture"),
            TaskType.VISION,
        )

    def test_the_persian_forms_reach_it_too(self) -> None:
        for request in (
            "از صفحه بخوان",
            "به این عکس نگاه کن",
            "به این پنجره نگاه کن",
            "چه چیزی روی صفحه است",
        ):
            with self.subTest(request=request):
                self.assertEqual(self.route(request), TaskType.VISION)


class TestVisionDoesNotStealOtherRequests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = TaskRouter()

    def route(self, text: str) -> TaskType:
        return self.router.route(text).task_type

    def test_opening_an_application_named_after_a_picture_is_computer(
        self,
    ) -> None:
        self.assertEqual(
            self.route("open the photo app"),
            TaskType.COMPUTER,
        )

    def test_closing_a_window_is_computer(self) -> None:
        self.assertEqual(
            self.route("close this window"),
            TaskType.COMPUTER,
        )

    def test_opening_a_file_is_computer_in_persian_too(self) -> None:
        self.assertEqual(
            self.route("این فایل را باز کن"),
            TaskType.COMPUTER,
        )

    def test_reading_a_chapter_is_not_looking_at_anything(self) -> None:
        self.assertEqual(
            self.route("read the third chapter carefully"),
            TaskType.HEAVY,
        )

    def test_navigation_still_wins_because_it_has_to_happen_first(self) -> None:
        self.assertEqual(
            self.route("go to the site and look at the chart"),
            TaskType.BROWSER,
        )

    def test_a_thing_to_look_at_alone_does_not_make_it_vision(self) -> None:
        self.assertNotEqual(
            self.route("run this app"),
            TaskType.VISION,
        )

    def test_an_act_of_looking_alone_does_not_make_it_vision(self) -> None:
        self.assertNotEqual(
            self.route("check my email"),
            TaskType.VISION,
        )


class TestB2RoutingCalibration(unittest.TestCase):
    def setUp(self) -> None:
        self.router = TaskRouter()

    def assert_route(self, text: str, expected: TaskType) -> None:
        self.assertEqual(
            self.router.route(text).task_type,
            expected,
            text,
        )

    def test_short_story_is_fast(self) -> None:
        self.assert_route("یک داستان کوتاه بگو", TaskType.FAST)
        self.assert_route("یک داستان سه جمله ای بگو", TaskType.FAST)
        self.assert_route("یک اسم برای داستان پیشنهاد بده", TaskType.FAST)

    def test_complex_story_stays_heavy(self) -> None:
        self.assert_route(
            "یک داستان مفصل و پیچیده بنویس",
            TaskType.HEAVY,
        )
        self.assert_route("این داستان را تحلیل کن", TaskType.HEAVY)
        self.assert_route("این داستان را بازنویسی کن", TaskType.HEAVY)

    def test_project_planning_is_heavy_not_computer(self) -> None:
        self.assert_route(
            "برای این پروژه یک برنامه بده",
            TaskType.HEAVY,
        )
        self.assert_route(
            "برای این پروژه یک برنامه مرحله به مرحله بده",
            TaskType.HEAVY,
        )

    def test_real_application_actions_stay_computer(self) -> None:
        self.assert_route("این برنامه را باز کن", TaskType.COMPUTER)
        self.assert_route("این برنامه را نصب کن", TaskType.COMPUTER)
        self.assert_route("برنامه پریمیر را ببند", TaskType.COMPUTER)

    def test_crash_debug_optimize_and_root_cause_are_heavy(self) -> None:
        self.assert_route(
            "چرا این کد کرش می کند؟",
            TaskType.HEAVY,
        )
        self.assert_route("این کد را دیباگ کن", TaskType.HEAVY)
        self.assert_route(
            "این الگوریتم را بهینه کن",
            TaskType.HEAVY,
        )
        self.assert_route(
            "علت اصلی این خطا را پیدا کن",
            TaskType.HEAVY,
        )

    def test_generic_step_by_step_guide_is_not_forced_heavy(self) -> None:
        self.assert_route(
            "یک راهنمای مرحله به مرحله برای نصب بده",
            TaskType.FAST,
        )

    def test_english_equivalents(self) -> None:
        self.assert_route(
            "debug why this code crashes",
            TaskType.HEAVY,
        )
        self.assert_route(
            "optimize this algorithm",
            TaskType.HEAVY,
        )
        self.assert_route(
            "find the root cause of this error",
            TaskType.HEAVY,
        )
        self.assert_route(
            "give me a step-by-step plan for this project",
            TaskType.HEAVY,
        )
        self.assert_route(
            "tell me a short story",
            TaskType.FAST,
        )


if __name__ == "__main__":
    unittest.main()
