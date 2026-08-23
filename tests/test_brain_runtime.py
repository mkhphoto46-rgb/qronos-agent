from __future__ import annotations

import unittest

from core.brain_runtime import (
    BrainRuntime,
    BrainRuntimeModelStatus,
)
from core.ollama_controller import OllamaController


class FakeBrainRuntime(BrainRuntime):
    """Small test runtime used to verify the BrainRuntime contract."""

    def __init__(self) -> None:
        self.running_models: list[
            BrainRuntimeModelStatus
        ] = []

    def health_check(self) -> bool:
        return True

    def chat(
        self,
        model_name: str,
        prompt: str,
        think: bool = False,
        num_predict: int | None = None,
        keep_alive: str = "5m",
    ) -> str:
        return (
            f"{model_name}|"
            f"{prompt}|"
            f"think={think}|"
            f"num_predict={num_predict}|"
            f"keep_alive={keep_alive}"
        )

    def list_running_models(
        self,
    ) -> list[BrainRuntimeModelStatus]:
        return list(
            self.running_models
        )

    def stop_model(
        self,
        model_name: str,
    ) -> None:
        self.running_models = [
            model
            for model in self.running_models
            if model.name != model_name
        ]

    def unload_all(self) -> None:
        self.running_models.clear()


class TestBrainRuntime(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = FakeBrainRuntime()

    def test_health_check(self) -> None:
        self.assertTrue(
            self.runtime.health_check()
        )

    def test_ollama_controller_implements_brain_runtime(
        self,
    ) -> None:
        controller = OllamaController()

        self.assertIsInstance(
            controller,
            BrainRuntime,
        )

    def test_chat_uses_runtime_contract(self) -> None:
        result = self.runtime.chat(
            model_name="qwen3:4b-instruct",
            prompt="Hello Qronos",
            think=False,
            num_predict=256,
            keep_alive="10m",
        )

        self.assertIn(
            "qwen3:4b-instruct",
            result,
        )

        self.assertIn(
            "Hello Qronos",
            result,
        )

    def test_running_model_status(self) -> None:
        model = BrainRuntimeModelStatus(
            name="qwen3:14b",
            size="9.3 GB",
            processor="CPU/GPU",
            context=4096,
        )

        self.runtime.running_models.append(
            model
        )

        running = (
            self.runtime.list_running_models()
        )

        self.assertEqual(
            len(running),
            1,
        )

        self.assertEqual(
            running[0].name,
            "qwen3:14b",
        )

    def test_stop_model(self) -> None:
        self.runtime.running_models.extend(
            [
                BrainRuntimeModelStatus(
                    name="qwen3:4b-instruct"
                ),
                BrainRuntimeModelStatus(
                    name="qwen3:14b"
                ),
            ]
        )

        self.runtime.stop_model(
            "qwen3:14b"
        )

        running = (
            self.runtime.list_running_models()
        )

        self.assertEqual(
            len(running),
            1,
        )

        self.assertEqual(
            running[0].name,
            "qwen3:4b-instruct",
        )

    def test_unload_all(self) -> None:
        self.runtime.running_models.extend(
            [
                BrainRuntimeModelStatus(
                    name="qwen3:4b-instruct"
                ),
                BrainRuntimeModelStatus(
                    name="qwen3:14b"
                ),
            ]
        )

        self.runtime.unload_all()

        self.assertEqual(
            self.runtime.list_running_models(),
            [],
        )


if __name__ == "__main__":
    unittest.main()