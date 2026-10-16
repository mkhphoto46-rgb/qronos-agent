"""The local validation runner plans and refuses without running anything here."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from build_support import local_validate as lv


@pytest.mark.parametrize("item", [
    "tests", "tests/", "-k", "--collect-only", "../tests/test_local_validate.py", "core/config.py",
    "tests/conftest.py", "tests/test_does_not_exist.py", "tests/test_local_validate.py::",
    str(lv.ROOT / "tests" / "test_local_validate.py"),
])
def test_only_existing_test_files_or_node_ids_under_tests_are_selectable(item):
    with pytest.raises(lv.SelectionError):
        lv.check_selection(item)


def test_a_file_and_a_node_id_are_accepted():
    assert lv.check_selection("tests/test_local_validate.py") == "tests/test_local_validate.py"
    assert lv.check_selection("tests\\test_local_validate.py::test_x") == "tests/test_local_validate.py::test_x"


def test_the_workspace_is_fresh_named_pytest_and_outside_the_repo(tmp_path):
    with pytest.raises(lv.SelectionError):
        lv.check_workspace(tmp_path / "plain")
    with pytest.raises(lv.SelectionError):
        lv.check_workspace(lv.ROOT / "pytest-inside")
    existing = tmp_path / "pytest-existing"
    existing.mkdir()
    with pytest.raises(lv.SelectionError):
        lv.check_workspace(existing)
    assert lv.check_workspace(tmp_path / "pytest-new") == (tmp_path / "pytest-new").resolve()


def test_tools_must_be_supplied_as_absolute_existing_files(tmp_path):
    for value in (None, "python", str(tmp_path / "missing.exe")):
        with pytest.raises(lv.SelectionError):
            lv.check_tool(value, "python")


def test_children_get_live_off_autostart_off_and_a_redirected_profile(tmp_path):
    base = {"QRONOS_RUN_LIVE_DEVICE_TESTS": "1", "QRONOS_OLLAMA_AUTO_START": "1",
            "QRONOS_DESKTOP_WAKE_BOOTSTRAP": "1", "QRONOS_ASSET_ROOT": "E:/real-assets", "PATH": "p"}
    environment = lv.child_environment(tmp_path, base)
    assert environment["QRONOS_RUN_LIVE_DEVICE_TESTS"] == "0"
    assert environment["QRONOS_OLLAMA_AUTO_START"] == "0"
    assert "QRONOS_DESKTOP_WAKE_BOOTSTRAP" not in environment
    for name in ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "QRONOS_ASSET_ROOT", "QRONOS_DATA_ROOT"):
        assert tmp_path in Path(environment[name]).parents
    assert environment["PATH"] == "p"


def test_without_a_selection_nothing_runs(capsys, monkeypatch):
    monkeypatch.setattr(lv.subprocess, "run", lambda argv, **kw: pytest.fail(f"ran {argv}")
                        if argv[0] != "git" else type("R", (), {"returncode": 1, "stdout": ""})())
    assert lv.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["executed"] is False and report["steps"] == []


def test_dry_run_plans_scoped_pytest_and_node_without_running(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(lv.subprocess, "run", lambda argv, **kw: pytest.fail(f"ran {argv}")
                        if argv[0] != "git" else type("R", (), {"returncode": 1, "stdout": ""})())
    assert lv.main(["--dry-run", "--frontend", "--workspace", str(tmp_path / "pytest-ws"),
                    "tests/test_local_validate.py"]) == 0
    report = json.loads(capsys.readouterr().out)
    python_step, node_step = report["steps"]
    assert python_step["argv"][1:4] == ["-m", "pytest", "tests/test_local_validate.py"]
    assert "-p" in python_step["argv"] and any(a.startswith("--basetemp=") for a in python_step["argv"])
    assert node_step["argv"][1] == "--test" and all(a.endswith(".test.mjs") for a in node_step["argv"][2:])
    assert not (tmp_path / "pytest-ws").exists()


def test_execution_without_a_supplied_python_is_refused(capsys, tmp_path):
    assert lv.main(["--workspace", str(tmp_path / "pytest-ws"), "tests/test_local_validate.py"]) == 2
    assert "--python" in json.loads(capsys.readouterr().out)["error"]
