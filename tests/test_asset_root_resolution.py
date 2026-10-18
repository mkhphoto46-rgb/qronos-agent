"""Asset-root discovery: process value, then the persisted Windows user value, then the default.

Every case injects both the process environment and the user-environment reader, so the real registry is never
read; the one real-reader case only checks it returns None for a name that cannot exist.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.config import read_persisted_user_variable, resolve_asset_root, resolve_paths


def user(values):
    return lambda name: values.get(name)


def test_a_stale_process_without_the_variable_uses_the_persisted_user_root(tmp_path):
    persisted = tmp_path / "persisted assets"
    paths = resolve_paths(root=tmp_path / "install", packaged=True,
                          environ={"LOCALAPPDATA": str(tmp_path / "local")},
                          user_environment=user({"QRONOS_ASSET_ROOT": str(persisted)}))
    assert paths.assets == persisted.resolve()
    assert paths.models == persisted.resolve() / "models"
    assert paths.data == (tmp_path / "local" / "Qronos").resolve()  # writable state does not move
    assert paths.voice_temp == paths.data / "Temp" / "Chatterbox"
    assert paths.voice_cache == paths.data / "Cache" / "Chatterbox"


def test_an_explicit_process_value_wins_over_the_persisted_one(tmp_path):
    process, persisted = tmp_path / "process", tmp_path / "persisted"
    assert resolve_asset_root({"QRONOS_ASSET_ROOT": str(process)},
                              user({"QRONOS_ASSET_ROOT": str(persisted)})) == process.resolve()


@pytest.mark.parametrize("process_value, user_value", [("", None), ("   ", ""), (None, "  ")])
def test_blank_values_keep_the_established_default(tmp_path, process_value, user_value):
    environment = {"LOCALAPPDATA": str(tmp_path / "local")}
    if process_value is not None:
        environment["QRONOS_ASSET_ROOT"] = process_value
    reader = user({} if user_value is None else {"QRONOS_ASSET_ROOT": user_value})
    packaged = resolve_paths(root=tmp_path / "install", packaged=True, environ=environment, user_environment=reader)
    assert packaged.assets == (tmp_path / "local" / "Qronos" / "RuntimeAssets").resolve()
    development = resolve_paths(root=tmp_path, packaged=False, environ=environment, user_environment=reader)
    assert development.assets == tmp_path.resolve() and development.models == tmp_path.resolve() / "models"


@pytest.mark.parametrize("where", ["process", "user"])
def test_a_relative_override_is_refused_not_replaced_by_the_default(tmp_path, where):
    environment = {"QRONOS_ASSET_ROOT": "relative/assets"} if where == "process" else {}
    reader = user({"QRONOS_ASSET_ROOT": "relative/assets"} if where == "user" else {})
    with pytest.raises(ValueError, match="absolute"):
        resolve_paths(root=tmp_path, packaged=False, environ={**environment, "QRONOS_DEV_USE_ASSET_ROOT": "1"},
                      user_environment=reader)
    with pytest.raises(ValueError, match="absolute"):
        resolve_paths(root=tmp_path / "install", packaged=True, environ=environment, user_environment=reader)


@pytest.mark.parametrize("opt_in", [None, "", "0", "yes"])
def test_development_keeps_the_checkout_models_unless_it_opts_in(tmp_path, opt_in):
    environment = {"QRONOS_ASSET_ROOT": str(tmp_path / "process assets")}
    if opt_in is not None:
        environment["QRONOS_DEV_USE_ASSET_ROOT"] = opt_in
    paths = resolve_paths(root=tmp_path / "checkout", packaged=False, environ=environment,
                          user_environment=user({"QRONOS_ASSET_ROOT": str(tmp_path / "user assets")}))
    checkout = (tmp_path / "checkout").resolve()
    assert paths.assets == checkout and paths.models == checkout / "models"
    assert paths.runtime_assets == checkout / "runtime"


def test_development_opted_in_uses_the_asset_root_but_keeps_its_data_logs_and_settings(tmp_path):
    assets = tmp_path / "assets"
    paths = resolve_paths(root=tmp_path / "checkout", packaged=False, environ={"QRONOS_DEV_USE_ASSET_ROOT": "1"},
                          user_environment=user({"QRONOS_ASSET_ROOT": str(assets)}))
    assert paths.assets == assets.resolve() and paths.models == assets.resolve() / "models"
    assert paths.runtime_assets == assets.resolve() / "runtime"
    checkout = (tmp_path / "checkout").resolve()
    assert (paths.root, paths.data, paths.logs, paths.memory) == (
        checkout, checkout / "data", checkout / "logs", checkout / "memory")
    # Writable voice state stays in the checkout, never in the configured asset root.
    assert paths.voice_temp == checkout / "runtime" / "chatterbox" / "temp"
    assert paths.voice_cache == checkout / "runtime" / "chatterbox" / "cache"


def test_an_injected_environment_never_reads_the_real_user_environment(tmp_path, monkeypatch):
    import core.config as config

    monkeypatch.setattr(config, "read_persisted_user_variable",
                        lambda name: pytest.fail(f"real user environment read for {name}"))
    paths = resolve_paths(root=tmp_path, packaged=False, environ={})
    assert paths.assets == tmp_path.resolve()


def test_the_real_reader_returns_none_for_an_absent_name():
    assert read_persisted_user_variable("QRONOS_TEST_ABSENT_VARIABLE_C122") is None
