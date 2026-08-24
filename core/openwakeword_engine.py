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

# openWakeWord uses 80 ms frames at the standard 16 kHz / 1280-sample
# configuration. Thirteen frames provide about one second of silent
# context before real listening begins.
DEFAULT_WARMUP_FRAMES = 13


ModelFactory = Callable[..., Any]


def _load_openwakeword_model(
    **kwargs: Any,
) -> Any:
    """
    Import OpenWakeWord only when the engine is started.
    """

    try:
        from openwakeword.model import Model

    except ImportError as exc:
        raise RuntimeError(
            "OpenWakeWord is not installed. Run: "
            ".venv\\Scripts\\python.exe "
            "-m pip install -r requirements.txt"
        ) from exc

    return Model(
        **kwargs
    )


class OpenWakeWordEngine:
    """
    Run the local Qronos ONNX wake-word model on 16 kHz PCM audio.

    The model is pre-warmed with silent frames after startup and after
    resuming from a triggered state. This avoids OpenWakeWord's initial
    zero-prediction frames from affecting the first real wake-word attempt.

    Resuming also resets the model's internal prediction/audio buffers so
    one spoken wake word cannot remain latched and trigger repeatedly.
    """

    def __init__(
        self,
        model_path: str | Path = DEFAULT_MODEL_PATH,
        threshold: float = DEFAULT_THRESHOLD,
        frame_size: int = DEFAULT_FRAME_SIZE,
        model_factory: ModelFactory | None = None,
        require_external_data: bool = True,
        warmup_frames: int = DEFAULT_WARMUP_FRAMES,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(
                "Threshold must be between 0.0 and 1.0."
            )

        if frame_size <= 0:
            raise ValueError(
                "Frame size must be greater than zero."
            )

        if warmup_frames < 0:
            raise ValueError(
                "warmup_frames must not be negative."
            )

        self.model_path = Path(
            model_path
        )

        self.threshold = float(
            threshold
        )

        self.frame_size = (
            frame_size
        )

        self.model_name = (
            self.model_path.stem
        )

        self.require_external_data = (
            require_external_data
        )

        self.warmup_frames = (
            warmup_frames
        )

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
    def external_data_path(
        self,
    ) -> Path:
        return Path(
            f"{self.model_path}.data"
        )

    @staticmethod
    def _reset_model(
        model: Any,
    ) -> None:
        reset = getattr(
            model,
            "reset",
            None,
        )

        if callable(reset):
            reset()

    def _warm_model(
        self,
        model: Any,
    ) -> None:
        """
        Prime OpenWakeWord with silent context.

        OpenWakeWord intentionally suppresses predictions during its first
        few frames after initialization/reset. Feeding silent frames here
        means those initialization frames happen before the user is asked
        to say the wake word.
        """

        if self.warmup_frames == 0:
            return

        silence = np.zeros(
            self.frame_size,
            dtype=np.int16,
        )

        for _ in range(
            self.warmup_frames
        ):
            model.predict(
                silence
            )

    def start(self) -> None:
        if self._running:
            return

        if not self.model_path.is_file():
            raise FileNotFoundError(
                "Wake-word model was not found: "
                f"{self.model_path}"
            )

        if (
            self.require_external_data
            and not self.external_data_path.is_file()
        ):
            raise FileNotFoundError(
                "The ONNX external data file "
                "was not found: "
                f"{self.external_data_path}"
            )

        model = self._model_factory(
            wakeword_models=[
                str(
                    self.model_path
                )
            ],
            inference_framework="onnx",
        )

        try:
            self._reset_model(
                model
            )

            self._warm_model(
                model
            )

        except Exception:
            self._reset_model(
                model
            )
            raise

        self._model = model
        self.last_score = 0.0
        self._paused = False
        self._running = True

    def stop(self) -> None:
        model = self._model

        if model is not None:
            self._reset_model(
                model
            )

        self._model = None
        self.last_score = 0.0
        self._paused = False
        self._running = False

    def pause(self) -> None:
        if self._running:
            self._paused = True

    def resume(self) -> None:
        """
        Resume listening with fresh model state.

        A reset prevents the previous wake-word activation from remaining
        in OpenWakeWord's rolling buffers. Silent warm-up then restores the
        context needed for immediate real-world detection.
        """

        if not self._running:
            return

        if not self._paused:
            return

        if self._model is None:
            raise RuntimeError(
                "Wake-word model is not loaded."
            )

        self._reset_model(
            self._model
        )

        self._warm_model(
            self._model
        )

        # Keep last_score available for diagnostics until the next real
        # microphone frame is processed.
        self._paused = False

    def is_running(self) -> bool:
        return self._running

    def process_audio(
        self,
        audio_data: bytes,
    ) -> bool:
        if (
            not self._running
            or self._paused
        ):
            return False

        if self._model is None:
            raise RuntimeError(
                "Wake-word model is not loaded."
            )

        if not isinstance(
            audio_data,
            bytes,
        ):
            raise TypeError(
                "Audio data must be bytes "
                "containing int16 PCM."
            )

        expected_bytes = (
            self.frame_size
            * np.dtype(
                np.int16
            ).itemsize
        )

        if len(audio_data) != expected_bytes:
            raise ValueError(
                "Audio frame has an "
                "unexpected size. "
                f"Expected {expected_bytes} "
                f"bytes, got "
                f"{len(audio_data)}."
            )

        samples = np.frombuffer(
            audio_data,
            dtype=np.int16,
        )

        prediction = (
            self._model.predict(
                samples
            )
        )

        if (
            self.model_name
            not in prediction
        ):
            available_names = ", ".join(
                sorted(
                    prediction
                )
            )

            raise RuntimeError(
                "Model score "
                f"'{self.model_name}' "
                "was not returned. "
                "Available scores: "
                f"{available_names or 'none'}"
            )

        self.last_score = float(
            prediction[
                self.model_name
            ]
        )

        return (
            self.last_score
            >= self.threshold
        )


if __name__ == "__main__":
    engine = (
        OpenWakeWordEngine()
    )

    engine.start()

    print(
        "Qronos wake-word model "
        "loaded successfully: "
        f"{engine.model_path}"
    )

    engine.stop()