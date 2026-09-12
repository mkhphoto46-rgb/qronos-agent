from __future__ import annotations

import unittest

from core.model_registry import MODELS, get_model


class TestModelRegistry(unittest.TestCase):
    def test_fast_role_contract(self) -> None:
        model = get_model("fast")

        self.assertEqual(model.role, "fast_brain")
        self.assertEqual(model.priority, 1)
        self.assertTrue(model.name)
        self.assertGreater(model.estimated_vram_gb, 0.0)
        self.assertGreater(model.context_tokens, 0)

    def test_heavy_role_contract(self) -> None:
        model = get_model("heavy")

        self.assertEqual(model.role, "heavy_brain")
        self.assertEqual(model.priority, 2)
        self.assertTrue(model.name)
        self.assertGreater(model.estimated_vram_gb, 0.0)
        self.assertGreater(model.context_tokens, 0)

    def test_fast_and_heavy_are_distinct_profiles(self) -> None:
        self.assertNotEqual(
            get_model("fast").name,
            get_model("heavy").name,
        )

    def test_unknown_model_role_fails(self) -> None:
        with self.assertRaises(ValueError):
            get_model("unknown")


class TestContextIsDeclared(unittest.TestCase):
    """
    Active model profiles must explicitly bound context memory.

    Exact model identities are configured in config/brains.json and may change
    without rewriting ModelManager. These tests therefore protect the role and
    resource contracts rather than duplicating old model names.
    """

    def test_every_model_declares_a_context(self) -> None:
        for role, model in MODELS.items():
            with self.subTest(role=role):
                self.assertGreater(model.context_tokens, 0)

    def test_no_model_asks_for_an_unbounded_context(self) -> None:
        for role, model in MODELS.items():
            with self.subTest(role=role):
                self.assertLessEqual(model.context_tokens, 32_768)

    def test_every_model_declares_a_positive_vram_estimate(self) -> None:
        for role, model in MODELS.items():
            with self.subTest(role=role):
                self.assertGreater(model.estimated_vram_gb, 0.0)

    def test_the_heavy_brain_gets_more_context_than_fast(self) -> None:
        self.assertGreater(
            get_model("heavy").context_tokens,
            get_model("fast").context_tokens,
        )


if __name__ == "__main__":
    unittest.main()
