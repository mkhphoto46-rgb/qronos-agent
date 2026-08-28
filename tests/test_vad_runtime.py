from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from core.vad_runtime import (
    VADRuntime,
)
from core.whisper_cpp_vad_runtime import (
    WhisperCppVADRuntime,
)


def make_pcm16(
    sample_count: int,
    value: int = 0,
) -> bytes:
    return np.full(
        sample_count,
        value,
        dtype=np.int16,
    ).tobytes()


class FakeVADRuntime(VADRuntime):
    @property
    def sample_rate(self) -> int:
        return 16_000

    def health_check(self) -> bool:
        return True

    def process_pcm16(
        self,
        audio_data: bytes,
    ) -> tuple[float, ...]:
        del audio_data

        return (
            0.10,
            0.80,
        )

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeWhisperCppVADRuntime(
    WhisperCppVADRuntime
):
    def __init__(
        self,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(
            *args,
            **kwargs,
        )

        self.inferred_sample_counts: list[int] = []
        self.native_reset_calls = 0
        self.native_close_calls = 0
        self.ensure_loaded_calls = 0

    def _ensure_loaded(self) -> None:
        if self._closed:
            raise RuntimeError(
                "VAD runtime is closed."
            )

        self.ensure_loaded_calls += 1

    def _infer_probabilities(
        self,
        samples: np.ndarray,
    ) -> tuple[float, ...]:
        self.inferred_sample_counts.append(
            int(
                samples.size
            )
        )

        count = (
            samples.size
            // self.window_samples
        )

        return tuple(
            0.75
            for _ in range(
                count
            )
        )

    def _reset_native_state(self) -> None:
        self.native_reset_calls += 1

    def _close_native(self) -> None:
        self.native_close_calls += 1


class TestVADRuntime(
    unittest.TestCase
):
    def test_fake_runtime(
        self,
    ) -> None:
        runtime = FakeVADRuntime()

        self.assertIsInstance(
            runtime,
            VADRuntime,
        )

        self.assertTrue(
            runtime.health_check()
        )

        self.assertEqual(
            runtime.sample_rate,
            16_000,
        )

        self.assertEqual(
            runtime.process_pcm16(
                b"\x00\x00"
            ),
            (
                0.10,
                0.80,
            ),
        )

    def test_invalid_thread_count(
        self,
    ) -> None:
        with self.assertRaises(
            ValueError
        ):
            WhisperCppVADRuntime(
                thread_count=0
            )

    def test_invalid_window_size(
        self,
    ) -> None:
        with self.assertRaises(
            ValueError
        ):
            WhisperCppVADRuntime(
                window_samples=0
            )

    def test_health_check_requires_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(
                directory
            )

            runtime = (
                WhisperCppVADRuntime(
                    library_path=(
                        root
                        / "missing.dll"
                    ),
                    model_path=(
                        root
                        / "missing.bin"
                    ),
                )
            )

            self.assertFalse(
                runtime.health_check()
            )

    def test_health_check_when_files_exist(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(
                directory
            )

            runtime_dir = (
                root
                / "runtime"
            )

            runtime_dir.mkdir()

            library = (
                runtime_dir
                / "whisper.dll"
            )

            ggml_library = (
                runtime_dir
                / "ggml.dll"
            )

            model = (
                root
                / "vad.bin"
            )

            library.write_bytes(
                b"dll"
            )

            ggml_library.write_bytes(
                b"ggml"
            )

            model.write_bytes(
                b"model"
            )

            runtime = (
                WhisperCppVADRuntime(
                    library_path=library,
                    model_path=model,
                    ggml_library_path=ggml_library,
                    backend_directory=runtime_dir,
                )
            )

            self.assertTrue(
                runtime.health_check()
            )

    def test_odd_pcm_byte_count_is_rejected(
        self,
    ) -> None:
        runtime = (
            FakeWhisperCppVADRuntime()
        )

        with self.assertRaises(
            ValueError
        ):
            runtime.process_pcm16(
                b"\x00"
            )

    def test_partial_window_is_buffered(
        self,
    ) -> None:
        runtime = (
            FakeWhisperCppVADRuntime()
        )

        result = runtime.process_pcm16(
            make_pcm16(
                300
            )
        )

        self.assertEqual(
            result,
            (),
        )

        self.assertEqual(
            runtime.inferred_sample_counts,
            [],
        )

        result = runtime.process_pcm16(
            make_pcm16(
                212
            )
        )

        self.assertEqual(
            result,
            (
                0.75,
            ),
        )

        self.assertEqual(
            runtime.inferred_sample_counts,
            [
                512,
            ],
        )

    def test_1280_sample_frames_are_split_without_padding(
        self,
    ) -> None:
        runtime = (
            FakeWhisperCppVADRuntime()
        )

        first = runtime.process_pcm16(
            make_pcm16(
                1_280
            )
        )

        self.assertEqual(
            first,
            (
                0.75,
                0.75,
            ),
        )

        self.assertEqual(
            runtime.inferred_sample_counts,
            [
                1_024,
            ],
        )

        second = runtime.process_pcm16(
            make_pcm16(
                1_280
            )
        )

        self.assertEqual(
            second,
            (
                0.75,
                0.75,
                0.75,
            ),
        )

        self.assertEqual(
            runtime.inferred_sample_counts,
            [
                1_024,
                1_536,
            ],
        )

    def test_reset_clears_pending_audio(
        self,
    ) -> None:
        runtime = (
            FakeWhisperCppVADRuntime()
        )

        runtime.process_pcm16(
            make_pcm16(
                300
            )
        )

        runtime.reset()

        result = runtime.process_pcm16(
            make_pcm16(
                300
            )
        )

        self.assertEqual(
            result,
            (),
        )

        self.assertEqual(
            runtime.inferred_sample_counts,
            [],
        )

        self.assertEqual(
            runtime.native_reset_calls,
            1,
        )

    def test_prepare_warms_runtime_and_resets_state(
        self,
    ) -> None:
        runtime = (
            FakeWhisperCppVADRuntime()
        )

        runtime.prepare()

        self.assertEqual(
            runtime.ensure_loaded_calls,
            1,
        )

        self.assertEqual(
            runtime.inferred_sample_counts,
            [
                512,
            ],
        )

        self.assertEqual(
            runtime.native_reset_calls,
            1,
        )

    def test_close_is_idempotent(
        self,
    ) -> None:
        runtime = (
            FakeWhisperCppVADRuntime()
        )

        runtime.close()
        runtime.close()

        self.assertEqual(
            runtime.native_close_calls,
            1,
        )

        self.assertFalse(
            runtime.health_check()
        )

        with self.assertRaises(
            RuntimeError
        ):
            runtime.process_pcm16(
                make_pcm16(
                    512
                )
            )


if __name__ == "__main__":
    unittest.main()
