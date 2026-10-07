"""Read-only setup diagnostics for the source and installed runtimes.

This command never opens a microphone, launches an application, loads a
model, or downloads assets. Model-server inspection is an explicit option
and goes through the existing bounded local Ollama controller.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable, Iterable

from core.config import CONFIG, QronosPaths
from core.model_registry import get_model


ASSET_GROUPS = {
    "wakeWord": (
        "models/wake_word/qronos.onnx",
        "models/wake_word/qronos.onnx.data",
    ),
    "speechRecognition": (
        "runtime/whisper/bin/Release/whisper-cli.exe",
        "runtime/whisper/bin/Release/ggml.dll",
        "runtime/whisper/bin/Release/whisper.dll",
        "runtime/whisper/models/ggml-large-v3-turbo-q5_0.bin",
        "runtime/whisper/models/ggml-silero-v6.2.0.bin",
    ),
    "persistentSpeechRecognition": (
        "runtime/whisper/bin/Release/whisper-server.exe",
        "runtime/whisper/models/ggml-large-v3-turbo-q5_0.bin",
    ),
    "speechSynthesis": (
        "runtime/chatterbox/bin/crispasr-windows-x86_64-vulkan/crispasr.exe",
        "runtime/chatterbox/models/t3-fa-q4_k.gguf",
        "runtime/chatterbox/models/chatterbox-s3gen-q8_0.gguf",
    ),
}


def snapshot(
    *,
    paths: QronosPaths = CONFIG.paths,
    installed_models: Callable[[], Iterable[str]] | None = None,
) -> dict[str, object]:
    features: dict[str, object] = {}
    for feature, relative_paths in ASSET_GROUPS.items():
        missing = [name for name in relative_paths if not (paths.assets / Path(name)).is_file()]
        features[feature] = {"assetsPresent": not missing, "missing": missing}
    models = {role: get_model(role).name for role in ("fast", "heavy", "vision")}
    server: dict[str, object] = {"status": "unchecked", "configured": models}
    if installed_models is not None:
        try:
            installed = frozenset(installed_models())
            server.update({
                "status": "available",
                "missingRoles": [role for role, name in models.items() if name not in installed],
            })
        except RuntimeError:
            server.update({"status": "unavailable", "missingRoles": None})
    return {
        "schemaVersion": 1,
        "packaged": paths.packaged,
        "assetRoot": str(paths.assets),
        "dataRoot": str(paths.data),
        "features": features,
        "modelServer": server,
        "notice": "Asset presence does not verify model quality, hardware compatibility or live acceptance.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", action="store_true", help="Inspect local model names without starting Ollama.")
    arguments = parser.parse_args()
    inspect = None
    if arguments.models:
        from core.ollama_controller import OllamaController
        inspect = OllamaController().installed_model_names
    print(json.dumps(snapshot(installed_models=inspect), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
