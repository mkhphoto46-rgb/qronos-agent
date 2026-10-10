"""Compile a throwaway, silent NSIS probe that runs ONLY installer-hooks/qronos-update-fence.nsh. Fixture only.

The probe is not the Qronos installer: it installs nothing, writes no registry value, shows no window
(SilentInstall/SilentUnInstall silent, RequestExecutionLevel user) and touches only the files named below, all
inside the test's own tmp folder. Its fence is a unique PRIVATE mutex name and its data root a tmp folder; the
production name and the user's folders are never used (QRONOS_UPDATE_FENCE_NAME / QRONOS_PROBE_DATA_ROOT).

Section (install):   QRONOS_FENCE_INSTALL -> refuse, or write "fenced" to <work>/state.txt; optionally Exec a
                     grandchild (a base-interpreter sleeper that waits for <go>.grandchild); wait for <go>
                     (bounded); optionally WriteUninstaller; QronosWriteReceipt; result "installed".
Section Uninstall:   un.QronosFenceUninstall -> refuse, or write "fenced" to <work>/un-state.txt; wait for
                     <go>.un (bounded); result "uninstalled".
Every outcome is written to <work>/result.txt ("refused: ..." comes from QRONOS_FENCE_REFUSE).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
FENCE_HOOK = REPO / "desktop" / "src-tauri" / "installer-hooks" / "qronos-update-fence.nsh"
_LOCAL = os.environ.get("LOCALAPPDATA", "")
MAKENSIS = Path(_LOCAL, "tauri", "NSIS", "makensis.exe") if _LOCAL else None


@dataclass
class Probe:
    exe: Path
    work: Path
    fence: str
    data_root: Path

    @property
    def result(self) -> Path:
        return self.work / "result.txt"

    @property
    def state(self) -> Path:
        return self.work / "state.txt"

    @property
    def go(self) -> Path:
        return self.work / "go"

    def read_result(self) -> str:
        if not self.result.exists():
            return ""
        raw = self.result.read_bytes()
        text = raw.decode("utf-16") if raw[:2] == b"\xff\xfe" else raw.decode("mbcs", "replace")
        return text.lstrip("﻿")


def _nsis_string(value: str) -> str:
    assert '"' not in value and "$" not in value and "\n" not in value, value
    return value


def build_probe(work: Path, *, fence: str, data_root: Path, version: str | None, python: str | None = None,
                grandchild: bool = False, uninstaller: bool = False, compile_log: list | None = None,
                complete: bool = True) -> Probe:
    """Write and compile the probe into ``work`` (which must be inside a pytest tmp folder)."""
    if MAKENSIS is None or not MAKENSIS.is_file():
        raise FileNotFoundError("Tauri's NSIS compiler is not installed on this machine.")
    work.mkdir(parents=True, exist_ok=True)
    exe = work / "probe.exe"
    go = work / "go"
    lines = [
        "Unicode true", 'Name "QronosFenceProbe"', f'OutFile "{_nsis_string(str(exe))}"',
        "RequestExecutionLevel user", "SilentInstall silent", "SilentUnInstall silent",
        f'InstallDir "{_nsis_string(str(work / "instdir"))}"',
        f'!define QRONOS_UPDATE_FENCE_NAME "{_nsis_string(fence)}"',
        f'!define QRONOS_PROBE_DATA_ROOT "{_nsis_string(str(data_root))}"',
        f'!define QRONOS_PROBE_RESULT "{_nsis_string(str(work / "result.txt"))}"',
        f'!include "{_nsis_string(str(FENCE_HOOK))}"',
        # As in Tauri's template: VERSION is defined only AFTER the hooks are included (None: not at all).
        *([f'!define VERSION "{_nsis_string(version)}"'] if version is not None else []),
        "Section",
        "  !insertmacro QRONOS_FENCE_INSTALL",
        '  ${If} $QronosFenceMessage != ""',
        '    !insertmacro QRONOS_FENCE_REFUSE "$QronosFenceMessage"',
        "  ${EndIf}",
        "  ClearErrors",  # as the Qronos template does right after NSIS_HOOK_PREINSTALL
        f'  FileOpen $R0 "{_nsis_string(str(work / "state.txt"))}" w',
        '  FileWrite $R0 "fenced update=$QronosFenceUpdate"',
        "  FileClose $R0",
    ]
    if grandchild:
        assert python is not None
        sleeper = ("import os,sys,time;open(sys.argv[1]+'.pid','w').write(str(os.getpid()));d=time.monotonic()+120\n"
                   "while not os.path.exists(sys.argv[1]) and time.monotonic()<d: time.sleep(0.05)")
        (work / "sleeper.py").write_text(sleeper, encoding="utf-8")
        lines.append(f"  Exec '\"{_nsis_string(python)}\" -B -I \"{_nsis_string(str(work / 'sleeper.py'))}\" "
                     f"\"{_nsis_string(str(go))}.grandchild\"'")
    lines += [
        "  StrCpy $R1 0",
        "  wait_go:",
        f'  IfFileExists "{_nsis_string(str(go))}" go_done',
        "  Sleep 50",
        "  IntOp $R1 $R1 + 50",
        "  IntCmp $R1 120000 0 wait_go 0",
        '  !insertmacro QRONOS_FENCE_REFUSE "probe: no go file in time"',
        "  go_done:",
    ]
    if uninstaller:
        lines.append(f'  WriteUninstaller "{_nsis_string(str(work / "uninstall.exe"))}"')
    lines += [
        # As the Qronos template does right before POSTINSTALL (complete=False: a probe that never reaches it).
        *(["  !insertmacro QRONOS_INSTALL_COMPLETE"] if complete else []),
        "  Call QronosWriteReceipt",
        f'  FileOpen $R0 "{_nsis_string(str(work / "result.txt"))}" w',
        '  FileWrite $R0 "installed receipt=[$QronosFenceMessage]"',
        "  FileClose $R0",
        "SectionEnd",
        "Section Uninstall",
        "  Call un.QronosFenceUninstall",
        '  ${If} $QronosFenceMessage != ""',
        '    !insertmacro QRONOS_FENCE_REFUSE "$QronosFenceMessage"',
        "  ${EndIf}",
        f'  FileOpen $R0 "{_nsis_string(str(work / "un-state.txt"))}" w',
        '  FileWrite $R0 "fenced"',
        "  FileClose $R0",
        "  StrCpy $R1 0",
        "  wait_un:",
        f'  IfFileExists "{_nsis_string(str(go))}.un" un_done',
        "  Sleep 50",
        "  IntOp $R1 $R1 + 50",
        "  IntCmp $R1 120000 0 wait_un 0",
        '  !insertmacro QRONOS_FENCE_REFUSE "probe: no uninstall go file in time"',
        "  un_done:",
        f'  FileOpen $R0 "{_nsis_string(str(work / "result.txt"))}" w',
        '  FileWrite $R0 "uninstalled"',
        "  FileClose $R0",
        "SectionEnd",
        "",
    ]
    script = work / "probe.nsi"
    script.write_text("\n".join(lines), encoding="utf-8")
    compiler = subprocess.Popen([str(MAKENSIS), "/V2", "/INPUTCHARSET", "UTF8", str(script)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # Quota bookkeeping: the native compiler's identity, recorded at once (in memory and beside the script).
    identity = {"pid": compiler.pid, "startedAt": time.time(), "script": str(script)}
    (work / "compiler.json").write_text(json.dumps(identity), encoding="utf-8")
    if compile_log is not None:
        compile_log.append(identity)
    try:
        output, _ = compiler.communicate(timeout=120)
    finally:
        if compiler.poll() is None:
            compiler.kill()
            compiler.wait(30)
    if compiler.returncode != 0 or not exe.is_file():
        raise AssertionError("makensis failed:\n" + output.decode("utf-8", "replace")[-4000:])
    return Probe(exe, work, fence, data_root)
