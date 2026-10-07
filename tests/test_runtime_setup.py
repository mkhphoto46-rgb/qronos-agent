"""Setup must report missing dependencies without loading models or sensors."""
from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from core import openwakeword_engine
from core.config import resolve_paths
from core.runtime_preflight import ASSET_GROUPS, snapshot


def test_custom_wake_head_alone_does_not_claim_complete_setup(tmp_path):
    paths = resolve_paths(root=tmp_path, packaged=False, environ={})
    head = tmp_path / "models/wake_word/qronos.onnx"
    head.parent.mkdir(parents=True)
    head.write_bytes(b"test fixture")
    Path(str(head) + ".data").write_bytes(b"test fixture")
    result = snapshot(paths=paths)
    assert result["features"]["wakeWord"]["assetsPresent"] is False
    assert set(result["features"]["wakeWord"]["missing"]) == {
        "models/openwakeword/" + name for name in openwakeword_engine.FEATURE_MODEL_FILES
    }


def test_wake_support_absence_refuses_before_model_import(tmp_path, monkeypatch):
    monkeypatch.setattr(openwakeword_engine, "DEFAULT_FEATURE_MODEL_ROOT", tmp_path / "absent")
    # No model or microphone boundary can run for an incomplete asset set.
    with pytest.raises(FileNotFoundError, match="support assets"):
        openwakeword_engine._load_openwakeword_model(wakeword_models=["fixture.onnx"])
    assert list(tmp_path.iterdir()) == []


def test_wake_loader_uses_only_explicit_external_weights_and_keeps_vad(tmp_path, monkeypatch):
    for name in openwakeword_engine.FEATURE_MODEL_FILES:
        (tmp_path / name).write_bytes(b"test fixture")
    monkeypatch.setattr(openwakeword_engine, "DEFAULT_FEATURE_MODEL_ROOT", tmp_path)
    calls = []
    model = SimpleNamespace()
    def factory(**kwargs):
        calls.append(kwargs)
        return model
    vad_calls = []
    vad = SimpleNamespace()
    def vad_factory(**kwargs):
        vad_calls.append(kwargs)
        return vad
    package = ModuleType("openwakeword")
    package.__path__ = []
    model_module = ModuleType("openwakeword.model")
    model_module.Model = factory
    vad_module = ModuleType("openwakeword.vad")
    vad_module.VAD = vad_factory
    for name, module in (("openwakeword", package), ("openwakeword.model", model_module), ("openwakeword.vad", vad_module)):
        monkeypatch.setitem(sys.modules, name, module)
    loaded = openwakeword_engine._load_openwakeword_model(
        wakeword_models=[str(tmp_path / "qronos.onnx")], inference_framework="onnx", vad_threshold=.5,
    )
    assert loaded is model and model.vad is vad and model.vad_threshold == .5
    assert calls == [{
        "wakeword_models": [str(tmp_path / "qronos.onnx")], "inference_framework": "onnx",
        "vad_threshold": 0, "melspec_model_path": str(tmp_path / "melspectrogram.onnx"),
        "embedding_model_path": str(tmp_path / "embedding_model.onnx"),
    }]
    assert vad_calls == [{"model_path": str(tmp_path / "silero_vad.onnx")}]


def test_setup_ui_command_is_closed_and_registered():
    root = Path(__file__).resolve().parent.parent
    backend = (root / "desktop/src-tauri/src/runtime/backend.rs").read_text(encoding="utf-8")
    runtime = (root / "desktop/src-tauri/src/runtime/mod.rs").read_text(encoding="utf-8")
    lib = (root / "desktop/src-tauri/src/lib.rs").read_text(encoding="utf-8")
    store = (root / "desktop/src/setupStore.ts").read_text(encoding="utf-8")
    assert 'Self::Doctor => "doctor"' in backend
    assert 'Self::Doctor => "core.runtime_preflight"' in backend
    assert "runtime::runtime_setup_snapshot" in lib
    assert 'invoke<SetupSnapshot>("runtime_setup_snapshot")' in store
    command = runtime.split("pub async fn runtime_setup_snapshot", 1)[1].split("#[", 1)[0]
    assert "service_ipc::request" in command and "Service::Doctor" in command
    ipc = (root / "desktop/src-tauri/src/runtime/service_ipc.rs").read_text(encoding="utf-8")
    assert 'command.arg("--models")' in ipc


def test_asset_groups_include_every_external_wake_dependency():
    assert {"models/openwakeword/" + name for name in openwakeword_engine.FEATURE_MODEL_FILES} <= set(ASSET_GROUPS["wakeWord"])
