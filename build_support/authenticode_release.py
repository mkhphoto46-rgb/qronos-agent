"""Authenticode signing and verification of a built Qronos installer (build tooling, owner's machine only).

Release builds only; nothing here is part of the product, and nothing here is a
Qronos network route. Contract: work/opus-production-installer-contract-round7.txt
section E; procedure: docs/local-update-packages.md.

Commands (``python -m build_support.authenticode_release ...``):

  check  --config JSON
      Checks the configured signtool and signing certificate. Signs nothing.
  sign   --config JSON --artifact EXE --build-provenance JSON
      Signs the exact built installer, then verifies every signature on it and
      writes EXE.authenticode.json beside it (never over an existing one).
  verify --config JSON --artifact EXE
      Verifies the installer as it is NOW. The release preflight's Authenticode
      gate opens only on this check, never on a provenance file.

The config is one JSON file, the only input (no environment variable, no PATH
search):

  {"schema": 1,
   "signtool": "<Program Files (x86)>\\Windows Kits\\10\\bin\\<10.0.x.y>\\x64\\signtool.exe",
   "certificate": "<absolute path of the public signing certificate, DER .cer, no private key>",
   "thumbprintSha1": "<40 hex: SHA-1 of that certificate>",
   "timestampUrl": "https://<an RFC 3161 time-stamp service>"}

What must hold, or nothing is signed or verified:

* signtool is under the fixed Windows SDK folder (Program Files (x86) as
  Windows reports it, not as an environment variable says), with no link on the
  way, and carries a valid Microsoft Authenticode signature.
* The certificate file is exactly the configured thumbprint. It is valid now,
  has the Code Signing usage (1.3.6.1.5.5.7.3.3), is not self-signed, and its
  chain, built offline, passes the Authenticode chain policy to a root held in
  the LOCAL MACHINE root stores. A root only the current user added is refused.
  Limit: an administrator-installed enterprise root also passes; the provenance
  lists the whole chain for the human release review.
* The artefact is a plain file named qronos-desktop_<authority version>_x64-
  setup.exe. Its unsigned SHA-256 equals the build provenance's (written by
  build_support.build_desktop), and that provenance is attributable to a clean
  source commit, the commit checked out now.
* After signing: ``signtool verify /pa /all /tw`` exits 0 (all signatures valid,
  time-stamped), and WinVerifyTrust (offline) accepts the file with the
  configured certificate as its signer.

Revocation is not checked offline here. ``signtool verify`` on the build
machine may consult revocation services; that is the build machine's own
network use, never Qronos's. No certificate store is written and no
certificate is installed. A self-signed certificate is never a substitute.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_support import desktop_installer_verify, release_signing  # noqa: E402
from build_support.desktop_prerequisites import PrerequisiteError, winverifytrust  # noqa: E402
from core import local_update  # noqa: E402

CODE_SIGNING_EKU = "1.3.6.1.5.5.7.3.3"
CONFIG_FIELDS = frozenset({"schema", "signtool", "certificate", "thumbprintSha1", "timestampUrl"})
SIGN_TIMEOUT_SECONDS = 600.0
VERIFY_TIMEOUT_SECONDS = 300.0
_MAX_CERTIFICATE_BYTES = 64 * 1024
_MAX_OUTPUT = 256 * 1024
_SDK_VERSION = re.compile(r"10\.0\.\d{1,6}\.\d{1,6}")


class AuthenticodeError(Exception):
    """A signing or verification step was refused; the message says why."""


# ---------------------------------------------------------------- Windows facts


def program_files_x86() -> Path:
    """Program Files (x86) as Windows itself reports it (SHGetKnownFolderPath), not an environment variable."""
    if os.name != "nt":
        raise AuthenticodeError("Authenticode signing runs on Windows only.")

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    folder = GUID(0x7C5A40EF, 0xA0FB, 0x4BFC, (ctypes.c_ubyte * 8)(0x87, 0x4A, 0xC0, 0xF2, 0xE0, 0xB9, 0xFA, 0x8E))
    shell = ctypes.WinDLL("shell32")
    ole = ctypes.WinDLL("ole32")
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                           ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long  # HRESULT
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    pointer = ctypes.c_void_p()
    if shell.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(pointer)) != 0 or not pointer.value:
        raise AuthenticodeError("Windows did not report the Program Files (x86) folder.")
    try:
        return Path(ctypes.wstring_at(pointer.value))
    finally:
        ole.CoTaskMemFree(pointer)


class _Blob(ctypes.Structure):  # CRYPT_INTEGER_BLOB / CERT_NAME_BLOB / CRYPT_OBJID_BLOB / CRYPT_HASH_BLOB
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _AlgorithmIdentifier(ctypes.Structure):  # CRYPT_ALGORITHM_IDENTIFIER
    _fields_ = [("pszObjId", ctypes.c_char_p), ("Parameters", _Blob)]


class _CertInfo(ctypes.Structure):  # CERT_INFO, leading fields up to Subject
    _fields_ = [("dwVersion", wintypes.DWORD), ("SerialNumber", _Blob), ("SignatureAlgorithm", _AlgorithmIdentifier),
                ("Issuer", _Blob), ("NotBefore", wintypes.FILETIME), ("NotAfter", wintypes.FILETIME),
                ("Subject", _Blob)]


class _CertContext(ctypes.Structure):  # CERT_CONTEXT
    _fields_ = [("dwCertEncodingType", wintypes.DWORD), ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
                ("cbCertEncoded", wintypes.DWORD), ("pCertInfo", ctypes.POINTER(_CertInfo)),
                ("hCertStore", ctypes.c_void_p)]


class _EnhancedKeyUsage(ctypes.Structure):  # CERT_ENHKEY_USAGE
    _fields_ = [("cUsageIdentifier", wintypes.DWORD), ("rgpszUsageIdentifier", ctypes.POINTER(ctypes.c_char_p))]


class _UsageMatch(ctypes.Structure):  # CERT_USAGE_MATCH
    _fields_ = [("dwType", wintypes.DWORD), ("Usage", _EnhancedKeyUsage)]


class _ChainPara(ctypes.Structure):  # CERT_CHAIN_PARA (base fields)
    _fields_ = [("cbSize", wintypes.DWORD), ("RequestedUsage", _UsageMatch)]


class _TrustStatus(ctypes.Structure):  # CERT_TRUST_STATUS
    _fields_ = [("dwErrorStatus", wintypes.DWORD), ("dwInfoStatus", wintypes.DWORD)]


class _ChainElement(ctypes.Structure):  # CERT_CHAIN_ELEMENT, leading fields
    _fields_ = [("cbSize", wintypes.DWORD), ("pCertContext", ctypes.POINTER(_CertContext)),
                ("TrustStatus", _TrustStatus)]


class _SimpleChain(ctypes.Structure):  # CERT_SIMPLE_CHAIN, leading fields
    _fields_ = [("cbSize", wintypes.DWORD), ("TrustStatus", _TrustStatus), ("cElement", wintypes.DWORD),
                ("rgpElement", ctypes.POINTER(ctypes.POINTER(_ChainElement)))]


class _ChainContext(ctypes.Structure):  # CERT_CHAIN_CONTEXT, leading fields
    _fields_ = [("cbSize", wintypes.DWORD), ("TrustStatus", _TrustStatus), ("cChain", wintypes.DWORD),
                ("rgpChain", ctypes.POINTER(ctypes.POINTER(_SimpleChain)))]


class _PolicyPara(ctypes.Structure):  # CERT_CHAIN_POLICY_PARA
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("pvExtraPolicyPara", ctypes.c_void_p)]


class _PolicyStatus(ctypes.Structure):  # CERT_CHAIN_POLICY_STATUS
    _fields_ = [("cbSize", wintypes.DWORD), ("dwError", wintypes.DWORD), ("lChainIndex", wintypes.LONG),
                ("lElementIndex", wintypes.LONG), ("pvExtraPolicyStatus", ctypes.c_void_p)]


_X509 = 0x1  # X509_ASN_ENCODING
_PKCS7 = 0x10000  # PKCS_7_ASN_ENCODING
_CHAIN_CACHE_ONLY_URL_RETRIEVAL = 0x00000004
_CHAIN_DISABLE_AUTH_ROOT_AUTO_UPDATE = 0x00000100
_POLICY_AUTHENTICODE = 2  # CERT_CHAIN_POLICY_AUTHENTICODE
_POLICY_IGNORE_ALL_REV_UNKNOWN = 0x00000F00
_FIND_EXT_ONLY_ENHKEY_USAGE = 0x2
_FIND_SHA1_HASH = 0x10000
_STORE_PROV_SYSTEM_W = 10
_SYSTEM_STORE_LOCAL_MACHINE = 0x00020000
_SYSTEM_STORE_CURRENT_USER = 0x00010000
_STORE_READONLY = 0x00008000
_STORE_OPEN_EXISTING = 0x00004000
_NAME_RDN_TYPE = 2
_NAME_ISSUER_FLAG = 0x1


def _crypt32() -> Any:
    c = ctypes.WinDLL("crypt32", use_last_error=True)
    PVOID, DWORD, BOOL = ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL
    context = ctypes.POINTER(_CertContext)

    def bind(function, argtypes, restype):
        function.argtypes, function.restype = argtypes, restype
        return function

    bind(c.CertCreateCertificateContext, [DWORD, ctypes.c_char_p, DWORD], context)
    bind(c.CertFreeCertificateContext, [context], BOOL)
    bind(c.CertVerifyTimeValidity, [ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(_CertInfo)], wintypes.LONG)
    bind(c.CertGetEnhancedKeyUsage, [context, DWORD, PVOID, ctypes.POINTER(DWORD)], BOOL)
    bind(c.CryptVerifyCertificateSignatureEx, [PVOID, DWORD, DWORD, PVOID, DWORD, PVOID, DWORD, PVOID], BOOL)
    bind(c.CertGetCertificateChain, [PVOID, context, ctypes.POINTER(wintypes.FILETIME), PVOID,
                                     ctypes.POINTER(_ChainPara), DWORD, PVOID,
                                     ctypes.POINTER(ctypes.POINTER(_ChainContext))], BOOL)
    bind(c.CertFreeCertificateChain, [ctypes.POINTER(_ChainContext)], None)
    bind(c.CertVerifyCertificateChainPolicy, [PVOID, ctypes.POINTER(_ChainContext), ctypes.POINTER(_PolicyPara),
                                              ctypes.POINTER(_PolicyStatus)], BOOL)
    bind(c.CertOpenStore, [PVOID, DWORD, PVOID, DWORD, wintypes.LPCWSTR], PVOID)
    bind(c.CertCloseStore, [PVOID, DWORD], BOOL)
    bind(c.CertFindCertificateInStore, [PVOID, DWORD, DWORD, DWORD, PVOID, context], context)
    bind(c.CertGetNameStringW, [context, DWORD, DWORD, PVOID, wintypes.LPWSTR, DWORD], DWORD)
    return c


def _name(c: Any, certificate: Any, issuer: bool) -> str:
    buffer = ctypes.create_unicode_buffer(1024)
    c.CertGetNameStringW(certificate, _NAME_RDN_TYPE, _NAME_ISSUER_FLAG if issuer else 0, None, buffer, 1024)
    return buffer.value


def _filetime(value: wintypes.FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def _blob_bytes(blob: _Blob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData) if blob.cbData else b""


def _in_system_store(c: Any, thumbprint: bytes, location: int, store_name: str) -> bool:
    store = c.CertOpenStore(_STORE_PROV_SYSTEM_W, 0, None, location | _STORE_READONLY | _STORE_OPEN_EXISTING,
                            store_name)
    if not store:
        return False
    try:
        buffer = ctypes.create_string_buffer(thumbprint, len(thumbprint))
        blob = _Blob(len(thumbprint), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        found = c.CertFindCertificateInStore(store, _X509 | _PKCS7, 0, _FIND_SHA1_HASH, ctypes.byref(blob), None)
        if found:
            c.CertFreeCertificateContext(found)
            return True
        return False
    finally:
        c.CertCloseStore(store, 0)


def certificate_facts(der: bytes) -> dict[str, Any]:
    """What Windows says about a DER certificate, read-only and offline (no store is written, no URL fetched)."""
    if os.name != "nt":
        raise AuthenticodeError("Certificate checks run on Windows only.")
    c = _crypt32()
    certificate = c.CertCreateCertificateContext(_X509, der, len(der))
    if not certificate:
        raise AuthenticodeError("The certificate file is not a DER X.509 certificate.")
    try:
        info = certificate.contents.pCertInfo.contents
        facts: dict[str, Any] = {
            "sha1": hashlib.sha1(der).hexdigest().upper(),
            "subject": _name(c, certificate, False), "issuer": _name(c, certificate, True),
            "notBefore": _filetime(info.NotBefore), "notAfter": _filetime(info.NotAfter),
            "timeValidity": int(c.CertVerifyTimeValidity(None, certificate.contents.pCertInfo)),
            "selfIssued": _blob_bytes(info.Issuer) == _blob_bytes(info.Subject),
        }
        # Self-signed: its own key verifies its signature (2 = CRYPT_VERIFY_CERT_SIGN_*_CERT).
        facts["selfSigned"] = bool(c.CryptVerifyCertificateSignatureEx(None, _X509, 2, certificate, 2, certificate,
                                                                       0, None))
        size = wintypes.DWORD()
        usages: list[str] = []
        if c.CertGetEnhancedKeyUsage(certificate, _FIND_EXT_ONLY_ENHKEY_USAGE, None, ctypes.byref(size)) \
                and size.value:
            buffer = ctypes.create_string_buffer(size.value)
            if c.CertGetEnhancedKeyUsage(certificate, _FIND_EXT_ONLY_ENHKEY_USAGE, buffer, ctypes.byref(size)):
                usage = ctypes.cast(buffer, ctypes.POINTER(_EnhancedKeyUsage)).contents
                usages = [usage.rgpszUsageIdentifier[i].decode("ascii") for i in range(usage.cUsageIdentifier)]
        facts["enhancedKeyUsage"] = usages
        facts.update(_chain_facts(c, certificate))
        return facts
    finally:
        c.CertFreeCertificateContext(certificate)


def _chain_facts(c: Any, certificate: Any) -> dict[str, Any]:
    oid = ctypes.c_char_p(CODE_SIGNING_EKU.encode("ascii"))
    para = _ChainPara(ctypes.sizeof(_ChainPara),
                      _UsageMatch(0, _EnhancedKeyUsage(1, ctypes.pointer(oid))))  # USAGE_MATCH_TYPE_AND
    chain = ctypes.POINTER(_ChainContext)()
    flags = _CHAIN_CACHE_ONLY_URL_RETRIEVAL | _CHAIN_DISABLE_AUTH_ROOT_AUTO_UPDATE  # offline; no revocation flag
    if not c.CertGetCertificateChain(None, certificate, None, None, ctypes.byref(para), flags, None,
                                     ctypes.byref(chain)):
        return {"chain": [], "chainPolicyError": 0xFFFFFFFF, "rootStore": "none"}
    try:
        status = _PolicyStatus(ctypes.sizeof(_PolicyStatus))
        policy = _PolicyPara(ctypes.sizeof(_PolicyPara), _POLICY_IGNORE_ALL_REV_UNKNOWN, None)
        if not c.CertVerifyCertificateChainPolicy(ctypes.c_void_p(_POLICY_AUTHENTICODE), chain,
                                                  ctypes.byref(policy), ctypes.byref(status)):
            status.dwError = 0xFFFFFFFF
        elements: list[dict[str, str]] = []
        root_der = b""
        if chain.contents.cChain:
            simple = chain.contents.rgpChain[0].contents
            for index in range(simple.cElement):
                element = simple.rgpElement[index].contents.pCertContext
                encoded = ctypes.string_at(element.contents.pbCertEncoded, element.contents.cbCertEncoded)
                elements.append({"subject": _name(c, element, False),
                                 "sha1": hashlib.sha1(encoded).hexdigest().upper()})
                root_der = encoded
        root_store = "none"
        if root_der:
            digest = hashlib.sha1(root_der).digest()
            if any(_in_system_store(c, digest, _SYSTEM_STORE_LOCAL_MACHINE, name) for name in ("AuthRoot", "Root")):
                root_store = "local_machine"
            elif _in_system_store(c, digest, _SYSTEM_STORE_CURRENT_USER, "Root"):
                root_store = "current_user_only"
        return {"chain": elements, "chainPolicyError": int(status.dwError), "rootStore": root_store}
    finally:
        c.CertFreeCertificateChain(chain)


# ------------------------------------------------------------- the decisions


def certificate_problems(facts: dict[str, Any], thumbprint: str) -> list[str]:
    """Why this certificate may not sign a Qronos release ([] when it may)."""
    problems = []
    if facts.get("sha1", "").upper() != thumbprint.upper():
        problems.append("the certificate file is not the configured thumbprint")
    if facts.get("timeValidity") != 0:
        problems.append("the certificate is not valid now (not yet valid or expired)")
    if CODE_SIGNING_EKU not in facts.get("enhancedKeyUsage", []):
        problems.append("the certificate does not carry the Code Signing usage (1.3.6.1.5.5.7.3.3)")
    if facts.get("selfSigned") or facts.get("selfIssued"):
        problems.append("the certificate is self-signed; a self-signed certificate is never a substitute")
    chain = facts.get("chain", [])
    if len(chain) < 2 or chain[0].get("sha1", "").upper() != facts.get("sha1", "").upper():
        problems.append("no certificate chain to an issuing authority could be built offline")
    if facts.get("chainPolicyError") != 0:
        problems.append(f"the chain fails the Authenticode chain policy (0x{facts.get('chainPolicyError', 0):08X})")
    if facts.get("rootStore") != "local_machine":
        problems.append("the chain's root is not in the local machine's trusted root stores"
                        + (" (only the current user added it)" if facts.get("rootStore") == "current_user_only"
                           else ""))
    return problems


def _plain_file(path: Path, what: str) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise AuthenticodeError(f"The {what} path must be absolute.")
    for part in (path, *path.parents):
        if os.path.lexists(part) and local_update._is_link(part):
            raise AuthenticodeError(f"The {what} path goes through a link or junction ({part}).")
    if not path.is_file():
        raise AuthenticodeError(f"The {what} is not a plain file: {path}")
    return path


def load_config(path: Path) -> dict[str, Any]:
    config_path = _plain_file(Path(path), "config file")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthenticodeError("The Authenticode config is not JSON.") from error
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS or config["schema"] != 1 \
            or not all(isinstance(config[key], str) for key in CONFIG_FIELDS - {"schema"}):
        raise AuthenticodeError(f"The Authenticode config needs exactly {sorted(CONFIG_FIELDS)}.")
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", config["thumbprintSha1"]):
        raise AuthenticodeError("thumbprintSha1 must be 40 hex digits.")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~/%-]*)?", config["timestampUrl"]):
        raise AuthenticodeError("timestampUrl must be a plain https URL of an RFC 3161 time-stamp service.")
    return config


def check_signtool(path: Path, *, program_files: Callable[[], Path] = program_files_x86,
                   trust: Callable[[Path], dict[str, str]] = winverifytrust) -> dict[str, str]:
    """signtool only from the fixed Windows SDK folder, link-free, signed by Microsoft."""
    path = _plain_file(Path(path), "signtool")
    bin_root = program_files() / "Windows Kits" / "10" / "bin"
    try:
        relative = Path(os.path.normcase(os.path.abspath(path))).relative_to(os.path.normcase(os.path.abspath(bin_root)))
    except ValueError:
        raise AuthenticodeError(f"signtool must be under {bin_root}; PATH and other copies are never used.") from None
    parts = relative.parts
    if len(parts) != 3 or not _SDK_VERSION.fullmatch(parts[0]) or parts[1] != "x64" or parts[2] != "signtool.exe":
        raise AuthenticodeError(f"signtool must be {bin_root}\\<10.0.x.y>\\x64\\signtool.exe.")
    try:
        signer = trust(path)
    except PrerequisiteError as error:
        raise AuthenticodeError(f"signtool has no valid Authenticode signature: {error}") from None
    if signer.get("subject") != "Microsoft Corporation":
        raise AuthenticodeError(f"signtool is signed by {signer.get('subject')!r}, not Microsoft Corporation.")
    return {"path": str(path), "sha256": desktop_installer_verify.sha256_file(path), "signer": signer["subject"],
            "signerThumbprintSha1": signer.get("thumbprintSha1", "")}


def check_certificate(config: dict[str, Any], *,
                      facts: Callable[[bytes], dict[str, Any]] = certificate_facts) -> dict[str, Any]:
    path = _plain_file(Path(config["certificate"]), "certificate file")
    data = path.read_bytes()
    if len(data) > _MAX_CERTIFICATE_BYTES:
        raise AuthenticodeError("The certificate file is too large to be a public certificate.")
    if b"PRIVATE KEY" in data or path.suffix.lower() in {".pfx", ".p12", ".pvk", ".key"}:
        raise AuthenticodeError("Give the PUBLIC certificate (.cer, DER); a private key never belongs in this config.")
    found = facts(data)
    problems = certificate_problems(found, config["thumbprintSha1"])
    if problems:
        raise AuthenticodeError("The signing certificate is refused: " + "; ".join(problems) + ".")
    return {key: found[key] for key in ("sha1", "subject", "issuer", "notBefore", "notAfter", "chain", "rootStore")}


Runner = Callable[[list[str], float], tuple[int, str]]


def run_signtool(argv: list[str], timeout: float) -> tuple[int, str]:
    """Runs signtool directly (no shell), bounded; returns the exit code and its bounded output."""
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, shell=False,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 1, f"signtool did not finish within {timeout:.0f} s"
    except OSError as error:
        return 1, f"signtool could not be started ({error.__class__.__name__})"
    output = (done.stdout + done.stderr)[:_MAX_OUTPUT].decode("utf-8", "replace")
    return done.returncode, output


def _artefact(path: Path, root: Path) -> Path:
    path = _plain_file(Path(path), "installer")
    version = release_signing.product_version(root)
    expected = f"qronos-desktop_{version}_x64-setup.exe"
    if path.name != expected:
        raise AuthenticodeError(f"The installer must be {expected} for product version {version}.")
    return path


def verify_signed(config: dict[str, Any], artefact: Path, *, root: Path = ROOT, runner: Runner = run_signtool,
                  tool: Callable[[Path], dict[str, str]] = check_signtool,
                  facts: Callable[[bytes], dict[str, Any]] = certificate_facts,
                  trust: Callable[[Path], dict[str, str]] = winverifytrust) -> dict[str, Any]:
    """The artefact as it is NOW: every signature valid and time-stamped, signed by the configured certificate."""
    signtool = tool(Path(config["signtool"]))
    certificate = check_certificate(config, facts=facts)
    path = _artefact(artefact, root)
    before = desktop_installer_verify.sha256_file(path)
    code, output = runner([signtool["path"], "verify", "/pa", "/all", "/tw", "/v", str(path)], VERIFY_TIMEOUT_SECONDS)
    if code != 0:
        raise AuthenticodeError(f"signtool verify /pa /all /tw did not succeed (exit {code}; 2 means a warning such "
                                "as a missing time stamp). Nothing is claimed.")
    try:
        signer = trust(path)
    except PrerequisiteError as error:
        raise AuthenticodeError(f"Windows does not accept the installer's signature: {error}") from None
    if signer.get("thumbprintSha1", "").upper() != config["thumbprintSha1"].upper():
        raise AuthenticodeError("The installer is not signed by the configured certificate.")
    if desktop_installer_verify.sha256_file(path) != before:
        raise AuthenticodeError("The installer changed while it was being verified.")
    return {"artifact": path.name, "sha256": before, "signer": signer, "certificate": certificate,
            "signtool": signtool, "verifyExit": code,
            "verifyOutputSha256": hashlib.sha256(output.encode("utf-8")).hexdigest()}


def _build_provenance(path: Path, artefact: Path, unsigned_sha256: str, root: Path) -> dict[str, Any]:
    record_path = _plain_file(Path(path), "build provenance")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    installer = record.get("installer", {}) if isinstance(record, dict) else {}
    if installer.get("fileName") != artefact.name or installer.get("sha256") != unsigned_sha256 \
            or installer.get("signed") is not False:
        raise AuthenticodeError("The build provenance does not describe this exact unsigned installer.")
    if record.get("attributableToDesktopSource") is not True:
        raise AuthenticodeError("The installer was not built from a clean source commit; a release is never signed "
                                "from an unattributable build.")
    now = desktop_installer_verify.source_state(root)
    if not now["clean"] or now["commit"] != record.get("desktopSource", {}).get("commit"):
        raise AuthenticodeError("The checked-out source is not the clean commit the installer was built from.")
    return {"path": str(record_path), "sha256": desktop_installer_verify.sha256_file(record_path),
            "sourceCommit": now["commit"]}


def sign(config: dict[str, Any], artefact: Path, build_provenance: Path, *, root: Path = ROOT,
         runner: Runner = run_signtool, tool: Callable[[Path], dict[str, str]] = check_signtool,
         facts: Callable[[bytes], dict[str, Any]] = certificate_facts,
         trust: Callable[[Path], dict[str, str]] = winverifytrust) -> Path:
    """Sign the exact built installer, verify every signature, and write its provenance beside it."""
    signtool = tool(Path(config["signtool"]))
    check_certificate(config, facts=facts)
    path = _artefact(artefact, root)
    record = path.with_name(path.name + ".authenticode.json")
    if os.path.lexists(record):
        raise AuthenticodeError(f"{record.name} already exists; this installer was already signed or attempted.")
    unsigned = desktop_installer_verify.sha256_file(path)
    build = _build_provenance(build_provenance, path, unsigned, root)
    code, _ = runner([signtool["path"], "sign", "/fd", "sha256", "/sha1", config["thumbprintSha1"].upper(),
                      "/tr", config["timestampUrl"], "/td", "sha256", str(path)], SIGN_TIMEOUT_SECONDS)
    if code != 0:
        raise AuthenticodeError(f"signtool sign did not succeed (exit {code}). The installer may be partly changed; "
                                "rebuild it before trying again.")
    verified = verify_signed(config, path, root=root, runner=runner, tool=lambda _: signtool, facts=facts,
                             trust=trust)
    if verified["sha256"] == unsigned:
        raise AuthenticodeError("signtool reported success but the installer is unchanged.")
    provenance = {"schema": 1, "artifact": path.name, "unsignedSha256": unsigned, "signedSha256": verified["sha256"],
                  "build": build, "signtool": signtool, "certificate": verified["certificate"],
                  "signer": verified["signer"], "timestampUrl": config["timestampUrl"],
                  "verify": {"command": "signtool verify /pa /all /tw /v", "exit": verified["verifyExit"],
                             "outputSha256": verified["verifyOutputSha256"], "winVerifyTrust": "valid (offline)"},
                  "notVerifiedHere": ["certificate revocation (offline build check)",
                                      "that an enterprise root is a public certificate authority (human review of "
                                      "the listed chain)"]}
    with open(record, "x", encoding="utf-8", newline="\n") as handle:  # never replaces a record
        handle.write(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    return record


# ------------------------------------------------------------------------ CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "sign", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        if name != "check":
            command.add_argument("--artifact", required=True, type=Path)
        if name == "sign":
            command.add_argument("--build-provenance", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        if arguments.command == "check":
            result: dict[str, Any] = {"ok": True, "signtool": check_signtool(Path(config["signtool"])),
                                      "certificate": check_certificate(config)}
        elif arguments.command == "sign":
            result = {"ok": True, "provenance": str(sign(config, arguments.artifact, arguments.build_provenance))}
        else:
            result = {"ok": True, "verified": verify_signed(config, arguments.artifact)}
    except (AuthenticodeError, release_signing.SigningError, local_update.UpdateError, OSError, ValueError) as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
