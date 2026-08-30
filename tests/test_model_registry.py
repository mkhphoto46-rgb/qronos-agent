from __future__ import annotations

import unittest

from core.model_registry import MODELS, get_model


class TestModelRegistry(unittest.TestCase):
    def test_fast_model(self) -> None:
        model = get_model("fast")

        self.assertEqual(model.name, "qwen3:4b-instruct")
        self.assertEqual(model.role, "fast_brain")
        self.assertEqual(model.estimated_vram_gb, 3.4)
        self.assertEqual(model.priority, 1)

    def test_heavy_model(self) -> None:
        model = get_model("heavy")

        self.assertEqual(model.name, "qwen3:14b")
        self.assertEqual(model.role, "heavy_brain")
        self.assertEqual(model.estimated_vram_gb, 10.0)
        self.assertEqual(model.priority, 2)

    def test_unknown_model_role_fails(self) -> None:
        with self.assertRaises(ValueError):
            get_model("unknown")


if __name__ == "__main__":
    unittest.main()


class TestContextIsDeclared(unittest.TestCase):
    """
    Every model states its own context window.

    Left to the server, these models load with 262144 tokens. Measured on a
    16 GB card, qwen3:4b-instruct — 2.3 GB of weights — then occupied 15,665
    MiB, almost all of it key/value cache for a conversation nobody was going
    to have. That filled the card, which tripped Qronos's own VRAM ceiling,
    which made it refuse the next thing the user said.

        num_ctx 262144 -> 15,665 MiB
        num_ctx  32768 ->  7,184 MiB
        num_ctx   8192 ->  5,258 MiB
    """

    def test_every_model_declares_a_context(self) -> None:
        for role, model in MODELS.items():
            with self.subTest(role=role):
                self.assertGreater(model.context_tokens, 0)

    def test_no_model_asks_for_a_context_it_cannot_afford(self) -> None:
        # The number that caused the problem was 262144. Anything approaching
        # it means the key/value cache, not the weights, decides whether
        # Qronos fits on the card.
        for role, model in MODELS.items():
            with self.subTest(role=role):
                self.assertLessEqual(model.context_tokens, 32_768)

    def test_the_estimate_covers_the_cache_not_just_the_weights(self) -> None:
        # qwen3:4b-instruct is 2.3 GB on disk and measured 3.34 GB resident at
        # 8k. An estimate at or below the file size would mean the policy is
        # reasoning about a number that cannot happen.
        self.assertGreater(get_model("fast").estimated_vram_gb, 2.3)
        self.assertGreater(get_model("heavy").estimated_vram_gb, 8.6)

    def test_the_heavy_brain_gets_more_room_to_think(self) -> None:
        self.assertGreater(
            get_model("heavy").context_tokens,
            get_model("fast").context_tokens,
        )
