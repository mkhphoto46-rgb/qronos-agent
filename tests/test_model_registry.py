from __future__ import annotations

import unittest

from core.model_registry import get_model


class TestModelRegistry(unittest.TestCase):
    def test_fast_model(self) -> None:
        model = get_model("fast")

        self.assertEqual(model.name, "qwen3.5:9b")
        self.assertEqual(model.role, "fast_brain")
        self.assertEqual(model.estimated_vram_gb, 6.6)
        self.assertEqual(model.priority, 1)

    def test_heavy_model(self) -> None:
        model = get_model("heavy")

        self.assertEqual(model.name, "qwen3.6:27b")
        self.assertEqual(model.role, "heavy_brain")
        self.assertEqual(model.estimated_vram_gb, 17.0)
        self.assertEqual(model.priority, 2)

    def test_unknown_model_role_fails(self) -> None:
        with self.assertRaises(ValueError):
            get_model("unknown")


if __name__ == "__main__":
    unittest.main()