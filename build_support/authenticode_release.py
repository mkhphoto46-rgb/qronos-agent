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
  chain, built offline as ONE simple chain, passes the Authenticode chain
  policy and ENDS IN A PINNED PUBLIC ROOT: the terminal certificate's DER
  SHA-256 and SHA-1 must equal an anchor in config/authenticode_public_roots.json,
  each recorded with its review provenance. Membership in any Windows store,
  an issuer name or the chain policy alone is never public trust: an
  enterprise, private or self-signed root is refused. The anchor set ships
  EMPTY, so today every certificate is refused.
* The artefact is a plain file named qronos-desktop_<authority version>_x64-
  setup.exe. Its unsigned SHA-256 equals the build provenance's (written by
  build_support.build_desktop), and that provenance is attributable to a clean
  source commit, the commit checked out now.
* After signing, and again whenever the release gate is asked: ``signtool
  verify /pa /all /tw`` exits 0 (all signatures valid, time-stamped), and
  WinVerifyTrust (offline) accepts EVERY signature on the file (primary and
  secondary), each signer chain Windows built ends in a pinned anchor, and the
  primary signer is the configured certificate.

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
from build_support.desktop_prerequisites import PrerequisiteError, winverifytrust  # noqa: E402  (signtool's own signer)
from core import local_update  # noqa: E402

CODE_SIGNING_EKU = "1.3.6.1.5.5.7.3.3"
#: Reviewed public root certificates a release chain may end in. Ships EMPTY (no reviewed anchor yet).
ANCHORS_PATH = ROOT / "config" / "authenticode_public_roots.json"
ANCHOR_FIELDS = frozenset({"subject", "sha256", "sha1", "provenance"})
PROVENANCE_FIELDS = frozenset({"source", "reviewedBy", "reviewedAt", "evidence"})
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


def _element(subject: str, der: bytes) -> dict[str, str]:
    return {"subject": subject, "sha1": hashlib.sha1(der).hexdigest().upper(),
            "sha256": hashlib.sha256(der).hexdigest()}


def _chain_facts(c: Any, certificate: Any) -> dict[str, Any]:
    oid = ctypes.c_char_p(CODE_SIGNING_EKU.encode("ascii"))
    para = _ChainPara(ctypes.sizeof(_ChainPara),
                      _UsageMatch(0, _EnhancedKeyUsage(1, ctypes.pointer(oid))))  # USAGE_MATCH_TYPE_AND
    chain = ctypes.POINTER(_ChainContext)()
    flags = _CHAIN_CACHE_ONLY_URL_RETRIEVAL | _CHAIN_DISABLE_AUTH_ROOT_AUTO_UPDATE  # offline; no revocation flag
    if not c.CertGetCertificateChain(None, certificate, None, None, ctypes.byref(para), flags, None,
                                     ctypes.byref(chain)):
        return {"chain": [], "chainPolicyError": 0xFFFFFFFF, "simpleChains": 0}
    try:
        status = _PolicyStatus(ctypes.sizeof(_PolicyStatus))
        policy = _PolicyPara(ctypes.sizeof(_PolicyPara), _POLICY_IGNORE_ALL_REV_UNKNOWN, None)
        if not c.CertVerifyCertificateChainPolicy(ctypes.c_void_p(_POLICY_AUTHENTICODE), chain,
                                                  ctypes.byref(policy), ctypes.byref(status)):
            status.dwError = 0xFFFFFFFF
        elements: list[dict[str, str]] = []
        if chain.contents.cChain:
            simple = chain.contents.rgpChain[0].contents
            for index in range(simple.cElement):
                element = simple.rgpElement[index].contents.pCertContext
                encoded = ctypes.string_at(element.contents.pbCertEncoded, element.contents.cbCertEncoded)
                elements.append(_element(_name(c, element, False), encoded))
        return {"chain": elements, "chainPolicyError": int(status.dwError),
                "simpleChains": int(chain.contents.cChain)}
    finally:
        c.CertFreeCertificateChain(chain)


# ------------------------------------------------------------- the decisions


def load_anchors(path: Path = ANCHORS_PATH) -> dict[str, dict[str, Any]]:
    """The reviewed public root anchors, by DER SHA-256. Fails closed: empty, missing or malformed refuses all."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthenticodeError(f"The public trust anchor list cannot be read ({error.__class__.__name__}); "
                                "nothing is trusted.") from None
    if not isinstance(data, dict) or set(data) - {"comment"} != {"schema", "anchors"} or data["schema"] != 1 \
            or not isinstance(data["anchors"], list):
        raise AuthenticodeError("The public trust anchor list needs schema 1 and an anchors list; nothing is trusted.")
    anchors: dict[str, dict[str, Any]] = {}
    for anchor in data["anchors"]:
        provenance = anchor.get("provenance") if isinstance(anchor, dict) else None
        if not isinstance(anchor, dict) or set(anchor) != ANCHOR_FIELDS \
                or not isinstance(anchor["subject"], str) or not anchor["subject"].strip() \
                or not isinstance(anchor["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", anchor["sha256"]) \
                or not isinstance(anchor["sha1"], str) or not re.fullmatch(r"[0-9A-F]{40}", anchor["sha1"]) \
                or not isinstance(provenance, dict) or set(provenance) != PROVENANCE_FIELDS \
                or not all(isinstance(provenance[key], str) and provenance[key].strip() for key in PROVENANCE_FIELDS) \
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", provenance["reviewedAt"]):
            raise AuthenticodeError("A public trust anchor entry is incomplete or malformed (subject, sha256, sha1 "
                                    "and review provenance are required); nothing is trusted.")
        if anchor["sha256"] in anchors:
            raise AuthenticodeError("A public trust anchor is listed twice; nothing is trusted.")
        anchors[anchor["sha256"]] = anchor
    if not anchors:
        raise AuthenticodeError("No reviewed public trust anchor is configured (config/authenticode_public_roots.json "
                                "is empty), so no release can be signed or verified.")
    return anchors


def pinned_anchor(chain: list[dict[str, str]], anchors: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The anchor the chain's terminal certificate IS (DER SHA-256 and SHA-1 both match), or None."""
    if not chain:
        return None
    terminal = chain[-1]
    anchor = anchors.get(str(terminal.get("sha256", "")).lower())
    if anchor is None or str(terminal.get("sha1", "")).upper() != anchor["sha1"]:
        return None
    return anchor


def certificate_problems(facts: dict[str, Any], thumbprint: str,
                         anchors: dict[str, dict[str, Any]]) -> list[str]:
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
    if facts.get("simpleChains") != 1:
        problems.append("Windows built an alternate (multi-part) chain; only one simple chain is accepted")
    if facts.get("chainPolicyError") != 0:
        problems.append(f"the chain fails the Authenticode chain policy (0x{facts.get('chainPolicyError', 0):08X})")
    if pinned_anchor(chain, anchors) is None:
        problems.append("the chain does not end in a reviewed, pinned public root (an enterprise, private or "
                        "unlisted root is never public trust)")
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


class _SignatureSettings(ctypes.Structure):  # WINTRUST_SIGNATURE_SETTINGS
    _fields_ = [("cbStruct", wintypes.DWORD), ("dwIndex", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("cSecondarySigs", wintypes.DWORD), ("dwVerifiedSigIndex", wintypes.DWORD),
                ("pCryptoPolicy", ctypes.c_void_p)]


class _FileInfo(ctypes.Structure):  # WINTRUST_FILE_INFO
    _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR), ("hFile", wintypes.HANDLE),
                ("pgKnownSubject", ctypes.c_void_p)]


class _TrustData(ctypes.Structure):  # WINTRUST_DATA
    _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                ("pFile", ctypes.POINTER(_FileInfo)), ("dwStateAction", wintypes.DWORD),
                ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPCWSTR),
                ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
                ("pSignatureSettings", ctypes.POINTER(_SignatureSettings))]


class _ProviderSigner(ctypes.Structure):  # CRYPT_PROVIDER_SGNR, leading fields
    _fields_ = [("cbStruct", wintypes.DWORD), ("sftVerifyAsOf", wintypes.FILETIME), ("csCertChain", wintypes.DWORD),
                ("pasCertChain", ctypes.c_void_p)]


class _ProviderCert(ctypes.Structure):  # CRYPT_PROVIDER_CERT, leading fields (stride = its own cbStruct)
    _fields_ = [("cbStruct", wintypes.DWORD), ("pCert", ctypes.POINTER(_CertContext))]


_WSS_VERIFY_SPECIFIC = 0x1
_WSS_GET_SECONDARY_SIG_COUNT = 0x2
_MAX_SIGNATURES = 8


def signature_chains(path: Path) -> list[dict[str, Any]]:
    """Every Authenticode signature on ``path`` as Windows verifies it (offline): its signer and the chain built.

    Raises AuthenticodeError when Windows rejects any signature. Index 0 is the
    primary signature; the rest are the secondary (nested) ones.
    """
    if os.name != "nt":
        raise AuthenticodeError("Authenticode verification runs on Windows only.")

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    action = GUID(0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    wintrust = ctypes.WinDLL("wintrust")
    wintrust.WinVerifyTrust.argtypes = (wintypes.HANDLE, ctypes.POINTER(GUID), ctypes.c_void_p)
    wintrust.WinVerifyTrust.restype = wintypes.LONG
    wintrust.WTHelperProvDataFromStateData.argtypes = (wintypes.HANDLE,)
    wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    wintrust.WTHelperGetProvSignerFromChain.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                                        wintypes.DWORD)
    wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(_ProviderSigner)
    c = _crypt32()

    def verify(index: int, flags: int) -> tuple[int, dict[str, Any]]:
        file_info = _FileInfo(ctypes.sizeof(_FileInfo), str(path), None, None)
        settings = _SignatureSettings(ctypes.sizeof(_SignatureSettings), index, flags, 0, 0, None)
        data = _TrustData()
        data.cbStruct = ctypes.sizeof(_TrustData)
        data.dwUIChoice = 2              # WTD_UI_NONE
        data.fdwRevocationChecks = 0     # WTD_REVOKE_NONE
        data.dwUnionChoice = 1           # WTD_CHOICE_FILE
        data.pFile = ctypes.pointer(file_info)
        data.dwStateAction = 1           # WTD_STATEACTION_VERIFY
        data.dwProvFlags = 0x1000 | 0x10  # WTD_CACHE_ONLY_URL_RETRIEVAL | WTD_REVOCATION_CHECK_NONE
        data.pSignatureSettings = ctypes.pointer(settings)
        result = wintrust.WinVerifyTrust(wintypes.HANDLE(-1), ctypes.byref(action), ctypes.byref(data))
        found: dict[str, Any] = {"index": index, "secondary": int(settings.cSecondarySigs), "chain": []}
        try:
            if result == 0:
                provider = wintrust.WTHelperProvDataFromStateData(data.hWVTStateData)
                signer = wintrust.WTHelperGetProvSignerFromChain(provider, 0, False, 0) if provider else None
                if signer and signer.contents.csCertChain and signer.contents.pasCertChain:
                    base = signer.contents.pasCertChain
                    stride = _ProviderCert.from_address(base).cbStruct
                    for position in range(signer.contents.csCertChain):
                        entry = _ProviderCert.from_address(base + position * stride)
                        encoded = ctypes.string_at(entry.pCert.contents.pbCertEncoded,
                                                   entry.pCert.contents.cbCertEncoded)
                        found["chain"].append(_element(_name(c, entry.pCert, False), encoded))
        finally:
            data.dwStateAction = 2       # WTD_STATEACTION_CLOSE
            wintrust.WinVerifyTrust(wintypes.HANDLE(-1), ctypes.byref(action), ctypes.byref(data))
        return result, found

    result, primary = verify(0, _WSS_VERIFY_SPECIFIC | _WSS_GET_SECONDARY_SIG_COUNT)
    if result != 0 or not primary["chain"]:
        raise AuthenticodeError(f"Windows does not accept the primary signature (WinVerifyTrust "
                                f"0x{result & 0xFFFFFFFF:08X}).")
    if primary["secondary"] > _MAX_SIGNATURES:
        raise AuthenticodeError("The installer carries more signatures than a release may have.")
    signatures = [primary]
    for index in range(1, primary["secondary"] + 1):
        result, secondary = verify(index, _WSS_VERIFY_SPECIFIC)
        if result != 0 or not secondary["chain"]:
            raise AuthenticodeError(f"Windows does not accept signature {index} (WinVerifyTrust "
                                    f"0x{result & 0xFFFFFFFF:08X}).")
        signatures.append(secondary)
    return signatures


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


def check_certificate(config: dict[str, Any], *, facts: Callable[[bytes], dict[str, Any]] = certificate_facts,
                      anchors: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    anchors = load_anchors() if anchors is None else anchors
    path = _plain_file(Path(config["certificate"]), "certificate file")
    data = path.read_bytes()
    if len(data) > _MAX_CERTIFICATE_BYTES:
        raise AuthenticodeError("The certificate file is too large to be a public certificate.")
    if b"PRIVATE KEY" in data or path.suffix.lower() in {".pfx", ".p12", ".pvk", ".key"}:
        raise AuthenticodeError("Give the PUBLIC certificate (.cer, DER); a private key never belongs in this config.")
    found = facts(data)
    problems = certificate_problems(found, config["thumbprintSha1"], anchors)
    if problems:
        raise AuthenticodeError("The signing certificate is refused: " + "; ".join(problems) + ".")
    anchor = pinned_anchor(found["chain"], anchors)
    return {**{key: found[key] for key in ("sha1", "subject", "issuer", "notBefore", "notAfter", "chain")},
            "anchor": {"subject": anchor["subject"], "sha256": anchor["sha256"],
                       "provenance": anchor["provenance"]}}


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
                  chains: Callable[[Path], list[dict[str, Any]]] = signature_chains,
                  anchors: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """The artefact as it is NOW: every signature valid, time-stamped and ending in a pinned public root."""
    anchors = load_anchors() if anchors is None else anchors
    signtool = tool(Path(config["signtool"]))
    certificate = check_certificate(config, facts=facts, anchors=anchors)
    path = _artefact(artefact, root)
    before = desktop_installer_verify.sha256_file(path)
    code, output = runner([signtool["path"], "verify", "/pa", "/all", "/tw", "/v", str(path)], VERIFY_TIMEOUT_SECONDS)
    if code != 0:
        raise AuthenticodeError(f"signtool verify /pa /all /tw did not succeed (exit {code}; 2 means a warning such "
                                "as a missing time stamp). Nothing is claimed.")
    signatures = chains(path)
    if not signatures:
        raise AuthenticodeError("Windows reported no signature on the installer.")
    for signature in signatures:
        if pinned_anchor(signature.get("chain", []), anchors) is None:
            raise AuthenticodeError(f"Signature {signature.get('index')} on the installer does not end in a reviewed, "
                                    "pinned public root.")
    signer = signatures[0]["chain"][0]
    if signer.get("sha1", "").upper() != config["thumbprintSha1"].upper():
        raise AuthenticodeError("The installer is not signed by the configured certificate.")
    if desktop_installer_verify.sha256_file(path) != before:
        raise AuthenticodeError("The installer changed while it was being verified.")
    return {"artifact": path.name, "sha256": before, "signer": {"subject": signer.get("subject"),
                                                                "thumbprintSha1": signer.get("sha1")},
            "signatures": [{"index": s.get("index"), "root": s["chain"][-1]} for s in signatures],
            "certificate": certificate, "signtool": signtool, "verifyExit": code,
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
         chains: Callable[[Path], list[dict[str, Any]]] = signature_chains,
         anchors: dict[str, dict[str, Any]] | None = None) -> Path:
    """Sign the exact built installer, verify every signature, and write its provenance beside it."""
    anchors = load_anchors() if anchors is None else anchors
    signtool = tool(Path(config["signtool"]))
    check_certificate(config, facts=facts, anchors=anchors)
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
                             chains=chains, anchors=anchors)
    if verified["sha256"] == unsigned:
        raise AuthenticodeError("signtool reported success but the installer is unchanged.")
    provenance = {"schema": 1, "artifact": path.name, "unsignedSha256": unsigned, "signedSha256": verified["sha256"],
                  "build": build, "signtool": signtool, "certificate": verified["certificate"],
                  "signer": verified["signer"], "signatures": verified["signatures"],
                  "timestampUrl": config["timestampUrl"],
                  "verify": {"command": "signtool verify /pa /all /tw /v", "exit": verified["verifyExit"],
                             "outputSha256": verified["verifyOutputSha256"], "winVerifyTrust": "valid (offline)"},
                  "notVerifiedHere": ["certificate revocation (offline build check)"]}
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
