"""The ownership manifest an installation carries (build_support.install_manifest)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from build_support import desktop_prerequisites as prerequisites
from build_support import install_manifest
from build_support.install_manifest import InstallManifestError

_LOCAL = os.environ.get("LOCALAPPDATA", "")
MAKENSIS = Path(_LOCAL, "tauri", "NSIS", "makensis.exe") if _LOCAL else None


def test_files_are_sorted_and_folders_listed_deepest_first():
    data = install_manifest.render(["qronos-runtime/_internal/b.pyd", "qronos-desktop.exe",
                                    "qronos-runtime/_internal/A.dll", "qronos-runtime/x.exe"])

    lines = data.decode("utf-16-le").split("\r\n")
    assert lines == [install_manifest.HEADER, "F qronos-desktop.exe", "F qronos-runtime\\_internal\\A.dll",
                     "F qronos-runtime\\_internal\\b.pyd", "F qronos-runtime\\x.exe",
                     "D qronos-runtime\\_internal", "D qronos-runtime", ""]
    assert install_manifest.parse(data) == (
        ["qronos-desktop.exe", "qronos-runtime\\_internal\\A.dll", "qronos-runtime\\_internal\\b.pyd",
         "qronos-runtime\\x.exe"], ["qronos-runtime\\_internal", "qronos-runtime"])


@pytest.mark.parametrize("path", ["../outside.txt", "a/../b.txt", "/abs.txt", "C:/x.txt", "a\\b.txt", "a/b:stream",
                                  "a/name.", "a/name ", "a/ name", "CON", "a/nul.txt", "a/COM1.log", "a/x..y", ""])
def test_unsafe_payload_paths_are_refused_at_build_time(path):
    with pytest.raises(InstallManifestError):
        install_manifest.render([path])


def test_case_only_duplicates_and_file_folder_clashes_are_refused():
    with pytest.raises(InstallManifestError, match="letter case"):
        install_manifest.render(["a/Readme.txt", "a/README.txt"])
    with pytest.raises(InstallManifestError, match="both a file and a folder"):
        install_manifest.render(["a/b", "a/b/c.txt"])


@pytest.mark.parametrize("line", ["F ..\\x", "X a", "F C:\\x", "F a/b"])
def test_parse_refuses_what_the_hook_refuses(line):
    data = (install_manifest.HEADER + "\r\n" + line + "\r\n").encode("utf-16-le")

    with pytest.raises(InstallManifestError):
        install_manifest.parse(data)


def test_parse_refuses_another_header():
    with pytest.raises(InstallManifestError, match="Not a Qronos install manifest"):
        install_manifest.parse("QRONOS-INSTALL-MANIFEST 2 x\r\n".encode("utf-16-le"))


def write_tree(root: Path, files: list[str]) -> bytes:
    for name in files:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"x")
    return install_manifest.render(files)


def test_an_installed_tree_must_match_its_manifest_exactly(tmp_path):
    files = ["qronos-desktop.exe", "uninstall.exe", "qronos-runtime/_internal/a.dll"]
    data = write_tree(tmp_path, files)
    (tmp_path / "$PLUGINSDIR").mkdir()
    (tmp_path / "$PLUGINSDIR" / "System.dll").write_bytes(b"x")  # NSIS plug-ins are not installed files

    assert install_manifest.verify_installed_tree(tmp_path, data) == 3

    (tmp_path / "qronos-runtime" / "_internal" / "stale.pyd").write_bytes(b"x")
    with pytest.raises(InstallManifestError, match="unlisted"):
        install_manifest.verify_installed_tree(tmp_path, data)
    (tmp_path / "qronos-runtime" / "_internal" / "stale.pyd").unlink()
    (tmp_path / "uninstall.exe").unlink()
    with pytest.raises(InstallManifestError, match="missing"):
        install_manifest.verify_installed_tree(tmp_path, data)


def test_an_unreadable_folder_fails_the_check_instead_of_being_skipped(tmp_path, monkeypatch):
    data = write_tree(tmp_path, ["qronos-desktop.exe"])
    real_walk = os.walk

    def walk(top, onerror=None, **kwargs):
        onerror(PermissionError(13, "Access is denied", str(tmp_path / "locked")))
        yield from real_walk(top, onerror=onerror, **kwargs)

    monkeypatch.setattr(install_manifest.os, "walk", walk)

    with pytest.raises(InstallManifestError, match="cannot be read"):
        install_manifest.verify_installed_tree(tmp_path, data)


def test_the_committed_hooks_compile_into_an_uninstaller_too(tmp_path):
    """The un. functions and NSIS_HOOK_POSTUNINSTALL compile (not run) inside an uninstaller."""
    if os.name != "nt" or MAKENSIS is None or not MAKENSIS.is_file():
        pytest.skip("Tauri's NSIS compiler is not installed on this machine.")
    payload = tmp_path / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    payload.write_bytes(b"never run")
    script = tmp_path / "compile.nsi"
    script.write_text("\n".join((
        "Unicode true", 'Name "compile-only"', f'OutFile "{tmp_path / "compile-only.exe"}"',
        "RequestExecutionLevel user", f'!define QRONOS_WEBVIEW2_INSTALLER "{payload}"',
        '!define QRONOS_WEBVIEW2_VERSION "1.2.3.4"',
        f'!define QRONOS_VCREDIST_INSTALLER "{payload}"', '!define QRONOS_VCREDIST_VERSION "14.44.35211.0"',
        '!define QRONOS_VCREDIST_MAJOR "14"', '!define QRONOS_VCREDIST_MINOR "44"',
        '!define QRONOS_VCREDIST_BUILD "35211"',
        f'!include "{prerequisites.HOOK_PATH}"',
        "Section Install", '  SetOutPath "$INSTDIR"', "  !insertmacro NSIS_HOOK_PREINSTALL",
        '  WriteUninstaller "$INSTDIR\\uninstall.exe"', "SectionEnd",
        "Section Uninstall", "  !insertmacro NSIS_HOOK_POSTUNINSTALL", "SectionEnd", "")), encoding="utf-8-sig")

    result = subprocess.run([str(MAKENSIS), "/V2", str(script)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "compile-only.exe").is_file()  # compiled only; never run
