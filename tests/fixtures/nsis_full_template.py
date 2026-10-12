"""Compile the FULL Qronos installer template into an inert, silent probe. Test fixture only.

What is real: Tauri's template as Qronos ships it (installer-template/installer.nsi, rendered by
tests/fixtures/nsis_template.py, whose output equals Tauri's own for the upstream template), every Qronos hook
(installer-hooks/*.nsh) and Tauri's NSIS 3.11 compiler. The probe runs the real sections, pages' functions and
hooks, in silent mode, as the current user (RequestExecutionLevel user from installMode currentUser).

What is made inert (every replacement below is applied an exact, asserted number of times; TRANSFORMS records it):
- REGISTRY: SHCTX -> HKCU (installMode currentUser selects the current user, as Windows does); Write/ReadRegStr, WriteRegDWORD, DeleteRegKey, DeleteRegValue -> Write/ReadINIStr, DeleteINISec,
  DeleteINIStr on <work>/fake-registry.ini ("<ROOT>\\<key>" sections); EnumRegKey -> no keys. Applied to the
  rendered script AND to the copied hooks; afterwards no registry command remains (asserted).
- FOLDERS: $SMPROGRAMS, $DESKTOP, $APPDATA, $LOCALAPPDATA in the rendered script -> folders in <work>.
- APP: nsis_tauri_utils::RunAsUser (never start the app) -> DetailPrint; MUI_UNGETLANGUAGE (a registry read)
  removed.
- SUPPORT FILES: utils.nsh, FileAssociation.nsh and the language file are fixture stubs: CheckIfAppIsRunning
  kills nothing (it logs "app-check", may start one owned grandchild and may wait for a go-file); no COM, pin or
  shortcut retargeting.
- SEAMS (inserted lines): QPROBE_REINSTALL_SECTION before "Section Install" (calls the real PageLeaveReinstall for
  the reinstall-page cases) and QPROBE_AFTER_POSTINSTALL right after NSIS_HOOK_POSTINSTALL (logs, may hold).
- PRIVATE VALUES in the hook wrapper only: QRONOS_UPDATE_FENCE_NAME (unique), QRONOS_PROBE_DATA_ROOT,
  QRONOS_PROBE_RESULT and the install-root QRONOS_PROBE_* folders, all inside the test's tmp folder.
The WebView2 runtime is reported present through the fake registry, so the embedded installer never runs.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from build_support import install_manifest
from tests.fixtures.nsis_fence_probe import MAKENSIS
from tests.fixtures.nsis_template import render

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE = REPO / "desktop" / "src-tauri" / "installer-template" / "installer.nsi"
HOOKS = REPO / "desktop" / "src-tauri" / "installer-hooks"
PLUGINS = MAKENSIS.parent / "Plugins" / "x86-unicode" / "additional" if MAKENSIS else None
PRODUCT = "Qronos Probe"
BINARY = "qronos-probe"
WEBVIEW2_KEY = "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
VCREDIST_KEY = "HKLM\\SOFTWARE\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\x64"
UNINSTKEY = f"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{PRODUCT}"

_REGISTRY = [
    ("WriteRegStr", re.compile(r'\bWriteRegStr (SHCTX|HKCU|HKLM) "'), r'WriteINIStr "${QPROBE_REG}" "\1\\'),
    ("WriteRegDWORD", re.compile(r'\bWriteRegDWORD (SHCTX|HKCU|HKLM) "'), r'WriteINIStr "${QPROBE_REG}" "\1\\'),
    ("ReadRegStr", re.compile(r'\bReadRegStr (\$\w+) (SHCTX|HKCU|HKLM) "'), r'ReadINIStr \1 "${QPROBE_REG}" "\2\\'),
    ("ReadRegDWORD", re.compile(r'\bReadRegDWORD (\$\w+) (SHCTX|HKCU|HKLM) "'), r'ReadINIStr \1 "${QPROBE_REG}" "\2\\'),
    ("DeleteRegKey", re.compile(r'\bDeleteRegKey (?:/ifempty )?(SHCTX|HKCU|HKLM) "'),
     r'DeleteINISec "${QPROBE_REG}" "\1\\'),
    ("DeleteRegValue", re.compile(r'\bDeleteRegValue (SHCTX|HKCU|HKLM) "'), r'DeleteINIStr "${QPROBE_REG}" "\1\\'),
    ("EnumRegKey", re.compile(r'\bEnumRegKey (\$\w+) \w+ "[^"]*" \$\w+'), r'StrCpy \1 ""'),
]
_ANY_REGISTRY = re.compile(r"\b(?:Write|Read|Delete|Enum)Reg\w*\b")


def _sub(text: str, pattern: re.Pattern | str, replacement: str, expected: int | None, name: str,
         log: dict[str, int]) -> str:
    if isinstance(pattern, str):
        count = text.count(pattern)
        result = text.replace(pattern, replacement)
    else:
        result, count = pattern.subn(replacement, text)
    if expected is not None and count != expected:
        raise AssertionError(f"transformation {name}: expected {expected} replacements, found {count}")
    log[name] = log.get(name, 0) + count
    return result


def _inert_registry(text: str, counts: dict[str, int] | None, log: dict[str, int], where: str) -> str:
    """counts None: record what was replaced (the script; a mutant template may differ); else exact."""
    for name, pattern, replacement in _REGISTRY:
        text = _sub(text, pattern, replacement, None if counts is None else counts.get(name, 0),
                    f"{where}:{name}", log)
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith((";", "#")))
    left = [m.group(0) for m in _ANY_REGISTRY.finditer(code)]
    if left:
        raise AssertionError(f"{where}: registry commands left after the transformation: {left}")
    return text


# The committed Qronos template renders exactly these (fixture context: no deep links or file associations);
# tests/test_installer_template.py pins them. Probes record the actual counts in transforms.json.
SCRIPT_REGISTRY = {"WriteRegStr": 12, "WriteRegDWORD": 3, "ReadRegStr": 15, "ReadRegDWORD": 0, "DeleteRegKey": 7,
                   "DeleteRegValue": 2, "EnumRegKey": 1}
HOOK_REGISTRY = {"webview2-offline.nsh": {"ReadRegStr": 3},
                 # the update transaction's metadata snapshot and restore (macros: each command once in the text)
                 "qronos-update-transaction.nsh": {"ReadRegStr": 1, "ReadRegDWORD": 1, "WriteRegStr": 1,
                                                   "WriteRegDWORD": 1, "DeleteRegValue": 2}}
# Folder variables the hooks name (the update transaction's shortcut paths), replaced in the copied hooks too, so a
# probe never reads or writes the real Start menu or desktop.
HOOK_FOLDERS = {"webview2-offline.nsh": {"$SMPROGRAMS": 1, "$DESKTOP": 1}}

STUB_UTILS = r"""; Fixture stub of Tauri's utils.nsh (nothing is killed, pinned or retargeted).
!macro SetContext
  SetShellVarContext current
!macroend
!macro CheckIfAppIsRunning executableName productName
  !insertmacro QPROBE_APP_CHECK
!macroend
!macro SetLnkAppUserModelId shortcut
!macroend
!macro DeleteAppUserModelId
!macroend
!macro UnpinShortcut shortcut
!macroend
!macro SetShortcutTarget shortcut target
!macroend
!macro IsShortcutTarget shortcut target
  Push 0
!macroend
"""

STUB_ASSOCIATIONS = """; Fixture stub of Tauri's FileAssociation.nsh (no association is configured or written).
!macro APP_ASSOCIATE EXT FILECLASS DESCRIPTION ICON COMMANDTEXT COMMAND
!macroend
!macro APP_UNASSOCIATE EXT FILECLASS
!macroend
"""


@dataclass
class FullProbe:
    exe: Path
    work: Path
    instdir: Path
    data_root: Path
    fence: str
    registry: Path
    smprograms: Path
    desktop: Path
    transforms: dict[str, int] = field(default_factory=dict)

    @property
    def result(self) -> Path:
        return self.work / "result.txt"

    @property
    def log(self) -> Path:
        return self.work / "probe-log.txt"

    @property
    def go(self) -> Path:
        return self.work / "go"

    @property
    def go2(self) -> Path:
        return self.work / "go2"

    def read_result(self) -> str:
        if not self.result.exists():
            return ""
        raw = self.result.read_bytes()
        return (raw.decode("utf-16") if raw[:2] == b"\xff\xfe" else raw.decode("mbcs", "replace")).lstrip("﻿")

    def read_log(self) -> str:
        return self.log.read_text(encoding="mbcs", errors="replace") if self.log.exists() else ""

    def install_tree(self) -> list[str]:
        """Every path the installation could create: install folder, shortcuts, fake registry sections."""
        paths = []
        for root in (self.instdir, self.smprograms, self.desktop):
            if root.exists():
                paths.append(str(root))
                paths += sorted(str(p) for p in root.rglob("*"))
        return paths

    def registry_text(self) -> str:
        return self.registry.read_text(encoding="utf-16") if self.registry.exists() else ""


def payload_manifest_files() -> list[str]:
    return [f"{BINARY}.exe", "uninstall.exe", "res/one.txt", "res2/two.txt", install_manifest.FILE_NAME]


def build_full_probe(work: Path, *, fence: str, data_root: Path, profile: dict[str, Path], version: str,
                     template: Path | None = None, instdir: Path | None = None, go: bool = False, go2: bool = False,
                     python: str | None = None, reinstall: dict[str, str] | None = None,
                     compile_log: list | None = None, deep_link: bool = False,
                     registry_fault: str | None = None, extra_defines: dict[str, str] | None = None,
                     owned_manifest_files: list[str] | None = None) -> FullProbe:
    """registry_fault: the ONE registry write whose rendered line contains this text goes to a read-only fake
    registry file (a real WriteINIStr failure); every other write succeeds. deep_link: render one protocol.
    extra_defines: private probe values for the hook wrapper (QRONOS_PROBE_* / QPROBE_* seams only).
    owned_manifest_files: the update transaction's own list (QRONOS_OWNED_MANIFEST) differs from the payload's
    manifest (an incoming-manifest mismatch); by default both are the same file, as build_desktop makes them."""
    if MAKENSIS is None or not MAKENSIS.is_file() or PLUGINS is None or not PLUGINS.is_dir():
        raise FileNotFoundError("Tauri's NSIS compiler (with its plug-ins) is not installed on this machine.")
    work.mkdir(parents=True, exist_ok=False)
    for name in ("payload", "hooks", "fake-start-menu", "fake-desktop", "fake-appdata", "fake-localappdata"):
        (work / name).mkdir()
    transforms: dict[str, int] = {}
    payload = work / "payload"
    (payload / f"{BINARY}.exe").write_bytes(b"MZ fixture main program, never run\n")
    (payload / "one.txt").write_text("fixture resource\n", encoding="utf-8")
    (payload / "two.txt").write_text("second fixture resource\n", encoding="utf-8")
    (payload / install_manifest.FILE_NAME).write_bytes(install_manifest.render(payload_manifest_files()))
    webview2 = payload / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    webview2.write_bytes(b"never run: the runtime is reported present")
    if owned_manifest_files:
        (work / "owned-list-mismatch.txt").write_bytes(install_manifest.render(owned_manifest_files))

    for hook in HOOKS.glob("*.nsh"):
        text = hook.read_text(encoding="utf-8")
        # installMode currentUser: SHCTX is HKCU, in the hooks as in the rendered script (one fake root).
        text = _sub(text, re.compile(r"\bSHCTX\b"), "HKCU", None, f"{hook.name}:SHCTX=HKCU", transforms)
        text = _inert_registry(text, HOOK_REGISTRY.get(hook.name, {}), transforms, hook.name)
        for variable, folder in (("$SMPROGRAMS", "QPROBE_SMPROGRAMS"), ("$DESKTOP", "QPROBE_DESKTOP")):
            # Every occurrence is replaced (recorded in transforms.json); the committed hooks' counts are pinned
            # by HOOK_FOLDERS in the tests, so an older tree under test still builds (pre-repair controls).
            pattern = re.compile(re.escape(variable) + r"(?![A-Za-z])")
            text = _sub(text, pattern, "${" + folder + "}", len(pattern.findall(text)), f"{hook.name}:{variable}",
                        transforms)
        (work / "hooks" / hook.name).write_text(text, encoding="utf-8-sig")
    (work / "utils.nsh").write_text(STUB_UTILS, encoding="utf-8-sig")
    (work / "FileAssociation.nsh").write_text(STUB_ASSOCIATIONS, encoding="utf-8-sig")

    # NSIS takes /D= only unquoted (last argument), so the probe install folder has no spaces.
    instdir = instdir or (work / "apps" / "QronosProbe")
    wrapper = work / "hooks-wrapper.nsh"
    exe = work / "probe-installer.exe"
    language = work / "English.nsh"
    context = {
        "compression": "none", "signed_plugins_path": "", "installer_hooks": str(wrapper),
        "manufacturer": "QronosProbe", "product_name": PRODUCT, "version": version,
        "version_with_build": f"{version}.0", "homepage": "", "install_mode": "currentUser", "license": "",
        "installer_icon": "", "sidebar_image": "", "header_image": "", "uninstaller_icon": "",
        "uninstaller_header_image": "", "main_binary_name": BINARY, "main_binary_path": str(payload / f"{BINARY}.exe"),
        "bundle_id": "com.qronos.probe", "copyright": "", "out_file": str(exe), "arch": "x64",
        "additional_plugins_path": str(PLUGINS), "allow_downgrades": "true", "display_language_selector": "false",
        "install_webview2_mode": "", "webview2_installer_args": "", "webview2_bootstrapper_path": "",
        "webview2_installer_path": "", "minimum_webview2_version": "", "uninstaller_sign_cmd": "",
        "estimated_size": "1", "start_menu_folder": "", "languages": ["English"], "language_files": [str(language)],
        "resources_dirs": ["res", "res2"],
        "resources": {str(payload / "one.txt"): ["", "res\\one.txt"], str(payload / "two.txt"): ["", "res2\\two.txt"],
                      str(payload / install_manifest.FILE_NAME): ["", install_manifest.FILE_NAME]},
        "binaries": {}, "file_associations": [], "deep_link_protocols": ["qronos-probe"] if deep_link else [],
        "resources_ancestors": ["res", "res2"],
    }
    script = render((template or TEMPLATE).read_text(encoding="utf-8"), context)  # TEMPLATE read at call time
    # installMode currentUser: SetContext selects the current user, so SHCTX is HKCU (one fake root, as in Windows).
    script = _sub(script, re.compile(r"\bSHCTX\b"), "HKCU", None, "installer.nsi:SHCTX=HKCU", transforms)
    script = _inert_registry(script, None, transforms, "installer.nsi")
    for variable, folder in (("$SMPROGRAMS", "QPROBE_SMPROGRAMS"), ("$DESKTOP", "QPROBE_DESKTOP"),
                             ("$APPDATA", "QPROBE_APPDATA"), ("$LOCALAPPDATA", "QPROBE_LOCALAPPDATA")):
        script = _sub(script, re.compile(re.escape(variable) + r"(?![A-Za-z])"), "${" + folder + "}",
                      len(re.findall(re.escape(variable) + r"(?![A-Za-z])", script)), f"installer.nsi:{variable}",
                      transforms)
    script = _sub(script, re.compile(r"nsis_tauri_utils::RunAsUser [^\r\n]*"),
                  'DetailPrint "probe: the app is never started"', 2, "installer.nsi:RunAsUser", transforms)
    script = _sub(script, "!insertmacro MUI_UNGETLANGUAGE", "; probe: MUI_UNGETLANGUAGE removed (registry read)", 1,
                  "installer.nsi:MUI_UNGETLANGUAGE", transforms)
    script = _sub(script, "\nSection Install\n", "\n!insertmacro QPROBE_REINSTALL_SECTION\nSection Install\n", 1,
                  "installer.nsi:seam-reinstall", transforms)
    script = _sub(script, "    !insertmacro NSIS_HOOK_POSTINSTALL\n  !endif\n",
                  "    !insertmacro NSIS_HOOK_POSTINSTALL\n  !endif\n  !insertmacro QPROBE_AFTER_POSTINSTALL\n", 1,
                  "installer.nsi:seam-postinstall", transforms)
    if registry_fault is not None:
        lines = script.split("\n")
        hits = [i for i, line in enumerate(lines) if line.lstrip().startswith('WriteINIStr "${QPROBE_REG}"')
                and registry_fault in line]
        if len(hits) != 1:
            raise AssertionError(f"registry fault {registry_fault!r} matches {len(hits)} writes")
        lines[hits[0]] = lines[hits[0]].replace("${QPROBE_REG}", "${QPROBE_FAULT_REG}", 1)
        script = "\n".join(lines)
        transforms["installer.nsi:registry-fault"] = 1
        fault = work / "fault-registry.ini"
        fault.write_text("[fault]\r\n", encoding="utf-16")
        fault.chmod(0o444)  # WriteINIStr fails for real; the test makes it writable again afterwards

    strings = sorted(set(re.findall(r"\$\((\w+)\)", script)) - {"^RTL"})
    language.write_text("\n".join(f'LangString {name} ${{LANG_ENGLISH}} "probe text {name}"' for name in strings)
                        + "\n", encoding="utf-8-sig")
    sleeper = work / "sleeper.py"
    sleeper.write_text("import os,sys,time;open(sys.argv[1]+'.pid','w').write(str(os.getpid()));"
                       "d=time.monotonic()+120\n"
                       "while not os.path.exists(sys.argv[1]) and time.monotonic()<d: time.sleep(0.05)",
                       encoding="utf-8")
    defines = {
        "QRONOS_WEBVIEW2_INSTALLER": str(webview2), "QRONOS_WEBVIEW2_VERSION": "1.2.3.4",
        "QRONOS_VCREDIST_INSTALLER": str(webview2), "QRONOS_VCREDIST_VERSION": "14.44.35211.0",
        "QRONOS_VCREDIST_MAJOR": "14", "QRONOS_VCREDIST_MINOR": "44", "QRONOS_VCREDIST_BUILD": "35211",
        "QRONOS_OWNED_MANIFEST": str(payload / install_manifest.FILE_NAME),
        **({"QRONOS_OWNED_MANIFEST": str(work / "owned-list-mismatch.txt")} if owned_manifest_files else {}),
        "QRONOS_UPDATE_FENCE_NAME": fence, "QRONOS_PROBE_DATA_ROOT": str(data_root),
        "QRONOS_PROBE_RESULT": str(work / "result.txt"),
        "QRONOS_PROBE_PROFILE": str(profile["profile"]), "QRONOS_PROBE_LOCALAPPDATA": str(profile["local"]),
        "QRONOS_PROBE_APPDATA": str(profile["roaming"]), "QRONOS_PROBE_DOCUMENTS": str(profile["documents"]),
        "QPROBE_REG": str(work / "fake-registry.ini"), "QPROBE_LOG": str(work / "probe-log.txt"),
        "QPROBE_SMPROGRAMS": str(work / "fake-start-menu"), "QPROBE_DESKTOP": str(work / "fake-desktop"),
        "QPROBE_APPDATA": str(work / "fake-appdata"), "QPROBE_LOCALAPPDATA": str(work / "fake-localappdata"),
        "QPROBE_SLEEPER": str(sleeper), "QPROBE_FAULT_REG": str(work / "fault-registry.ini"),
    }
    if go:
        defines["QPROBE_GO"] = str(work / "go")
    if go2:
        defines["QPROBE_GO2"] = str(work / "go2")
    if python:
        defines["QPROBE_GRANDCHILD"] = "1"
        defines["QPROBE_PYTHON"] = python
    for name, value in (extra_defines or {}).items():
        assert name.startswith(("QRONOS_PROBE_", "QPROBE_")), name
        defines[name] = value
    if reinstall:
        defines["QPROBE_REINSTALL"] = "1"
        defines.update({f"QPROBE_REINSTALL_{key.upper()}": value for key, value in reinstall.items()})
    for value in defines.values():
        assert '"' not in value and "$" not in value and "\n" not in value, value
    wrapper.write_text("\n".join([f'!define {name} "{value}"' for name, value in defines.items()] + [
        f'!include "{work / "hooks" / "webview2-offline.nsh"}"',
        "Var QProbeChild",
        "Var QProbeAppChecks",
        "!macro QPROBE_LOG TEXT",
        '  FileOpen $R5 "${QPROBE_LOG}" a',
        "  FileSeek $R5 0 END",
        '  FileWrite $R5 "${TEXT}$\\r$\\n"',
        "  FileClose $R5",
        "!macroend",
        "!macro QPROBE_WAIT FILE",
        "  StrCpy $R4 0",
        "  ${Do}",
        '    ${IfThen} ${FileExists} "${FILE}" ${|} ${ExitDo} ${|}',
        "    Sleep 50",
        "    IntOp $R4 $R4 + 50",
        '    ${IfThen} $R4 > 120000 ${|} Abort "probe: no go file in time" ${|}',
        "  ${Loop}",
        "!macroend",
        "!macro QPROBE_APP_CHECK",
        '  !insertmacro QPROBE_LOG "app-check"',
        "  IntOp $QProbeAppChecks $QProbeAppChecks + 1",
        "  !ifdef QPROBE_ABORT_SECOND_APP_CHECK",
        '    ${IfThen} $QProbeAppChecks = 2 ${|} Abort "probe: the person declined to close the app" ${|}',
        "  !endif",
        "  !ifdef QPROBE_GRANDCHILD",
        '    ${If} $QProbeChild != "1"',
        '      StrCpy $QProbeChild "1"',
        "      Exec '\"${QPROBE_PYTHON}\" -B -I \"${QPROBE_SLEEPER}\" \"${QPROBE_GO}.grandchild\"'",
        "    ${EndIf}",
        "  !endif",
        "  !ifdef QPROBE_GO",
        '    !insertmacro QPROBE_WAIT "${QPROBE_GO}"',
        "  !endif",
        "!macroend",
        "!macro QPROBE_AFTER_POSTINSTALL",
        '  !insertmacro QPROBE_LOG "postinstall receipt=[$QronosFenceMessage]"',
        "  !ifdef QPROBE_GO2",
        '    !insertmacro QPROBE_WAIT "${QPROBE_GO2}"',
        "  !endif",
        "!macroend",
        "!macro QPROBE_REINSTALL_SECTION",
        "  !ifdef QPROBE_REINSTALL",
        "    Section QronosProbeReinstallPage",
        '      StrCpy $R0 "${QPROBE_REINSTALL_R0}"',
        '      StrCpy $WixMode "${QPROBE_REINSTALL_WIX}"',
        '      StrCpy $PassiveMode "${QPROBE_REINSTALL_PASSIVE}"',
        '      StrCpy $R6 "QPROBE\\WixKey"',
        '      !insertmacro QPROBE_LOG "reinstall-page called"',
        "      Call PageLeaveReinstall",
        '      !insertmacro QPROBE_LOG "reinstall-page returned"',
        "    SectionEnd",
        "  !endif",
        "!macroend",
        "",
    ]), encoding="utf-8-sig")
    nsi = work / "installer.nsi"
    nsi.write_text(script, encoding="utf-8-sig")
    (work / "transforms.json").write_text(json.dumps(transforms, indent=1, sort_keys=True), encoding="utf-8")
    compiler = subprocess.Popen([str(MAKENSIS), "/V2", "/INPUTCHARSET", "UTF8", str(nsi)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=work)
    identity = {"pid": compiler.pid, "startedAt": time.time(), "script": str(nsi)}
    (work / "compiler.json").write_text(json.dumps(identity), encoding="utf-8")
    if compile_log is not None:
        compile_log.append(identity)
    try:
        output, _ = compiler.communicate(timeout=180)
    finally:
        if compiler.poll() is None:
            compiler.kill()
            compiler.wait(30)
    (work / "compile.log").write_bytes(output)
    if compiler.returncode != 0 or not exe.is_file():
        raise AssertionError("makensis failed:\n" + output.decode("utf-8", "replace")[-5000:])
    return FullProbe(exe, work, instdir, data_root, fence, work / "fake-registry.ini", work / "fake-start-menu",
                     work / "fake-desktop", transforms)


def seed_registry(probe: FullProbe, sections: dict[str, dict[str, str]]) -> None:
    """The fake registry file (UTF-16 INI). WebView2 and the pinned VC++ runtime are always reported present."""
    sections = {WEBVIEW2_KEY: {"pv": "120.0.0.0"},
                VCREDIST_KEY: {"Installed": "1", "Major": "14", "Minor": "44", "Bld": "35211"}, **sections}
    lines = []
    for section, values in sections.items():
        lines.append(f"[{section}]")
        lines += [f"{key}={value}" for key, value in values.items()]
    probe.registry.write_text("\r\n".join(lines) + "\r\n", encoding="utf-16")


def copy_template(destination: Path) -> Path:
    """A disposable copy of the Qronos template for mutants (the committed file is never mutated)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATE, destination)
    return destination
