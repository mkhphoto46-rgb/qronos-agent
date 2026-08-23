from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import numpy as np

from core.openwakeword_engine import OpenWakeWordEngine


class FakeOpenWakeWordModel:
    def __init__(self, model_name: str = "qronos") -> None:
        self.model_name = model_name
        self.score = 0.0
        self.reset_count = 0
        self.last_audio: np.ndarray | None = None

    def reset(self) -> None:
        self.reset_count += 1

    def predict(self, audio: np.ndarray) -> dict[str, float]:
        self.last_audio = audio
        return {self.model_name: self.score}


class RecordingModelFactory:
    def __init__(self, model: FakeOpenWakeWordModel) -> None:
        self.model = model
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> FakeOpenWakeWordModel:
        self.calls.append(kwargs)
        return self.model


class TestOpenWakeWordEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)

        self.model_path = (
            Path(self.temp_directory.name)
            / "qronos.onnx"
        )
        self.model_path.write_bytes(b"fake onnx")
        Path(f"{self.model_path}.data").write_bytes(
            b"fake external data"
        )

        self.fake_model = FakeOpenWakeWordModel()
        self.factory = RecordingModelFactory(
            self.fake_model,
        )
        self.engine = OpenWakeWordEngine(
            model_path=self.model_path,
            threshold=0.66,
            model_factory=self.factory,
        )

    @staticmethod
    def make_audio_frame() -> bytes:
        return np.zeros(
            1_280,
            dtype=np.int16,
        ).tobytes()

    def test_start_loads_expected_onnx_model(self) -> None:
        self.engine.start()

        self.assertEqual(
            self.factory.calls,
            [
                {
                    "wakeword_models": [
                        str(self.model_path),
                    ],
                    "inference_framework": "onnx",
                }
            ],
        )
        self.assertTrue(self.engine.is_running())
        self.assertEqual(self.fake_model.reset_count, 1)

    def test_start_is_idempotent(self) -> None:
        self.engine.start()
        self.engine.start()

        self.assertEqual(len(self.factory.calls), 1)

    def test_start_requires_model_file(self) -> None:
        missing_model = OpenWakeWordEngine(
            model_path=(
                Path(self.temp_directory.name)
                / "missing.onnx"
            ),
            model_factory=self.factory,
        )

        with self.assertRaises(FileNotFoundError):
            missing_model.start()

    def test_start_requires_external_data_file(self) -> None:
        self.engine.external_data_path.unlink()

        with self.assertRaises(FileNotFoundError):
            self.engine.start()

    def test_score_below_threshold_does_not_trigger(self) -> None:
        self.fake_model.score = 0.65
        self.engine.start()

        detected = self.engine.process_audio(
            self.make_audio_frame(),
        )

        self.assertFalse(detected)
        self.assertEqual(self.engine.last_score, 0.65)

    def test_score_at_threshold_triggers(self) -> None:
        self.fake_model.score = 0.66
        self.engine.start()

        detected = self.engine.process_audio(
            self.make_audio_frame(),
        )

        self.assertTrue(detected)

    def test_audio_is_converted_to_int16_samples(self) -> None:
        self.engine.start()

        self.engine.process_audio(
            self.make_audio_frame(),
        )

        self.assertIsNotNone(self.fake_model.last_audio)
        self.assertEqual(
            self.fake_model.last_audio.dtype,
            np.dtype(np.int16),
        )
        self.assertEqual(
            self.fake_model.last_audio.shape,
            (1_280,),
        )

    def test_wrong_audio_frame_size_fails(self) -> None:
        self.engine.start()

        with self.assertRaises(ValueError):
            self.engine.process_audio(b"too short")

    def test_non_bytes_audio_fails(self) -> None:
        self.engine.start()

        with self.assertRaises(TypeError):
            self.engine.process_audio(bytearray(2_560))  # type: ignore[arg-type]

    def test_paused_engine_ignores_audio(self) -> None:
        self.fake_model.score = 1.0
        self.engine.start()
        self.engine.pause()

        detected = self.engine.process_audio(
            self.make_audio_frame(),
        )

        self.assertFalse(detected)
        self.assertIsNone(self.fake_model.last_audio)

    def test_stop_releases_model(self) -> None:
        self.engine.start()
        self.engine.stop()

        self.assertFalse(self.engine.is_running())
        self.assertEqual(self.engine.last_score, 0.0)
        self.assertEqual(self.fake_model.reset_count, 2)

    def test_threshold_must_be_a_probability(self) -> None:
        with self.assertRaises(ValueError):
            OpenWakeWordEngine(
                model_path=self.model_path,
                threshold=1.1,
                model_factory=self.factory,
            )


if __name__ == "__main__":
    unittest.main()
