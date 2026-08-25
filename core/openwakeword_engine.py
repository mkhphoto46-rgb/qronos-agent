from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import numpy as np

from core.config import CONFIG


DEFAULT_MODEL_PATH = (
    CONFIG.paths.models
    / "wake_word"
    / "qronos.onnx"
)
DEFAULT_THRESHOLD = 0.660167
DEFAULT_FRAME_SIZE = 1_280

ModelFactory = Callable[..., Any]


def _load_openwakeword_model(**kwargs: Any) -> Any:
    """Import OpenWakeWord only when the engine is started."""
    try:
        from openwakeword.model import Model
    except ImportError as exc:
        raise RuntimeError(
            "OpenWakeWord is not installed. Run: "
            ".venv\\Scripts\\python.exe -m pip install -r requirements.txt"
        ) from exc

    return Model(**kwargs)


class OpenWakeWordEngine:
    """Run the local Qronos ONNX wake-word model on 16 kHz PCM audio."""

    def __init__(
        self,
        model_path: str | Path = DEFAULT_MODEL_PATH,
        threshold: float = DEFAULT_THRESHOLD,
        frame_size: int = DEFAULT_FRAME_SIZE,
        model_factory: ModelFactory | None = None,
        require_external_data: bool = True,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("Threshold must be between 0.0 and 1.0.")

        if frame_size <= 0:
            raise ValueError("Frame size must be greater than zero.")

        self.model_path = Path(model_path)
        self.threshold = float(threshold)
        self.frame_size = frame_size
        self.model_name = self.model_path.stem
        self.require_external_data = require_external_data

        self.last_score = 0.0

        self._model_factory = (
            model_factory
            if model_factory is not None
            else _load_openwakeword_model
        )
        self._model: Any | None = None
        self._running = False
        self._paused = False

    @property
    def external_data_path(self) -> Path:
        return Path(f"{self.model_path}.data")

    def start(self) -> None:
        if self._running:
            return

        if not self.model_path.is_file():
            raise FileNotFoundError(
                f"Wake-word model was not found: {self.model_path}"
            )

        if (
            self.require_external_data
            and not self.external_data_path.is_file()
        ):
            raise FileNotFoundError(
                "The ONNX external data file was not found: "
                f"{self.external_data_path}"
            )

        model = self._model_factory(
            wakeword_models=[str(self.model_path)],
            inference_framework="onnx",
        )

        reset = getattr(model, "reset", None)
        if callable(reset):
            reset()

        self._model = model
        self.last_score = 0.0
        self._paused = False
        self._running = True

    def stop(self) -> None:
        model = self._model
        reset = getattr(model, "reset", None)
        if callable(reset):
            reset()

        self._model = None
        self.last_score = 0.0
        self._paused = False
        self._running = False

    def pause(self) -> None:
        if self._running:
            self._paused = True

    def resume(self) -> None:
        if self._running:
            self._paused = False

    def is_running(self) -> bool:
        return self._running

    def process_audio(self, audio_data: bytes) -> bool:
        if not self._running or self._paused:
            return False

        if self._model is None:
            raise RuntimeError("Wake-word model is not loaded.")

        if not isinstance(audio_data, bytes):
            raise TypeError("Audio data must be bytes containing int16 PCM.")

        expected_bytes = self.frame_size * np.dtype(np.int16).itemsize
        if len(audio_data) != expected_bytes:
            raise ValueError(
                "Audio frame has an unexpected size. "
                f"Expected {expected_bytes} bytes, got {len(audio_data)}."
            )

        samples = np.frombuffer(
            audio_data,
            dtype=np.int16,
        )
        prediction = self._model.predict(samples)

        if self.model_name not in prediction:
            available_names = ", ".join(sorted(prediction))
            raise RuntimeError(
                f"Model score '{self.model_name}' was not returned. "
                f"Available scores: {available_names or 'none'}"
            )

        self.last_score = float(prediction[self.model_name])

        return self.last_score >= self.threshold


if __name__ == "__main__":
    engine = OpenWakeWordEngine()
    engine.start()
    print(
        "Qronos wake-word model loaded successfully: "
        f"{engine.model_path}"
    )
    engine.stop()
