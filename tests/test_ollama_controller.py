from __future__ import annotations

import unittest

from core.ollama_controller import OllamaController


class TestOllamaController(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = OllamaController()

    def test_health_check(self) -> None:
        self.assertTrue(self.controller.health_check())

    def test_empty_model_list_when_nothing_is_loaded(self) -> None:
        self.controller.unload_all()
        models = self.controller.list_running_models()

        self.assertEqual(models, [])

    def test_chat_with_fast_model(self) -> None:
        response = self.controller.chat(
            model_name="qwen3.5:9b",
            prompt="Reply with exactly: Qronos test passed.",
            think=False,
            num_predict=20,
            keep_alive="0",
        )

        self.assertIn("Qronos test passed.", response)

        self.controller.stop_model("qwen3.5:9b")

    def test_unload_fast_model(self) -> None:
        self.controller.chat(
            model_name="qwen3.5:9b",
            prompt="Reply with one word: ready.",
            think=False,
            num_predict=5,
            keep_alive="5m",
        )

        self.controller.stop_model("qwen3.5:9b")

        models = self.controller.list_running_models()

        self.assertEqual(models, [])


if __name__ == "__main__":
    unittest.main()