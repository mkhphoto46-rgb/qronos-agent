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


if __name__ == "__main__":
    unittest.main()