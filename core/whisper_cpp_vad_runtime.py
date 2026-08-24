from __future__ import annotations

import ctypes
import os
from pathlib import Path

import numpy as np

from core.config import CONFIG
from core.vad_runtime import VADRuntime


DEFAULT_RUNTIME_DIR = (
    CONFIG.paths.root
    / "runtime"
    / "whisper"
    / "bin"
    / "Release"
)

DEFAULT_GGML_DLL = (
    DEFAULT_RUNTIME_DIR
    / "ggml.dll"
)

DEFAULT_WHISPER_DLL = (
    DEFAULT_RUNTIME_DIR
    / "whisper.dll"
)

DEFAULT_VAD_MODEL = (
    CONFIG.paths.root
    / "runtime"
    / "whisper"
    / "models"
    / "ggml-silero-v6.2.0.bin"
)

WHISPER_SAMPLE_RATE = 16_000
SILERO_WINDOW_SAMPLES = 512


class _WhisperVADContextParams(
    ctypes.Structure
):
    _fields_ = [
        (
            "n_threads",
            ctypes.c_int,
        ),
        (
            "use_gpu",
            ctypes.c_bool,
        ),
        (
            "gpu_device",
            ctypes.c_int,
        ),
    ]


_WhisperLogCallback = ctypes.CFUNCTYPE(
    None,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_void_p,
)


@_WhisperLogCallback
def _silent_whisper_log(
    level,
    text,
    user_data,
) -> None:
    del level
    del text
    del user_data


class WhisperCppVADRuntime(VADRuntime):
    """
    Native streaming Silero VAD adapter using whisper.cpp.

    GGML dynamic backends are loaded explicitly from the bundled Qronos
    whisper.cpp runtime directory.

    PCM16 input may arrive in arbitrary frame sizes. The adapter buffers
    incomplete samples and feeds only complete Silero windows to VAD.
    """

    def __init__(
        self,
        library_path: str | Path = DEFAULT_WHISPER_DLL,
        model_path: str | Path = DEFAULT_VAD_MODEL,
        ggml_library_path: str | Path = DEFAULT_GGML_DLL,
        backend_directory: str | Path = DEFAULT_RUNTIME_DIR,
        thread_count: int = 2,
        use_gpu: bool = False,
        gpu_device: int = 0,
        window_samples: int = SILERO_WINDOW_SAMPLES,
        suppress_logs: bool = True,
    ) -> None:
        if thread_count <= 0:
            raise ValueError(
                "thread_count must be greater than zero."
            )

        if window_samples <= 0:
            raise ValueError(
                "window_samples must be greater than zero."
            )

        if gpu_device < 0:
            raise ValueError(
                "gpu_device must not be negative."
            )

        self.library_path = Path(
            library_path
        )

        self.model_path = Path(
            model_path
        )

        self.ggml_library_path = Path(
            ggml_library_path
        )

        self.backend_directory = Path(
            backend_directory
        )

        self.thread_count = thread_count
        self.use_gpu = use_gpu
        self.gpu_device = gpu_device
        self.window_samples = window_samples
        self.suppress_logs = suppress_logs

        self._sample_rate = (
            WHISPER_SAMPLE_RATE
        )

        self._pending_samples = np.empty(
            0,
            dtype=np.float32,
        )

        self._ggml = None
        self._dll = None
        self._context = None
        self._dll_directory_handle = None
        self._closed = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def health_check(self) -> bool:
        return (
            not self._closed
            and self.ggml_library_path.is_file()
            and self.library_path.is_file()
            and self.model_path.is_file()
            and self.backend_directory.is_dir()
        )

    def prepare(self) -> None:
        """
        Fully initialize and warm the native VAD runtime before capture.

        This ensures GGML backend loading, model loading, and the first
        Silero inference happen before the user begins speaking.
        """

        if self._closed:
            raise RuntimeError(
                "VAD runtime is closed."
            )

        self._ensure_loaded()

        silence = np.zeros(
            self.window_samples,
            dtype=np.float32,
        )

        probabilities = (
            self._infer_probabilities(
                silence
            )
        )

        if len(probabilities) != 1:
            raise RuntimeError(
                "VAD warm-up returned an "
                "unexpected probability count."
            )

        self.reset()

    @staticmethod
    def _configure_ggml_library(
        ggml,
    ) -> None:
        ggml.ggml_backend_load_all_from_path.argtypes = [
            ctypes.c_char_p,
        ]

        ggml.ggml_backend_load_all_from_path.restype = (
            None
        )

        ggml.ggml_backend_dev_count.argtypes = []

        ggml.ggml_backend_dev_count.restype = (
            ctypes.c_size_t
        )

    @staticmethod
    def _configure_whisper_library(
        dll,
    ) -> None:
        dll.whisper_vad_default_context_params.argtypes = []
        dll.whisper_vad_default_context_params.restype = (
            _WhisperVADContextParams
        )

        dll.whisper_vad_init_from_file_with_params.argtypes = [
            ctypes.c_char_p,
            _WhisperVADContextParams,
        ]

        dll.whisper_vad_init_from_file_with_params.restype = (
            ctypes.c_void_p
        )

        dll.whisper_vad_detect_speech_no_reset.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(
                ctypes.c_float
            ),
            ctypes.c_int,
        ]

        dll.whisper_vad_detect_speech_no_reset.restype = (
            ctypes.c_bool
        )

        dll.whisper_vad_reset_state.argtypes = [
            ctypes.c_void_p,
        ]

        dll.whisper_vad_reset_state.restype = (
            None
        )

        dll.whisper_vad_n_probs.argtypes = [
            ctypes.c_void_p,
        ]

        dll.whisper_vad_n_probs.restype = (
            ctypes.c_int
        )

        dll.whisper_vad_probs.argtypes = [
            ctypes.c_void_p,
        ]

        dll.whisper_vad_probs.restype = (
            ctypes.POINTER(
                ctypes.c_float
            )
        )

        dll.whisper_vad_free.argtypes = [
            ctypes.c_void_p,
        ]

        dll.whisper_vad_free.restype = (
            None
        )

        dll.whisper_log_set.argtypes = [
            _WhisperLogCallback,
            ctypes.c_void_p,
        ]

        dll.whisper_log_set.restype = (
            None
        )

    def _open_dll_directory(
        self,
    ) -> None:
        if (
            os.name == "nt"
            and hasattr(
                os,
                "add_dll_directory",
            )
            and self._dll_directory_handle
            is None
        ):
            self._dll_directory_handle = (
                os.add_dll_directory(
                    str(
                        self.backend_directory
                    )
                )
            )

    def _load_ggml_backends(
        self,
    ) -> None:
        if self._ggml is not None:
            return

        if not self.ggml_library_path.is_file():
            raise FileNotFoundError(
                "ggml.dll was not found: "
                f"{self.ggml_library_path}"
            )

        if not self.backend_directory.is_dir():
            raise FileNotFoundError(
                "GGML backend directory was not found: "
                f"{self.backend_directory}"
            )

        try:
            ggml = ctypes.CDLL(
                str(
                    self.ggml_library_path
                )
            )

        except OSError as exc:
            raise RuntimeError(
                "Could not load ggml.dll."
            ) from exc

        self._configure_ggml_library(
            ggml
        )

        ggml.ggml_backend_load_all_from_path(
            str(
                self.backend_directory
            ).encode(
                "utf-8"
            )
        )

        device_count = int(
            ggml.ggml_backend_dev_count()
        )

        if device_count <= 0:
            raise RuntimeError(
                "GGML did not register any "
                "backend devices."
            )

        self._ggml = ggml

    def _ensure_loaded(self) -> None:
        if self._closed:
            raise RuntimeError(
                "VAD runtime is closed."
            )

        if self._context is not None:
            return

        if not self.library_path.is_file():
            raise FileNotFoundError(
                "whisper.dll was not found: "
                f"{self.library_path}"
            )

        if not self.model_path.is_file():
            raise FileNotFoundError(
                "Silero VAD model was not found: "
                f"{self.model_path}"
            )

        self._open_dll_directory()

        try:
            self._load_ggml_backends()

            dll = ctypes.CDLL(
                str(
                    self.library_path
                )
            )

        except Exception:
            self._close_dll_directory()
            raise

        self._configure_whisper_library(
            dll
        )

        if self.suppress_logs:
            dll.whisper_log_set(
                _silent_whisper_log,
                None,
            )

        params = (
            dll.whisper_vad_default_context_params()
        )

        params.n_threads = (
            self.thread_count
        )

        params.use_gpu = (
            self.use_gpu
        )

        params.gpu_device = (
            self.gpu_device
        )

        context = (
            dll.whisper_vad_init_from_file_with_params(
                str(
                    self.model_path
                ).encode(
                    "utf-8"
                ),
                params,
            )
        )

        if not context:
            self._dll = dll
            self._close_native()

            raise RuntimeError(
                "Could not initialize "
                "whisper.cpp VAD context."
            )

        self._dll = dll
        self._context = context

    def _infer_probabilities(
        self,
        samples: np.ndarray,
    ) -> tuple[float, ...]:
        self._ensure_loaded()

        contiguous = np.ascontiguousarray(
            samples,
            dtype=np.float32,
        )

        success = (
            self._dll.whisper_vad_detect_speech_no_reset(
                self._context,
                contiguous.ctypes.data_as(
                    ctypes.POINTER(
                        ctypes.c_float
                    )
                ),
                int(
                    contiguous.size
                ),
            )
        )

        if not success:
            raise RuntimeError(
                "whisper.cpp VAD inference failed."
            )

        probability_count = int(
            self._dll.whisper_vad_n_probs(
                self._context
            )
        )

        if probability_count <= 0:
            return ()

        probability_pointer = (
            self._dll.whisper_vad_probs(
                self._context
            )
        )

        if not probability_pointer:
            raise RuntimeError(
                "whisper.cpp VAD returned "
                "an invalid probability buffer."
            )

        return tuple(
            float(
                probability_pointer[index]
            )
            for index in range(
                probability_count
            )
        )

    def process_pcm16(
        self,
        audio_data: bytes,
    ) -> tuple[float, ...]:
        if self._closed:
            raise RuntimeError(
                "VAD runtime is closed."
            )

        if len(audio_data) % 2 != 0:
            raise ValueError(
                "PCM16 audio must contain "
                "an even number of bytes."
            )

        if not audio_data:
            return ()

        int_samples = np.frombuffer(
            audio_data,
            dtype=np.int16,
        )

        float_samples = (
            int_samples.astype(
                np.float32
            )
            / 32768.0
        )

        if self._pending_samples.size:
            combined = np.concatenate(
                (
                    self._pending_samples,
                    float_samples,
                )
            )
        else:
            combined = float_samples

        complete_sample_count = (
            combined.size
            // self.window_samples
            * self.window_samples
        )

        if complete_sample_count == 0:
            self._pending_samples = (
                combined.copy()
            )

            return ()

        complete_samples = (
            combined[
                :complete_sample_count
            ]
        )

        self._pending_samples = (
            combined[
                complete_sample_count:
            ].copy()
        )

        probabilities = (
            self._infer_probabilities(
                complete_samples
            )
        )

        expected_count = (
            complete_sample_count
            // self.window_samples
        )

        if (
            len(probabilities)
            != expected_count
        ):
            raise RuntimeError(
                "whisper.cpp VAD returned "
                "an unexpected number of "
                "speech probabilities."
            )

        return probabilities

    def _reset_native_state(self) -> None:
        if (
            self._dll is not None
            and self._context is not None
        ):
            self._dll.whisper_vad_reset_state(
                self._context
            )

    def reset(self) -> None:
        if self._closed:
            raise RuntimeError(
                "VAD runtime is closed."
            )

        self._pending_samples = np.empty(
            0,
            dtype=np.float32,
        )

        self._reset_native_state()

    def _close_dll_directory(self) -> None:
        if (
            self._dll_directory_handle
            is not None
        ):
            self._dll_directory_handle.close()
            self._dll_directory_handle = None

    def _close_native(self) -> None:
        if (
            self._dll is not None
            and self._context is not None
        ):
            self._dll.whisper_vad_free(
                self._context
            )

        self._context = None
        self._dll = None
        self._ggml = None

        self._close_dll_directory()

    def close(self) -> None:
        if self._closed:
            return

        self._pending_samples = np.empty(
            0,
            dtype=np.float32,
        )

        self._close_native()

        self._closed = True