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


def winverifytrust(path: Path) -> dict[str, str]:
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
        raise PrerequisiteError(f"The WebView2 installer has no valid Authenticode signature "
                                f"(WinVerifyTrust 0x{result & 0xFFFFFFFF:08X}).")
    if not identity:
        raise PrerequisiteError("The WebView2 installer's signing certificate could not be read.")
    return identity


def verify_installer(
    path: Path,
    pin: dict[str, Any],
    trust: Callable[[Path], dict[str, str]] = winverifytrust,
) -> dict[str, Any]:
    """The cached installer is exactly the pinned file, signed by the pinned certificate."""
    if _is_link(path) or _is_link(path.parent):
        raise PrerequisiteError("The cached WebView2 installer is missing, or it or its folder is a link.")
    if not path.is_file():
        raise PrerequisiteError("The cached WebView2 installer is not a regular file.")
    if path.name != pin["fileName"]:
        raise PrerequisiteError(f"The cached WebView2 installer must be named {pin['fileName']}.")
    size = path.stat().st_size
    if size != pin["bytes"]:
        raise PrerequisiteError(f"The cached WebView2 installer has {size} bytes; the pin says {pin['bytes']}.")
    digest = sha256_file(path)
    if digest != pin["sha256"]:
        raise PrerequisiteError(f"The cached WebView2 installer SHA-256 {digest} does not match the pin.")
    signer = trust(path)
    expected = pin["authenticode"]
    if (signer.get("thumbprintSha1", "").upper() != expected["signerCertificateSha1"].upper()
            or signer.get("subject") != expected["signer"] or signer.get("issuer") != expected["issuer"]):
        raise PrerequisiteError(
            f"The WebView2 installer is signed by {signer.get('subject')!r} (issuer {signer.get('issuer')!r}, "
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


def hook_wrapper(installer: Path, pin: dict[str, Any], hook: Path = HOOK_PATH) -> str:
    """The generated NSIS include that hands the verified installer to the committed hook."""
    for value in (str(installer), str(hook)):
        if _NSIS_UNSAFE.search(value):
            raise PrerequisiteError("A WebView2 hook path contains characters NSIS would interpret.")
    return "\n".join((
        "; Generated by build_support.build_desktop for one build. Do not edit.",
        f"; Pinned WebView2 installer SHA-256 {pin['sha256']} ({pin['bytes']} bytes).",
        f'!define QRONOS_WEBVIEW2_INSTALLER "{installer}"',
        f'!define QRONOS_WEBVIEW2_VERSION "{pin["runtime"]["version"]}"',
        f'!define QRONOS_WEBVIEW2_SHA256 "{pin["sha256"]}"',
        f'!include "{hook}"',
        "",
    ))


def write_hook_wrapper(directory: Path, installer: Path, pin: dict[str, Any], hook: Path = HOOK_PATH) -> Path:
    wrapper = directory / "qronos-installer-hooks.nsh"
    with open(wrapper, "x", encoding="utf-8", newline="\r\n") as handle:  # never replaces a file
        handle.write(hook_wrapper(installer, pin, hook))
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


def extract_and_hash_payload(setup: Path, seven_zip: Path, scratch: Path,
                             runner: Callable[..., Any] = subprocess.run) -> str:
    """SHA-256 of the embedded WebView2 installer, extracted into a new scratch folder."""
    scratch.mkdir(parents=True, exist_ok=False)
    result = runner([str(seven_zip), "e", "-y", f"-o{scratch}", str(setup), EMBEDDED_PATH],
                    capture_output=True, text=True, encoding="utf-8", check=False)
    extracted = scratch / EMBEDDED_PATH.rsplit("\\", 1)[-1]
    if result.returncode != 0 or not extracted.is_file():
        raise PrerequisiteError("7-Zip could not extract the embedded WebView2 installer.")
    return sha256_file(extracted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a cached WebView2 installer against the pin (offline).")
    parser.add_argument("installer", type=Path, nargs="?", default=None,
                        help="Default: .cache/wv2/<pinned file name>.")
    arguments = parser.parse_args(argv)
    try:
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
