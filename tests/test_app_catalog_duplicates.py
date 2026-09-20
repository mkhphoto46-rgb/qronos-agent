from core.computer_executor import AppSpec, resolve_application_id
import pytest


def test_registry_and_matching_version_shortcut_resolve_to_executable(monkeypatch, tmp_path):
    executable = AppSpec("discovered:exe:one", str(tmp_path / "Adobe Photoshop 2026" / "Photoshop.exe"), "Photoshop", aliases=("فوتوشاپ",))
    shortcut = AppSpec("discovered:shortcut:one", "", "Adobe Photoshop 2026", shortcut=str(tmp_path / "Photoshop.lnk"), aliases=("فوتوشاپ",))
    monkeypatch.setattr("core.computer_executor.application_catalog", lambda: {s.app_id: s for s in (executable, shortcut)})
    assert resolve_application_id("فوتوشاپ") == executable.app_id


def test_two_installed_versions_remain_ambiguous(monkeypatch, tmp_path):
    specs = [AppSpec(f"discovered:exe:{year}", str(tmp_path / str(year) / "Photoshop.exe"), "Photoshop", aliases=("فوتوشاپ",), process_names=("photoshop.exe",)) for year in (2025, 2026)]
    monkeypatch.setattr("core.computer_executor.application_catalog", lambda: {s.app_id: s for s in specs})
    with pytest.raises(LookupError, match="More than one"):
        resolve_application_id("فوتوشاپ")
