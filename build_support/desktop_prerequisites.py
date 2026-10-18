"""The offline Microsoft Edge WebView2 prerequisite of the Qronos Windows installer.

Tauri's default ``downloadBootstrapper`` mode downloads Microsoft's bootstrapper
when WebView2 is missing, so installation fails offline. Tauri's own
``offlineInstaller`` mode is not used either: the locked bundler (tauri-bundler
2.9.4, used by @tauri-apps/cli 2.11.4) resolves Microsoft's moving fwlink on the
network at every build and embeds whatever version it finds, with no pin.

Instead:

* ``build_support/webview2_prerequisite.json`` pins the exact Evergreen
  Standalone Installer (x64): its bytes, SHA-256, immutable download URL,
  declared runtime version and signing evidence.
* A developer fetches that URL once into ``.cache/wv2/``, together with the
  other one-time build tools (Cargo crates, Tauri's NSIS folder). The build
  does not download: it verifies the cached file offline (size, SHA-256, and an
  Authenticode signature by the pinned certificate through Windows
  ``WinVerifyTrust`` without revocation network access), refuses to start
  when Tauri's NSIS tools are incomplete, and runs Cargo with ``--offline``.
* ``tauri.conf.json`` sets ``webviewInstallMode`` to ``skip`` and points to
  ``desktop/src-tauri/installer-hooks/webview2-offline.nsh``. build_desktop
  generates a wrapper that defines the verified path and includes that hook,
  which embeds the installer and runs it only when WebView2 is absent.
* After the build, the generated NSIS script and the installer itself are
  checked: no download mode, no NSIS download plugin, and exactly the pinned
  bytes embedded.

The Microsoft Visual C++ 2015-2022 Redistributable (x64) is handled the same
way (``build_support/vcredist_prerequisite.json``, ``.cache/vcredist/``): pinned
bytes, SHA-256 and signer, verified offline, embedded once by the same hook,
and run only when the registered x64 runtime is missing or older than the pin,
before anything of a previous installation changes.

Verification is technical evidence. It is not Microsoft redistribution
clearance, and it does not sign the Qronos installer.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
PIN_PATH = ROOT / "build_support" / "webview2_prerequisite.json"
HOOK_PATH = ROOT / "desktop" / "src-tauri" / "installer-hooks" / "webview2-offline.nsh"
#: Short on purpose: deep paths under a long checkout have hit Windows MAX_PATH.
CACHE_DIR = ROOT / ".cache" / "wv2"
WINDOWS_TARGET = "x86_64-pc-windows-msvc"

#: Where the hook stores the embedded installer inside the NSIS archive.
EMBEDDED_PATH = "$PLUGINSDIR\\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
#: NSIS plug-ins that download over the network; an offline installer contains none.
DOWNLOAD_PLUGINS = ("nsisdl.dll", "inetc.dll", "nsisdownload.dll")

_PIN_FIELDS = frozenset({"schema", "comment", "component", "target", "architecture", "fileName", "bytes",
                         "sha256", "url", "resolvedFrom", "resolvedOn", "runtime", "setupStub",
                         "authenticode", "installArguments", "ownerGates", "references"})

VCREDIST_PIN_PATH = ROOT / "build_support" / "vcredist_prerequisite.json"
VCREDIST_CACHE_DIR = ROOT / ".cache" / "vcredist"
#: Where the hook stores the embedded Visual C++ Redistributable inside the NSIS archive.
VCREDIST_EMBEDDED_PATH = "$PLUGINSDIR\\vc_redist.x64.exe"
_VCREDIST_PIN_FIELDS = frozenset({"schema", "comment", "component", "target", "architecture", "fileName", "bytes",
                                  "sha256", "url", "resolvedFrom", "resolvedOn", "runtime", "bundle", "authenticode",
                                  "installArguments", "successExitCodes", "licence", "ownerGates", "references"})
#: What the hook accepts from vc_redist: success, a newer runtime already present, success needing a restart.
VCREDIST_SUCCESS_EXIT_CODES = (0, 1638, 3010)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_VERSION = re.compile(r"[0-9]{1,5}(\.[0-9]{1,5}){3}")
_NSIS_UNSAFE = re.compile(r'["$`\r\n;]')
_READ_CHUNK = 4 * 1024 * 1024


#: What the locked tauri-bundler 2.9.4 (inside @tauri-apps/cli 2.11.4) requires in its NSIS tool
#: folder (src/bundle/windows/nsis/mod.rs NSIS_REQUIRED_FILES and NSIS_REQUIRED_FILES_HASH). When
#: any file is missing or the plug-in hash differs, that bundler downloads NSIS 3.11 and
#: nsis_tauri_utils.dll from GitHub during the build; the preflight refuses instead.
TAURI_CLI_VERSION = "2.11.4"
NSIS_REQUIRED_FILES = (
    "makensis.exe", "Bin/makensis.exe", "Stubs/lzma-x86-unicode", "Stubs/lzma_solid-x86-unicode",
    "Plugins/x86-unicode/additional/nsis_tauri_utils.dll", "Include/MUI2.nsh", "Include/FileFunc.nsh",
    "Include/x64.nsh", "Include/nsDialogs.nsh", "Include/WinMessages.nsh", "Include/Win/COM.nsh",
    "Include/Win/Propkey.nsh", "Include/Win/RestartManager.nsh",
)
NSIS_TAURI_UTILS = "Plugins/x86-unicode/additional/nsis_tauri_utils.dll"
NSIS_TAURI_UTILS_SHA1 = "75197FEE3C6A814FE035788D1C34EAD39349B860"
NSIS_URL = "https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.11/nsis-3.11.zip"
NSIS_SHA1 = "EF7FF767E5CBD9EDD22ADD3A32C9B8F4500BB10D"
NSIS_TAURI_UTILS_URL = ("https://github.com/tauri-apps/nsis-tauri-utils/releases/download/"
                        "nsis_tauri_utils-v0.5.3/nsis_tauri_utils.dll")


class PrerequisiteError(RuntimeError):
    """The offline WebView2 prerequisite cannot be shown to be exactly what is pinned."""


# ------------------------------------------------------------------ pin


def load_pin(path: Path = PIN_PATH) -> dict[str, Any]:
    pin = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(pin, dict) or set(pin) != _PIN_FIELDS or pin.get("schema") != 1:
        raise PrerequisiteError(f"The WebView2 pin must have exactly the fields {sorted(_PIN_FIELDS)}.")
    if pin["target"] != WINDOWS_TARGET or pin["architecture"] != "x64":
        raise PrerequisiteError(f"The WebView2 pin must be the x64 installer for {WINDOWS_TARGET}.")
    if pin["fileName"] != "MicrosoftEdgeWebView2RuntimeInstallerX64.exe":
        raise PrerequisiteError("The WebView2 pin must name the x64 Evergreen Standalone Installer.")
    if not isinstance(pin["bytes"], int) or isinstance(pin["bytes"], bool) or pin["bytes"] <= 0:
        raise PrerequisiteError("The WebView2 pin needs a positive byte size.")
    if not isinstance(pin["sha256"], str) or not _SHA256.fullmatch(pin["sha256"]):
        raise PrerequisiteError("The WebView2 pin needs a lower-case SHA-256.")
    url = pin["url"]
    if not isinstance(url, str) or not url.startswith(
            "https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/") \
            or not url.endswith("/" + pin["fileName"]):
        raise PrerequisiteError("The WebView2 pin URL must be Microsoft's immutable delivery URL for the file.")
    runtime = pin["runtime"]
    if not isinstance(runtime, dict) or runtime.get("appId") != "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" \
            or not _RUNTIME_VERSION.fullmatch(str(runtime.get("version", ""))):
        raise PrerequisiteError("The WebView2 pin must declare the WebView2 Runtime appId and a four-part version.")
    authenticode = pin["authenticode"]
    if not isinstance(authenticode, dict) or not re.fullmatch(r"[0-9A-Fa-f]{40}",
                                                               str(authenticode.get("signerCertificateSha1", "")))             or not authenticode.get("signer") or not authenticode.get("issuer"):
        raise PrerequisiteError("The WebView2 pin must record the signer, issuer and SHA-1 thumbprint of the "
                                "signing certificate.")
    if pin["installArguments"] != "/silent /install":
        raise PrerequisiteError("The WebView2 pin must use the silent install arguments the hook runs.")
    if not isinstance(pin["ownerGates"], list) or not pin["ownerGates"]:
        raise PrerequisiteError("The WebView2 pin must keep its owner gates visible.")
    return pin


def load_vcredist_pin(path: Path = VCREDIST_PIN_PATH) -> dict[str, Any]:
    """The pinned Microsoft Visual C++ 2015-2022 Redistributable (x64)."""
    pin = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(pin, dict) or set(pin) != _VCREDIST_PIN_FIELDS or pin.get("schema") != 1:
        raise PrerequisiteError(f"The VC++ pin must have exactly the fields {sorted(_VCREDIST_PIN_FIELDS)}.")
    if pin["target"] != WINDOWS_TARGET or pin["architecture"] != "x64" or pin["fileName"] != "vc_redist.x64.exe":
        raise PrerequisiteError(f"The VC++ pin must be vc_redist.x64.exe for {WINDOWS_TARGET}.")
    if not isinstance(pin["bytes"], int) or isinstance(pin["bytes"], bool) or pin["bytes"] <= 0:
        raise PrerequisiteError("The VC++ pin needs a positive byte size.")
    if not isinstance(pin["sha256"], str) or not _SHA256.fullmatch(pin["sha256"]):
        raise PrerequisiteError("The VC++ pin needs a lower-case SHA-256.")
    url = pin["url"]
    # Microsoft's immutable download URL names the file's SHA-256 in its path.
    if not isinstance(url, str) or not url.startswith("https://download.visualstudio.microsoft.com/download/pr/") \
            or f"/{pin['sha256'].upper()}/" not in url or not url.lower().endswith("/vc_redist.x64.exe"):
        raise PrerequisiteError("The VC++ pin URL must be Microsoft's immutable download URL naming the pinned SHA-256.")
    runtime = pin["runtime"]
    numbers = [runtime.get(name) if isinstance(runtime, dict) else None for name in ("major", "minor", "build")]
    if not all(isinstance(n, int) and not isinstance(n, bool) and 0 <= n < 100000 for n in numbers) \
            or numbers[0] != 14 or not _RUNTIME_VERSION.fullmatch(str(runtime.get("version", ""))) \
            or not str(runtime["version"]).startswith(f"{numbers[0]}.{numbers[1]}.{numbers[2]}."):
        raise PrerequisiteError("The VC++ pin must declare the 14.x runtime version and its major/minor/build.")
    authenticode = pin["authenticode"]
    if not isinstance(authenticode, dict) or not re.fullmatch(r"[0-9A-Fa-f]{40}",
                                                               str(authenticode.get("signerCertificateSha1", ""))) \
            or authenticode.get("signer") != "Microsoft Corporation" or not authenticode.get("issuer"):
        raise PrerequisiteError("The VC++ pin must record Microsoft's signer, issuer and SHA-1 thumbprint.")
    if pin["installArguments"] != "/install /quiet /norestart" \
            or tuple(pin["successExitCodes"]) != VCREDIST_SUCCESS_EXIT_CODES:
        raise PrerequisiteError("The VC++ pin must use the install arguments and exit codes the hook applies.")
    if not isinstance(pin["ownerGates"], list) or not pin["ownerGates"] or not isinstance(pin["licence"], dict) \
            or not pin["licence"].get("terms") or not pin["licence"].get("condition"):
        raise PrerequisiteError("The VC++ pin must keep its licence terms and owner gates visible.")
    return pin


# ------------------------------------------------------- the cached file


def _is_link(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def winverifytrust(path: Path, what: str = "The WebView2 installer") -> dict[str, str]:
    """A valid Authenticode signature chaining to a trusted root, checked offline.

    Returns the identity of the certificate that signed the file (its SHA-1
    thumbprint, subject and issuer names) so the caller can compare it with the pin.
    """
    if os.name != "nt":
        raise PrerequisiteError("Authenticode verification needs Windows.")
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class FileInfo(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.c_void_p)]

    class ProviderCert(ctypes.Structure):  # CRYPT_PROVIDER_CERT, leading fields only
        _fields_ = [("cbStruct", wintypes.DWORD), ("pCert", ctypes.c_void_p)]

    class ProviderSigner(ctypes.Structure):  # CRYPT_PROVIDER_SGNR, leading fields only
        _fields_ = [("cbStruct", wintypes.DWORD), ("sftVerifyAsOf", wintypes.FILETIME),
                    ("csCertChain", wintypes.DWORD), ("pasCertChain", ctypes.POINTER(ProviderCert))]

    class CertContext(ctypes.Structure):  # CERT_CONTEXT
        _fields_ = [("dwCertEncodingType", wintypes.DWORD), ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
                    ("cbCertEncoded", wintypes.DWORD), ("pCertInfo", ctypes.c_void_p),
                    ("hCertStore", ctypes.c_void_p)]

    class TrustData(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(FileInfo)), ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPCWSTR),
                    ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    generic_verify_v2 = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                             (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    file_info = FileInfo(ctypes.sizeof(FileInfo), str(path), None, None)
    data = TrustData()
    data.cbStruct = ctypes.sizeof(TrustData)
    data.dwUIChoice = 2              # WTD_UI_NONE
    data.fdwRevocationChecks = 0     # WTD_REVOKE_NONE
    data.dwUnionChoice = 1           # WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = 1           # WTD_STATEACTION_VERIFY
    data.dwProvFlags = 0x1000 | 0x10  # WTD_CACHE_ONLY_URL_RETRIEVAL | WTD_REVOCATION_CHECK_NONE
    wintrust = ctypes.WinDLL("wintrust")
    wintrust.WinVerifyTrust.argtypes = (wintypes.HANDLE, ctypes.POINTER(GUID), ctypes.c_void_p)
    wintrust.WinVerifyTrust.restype = wintypes.LONG
    wintrust.WTHelperProvDataFromStateData.argtypes = (wintypes.HANDLE,)
    wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    wintrust.WTHelperGetProvSignerFromChain.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                                        wintypes.DWORD)
    wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(ProviderSigner)
    crypt32 = ctypes.WinDLL("crypt32")
    crypt32.CertGetNameStringW.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                           wintypes.LPWSTR, wintypes.DWORD)
    crypt32.CertGetNameStringW.restype = wintypes.DWORD

    def name(certificate: int, issuer: bool) -> str:
        buffer = ctypes.create_unicode_buffer(512)
        crypt32.CertGetNameStringW(certificate, 4, 1 if issuer else 0, None, buffer, 512)  # SIMPLE_DISPLAY
        return buffer.value

    result = wintrust.WinVerifyTrust(wintypes.HANDLE(-1), ctypes.byref(generic_verify_v2), ctypes.byref(data))
    identity: dict[str, str] = {}
    try:
        if result == 0:
            provider = wintrust.WTHelperProvDataFromStateData(data.hWVTStateData)
            signer = wintrust.WTHelperGetProvSignerFromChain(provider, 0, False, 0) if provider else None
            if signer and signer.contents.csCertChain and signer.contents.pasCertChain:
                certificate = signer.contents.pasCertChain[0].pCert
                context = ctypes.cast(certificate, ctypes.POINTER(CertContext)).contents
                encoded = ctypes.string_at(context.pbCertEncoded, context.cbCertEncoded)
                identity = {"thumbprintSha1": hashlib.sha1(encoded).hexdigest().upper(),
                            "subject": name(certificate, False), "issuer": name(certificate, True)}
    finally:
        data.dwStateAction = 2       # WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(wintypes.HANDLE(-1), ctypes.byref(generic_verify_v2), ctypes.byref(data))
    if result != 0:
        raise PrerequisiteError(f"{what} has no valid Authenticode signature "
                                f"(WinVerifyTrust 0x{result & 0xFFFFFFFF:08X}).")
    if not identity:
        raise PrerequisiteError(f"{what}'s signing certificate could not be read.")
    return identity


def verify_installer(
    path: Path,
    pin: dict[str, Any],
    trust: Callable[[Path], dict[str, str]] = winverifytrust,
    *,
    what: str = "WebView2 installer",
) -> dict[str, Any]:
    """The cached installer is exactly the pinned file, signed by the pinned certificate."""
    if _is_link(path) or _is_link(path.parent):
        raise PrerequisiteError(f"The cached {what} is missing, or it or its folder is a link.")
    if not path.is_file():
        raise PrerequisiteError(f"The cached {what} is not a regular file.")
    if path.name != pin["fileName"]:
        raise PrerequisiteError(f"The cached {what} must be named {pin['fileName']}.")
    size = path.stat().st_size
    if size != pin["bytes"]:
        raise PrerequisiteError(f"The cached {what} has {size} bytes; the pin says {pin['bytes']}.")
    digest = sha256_file(path)
    if digest != pin["sha256"]:
        raise PrerequisiteError(f"The cached {what} SHA-256 {digest} does not match the pin.")
    signer = trust(path)
    expected = pin["authenticode"]
    if (signer.get("thumbprintSha1", "").upper() != expected["signerCertificateSha1"].upper()
            or signer.get("subject") != expected["signer"] or signer.get("issuer") != expected["issuer"]):
        raise PrerequisiteError(
            f"The {what} is signed by {signer.get('subject')!r} (issuer {signer.get('issuer')!r}, "
            f"thumbprint {signer.get('thumbprintSha1')}), not the pinned {expected['signer']!r} certificate "
            f"{expected['signerCertificateSha1']}.")
    return {"fileName": path.name, "bytes": size, "sha256": digest, "runtimeVersion": pin["runtime"]["version"],
            "authenticode": f"valid (WinVerifyTrust); signer {signer['subject']}, thumbprint {signer['thumbprintSha1']}"}


# ------------------------------------------------- build-tool preflight


def tauri_tools_dir() -> Path:
    """Where the locked bundler keeps NSIS: dirs::cache_dir() / "tauri", i.e. %LOCALAPPDATA%/tauri."""
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        raise PrerequisiteError("LOCALAPPDATA is not set, so Tauri's NSIS tool folder cannot be located.")
    return Path(local) / "tauri"


def verify_build_tools(tools: Path, tauri_cli_version: str) -> dict[str, Any]:
    """The prepared NSIS tool folder is complete, so the locked bundler will not download anything."""
    if tauri_cli_version != TAURI_CLI_VERSION:
        raise PrerequisiteError(f"@tauri-apps/cli is {tauri_cli_version}; the build-tool preflight was reviewed "
                                f"against {TAURI_CLI_VERSION}. Re-check its NSIS download rules before building.")
    nsis = tools / "NSIS"
    missing = [name for name in NSIS_REQUIRED_FILES if not (nsis / name).is_file()]
    if missing:
        raise PrerequisiteError(
            f"The Tauri NSIS tool folder {nsis} is missing {', '.join(missing)}; the build would download "
            "NSIS from GitHub. Prepare it once as described in docs/offline-windows-installer.md.")
    digest = hashlib.sha1((nsis / NSIS_TAURI_UTILS).read_bytes()).hexdigest().upper()
    if digest != NSIS_TAURI_UTILS_SHA1:
        raise PrerequisiteError(f"{NSIS_TAURI_UTILS} has SHA-1 {digest}, not {NSIS_TAURI_UTILS_SHA1}; the build "
                                "would download a replacement. Prepare it once as described in "
                                "docs/offline-windows-installer.md.")
    return {"nsisToolDir": "%LOCALAPPDATA%/tauri/NSIS", "nsisTauriUtilsSha1": digest}


# ------------------------------------------------------------ NSIS hook


def hook_wrapper(installer: Path, pin: dict[str, Any], hook: Path = HOOK_PATH, *,
                 vcredist: tuple[Path, dict[str, Any]]) -> str:
    """The generated NSIS include that hands the verified WebView2 installer and the verified Visual C++
    Redistributable (``vcredist`` = (file, pin)) to the hook."""
    vc_installer, vc_pin = vcredist
    for value in (str(installer), str(hook), str(vc_installer)):
        if _NSIS_UNSAFE.search(value):
            raise PrerequisiteError("A prerequisite hook path contains characters NSIS would interpret.")
    runtime = vc_pin["runtime"]
    return "\n".join((
        "; Generated by build_support.build_desktop for one build. Do not edit.",
        f"; Pinned WebView2 installer SHA-256 {pin['sha256']} ({pin['bytes']} bytes).",
        f"; Pinned VC++ 2015-2022 x64 redistributable SHA-256 {vc_pin['sha256']} ({vc_pin['bytes']} bytes).",
        f'!define QRONOS_WEBVIEW2_INSTALLER "{installer}"',
        f'!define QRONOS_WEBVIEW2_VERSION "{pin["runtime"]["version"]}"',
        f'!define QRONOS_WEBVIEW2_SHA256 "{pin["sha256"]}"',
        f'!define QRONOS_VCREDIST_INSTALLER "{vc_installer}"',
        f'!define QRONOS_VCREDIST_VERSION "{runtime["version"]}"',
        f'!define QRONOS_VCREDIST_MAJOR "{int(runtime["major"])}"',
        f'!define QRONOS_VCREDIST_MINOR "{int(runtime["minor"])}"',
        f'!define QRONOS_VCREDIST_BUILD "{int(runtime["build"])}"',
        f'!define QRONOS_VCREDIST_SHA256 "{vc_pin["sha256"]}"',
        f'!include "{hook}"',
        "",
    ))


def write_hook_wrapper(directory: Path, installer: Path, pin: dict[str, Any], hook: Path = HOOK_PATH, *,
                       vcredist: tuple[Path, dict[str, Any]]) -> Path:
    wrapper = directory / "qronos-installer-hooks.nsh"
    with open(wrapper, "x", encoding="utf-8", newline="\r\n") as handle:  # never replaces a file
        handle.write(hook_wrapper(installer, pin, hook, vcredist=vcredist))
    return wrapper


# ------------------------------------------------- built-artifact checks


def _defines(script: str) -> dict[str, str]:
    return {match.group(1): match.group(2)
            for match in re.finditer(r'^!define\s+(\w+)\s+"([^"\r\n]*)"', script, re.MULTILINE)}


def verify_generated_script(script: str, wrapper: Path) -> None:
    """Tauri's generated installer.nsi has no WebView2 download path and includes our hook."""
    defines = _defines(script)
    expected_empty = ("INSTALLWEBVIEW2MODE", "WEBVIEW2BOOTSTRAPPERPATH", "WEBVIEW2INSTALLERPATH",
                      "MINIMUMWEBVIEW2VERSION")
    for name in expected_empty:
        if name not in defines:
            raise PrerequisiteError(f"The generated installer script does not define {name}.")
        if defines[name] != "":
            raise PrerequisiteError(f"The generated installer script sets {name} to {defines[name]!r}; "
                                    "Tauri's own WebView2 handling must be skipped.")
    includes = {os.path.normcase(os.path.normpath(match.group(1)))
                for match in re.finditer(r'^!include\s+"([^"\r\n]+)"', script, re.MULTILINE)}
    if os.path.normcase(os.path.normpath(str(wrapper))) not in includes:
        raise PrerequisiteError("The generated installer script does not include the verified WebView2 hook.")


# ------------------------------------------------- the pinned installer template

#: The Qronos NSIS template (installer-template/PROVENANCE.md): tauri-bundler 2.9.4's installer.nsi plus a
#: reviewed diff. tauri.conf.json selects it; the build refuses anything else.
TEMPLATE_DIR = ROOT / "desktop" / "src-tauri" / "installer-template"
TEMPLATE_SETTING = "installer-template/installer.nsi"
UPSTREAM_TEMPLATE_SHA256 = "20f4ecc730defb71f1342eaeaec4021df13be3d843abba0effe88ea5835fa079"
TEMPLATE_LICENCES = {"License_MIT.md": "86fc515b82835f7cb6ad400cc64a9d3380d22d55b593e8c34f51efee9f6a250b",
                     "License_Apache.md": "a0746c84c2a9663f57491dda8a619cc64152a8447889c0e17aa2f3e592e0097e"}
#: Steps every generated Qronos installer checks (QRONOS_STEP), at least once each, in this order.
TEMPLATE_STEPS = ("the install folder", "the main program", "the uninstaller", "the install location registration",
                  "the Apps & features registration", "the size registration", "the start menu shortcut",
                  "the desktop shortcut")
#: Statements whose failure only sets the NSIS error flag: each must be followed IMMEDIATELY by its QRONOS_STEP.
_MATERIAL = re.compile(r"^(SetOutPath|File|CreateDirectory|WriteUninstaller|WriteRegStr|WriteRegDWORD|"
                       r"CreateShortcut|Call CreateOrUpdate\w+Shortcut)\b")
#: Allowed between a material statement and its step (they never touch the error flag).
_NEUTRAL = ("${EndIf}", "!insertmacro MUI_STARTMENU_WRITE_END", "!endif")
#: Commands that clear or set the error flag; inside the shortcut functions they could hide a failure.
_FLAG_CHANGERS = re.compile(r"\b(ClearErrors|IfErrors|SetErrors|ReadRegStr|ReadINIStr|GetSize|\$\{Errors\})")
#: Values only test probes define; a production script or hook wrapper with one is refused.
_PROBE_ONLY = re.compile(r"^\s*!define\s+(QRONOS_PROBE_\w+|QPROBE_\w+)\b", re.MULTILINE)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_template_source(directory: Path = TEMPLATE_DIR) -> None:
    """The committed template is the pinned upstream file plus exactly the reviewed diff, with its licences."""
    import difflib

    upstream = directory / "upstream" / "installer.nsi"
    if not upstream.is_file() or _file_sha256(upstream) != UPSTREAM_TEMPLATE_SHA256:
        raise PrerequisiteError("installer-template/upstream/installer.nsi is not tauri-bundler 2.9.4's template.")
    for name, digest in TEMPLATE_LICENCES.items():
        if not (directory / name).is_file() or _file_sha256(directory / name) != digest:
            raise PrerequisiteError(f"installer-template/{name} is not the upstream licence text.")
    ours = (directory / "installer.nsi").read_text(encoding="utf-8").splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(upstream.read_text(encoding="utf-8").splitlines(keepends=True), ours,
                                        "upstream/installer.nsi", "installer.nsi", n=3))
    if diff != (directory / "qronos-template.diff").read_text(encoding="utf-8"):
        raise PrerequisiteError("installer-template/installer.nsi differs from the reviewed qronos-template.diff.")


def _block(script: str, opening: str, closing: str) -> str:
    start = script.find(f"\n{opening}\n")
    if start < 0:
        raise PrerequisiteError(f"The generated installer script has no {opening!r}.")
    end = script.find(f"\n{closing}", start + 1)
    return script[start + 1:end]


def _statements(block: str) -> list[str]:
    return [line.strip() for line in block.split("\n")[1:]
            if line.strip() and not line.strip().startswith((";", "#"))]


def verify_generated_order(script: str) -> None:
    """Tauri's REAL generated installer.nsi has the Qronos order."""
    text = script.lstrip("\ufeff").replace("\r\n", "\n")
    if "!ifndef QRONOS_TEMPLATE_HOOKS" not in text:
        raise PrerequisiteError("The generated installer script is not from the Qronos template.")
    install = _statements(_block(text, "Section Install", "SectionEnd"))
    if not install or install[0] != "!insertmacro NSIS_HOOK_PREINSTALL":
        raise PrerequisiteError("The generated installer does not run the Qronos checks first.")
    if install.count("SetOutPath $INSTDIR") != 1:
        raise PrerequisiteError("The generated installer does not create the install folder exactly once.")
    pending = None
    for statement in install:
        if statement.startswith("!insertmacro QRONOS_STEP "):
            pending = None
        elif pending is not None and statement not in _NEUTRAL:
            raise PrerequisiteError(f"The generated installer does not check {pending!r} immediately.")
        if _MATERIAL.match(statement):
            pending = statement
    if pending is not None:
        raise PrerequisiteError(f"The generated installer does not check {pending!r}.")
    steps = re.findall(r'^!insertmacro QRONOS_STEP "(.*)"$', "\n".join(install), re.MULTILINE)
    positions = [steps.index(step) if step in steps else -1 for step in TEMPLATE_STEPS]
    if -1 in positions or positions != sorted(positions):
        raise PrerequisiteError(f"The generated installer does not check every install step: {steps}.")
    if install.count("!insertmacro QRONOS_INSTALL_COMPLETE") != 1:
        raise PrerequisiteError("The generated installer has no single install-complete step.")
    after = install[install.index("!insertmacro QRONOS_INSTALL_COMPLETE") + 1:]
    if after[:2] != ["!ifmacrodef NSIS_HOOK_POSTINSTALL", "!insertmacro NSIS_HOOK_POSTINSTALL"]:
        raise PrerequisiteError("The install-complete step is not immediately before the post-install hook.")
    if any(statement.startswith(("Delete ", "RMDir ", "RmDir ")) for statement in install):
        raise PrerequisiteError("The generated Install section deletes outside the owned file list.")
    uninstall = _statements(_block(text, "Section Uninstall", "SectionEnd"))
    if not uninstall or uninstall[0] != "!insertmacro NSIS_HOOK_PREUNINSTALL":
        raise PrerequisiteError("The generated uninstaller does not run the Qronos checks first.")
    deletions = [s for s in uninstall if s.startswith(("Delete ", "RMDir ", "RmDir "))]
    allowed = re.compile(r'^(Delete "\$INSTDIR\\uninstall\.exe"|Delete "\$(SMPROGRAMS|DESKTOP)\\[^"]*\.lnk"|'
                         r'RMDir "\$SMPROGRAMS\\\$AppStartMenuFolder")$')
    unexpected = [s for s in deletions if not allowed.match(s)]
    if unexpected:
        raise PrerequisiteError(f"The generated uninstaller deletes outside the owned file list: {unexpected}.")
    for index, statement in enumerate(uninstall):
        if statement == 'Delete "$INSTDIR\\uninstall.exe"' and \
                (index == 0 or uninstall[index - 1] != '${If} $QronosUninstallerListed == "1"'):
            raise PrerequisiteError("The generated uninstaller deletes uninstall.exe without the owned list naming it.")
    if re.search(r"^\s*R[Mm]Dir\s+/r\b", text, re.MULTILINE):
        raise PrerequisiteError("The generated installer removes a folder recursively.")
    if re.search(r"^\s*!define MUI_PAGE_CUSTOMFUNCTION_(SHOW|LEAVE) un\.Confirm", text, re.MULTILINE):
        raise PrerequisiteError("The generated uninstaller offers the recursive app-data removal.")
    for function in ("PageReinstall", "PageLeaveReinstall"):
        if "ExecWait" in _block(text, f"Function {function}", "FunctionEnd"):
            raise PrerequisiteError(f"The generated installer can run a previous uninstaller ({function}).")
    for function in ("CreateOrUpdateStartMenuShortcut", "CreateOrUpdateDesktopShortcut"):
        if _FLAG_CHANGERS.search("\n".join(_statements(_block(text, f"Function {function}", "FunctionEnd")))):
            raise PrerequisiteError(f"{function} could hide a failed shortcut from its install step.")
    if _PROBE_ONLY.search(text):
        raise PrerequisiteError("The generated installer script defines a test-probe value.")


def verify_hook_wrapper(text: str) -> None:
    """The generated hook wrapper never carries a test-probe value, and names the verified VC++ redistributable."""
    if _PROBE_ONLY.search(text):
        raise PrerequisiteError("The hook wrapper defines a test-probe value.")
    for name in ("QRONOS_VCREDIST_INSTALLER", "QRONOS_VCREDIST_MAJOR", "QRONOS_VCREDIST_MINOR",
                 "QRONOS_VCREDIST_BUILD"):
        if not re.search(rf'^!define\s+{name}\s+"[^"\r\n]+"', text, re.MULTILINE):
            raise PrerequisiteError(f"The hook wrapper does not define {name} (the verified VC++ redistributable).")


def find_seven_zip() -> Path:
    candidates = [os.environ.get("QRONOS_7ZIP"), shutil.which("7z"),
                  os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "7-Zip", "7z.exe")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise PrerequisiteError("7-Zip (7z.exe) is required to verify the built installer; set QRONOS_7ZIP.")


def list_installer(setup: Path, seven_zip: Path,
                   runner: Callable[..., Any] = subprocess.run) -> dict[str, int | None]:
    """Archive paths and sizes inside an NSIS installer, read with 7-Zip (nothing is run).

    7-Zip reports no size for non-solid compressed entries; those sizes are None.
    """
    result = runner([str(seven_zip), "l", "-slt", "-sccUTF-8", str(setup)],
                    capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise PrerequisiteError(f"7-Zip could not list the installer (exit {result.returncode}).")
    entries: dict[str, int | None] = {}
    path = None
    for line in result.stdout.splitlines():
        if line.startswith("Path = "):
            path = line[len("Path = "):]
        elif line.startswith("Size = ") and path is not None:
            value = line[len("Size = "):].strip()
            entries[path] = int(value) if value else None
            path = None
    entries.pop(str(setup), None)
    return entries


def verify_embedded_payload(entries: dict[str, int | None], pin: dict[str, Any]) -> None:
    """The installer embeds exactly one WebView2 installer, of the pinned size, and no download plug-in.

    The bytes themselves are compared by extract_and_hash_payload.
    """
    lowered = {name.lower(): name for name in entries}
    downloaders = sorted(lowered[name] for name in lowered
                         if name.rsplit("\\", 1)[-1] in DOWNLOAD_PLUGINS)
    if downloaders:
        raise PrerequisiteError(f"The installer contains an NSIS download plug-in: {', '.join(downloaders)}.")
    embedded = [name for name in entries if name.lower().endswith("\\" + pin["fileName"].lower())
                or name.lower() == pin["fileName"].lower()]
    if embedded != [EMBEDDED_PATH]:
        raise PrerequisiteError(f"The installer must embed the WebView2 installer exactly once at {EMBEDDED_PATH}; "
                                f"found {embedded or 'none'}.")
    if entries[EMBEDDED_PATH] is not None and entries[EMBEDDED_PATH] != pin["bytes"]:
        raise PrerequisiteError(f"The embedded WebView2 installer has {entries[EMBEDDED_PATH]} bytes; "
                                f"the pin says {pin['bytes']}.")


def verify_vcredist_installer(path: Path, pin: dict[str, Any],
                              trust: Callable[[Path], dict[str, str]] | None = None) -> dict[str, Any]:
    """The cached VC++ redistributable is exactly the pinned file, signed by the pinned Microsoft certificate."""
    return verify_installer(path, pin, trust or (lambda file: winverifytrust(file, "The VC++ redistributable")),
                            what="VC++ redistributable")


def extract_and_hash_vcredist(setup: Path, seven_zip: Path, scratch: Path,
                              runner: Callable[..., Any] = subprocess.run) -> str:
    """SHA-256 of the embedded VC++ redistributable, extracted into a new scratch folder."""
    return extract_and_hash_payload(setup, seven_zip, scratch, runner, member=VCREDIST_EMBEDDED_PATH)


def verify_embedded_vcredist(entries: dict[str, int | None], pin: dict[str, Any]) -> None:
    """The installer embeds exactly one VC++ redistributable, of the pinned size (bytes compared on extraction)."""
    embedded = [name for name in entries if name.lower().endswith("\\" + pin["fileName"].lower())
                or name.lower() == pin["fileName"].lower()]
    if embedded != [VCREDIST_EMBEDDED_PATH]:
        raise PrerequisiteError(f"The installer must embed the VC++ redistributable exactly once at "
                                f"{VCREDIST_EMBEDDED_PATH}; found {embedded or 'none'}.")
    if entries[VCREDIST_EMBEDDED_PATH] is not None and entries[VCREDIST_EMBEDDED_PATH] != pin["bytes"]:
        raise PrerequisiteError(f"The embedded VC++ redistributable has {entries[VCREDIST_EMBEDDED_PATH]} bytes; "
                                f"the pin says {pin['bytes']}.")


def extract_and_hash_payload(setup: Path, seven_zip: Path, scratch: Path,
                             runner: Callable[..., Any] = subprocess.run, member: str = EMBEDDED_PATH) -> str:
    """SHA-256 of one embedded prerequisite (default: the WebView2 installer), extracted into a new scratch
    folder."""
    scratch.mkdir(parents=True, exist_ok=False)
    result = runner([str(seven_zip), "e", "-y", f"-o{scratch}", str(setup), member],
                    capture_output=True, text=True, encoding="utf-8", check=False)
    extracted = scratch / member.rsplit("\\", 1)[-1]
    if result.returncode != 0 or not extracted.is_file():
        raise PrerequisiteError(f"7-Zip could not extract the embedded {member.rsplit(chr(92), 1)[-1]}.")
    return sha256_file(extracted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a cached WebView2 installer against the pin (offline).")
    parser.add_argument("installer", type=Path, nargs="?", default=None,
                        help="Default: .cache/wv2/<pinned file name> (with --vcredist: .cache/vcredist/<pinned>).")
    parser.add_argument("--vcredist", action="store_true",
                        help="Verify the cached Visual C++ 2015-2022 x64 redistributable against its pin instead.")
    arguments = parser.parse_args(argv)
    try:
        if arguments.vcredist:
            pin = load_vcredist_pin()
            installer = arguments.installer or VCREDIST_CACHE_DIR / pin["fileName"]
            evidence = verify_vcredist_installer(installer, pin)
        else:
            pin = load_pin()
            installer = arguments.installer or CACHE_DIR / pin["fileName"]
            evidence = verify_installer(installer, pin)
    except (PrerequisiteError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
