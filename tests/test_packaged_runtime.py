from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from build_support.build_runtime import verify_manifest, write_manifest
from build_support.runtime_entry import main
from core.config import resolve_paths
from core.runtime_preflight import ASSET_GROUPS, snapshot


def test_development_paths_preserve_existing_layout(tmp_path):
    paths = resolve_paths(root=tmp_path, packaged=False, environ={})
    assert paths.root == tmp_path
    assert paths.assets == tmp_path
    assert paths.data == tmp_path / "data"
    assert paths.voice_temp == tmp_path / "runtime/chatterbox/temp"
    assert paths.voice_cache == tmp_path / "runtime/chatterbox/cache"
    assert not paths.packaged
    assert list(tmp_path.iterdir()) == []


def test_installed_paths_use_user_state_and_separate_assets(tmp_path):
    bundle = tmp_path / "read-only-install"
    local = tmp_path / "local-app-data"
    paths = resolve_paths(root=bundle, packaged=True, environ={"LOCALAPPDATA": str(local)})
    assert paths.root == bundle
    assert paths.data == local / "Qronos"
    assert paths.assets == local / "Qronos/RuntimeAssets"
    assert paths.models == paths.assets / "models"
    assert paths.voice_temp == paths.data / "Temp/Chatterbox"
    assert paths.voice_cache == paths.data / "Cache/Chatterbox"
    assert not bundle.exists()
    assert not local.exists()


def test_packaged_assets_override_does_not_move_writable_state(tmp_path):
    paths = resolve_paths(root=tmp_path / "install", packaged=True, environ={
        "QRONOS_DATA_ROOT": str(tmp_path / "profile"),
        "QRONOS_ASSET_ROOT": str(tmp_path / "provided assets"),
    })
    assert paths.assets == tmp_path / "provided assets"
    assert paths.voice_temp == tmp_path / "profile/Temp/Chatterbox"


@pytest.mark.parametrize("variable", ["QRONOS_DATA_ROOT", "QRONOS_ASSET_ROOT"])
def test_relative_installed_paths_are_rejected(tmp_path, variable):
    with pytest.raises(ValueError, match="absolute"):
        resolve_paths(root=tmp_path / "install", packaged=True, environ={variable: "relative/path"})


def test_installed_state_cannot_write_inside_bundle(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        resolve_paths(root=tmp_path, packaged=True, environ={"QRONOS_DATA_ROOT": str(tmp_path / "data")})


@pytest.mark.parametrize("arguments", [[], ["-c", "print(1)"], ["os"], ["../../../script.py"], ["core.settings_cli"]])
def test_dispatch_refuses_arbitrary_python_modules_and_scripts(arguments):
    output = StringIO()
    with patch("build_support.runtime_entry.importlib.import_module") as imported, redirect_stdout(output):
        assert main(arguments) == 2
    imported.assert_not_called()
    assert json.loads(output.getvalue())["ok"] is False


def test_dispatch_routes_only_known_service_and_preserves_arguments():
    with patch.object(sys, "argv", ["runtime"]), patch("build_support.runtime_entry.importlib.import_module") as imported:
        imported.return_value.main.return_value = 0
        assert main(["settings", "snapshot"]) == 0
        imported.assert_called_once_with("core.settings_cli")
        assert sys.argv == ["settings", "snapshot"]


def test_dispatch_preserves_cli_errors_in_frozen_mode():
    output = StringIO()
    with patch.object(sys, "argv", ["runtime"]), patch("build_support.runtime_entry.importlib.import_module") as imported, redirect_stdout(output):
        imported.return_value.main.side_effect = RuntimeError("Backend unavailable.")
        assert main(["library", "snapshot"]) == 1
    assert json.loads(output.getvalue()) == {"ok": False, "error": "Backend unavailable.", "errorType": "RuntimeError"}


def test_installed_storage_locator_uses_the_same_isolated_profile(tmp_path, monkeypatch):
    from dataclasses import replace
    from core import storage_locations
    paths = resolve_paths(root=tmp_path / "install", packaged=True, environ={"QRONOS_DATA_ROOT": str(tmp_path / "profile")})
    monkeypatch.setattr(storage_locations, "CONFIG", replace(storage_locations.CONFIG, paths=paths))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "other-user-data"))
    monkeypatch.setattr(storage_locations, "_documents", lambda: tmp_path / "documents")
    assert storage_locations.locator_path() == paths.data / "storage_locations.json"
    result = storage_locations.load_storage_locations(create=False)
    assert result.history_root == str(paths.data / "History")
    assert not paths.data.exists()


def test_preflight_reports_absence_without_creating_directories(tmp_path):
    paths = resolve_paths(root=tmp_path, packaged=False, environ={})
    result = snapshot(paths=paths)
    assert all(not feature["assetsPresent"] for feature in result["features"].values())
    assert result["modelServer"]["status"] == "unchecked"
    assert list(tmp_path.iterdir()) == []


def test_preflight_distinguishes_missing_models_and_missing_server(tmp_path):
    paths = resolve_paths(root=tmp_path, packaged=False, environ={})
    available = snapshot(paths=paths, installed_models=lambda: ())
    assert available["modelServer"]["status"] == "available"
    assert set(available["modelServer"]["missingRoles"]) == {"fast", "heavy", "vision"}
    def unavailable():
        raise RuntimeError("private server response body")
    failed = snapshot(paths=paths, installed_models=unavailable)
    assert failed["modelServer"]["status"] == "unavailable"
    assert failed["modelServer"]["missingRoles"] is None
    assert "private server" not in json.dumps(failed)


def test_preflight_checks_all_assets_for_feature(tmp_path):
    paths = resolve_paths(root=tmp_path, packaged=False, environ={})
    for relative in ASSET_GROUPS["wakeWord"]:
        asset = tmp_path / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"fixture")
    assert snapshot(paths=paths)["features"]["wakeWord"]["assetsPresent"] is True


def bundle_fixture(tmp_path):
    import os
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / ("qronos-runtime.exe" if os.name == "nt" else "qronos-runtime")).write_bytes(b"executable fixture")
    (bundle / "data.json").write_text("{}", encoding="utf-8")
    write_manifest(bundle)
    return bundle


def test_manifest_detects_tampered_runtime_files(tmp_path):
    bundle = bundle_fixture(tmp_path)
    verify_manifest(bundle)
    (bundle / "data.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        verify_manifest(bundle)


def test_manifest_refuses_unlisted_files(tmp_path):
    bundle = bundle_fixture(tmp_path)
    (bundle / "unreviewed.py").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="complete"):
        verify_manifest(bundle)


@pytest.mark.parametrize("name", ["model.onnx", "model.onnx.data", "model.gguf", "model.bin", "model.tflite", "model.safetensors"])
def test_manifest_refuses_silently_collected_model_assets(tmp_path, name):
    bundle = bundle_fixture(tmp_path)
    (bundle / name).write_bytes(b"unreviewed weights")
    with pytest.raises(ValueError, match="separate approved provisioning"):
        write_manifest(bundle)
    with pytest.raises(ValueError, match="separate approved provisioning"):
        verify_manifest(bundle)


def test_manifest_refuses_path_escape(tmp_path):
    bundle = bundle_fixture(tmp_path)
    manifest = bundle / "runtime-manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"][0]["path"] = "../outside.txt"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="path"):
        verify_manifest(bundle)


@pytest.mark.parametrize("invalid", ["/outside.txt", "C:/outside.txt", "data\\file", "./data.json", "folder/../data.json", "folder//data.json"])
def test_manifest_refuses_noncanonical_paths(tmp_path, invalid):
    bundle = bundle_fixture(tmp_path)
    manifest = bundle / "runtime-manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"][0]["path"] = invalid
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="path"):
        verify_manifest(bundle)


def test_manifest_refuses_file_or_directory_links(tmp_path):
    bundle = bundle_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("must not be bundled", encoding="utf-8")
    try:
        (bundle / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating Windows symbolic links requires Developer Mode or administrator privileges.")
    with pytest.raises(ValueError, match="links"):
        write_manifest(bundle)
    with pytest.raises(ValueError, match="links"):
        verify_manifest(bundle)
