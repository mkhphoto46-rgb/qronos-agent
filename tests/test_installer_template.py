"""The pinned Qronos installer template (desktop/src-tauri/installer-template), proven on the FULL template.

Source checks: exact upstream bytes and licences, the reviewed diff, renderer fidelity against Tauri's own output.
Native checks compile the whole template (tests/fixtures/nsis_full_template.py: real sections, page functions and
hooks; registry, shortcuts, app start and app kill made inert, every transformation count recorded) and run it
silently as the current user, only inside the test's tmp folder:
F2 the reinstall page never starts a previous uninstaller (a planted marker program stands in for it);
F3 a real failure at each material step stops the installation (it never ends successfully);
F5 an installed probe uninstalls its listed files and its registration;
F7 nothing is stopped, deleted or registered unless the folder and its list are Qronos's own;
F8 the uninstaller removes only what the owned list names.
No real installer, uninstaller, app, registry, UAC, SCM or WebView2 installer is involved.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.fixtures.nsis_full_template import MAKENSIS
from tests.fixtures.nsis_template import context_from_generated, normalise, render

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "desktop" / "src-tauri" / "installer-template"
UPSTREAM_SHA256 = "20f4ecc730defb71f1342eaeaec4021df13be3d843abba0effe88ea5835fa079"
LICENCES = {"License_MIT.md": "86fc515b82835f7cb6ad400cc64a9d3380d22d55b593e8c34f51efee9f6a250b",
            "License_Apache.md": "a0746c84c2a9663f57491dda8a619cc64152a8447889c0e17aa2f3e592e0097e"}
IB1 = ROOT.parent / "ib1" / "release" / "nsis" / "x64" / "installer.nsi"  # the 10-08 packaging build (not in Git)
VERSION = "0.1.0"
COMPILES: list = []


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def template_diff() -> str:
    upstream = (TEMPLATE_DIR / "upstream" / "installer.nsi").read_text(encoding="utf-8").splitlines(keepends=True)
    ours = (TEMPLATE_DIR / "installer.nsi").read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(difflib.unified_diff(upstream, ours, "upstream/installer.nsi", "installer.nsi", n=3))


# ------------------------------------------------------------------------------- source and provenance


def test_the_upstream_copy_and_licences_are_byte_exact_and_the_shipped_diff_is_the_reviewed_one():
    assert sha256(TEMPLATE_DIR / "upstream" / "installer.nsi") == UPSTREAM_SHA256
    for name, digest in LICENCES.items():
        assert sha256(TEMPLATE_DIR / name) == digest
    assert (TEMPLATE_DIR / "qronos-template.diff").read_text(encoding="utf-8") == template_diff()
    provenance = (TEMPLATE_DIR / "PROVENANCE.md").read_text(encoding="utf-8")
    for fact in ("8909f221d1515955fc843808032bdc5d62209c96", "tauri-bundler-v2.9.4", UPSTREAM_SHA256,
                 "Apache-2.0 OR MIT", "crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi"):
        assert fact in provenance
    config = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    assert config["bundle"]["windows"]["nsis"]["template"] == "installer-template/installer.nsi"


def _section(text: str, name: str) -> str:
    body = text[text.index(f"\nSection {name}\n"):]
    return body[:body.index("\nSectionEnd")]


def _function(text: str, name: str) -> str:
    body = text[text.index(f"\nFunction {name}\n"):]
    return body[:body.index("\nFunctionEnd")]


def test_the_template_checks_before_setoutpath_and_never_runs_a_previous_uninstaller():
    ours = (TEMPLATE_DIR / "installer.nsi").read_text(encoding="utf-8")
    install = _section(ours, "Install")
    statements = [line.strip() for line in install.splitlines()[2:] if line.strip() and not line.strip().startswith(";")]
    assert statements[:3] == ["!insertmacro NSIS_HOOK_PREINSTALL", "ClearErrors", "SetOutPath $INSTDIR"]
    leave = _function(ours, "PageLeaveReinstall")
    assert "ExecWait" not in leave and "UninstallString" not in leave
    assert "ExecWait" not in _function(ours, "PageReinstall")
    assert "!ifndef QRONOS_TEMPLATE_HOOKS" in ours
    complete = install.index("!insertmacro QRONOS_INSTALL_COMPLETE")
    assert complete < install.index("!insertmacro NSIS_HOOK_POSTINSTALL")
    # The stock template is the stale order this replaces.
    upstream = (TEMPLATE_DIR / "upstream" / "installer.nsi").read_text(encoding="utf-8")
    stock = _section(upstream, "Install")
    assert stock.index("SetOutPath $INSTDIR") < stock.index("NSIS_HOOK_PREINSTALL")
    assert "ExecWait '$R1' $0" in _function(upstream, "PageLeaveReinstall")


@pytest.mark.skipif(not IB1.is_file(), reason="no real Tauri-generated installer.nsi (work/ib1) on this machine")
def test_the_fixture_renderer_reproduces_taurus_real_output_exactly():
    upstream = (TEMPLATE_DIR / "upstream" / "installer.nsi").read_text(encoding="utf-8")
    generated = IB1.read_text(encoding="utf-8")
    assert render(upstream, context_from_generated(upstream, generated)) == normalise(generated)


def test_the_committed_template_renders_the_pinned_registry_commands():
    from tests.fixtures import nsis_full_template as full

    ours = (TEMPLATE_DIR / "installer.nsi").read_text(encoding="utf-8")
    import re

    context = {name: "" for name in re.findall(r"\{\{(\w+)\}\}", ours)}
    context.update({"compression": "none", "languages": ["English"], "language_files": ["x"],
                    "resources_dirs": ["res"], "resources": {"a": ["", "res\\one.txt"]}, "binaries": {},
                    "file_associations": [], "deep_link_protocols": [], "resources_ancestors": ["res"],
                    "installer_hooks": "w", "version": VERSION})
    script = render(ours, context)
    assert {name: len(pattern.findall(script)) for name, pattern, _ in full._REGISTRY} == full.SCRIPT_REGISTRY


# ------------------------------------------------------------------------------- native full-template probes

native = pytest.mark.skipif(os.name != "nt" or MAKENSIS is None or not MAKENSIS.is_file(),
                            reason="Windows with Tauri's NSIS compiler")


class Env:
    """One test's private world: profile folders."""

    def __init__(self, tmp_path: Path) -> None:
        self.base = tmp_path
        self.profile = {"profile": tmp_path / "profile", "local": tmp_path / "profile" / "AppData" / "Local",
                        "roaming": tmp_path / "profile" / "AppData" / "Roaming",
                        "documents": tmp_path / "profile" / "Documents"}
        for folder in self.profile.values():
            folder.mkdir(parents=True, exist_ok=True)

    def probe(self, name: str, **options):
        from tests.fixtures.nsis_full_template import build_full_probe, seed_registry

        probe = build_full_probe(self.base / name, profile=self.profile, version=VERSION, compile_log=COMPILES,
                                 **options)
        seed_registry(probe, {})  # only the WebView2 version; F2 re-seeds with a planted previous installation
        return probe


def run(probe, *arguments: str, stdin=subprocess.DEVNULL, timeout: float = 120) -> int:
    process = subprocess.Popen([str(probe.exe), *arguments, f"/D={probe.instdir}"], stdin=stdin)
    try:
        return process.wait(timeout=timeout)
    finally:
        if process.poll() is None:
            process.kill()  # this test's own probe, by its own handle
            process.wait(30)


def seeded_only(probe) -> bool:
    """The fake registry holds only what the test seeded (the WebView2 version): the installer wrote nothing."""
    text = probe.registry_text()
    return "[HKCU" not in text


# ------------------------------------------------------------------------------- F2 reinstall page


def _marker_program(folder: Path) -> tuple[Path, Path]:
    """A planted stand-in for a previous uninstaller: a tiny silent program that only writes a marker file."""
    folder.mkdir(parents=True)
    marker, exe, script = folder / "old-uninstaller-ran.txt", folder / "old-uninstaller.exe", folder / "marker.nsi"
    script.write_text("\n".join(("Unicode true", 'Name "marker"', f'OutFile "{exe}"', "RequestExecutionLevel user",
                                 "SilentInstall silent", "Section", f'  FileOpen $0 "{marker}" w',
                                 '  FileWrite $0 "ran: $CMDLINE"', "  FileClose $0", "SectionEnd", "")),
                      encoding="utf-8")
    compiler = subprocess.Popen([str(MAKENSIS), "/V1", str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    COMPILES.append({"pid": compiler.pid, "startedAt": time.time(), "script": str(script)})
    output, _ = compiler.communicate(timeout=120)
    assert compiler.returncode == 0, output
    return exe, marker


REINSTALL_CASES = {
    "same_version_uninstall_choice": ({"r0": "0", "wix": "0", "passive": "0"}, "refused"),
    "wix_registration": ({"r0": "1", "wix": "1", "passive": "0"}, "refused"),
    "passive_same_version": ({"r0": "0", "wix": "0", "passive": "1"}, "in_place"),
    "upgrade": ({"r0": "1", "wix": "0", "passive": "0"}, "in_place"),
    "downgrade": ({"r0": "-1", "wix": "0", "passive": "0"}, "in_place"),
}


@native
@pytest.mark.parametrize("case", sorted(REINSTALL_CASES))
def test_f2_the_reinstall_page_never_starts_a_previous_uninstaller(tmp_path, case):
    from tests.fixtures.nsis_full_template import UNINSTKEY, seed_registry

    values, expected = REINSTALL_CASES[case]
    env = Env(tmp_path)
    old, marker = _marker_program(tmp_path / "old")  # a path without spaces: the registry value is unquoted
    probe = env.probe("reinstall", reinstall=values)
    seed_registry(probe, {UNINSTKEY: {"UninstallString": str(old), "DisplayVersion": VERSION},
                          "HKLM\\QPROBE\\WixKey": {"UninstallString": str(old)}})
    code = run(probe, "/S")
    log = probe.read_log()
    assert "reinstall-page called" in log
    assert not marker.exists(), "a previous uninstaller was started"
    if expected == "refused":
        assert "reinstall-page returned" not in log and not probe.instdir.exists()
    else:
        assert code == 0 and "reinstall-page returned" in log and (probe.instdir / "qronos-probe.exe").is_file()


# ------------------------------------------------------------------------------- F3 a failed step stops the install

def _prior_install(folder: Path, files: list[str]) -> None:
    """A previous Qronos installation's folder: its manifest lists only `files` (forward slashes)."""
    from build_support import install_manifest

    folder.mkdir(parents=True)
    (folder / install_manifest.FILE_NAME).write_bytes(install_manifest.render(files))


def _lock(path: Path):
    import _winapi

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"held by the test")
    return _winapi.CreateFile(str(path), 0x80000000, 0, 0, 3, 0x80, 0)  # no sharing: writes and deletes fail


FAILURES = {
    # name: (probe options, expected failed step). Every case injects a REAL failure at the FIRST command of its
    # loop or group while every LATER command of that group would succeed; the installation must still stop.
    "main_program_locked": ({}, "the main program"),
    "first_resource_folder_blocked": ({}, "a resource folder"),
    "first_resource_file_locked": ({}, "a resource file"),
    "first_link_registration_write": ({"deep_link": True, "registry_fault": '"URL Protocol"'}, "a link registration"),
    "uninstaller_is_a_folder": ({}, "the uninstaller"),
    "install_location_write": ({"registry_fault": '${MANUPRODUCTKEY}" "" $INSTDIR'},
                               "the install location registration"),
    "first_apps_and_features_write": ({"registry_fault": '"MainBinaryName" "${MAINBINARYNAME}.exe"'},
                                      "the Apps & features registration"),
    "size_write": ({"registry_fault": '"EstimatedSize"'}, "the size registration"),
    "whole_registry_read_only": ({}, "the install location registration"),
    "start_menu_shortcut_is_a_folder": ({}, "the start menu shortcut"),
    "desktop_shortcut_is_a_folder": ({}, "the desktop shortcut"),
    "owned_payload_locked": ({}, None),
}
# step None: the typed refusal before anything new is written.
REFUSALS = {"owned_payload_locked": "could not be removed"}


@native
@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_f3_a_failure_at_any_material_step_stops_the_installation(tmp_path, failure):
    import _winapi
    from build_support import install_manifest

    options, step = FAILURES[failure]
    env = Env(tmp_path)
    probe = env.probe("fail", **options)
    held = None
    manifest_only = [install_manifest.FILE_NAME]
    if failure == "main_program_locked":
        _prior_install(probe.instdir, manifest_only)
        held = _lock(probe.instdir / "qronos-probe.exe")
    elif failure == "first_resource_folder_blocked":
        _prior_install(probe.instdir, manifest_only)
        (probe.instdir / "res").write_text("an unlisted FILE where the first resource folder goes", encoding="utf-8")
    elif failure == "first_resource_file_locked":
        _prior_install(probe.instdir, manifest_only)
        held = _lock(probe.instdir / "res" / "one.txt")  # res2\two.txt (later) would be written fine
    elif failure == "uninstaller_is_a_folder":
        _prior_install(probe.instdir, manifest_only)
        (probe.instdir / "uninstall.exe" / "inside").mkdir(parents=True)
    elif failure == "whole_registry_read_only":
        os.chmod(probe.registry, 0o444)
    elif failure == "start_menu_shortcut_is_a_folder":
        (probe.smprograms / "Qronos Probe.lnk" / "inside").mkdir(parents=True)  # the desktop one (later) works
    elif failure == "desktop_shortcut_is_a_folder":
        (probe.desktop / "Qronos Probe.lnk" / "inside").mkdir(parents=True)
    elif failure == "owned_payload_locked":
        _prior_install(probe.instdir, ["qronos-probe.exe", install_manifest.FILE_NAME])
        held = _lock(probe.instdir / "qronos-probe.exe")
    try:
        code = run(probe, "/S")
    finally:
        if held is not None:
            _winapi.CloseHandle(held)
        for path in (probe.registry, probe.work / "fault-registry.ini"):
            if path.exists():
                os.chmod(path, 0o666)
    assert code != 0, "a failed installation must not end successfully"
    assert "postinstall" not in probe.read_log()
    if step is not None:
        assert probe.read_result() == f"step failed: {step}"
    else:
        assert REFUSALS[failure] in probe.read_result(), probe.read_result()


# ------------------------------------------------------------------------------- F5 the uninstaller


@native
def test_f5_an_installed_probe_uninstalls_its_listed_files_and_registration(tmp_path):
    env = Env(tmp_path)
    probe = env.probe("uninstall")
    assert run(probe, "/S") == 0
    uninstaller = probe.instdir / "uninstall.exe"
    assert uninstaller.is_file() and "[HKCU" in probe.registry_text()

    def uninstall() -> int:
        process = subprocess.Popen([str(uninstaller), "/S", f"_?={probe.instdir}"], stdin=subprocess.DEVNULL)
        try:
            return process.wait(timeout=120)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(30)

    assert uninstall() == 0
    # Removed; uninstall.exe itself stays while it runs in place (_?=).
    assert not (probe.instdir / "qronos-probe.exe").exists() and not (probe.instdir / "res").exists()
    assert "[HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall" not in probe.registry_text()


# ------------------------------------------------------------------------------- F7 nothing is deleted unless owned

DELETION_CASES = ["protected_root", "protected_root_with_a_list", "protected_ancestor_alias", "nested_payload_junction",
                  "missing_manifest_with_sentinels", "malformed_manifest_with_sentinels"]


def _snapshot(*roots: Path) -> dict[str, bytes | None]:
    found = {}
    for root in roots:
        if root.exists():
            found[str(root)] = None
            for path in root.rglob("*"):
                found[str(path)] = path.read_bytes() if path.is_file() else None
    return found


def _deletion_case(env: Env, case: str) -> tuple[Path, list[Path]]:
    """The folder the installer or uninstaller is pointed at, and every root whose content must stay untouched."""
    import _winapi
    from build_support import install_manifest

    local = env.profile["local"]
    data_folder = local / "Qronos"  # a protected Qronos user-data folder (install-root I6-I9)
    data_folder.mkdir(parents=True, exist_ok=True)
    (data_folder / "settings.json").write_text('{"sentinel": true}', encoding="utf-8")
    outside = env.base / "outside"
    outside.mkdir()
    (outside / "one.txt").write_text("foreign file behind a junction", encoding="utf-8")
    apps = env.base / "apps-target"
    apps.mkdir()
    if case == "protected_root":
        return data_folder, [data_folder, outside, apps]
    if case == "protected_root_with_a_list":
        # A well-formed Qronos list inside the user-data folder: the list alone never makes a folder Qronos's.
        (data_folder / install_manifest.FILE_NAME).write_bytes(
            install_manifest.render(["settings.json", install_manifest.FILE_NAME]))
        return data_folder, [data_folder, outside, apps]
    if case == "protected_ancestor_alias":
        _winapi.CreateJunction(str(local), str(apps / "alias"))  # apps-target\alias -> ...\AppData\Local
        return apps / "alias" / "Qronos", [data_folder, outside]
    target = apps / "QronosProbe"
    target.mkdir()
    (target / "qronos-probe.exe").write_bytes(b"MZ foreign or previous program")
    (target / "keep.txt").write_text("a foreign unlisted file", encoding="utf-8")
    if case == "nested_payload_junction":
        (target / install_manifest.FILE_NAME).write_bytes(
            install_manifest.render(["qronos-probe.exe", "res/one.txt", install_manifest.FILE_NAME]))
        _winapi.CreateJunction(str(outside), str(target / "res"))  # a listed folder is a junction elsewhere
    elif case == "malformed_manifest_with_sentinels":
        (target / install_manifest.FILE_NAME).write_bytes("NOT-A-QRONOS-LIST\r\nF keep.txt\r\n".encode("utf-16-le"))
    return target, [target, outside, data_folder]


def _remove_junctions(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: -len(p.parts)) if root.exists() else []:
        if path.is_junction():
            os.rmdir(path)  # the junction itself only, never its target


@native
@pytest.mark.parametrize("side", ["install", "uninstall"])
@pytest.mark.parametrize("case", DELETION_CASES)
def test_f7_nothing_is_stopped_deleted_or_registered_unless_the_folder_and_its_list_are_qronos_own(tmp_path, side,
                                                                                                   case):
    env = Env(tmp_path)
    probe = env.probe("deletion")
    if side == "uninstall":
        assert run(probe, "/S") == 0  # a normal installation elsewhere provides the uninstaller
        program = [str(probe.instdir / "uninstall.exe"), "/S"]
    else:
        program = [str(probe.exe), "/S"]
    target, roots = _deletion_case(env, case)
    try:
        before = _snapshot(*roots, probe.instdir, probe.smprograms, probe.desktop)
        registry, log = probe.registry_text(), probe.read_log()
        argument = f"_?={target}" if side == "uninstall" else f"/D={target}"
        process = subprocess.Popen([*program, argument], stdin=subprocess.DEVNULL)
        try:
            code = process.wait(timeout=120)
        finally:
            if process.poll() is None:
                process.kill()  # this test's own probe, by its own handle
                process.wait(30)
        assert code != 0, f"{side} into {case} was not refused: {probe.read_result()!r}"
        assert _snapshot(*roots, probe.instdir, probe.smprograms, probe.desktop) == before, "something was changed"
        assert probe.registry_text() == registry, "the (fake) registry was changed"
        assert probe.read_log() == log, "the app was checked/stopped or the install went on after the refusal"
    finally:
        _remove_junctions(env.base / "apps-target")


# ------------------------------------------------------------------------------- F8 only listed files go


@native
def test_f8_the_uninstaller_removes_only_what_the_owned_list_names(tmp_path):
    """A genuine Qronos folder: listed files and folders go, unlisted ones stay (the stock template deleted the
    main program, every resource and the resource folders by name, whatever the list said)."""
    from build_support import install_manifest

    env = Env(tmp_path)
    probe = env.probe("listed")
    assert run(probe, "/S") == 0
    # The list no longer names the main program; foreign files sit next to listed ones.
    (probe.instdir / install_manifest.FILE_NAME).write_bytes(install_manifest.render(
        ["uninstall.exe", "res/one.txt", "res2/two.txt", install_manifest.FILE_NAME]))
    (probe.instdir / "keep.txt").write_text("foreign", encoding="utf-8")
    (probe.instdir / "res" / "mine.txt").write_text("foreign, inside a listed folder", encoding="utf-8")
    process = subprocess.Popen([str(probe.instdir / "uninstall.exe"), "/S", f"_?={probe.instdir}"],
                               stdin=subprocess.DEVNULL)
    try:
        assert process.wait(timeout=120) == 0, probe.read_result()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(30)
    assert not (probe.instdir / "res" / "one.txt").exists() and not (probe.instdir / "res2").exists()
    assert (probe.instdir / "qronos-probe.exe").is_file(), "an unlisted program was deleted"
    assert (probe.instdir / "keep.txt").is_file() and (probe.instdir / "res" / "mine.txt").is_file()
    assert not (probe.instdir / install_manifest.FILE_NAME).exists()


@native
@pytest.mark.parametrize("name", ["..\\outside-sentinel.txt", "*.txt"])
def test_f8_a_program_name_read_from_the_registry_is_never_deleted(tmp_path, name):
    from build_support import install_manifest
    from tests.fixtures.nsis_full_template import UNINSTKEY, seed_registry

    env = Env(tmp_path)
    probe = env.probe("registry-name")
    seed_registry(probe, {UNINSTKEY: {"MainBinaryName": name}})
    sentinel = probe.instdir.parent / "outside-sentinel.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("not Qronos's", encoding="utf-8")
    assert run(probe, "/S") == 0, probe.read_result()
    assert sentinel.read_text(encoding="utf-8") == "not Qronos's"
    assert (probe.instdir / install_manifest.FILE_NAME).is_file()  # "*.txt" would have matched the list
    assert (probe.instdir / "res" / "one.txt").is_file()


def _uninstall_into(probe, target: Path) -> int:
    process = subprocess.Popen([str(probe.instdir / "uninstall.exe"), "/S", f"_?={target}"], stdin=subprocess.DEVNULL)
    try:
        return process.wait(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()  # this test's own probe, by its own handle
            process.wait(30)


@native
def test_f8_a_file_named_uninstall_exe_is_deleted_only_when_the_owned_list_names_it(tmp_path):
    """Root round 12: the fixed-name uninstaller deletion needs list membership, not just the name."""
    from build_support import install_manifest

    env = Env(tmp_path)
    probe = env.probe("unlisted-uninstaller")
    assert run(probe, "/S") == 0
    target = env.base / "apps-target" / "QronosProbe"
    target.mkdir(parents=True)
    (target / "listed.txt").write_text("listed", encoding="utf-8")
    (target / install_manifest.FILE_NAME).write_bytes(install_manifest.render(["listed.txt",
                                                                               install_manifest.FILE_NAME]))
    (target / "uninstall.exe").write_bytes(b"a foreign file the list does not name")
    assert _uninstall_into(probe, target) == 0, probe.read_result()
    assert not (target / "listed.txt").exists()
    assert (target / "uninstall.exe").read_bytes() == b"a foreign file the list does not name"


@native
def test_f8_an_uninstall_without_a_file_list_is_refused_even_for_an_empty_folder(tmp_path):
    env = Env(tmp_path)
    probe = env.probe("no-list")
    assert run(probe, "/S") == 0
    target = env.base / "apps-target" / "Empty"
    target.mkdir(parents=True)
    registry, log = probe.registry_text(), probe.read_log()
    assert _uninstall_into(probe, target) != 0
    assert "no Qronos file list" in probe.read_result(), probe.read_result()
    assert target.is_dir() and probe.registry_text() == registry and probe.read_log() == log


def test_compiler_identities_were_recorded():
    assert all({"pid", "startedAt", "script"} <= set(entry) for entry in COMPILES)


# ------------------------------------------------------------------------------- the build wrapper's checks


def _minimal_context(template: str, wrapper: str = "C:/build/qronos-installer-hooks.nsh") -> dict:
    import re

    context = {name: "" for name in re.findall(r"\{\{(\w+)\}\}", template)}
    context.update({"compression": "lzma", "languages": ["English"], "language_files": [], "resources_dirs": ["a"],
                    "resources": {"C:/src/x": ["", "a/x"]}, "binaries": {}, "file_associations": [],
                    "deep_link_protocols": [], "resources_ancestors": ["a"], "installer_hooks": wrapper,
                    "version": VERSION, "product_name": "qronos-desktop"})
    return context


def test_the_build_verifies_the_template_source_and_refuses_any_unreviewed_change(tmp_path):
    import shutil

    from build_support import desktop_prerequisites as prerequisites

    prerequisites.verify_template_source()
    copy = tmp_path / "installer-template"
    shutil.copytree(TEMPLATE_DIR, copy)
    text = (copy / "installer.nsi").read_text(encoding="utf-8")
    (copy / "installer.nsi").write_text(text.replace("  SetOutPath $INSTDIR\n", "  SetOutPath $INSTDIR \n", 1),
                                        encoding="utf-8")
    with pytest.raises(prerequisites.PrerequisiteError, match="reviewed qronos-template.diff"):
        prerequisites.verify_template_source(copy)
    shutil.copyfile(TEMPLATE_DIR / "installer.nsi", copy / "installer.nsi")
    (copy / "License_MIT.md").write_text("MIT", encoding="utf-8")
    with pytest.raises(prerequisites.PrerequisiteError, match="licence"):
        prerequisites.verify_template_source(copy)


def test_the_build_checks_the_real_rendered_order_and_refuses_the_stale_one():
    from build_support import desktop_prerequisites as prerequisites

    ours = (TEMPLATE_DIR / "installer.nsi").read_text(encoding="utf-8")
    upstream = (TEMPLATE_DIR / "upstream" / "installer.nsi").read_text(encoding="utf-8")
    good = render(ours, _minimal_context(ours))
    prerequisites.verify_generated_order(good)
    prerequisites.verify_generated_order("\ufeff" + good.replace("\n", "\r\n"))  # as Tauri writes it
    with pytest.raises(prerequisites.PrerequisiteError):
        prerequisites.verify_generated_order(render(upstream, _minimal_context(upstream)))  # the stock order
    broken = {
        "setoutpath_first": good.replace("  !insertmacro NSIS_HOOK_PREINSTALL\n", "  SetOutPath $INSTDIR\n"
                                         "  !insertmacro NSIS_HOOK_PREINSTALL\n", 1),
        "step_dropped": good.replace('  !insertmacro QRONOS_STEP "the uninstaller"\n', "", 1),
        "complete_moved": good.replace("  !insertmacro QRONOS_INSTALL_COMPLETE\n", "", 1),
        "old_uninstaller": good.replace("  reinst_uninstall:\n", "  reinst_uninstall:\n    ExecWait '$R1' $0\n", 1),
        "clear_before_step": good.replace(
            '  WriteRegStr SHCTX "${UNINSTKEY}" "DisplayName" "${PRODUCTNAME}"\n',
            '  WriteRegStr SHCTX "${UNINSTKEY}" "DisplayName" "${PRODUCTNAME}"\n  ClearErrors\n', 1),
        "write_step_dropped": good.replace(
            '  WriteRegStr SHCTX "${UNINSTKEY}" "Publisher" "${MANUFACTURER}"\n'
            '  !insertmacro QRONOS_STEP "the Apps & features registration"\n',
            '  WriteRegStr SHCTX "${UNINSTKEY}" "Publisher" "${MANUFACTURER}"\n', 1),
        "loop_step_dropped": good.replace('    !insertmacro QRONOS_STEP "a resource file"\n', "", 1),
        "both_mode_step_dropped": good.replace('    !insertmacro QRONOS_STEP "the install mode registration"\n', "", 1),
        "registry_named_delete": good.replace("  ; Save current MAINBINARYNAME for future updates\n",
                                              '  Delete "$INSTDIR\\$OldMainBinaryName"\n'
                                              "  ; Save current MAINBINARYNAME for future updates\n", 1),
        "stock_uninstall_delete": good.replace("  ; Delete uninstaller\n",
                                               '  Delete "$INSTDIR\\${MAINBINARYNAME}.exe"\n  ; Delete uninstaller\n', 1),
        "unguarded_uninstaller_delete": good.replace('  ${If} $QronosUninstallerListed == "1"\n'
                                                     '    Delete "$INSTDIR\\uninstall.exe"\n',
                                                     '  ${If} 1 == 1\n    Delete "$INSTDIR\\uninstall.exe"\n', 1),
        "stock_uninstall_rmdir": good.replace("  ; Delete uninstaller\n", '  RMDir "$INSTDIR"\n  ; Delete uninstaller\n',
                                              1),
        "recursive_removal": good.replace("    ; Qronos: no recursive folder removal",
                                          '    RmDir /r "$APPDATA\\${BUNDLEID}"\n    ; Qronos: no recursive folder removal', 1),
        "app_data_checkbox": good.replace("Function un.ConfirmShow", "!define MUI_PAGE_CUSTOMFUNCTION_SHOW "
                                          "un.ConfirmShow\nFunction un.ConfirmShow", 1),
        "probe_define": good.replace("!define MANUFACTURER", '!define QRONOS_PROBE_RESULT "C:/x"\n'
                                     "!define MANUFACTURER", 1),
    }
    for name, script in broken.items():
        assert script != good, name
        with pytest.raises(prerequisites.PrerequisiteError):
            prerequisites.verify_generated_order(script)


@pytest.mark.skipif(not IB1.is_file(), reason="no real Tauri-generated installer.nsi (work/ib1) on this machine")
def test_the_real_10_08_installer_script_is_refused_and_its_qronos_render_accepted():
    from build_support import desktop_prerequisites as prerequisites

    generated = IB1.read_text(encoding="utf-8")
    with pytest.raises(prerequisites.PrerequisiteError):
        prerequisites.verify_generated_order(generated)  # built from the stock template
    upstream = (TEMPLATE_DIR / "upstream" / "installer.nsi").read_text(encoding="utf-8")
    ours = (TEMPLATE_DIR / "installer.nsi").read_text(encoding="utf-8")
    prerequisites.verify_generated_order(render(ours, context_from_generated(upstream, generated)))


def test_the_production_hook_wrapper_carries_no_probe_value(tmp_path):
    from build_support import desktop_prerequisites as prerequisites

    wrapper = prerequisites.hook_wrapper(tmp_path / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
                                         prerequisites.load_pin(),
                                         vcredist=(tmp_path / "vc_redist.x64.exe", prerequisites.load_vcredist_pin()))
    prerequisites.verify_hook_wrapper(wrapper)
    with pytest.raises(prerequisites.PrerequisiteError):
        prerequisites.verify_hook_wrapper(wrapper + '!define QRONOS_PROBE_RESULT "C:/x"\n')
