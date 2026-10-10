"""Owner update key and signed update manifests, kept outside Git (docs/local-update-packages.md).

Commands (run by the owner, on their own Windows account; see the docs for the
reviewed step-by-step procedure):

  create-key --key-file PATH --owner NAME
      Generates an Ed25519 owner key. The private key is protected with Windows
      DPAPI for the current user and written to a NEW file outside every Git
      checkout, created with an access list that admits the current user alone
      (set in the same call that creates the file). Only the public
      trusted-key entry is printed.
  public-entry --key-file PATH
      Prints the entry for config/update_trusted_keys.json (public key only),
      after proving custody: the key file must unlock on this account to the
      private key of the public key it lists.
  sign --key-file PATH --installer EXE --security-floor VERSION
       [--minimum-version VERSION] [--rollback] [--notes-file TXT]
      Writes EXE.qronos-update.json (payload schema 2) beside the installer.
      The version comes from the product version authority
      (desktop/src-tauri/tauri.conf.json), which must agree with the other
      version fields and with the installer's file name; data formats come
      from core.update_stores. The result is verified before it is kept.
  preflight [--authenticode-config JSON --artifact EXE]
      Reports the release gates (owner key configured, version authority,
      Authenticode signing) and exits non-zero while any is closed. The
      Authenticode gate opens only when the given built installer verifies now
      (build_support/authenticode_release.py).

The private key never appears on the command line, in the environment, in
output, logs or reports; it exists decrypted only in this process's memory.
Nothing here reaches the network, installs a certificate or touches a trust store.

Limits: DPAPI CurrentUser protects against other accounts and offline copies,
not against software running as the owner's own account. Wiping is best effort:
the ctypes buffers and byte arrays this module controls are zeroed, but Python's
own copies of bytes objects cannot be. Without a public-CA code-signing
certificate and a signed installer that verifies now, the Authenticode gate
stays closed.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import ed25519, local_update, update_stores  # noqa: E402
from core.local_update import UpdateError  # noqa: E402

KEY_FILE_KIND = "qronos-owner-update-key"
DPAPI_ENTROPY = b"Qronos owner update key v1"
TAURI_CONFIG = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
PACKAGE_JSON = ROOT / "desktop" / "package.json"
CARGO_TOML = ROOT / "desktop" / "src-tauri" / "Cargo.toml"
TRUSTED_KEYS = ROOT / "config" / "update_trusted_keys.json"
_KEY_FILE_LIMIT = 16 * 1024
_OWNER = re.compile(r"[\w .,'()-]{1,80}")


class SigningError(Exception):
    """A release-signing step was refused; the message says why and what to do."""


# ------------------------------------------------------- Win32 (exact signatures)
#
# Every call is bound with its documented argument and result types, so no
# pointer or handle is truncated on 64-bit Windows. No external program (whoami,
# icacls) is used: the account comes from this process's token, and access lists
# are set at creation and read through the open handle.


class _Blob(ctypes.Structure):  # DATA_BLOB
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class _SecurityAttributes(ctypes.Structure):  # SECURITY_ATTRIBUTES
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class _SidAndAttributes(ctypes.Structure):  # SID_AND_ATTRIBUTES, the head of TOKEN_USER
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _ByHandleFileInformation(ctypes.Structure):  # BY_HANDLE_FILE_INFORMATION
    _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD)]


class _Win32:
    """Lazily bound kernel32/advapi32/crypt32 entry points."""

    _bound: "_Win32 | None" = None

    def __init__(self) -> None:
        if os.name != "nt":
            raise SigningError("The owner key is protected with Windows DPAPI; use the owner's Windows account.")
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        c = ctypes.WinDLL("crypt32", use_last_error=True)
        HANDLE, DWORD, BOOL, LPCWSTR = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR
        PVOID = ctypes.c_void_p

        def bind(function, argtypes, restype):
            function.argtypes, function.restype = argtypes, restype
            return function

        self.LocalFree = bind(k.LocalFree, [PVOID], PVOID)
        self.CloseHandle = bind(k.CloseHandle, [HANDLE], BOOL)
        self.GetCurrentProcess = bind(k.GetCurrentProcess, [], HANDLE)
        self.CreateFileW = bind(k.CreateFileW, [LPCWSTR, DWORD, DWORD, ctypes.POINTER(_SecurityAttributes), DWORD,
                                                DWORD, HANDLE], HANDLE)
        self.WriteFile = bind(k.WriteFile, [HANDLE, ctypes.c_char_p, DWORD, ctypes.POINTER(DWORD), PVOID], BOOL)
        self.ReadFile = bind(k.ReadFile, [HANDLE, PVOID, DWORD, ctypes.POINTER(DWORD), PVOID], BOOL)
        self.FlushFileBuffers = bind(k.FlushFileBuffers, [HANDLE], BOOL)
        self.GetFinalPathNameByHandleW = bind(k.GetFinalPathNameByHandleW, [HANDLE, wintypes.LPWSTR, DWORD, DWORD],
                                              DWORD)
        self.GetFileInformationByHandle = bind(k.GetFileInformationByHandle,
                                               [HANDLE, ctypes.POINTER(_ByHandleFileInformation)], BOOL)
        self.SetFileInformationByHandle = bind(k.SetFileInformationByHandle, [HANDLE, ctypes.c_int, PVOID, DWORD],
                                               BOOL)
        self.OpenProcessToken = bind(a.OpenProcessToken, [HANDLE, DWORD, ctypes.POINTER(HANDLE)], BOOL)
        self.GetTokenInformation = bind(a.GetTokenInformation, [HANDLE, ctypes.c_int, PVOID, DWORD,
                                                                ctypes.POINTER(DWORD)], BOOL)
        self.ConvertSidToStringSidW = bind(a.ConvertSidToStringSidW, [PVOID, ctypes.POINTER(wintypes.LPWSTR)], BOOL)
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW = bind(
            a.ConvertStringSecurityDescriptorToSecurityDescriptorW,
            [LPCWSTR, DWORD, ctypes.POINTER(PVOID), ctypes.POINTER(wintypes.ULONG)], BOOL)
        self.ConvertSecurityDescriptorToStringSecurityDescriptorW = bind(
            a.ConvertSecurityDescriptorToStringSecurityDescriptorW,
            [PVOID, DWORD, DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG)], BOOL)
        self.GetSecurityInfo = bind(a.GetSecurityInfo, [HANDLE, ctypes.c_int, DWORD, ctypes.POINTER(PVOID),
                                                        ctypes.POINTER(PVOID), ctypes.POINTER(PVOID),
                                                        ctypes.POINTER(PVOID), ctypes.POINTER(PVOID)], DWORD)
        self.CryptProtectData = bind(c.CryptProtectData, [ctypes.POINTER(_Blob), LPCWSTR, ctypes.POINTER(_Blob),
                                                          PVOID, PVOID, DWORD, ctypes.POINTER(_Blob)], BOOL)
        self.CryptUnprotectData = bind(c.CryptUnprotectData, [ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR),
                                                              ctypes.POINTER(_Blob), PVOID, PVOID, DWORD,
                                                              ctypes.POINTER(_Blob)], BOOL)

    @classmethod
    def get(cls) -> "_Win32":
        if cls._bound is None:
            cls._bound = cls()
        return cls._bound


_INVALID_HANDLE = ctypes.c_void_p(-1).value
_GENERIC_READ, _GENERIC_WRITE, _DELETE, _READ_CONTROL = 0x80000000, 0x40000000, 0x00010000, 0x00020000
_OPEN_REPARSE_POINT = 0x00200000
_REPARSE_ATTRIBUTE = 0x400


def _blob(data: bytes) -> tuple[_Blob, Any]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi(data: bytes, protect: bool) -> bytes:
    win = _Win32.get()
    source, keep_source = _blob(data)
    entropy, keep_entropy = _blob(DPAPI_ENTROPY)
    result = _Blob()
    # CRYPTPROTECT_UI_FORBIDDEN: never show a prompt.
    if protect:
        ok = win.CryptProtectData(ctypes.byref(source), "Qronos owner update key", ctypes.byref(entropy), None, None,
                                  0x1, ctypes.byref(result))
    else:
        ok = win.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1,
                                    ctypes.byref(result))
    # Best effort: zero the ctypes copy of the input (the plaintext key when protecting).
    # Python's own bytes objects cannot be wiped; see the module documentation.
    ctypes.memset(keep_source, 0, len(data))
    del keep_source, keep_entropy
    if not ok:
        raise SigningError("Windows could not " + ("protect" if protect else "unlock") + " the owner key "
                           "(DPAPI). It can be unlocked only by the Windows account that created it.")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.memset(result.pbData, 0, result.cbData)
        win.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def current_user_sid() -> str:
    """The SID of the account this process runs as, from its own token."""
    win = _Win32.get()
    token = wintypes.HANDLE()
    if not win.OpenProcessToken(win.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise SigningError("Could not determine the current Windows account.")
    try:
        needed = wintypes.DWORD()
        win.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))  # TokenUser: ask for the size
        buffer = ctypes.create_string_buffer(needed.value)
        if not win.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise SigningError("Could not determine the current Windows account.")
        user = _SidAndAttributes.from_buffer(buffer)
        text = wintypes.LPWSTR()
        if not win.ConvertSidToStringSidW(user.Sid, ctypes.byref(text)):
            raise SigningError("Could not determine the current Windows account.")
        try:
            sid = text.value
        finally:
            win.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        win.CloseHandle(token)
    if not re.fullmatch(r"S-1-5-21(-\d+){4}", sid or ""):
        raise SigningError("The owner key must belong to an ordinary Windows user account.")
    return sid


def _handle_sddl(handle: Any) -> str:
    win = _Win32.get()
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    # SE_FILE_OBJECT; OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    if win.GetSecurityInfo(handle, 1, 0x1 | 0x4, ctypes.byref(owner), None, ctypes.byref(dacl), None,
                           ctypes.byref(descriptor)):
        raise SigningError("Could not read the key file's access list.")
    text = wintypes.LPWSTR()
    try:
        if not win.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 0x1 | 0x4,
                                                                        ctypes.byref(text), None):
            raise SigningError("Could not read the key file's access list.")
        try:
            return text.value or ""
        finally:
            win.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        win.LocalFree(descriptor)


def _check_handle(handle: Any, sid: str) -> None:
    """Through the open handle: a plain single-link file owned by and accessible only to ``sid``."""
    win = _Win32.get()
    info = _ByHandleFileInformation()
    if not win.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise SigningError("Could not inspect the key file.")
    if info.dwFileAttributes & (_REPARSE_ATTRIBUTE | 0x10) or info.nNumberOfLinks != 1:
        raise SigningError("The key file is a link, a folder or has another hard link.")
    sddl = _handle_sddl(handle)
    owner = re.match(r"O:(.+?)(?=G:|D:|S:|$)", sddl)
    dacl = sddl[sddl.index("D:"):] if "D:" in sddl else ""
    aces = re.findall(r"\(([^)]*)\)", dacl)
    if owner is None or owner.group(1) != sid or not dacl.startswith("D:P") or not aces \
            or any(ace.split(";")[0] != "A" or ace.split(";")[-1] != sid for ace in aces):
        raise SigningError("The key file must be owned by and accessible only to your Windows account. Create a "
                           "new key file; do not loosen its permissions.")


def _open(path: Path, access: int, share: int, disposition: int,
          attributes: _SecurityAttributes | None = None) -> Any:
    win = _Win32.get()
    handle = win.CreateFileW(str(path), access, share, ctypes.byref(attributes) if attributes else None,
                             disposition, _OPEN_REPARSE_POINT, None)
    if handle in (None, _INVALID_HANDLE):
        error = ctypes.get_last_error()
        if error == 80:  # ERROR_FILE_EXISTS
            raise SigningError(f"{path} already exists; an owner key is never overwritten.")
        raise SigningError(f"{path.name} could not be opened (Windows error {error}).")
    return handle


def _final_path(handle: Any) -> str:
    win = _Win32.get()
    buffer = ctypes.create_unicode_buffer(32768)
    length = win.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)  # FILE_NAME_NORMALIZED, VOLUME_NAME_DOS
    if not 0 < length < len(buffer):
        raise SigningError("Windows could not say which file the key file handle refers to.")
    text = buffer.value
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    return text[4:] if text.startswith("\\\\?\\") else text


def _check_final_path(handle: Any, intended: Path) -> None:
    """The open handle must be the intended file: no ancestor was swapped for a link meanwhile."""
    actual = os.path.normcase(os.path.normpath(_final_path(handle)))
    if actual != os.path.normcase(os.path.normpath(str(Path(os.path.abspath(intended))))):
        raise SigningError("The key file that was opened is not at the intended path (a folder on the way "
                           "changed); nothing was used.")


def check_private_access(path: Path, sid: str | None = None) -> None:
    """The key file at ``path``: plain, single-link, owned by and accessible only to the current user."""
    win = _Win32.get()
    handle = _open(path, _GENERIC_READ | _READ_CONTROL, 0x1, 3)  # FILE_SHARE_READ, OPEN_EXISTING
    try:
        _check_handle(handle, sid or current_user_sid())
    finally:
        win.CloseHandle(handle)


def _create_private_file(path: Path, data: bytes, sid: str) -> None:
    """CREATE_NEW with an owner-only, non-inheriting access list from the first instant; deleted on any failure."""
    win = _Win32.get()
    descriptor, size = ctypes.c_void_p(), wintypes.ULONG()
    if not win.ConvertStringSecurityDescriptorToSecurityDescriptorW(f"O:{sid}D:P(A;;FA;;;{sid})", 1,
                                                                     ctypes.byref(descriptor), ctypes.byref(size)):
        raise SigningError("Could not prepare the key file's access list.")
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        handle = _open(path, _GENERIC_WRITE | _DELETE | _READ_CONTROL, 0, 1, attributes)  # no sharing, CREATE_NEW
    finally:
        win.LocalFree(descriptor)
    keep = False
    cleanup_failed = False
    try:
        _check_final_path(handle, path)
        written = wintypes.DWORD()
        if not win.WriteFile(handle, data, len(data), ctypes.byref(written), None) or written.value != len(data) \
                or not win.FlushFileBuffers(handle):
            raise SigningError("The key file could not be written.")
        _check_access_after_create(handle, sid)
        keep = True
    finally:
        if not keep:  # remove exactly this file, through its handle
            disposition = ctypes.c_ubyte(1)
            cleanup_failed = not win.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), 1)
        win.CloseHandle(handle)
        if cleanup_failed:
            # Reported, never silent: the partial file holds a DPAPI-protected key blob.
            raise SigningError(f"Creating the key failed, and the partial key file at {path} could not be removed. "
                               "Delete it yourself before trying again.")


def _check_access_after_create(handle: Any, sid: str) -> None:
    _check_handle(handle, sid)


def _read_private_file(path: Path, sid: str, limit: int) -> bytes:
    """Reads the key file through one handle that was checked first (no swap between check and read)."""
    win = _Win32.get()
    handle = _open(path, _GENERIC_READ | _READ_CONTROL, 0x1, 3)
    buffer = ctypes.create_string_buffer(limit + 1)
    try:
        _check_final_path(handle, path)
        _check_handle(handle, sid)
        read = wintypes.DWORD()
        if not win.ReadFile(handle, buffer, limit + 1, ctypes.byref(read), None):
            raise SigningError("The key file could not be read.")
        if read.value > limit:
            raise SigningError("The key file is too large to be an owner key file.")
        return buffer.raw[:read.value]
    finally:
        ctypes.memset(buffer, 0, limit + 1)
        win.CloseHandle(handle)


# --------------------------------------------------------------- location


def _inside_git(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _check_key_location(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    checkout = _inside_git(path.parent)
    if checkout is not None:
        raise SigningError(f"The key file must be outside every Git checkout; {checkout} is one. Choose a folder in "
                           "your own profile that is not synchronised or shared.")
    for parent in [path.parent, *path.parent.parents]:
        if os.path.lexists(parent) and local_update._is_link(parent):
            raise SigningError(f"{parent} is a link or junction; choose a plain folder for the key.")
    if not path.parent.is_dir():
        raise SigningError(f"The folder {path.parent} does not exist; create it first.")
    return path


# ---------------------------------------------------------------- key file


def _public_entry(public: bytes, owner: str) -> dict[str, Any]:
    return {"keyId": local_update.key_id(public), "publicKey": base64.b64encode(public).decode("ascii"),
            "owner": owner, "revoked": False}


def create_key(key_file: Path, owner: str, *, random: Callable[[int], bytes] = os.urandom) -> dict[str, Any]:
    if not _OWNER.fullmatch(owner):
        raise SigningError("The owner name must be 1 to 80 letters, digits, spaces or . , ' ( ) -.")
    path = _check_key_location(key_file)
    if os.path.lexists(path):
        raise SigningError(f"{path} already exists; an owner key is never overwritten.")
    sid = current_user_sid()
    secret = bytearray(random(32))
    try:
        public = ed25519.public_key(bytes(secret))
        ed25519.check_public_key(public)
        record = {"schema": 1, "kind": KEY_FILE_KIND, "protection": "dpapi-currentuser",
                  "keyId": local_update.key_id(public), "publicKey": base64.b64encode(public).decode("ascii"),
                  "owner": owner, "protected": base64.b64encode(_dpapi(bytes(secret), True)).decode("ascii")}
    finally:
        secret[:] = bytes(len(secret))
    _create_private_file(path, (json.dumps(record, indent=1) + "\n").encode("utf-8"), sid)
    return _public_entry(public, owner)


def _read_key_record(key_file: Path) -> dict[str, Any]:
    path = _check_key_location(key_file)  # outside Git, no link on the way, every time it is used
    data = _read_private_file(path, current_user_sid(), _KEY_FILE_LIMIT)
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SigningError(f"{path.name} is not an owner key file.") from error
    if not isinstance(record, dict) or set(record) != {"schema", "kind", "protection", "keyId", "publicKey", "owner",
                                                       "protected"} \
            or record["schema"] != 1 or record["kind"] != KEY_FILE_KIND or record["protection"] != "dpapi-currentuser":
        raise SigningError(f"{path.name} is not an owner key file.")
    return record


def public_entry(key_file: Path) -> dict[str, Any]:
    """The trusted-key entry, printed only after custody is proved on this account.

    The file's own ``publicKey`` field is not evidence: the protected secret is
    unlocked (DPAPI, this Windows account) and must derive exactly that public
    key. The unlocked secret is wiped before the entry is returned.
    """
    record = _read_key_record(key_file)
    public = base64.b64decode(record["publicKey"], validate=True)
    if local_update.key_id(public) != record["keyId"]:
        raise SigningError("The key file's public key and key id do not match.")
    if not isinstance(record["owner"], str) or not _OWNER.fullmatch(record["owner"]):
        raise SigningError("The key file's owner name is not valid.")
    try:
        ed25519.check_public_key(public)
    except ed25519.SignatureError as error:
        raise SigningError("The key file's public key is not an acceptable Ed25519 key.") from error
    secret = _unlock(record)  # refuses a file whose protected secret is not the key of its public key
    secret[:] = bytes(len(secret))
    return _public_entry(public, record["owner"])


def _unlock(record: dict[str, Any]) -> bytearray:
    secret = bytearray(_dpapi(base64.b64decode(record["protected"], validate=True), False))
    if len(secret) != 32 or ed25519.public_key(bytes(secret)) != base64.b64decode(record["publicKey"]):
        secret[:] = bytes(len(secret))
        raise SigningError("The key file does not unlock to its own public key.")
    return secret


# ---------------------------------------------------------- version authority


def product_version(root: Path = ROOT) -> str:
    """tauri.conf.json is the authority; package.json and Cargo.toml must agree with it."""
    version = json.loads((root / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))["version"]
    package = json.loads((root / "desktop" / "package.json").read_text(encoding="utf-8"))["version"]
    cargo = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (root / "desktop" / "src-tauri" / "Cargo.toml")
                      .read_text(encoding="utf-8"))
    local_update.parse_version(version)
    if package != version or cargo is None or cargo.group(1) != version:
        raise SigningError(f"The product version is not consistent: tauri.conf.json {version}, package.json "
                           f"{package}, Cargo.toml {cargo.group(1) if cargo else 'missing'}.")
    return version


def release_data_stores() -> dict[str, Any]:
    profile = update_stores.current_profile()
    return {spec.store_id: {"writes": profile.stores[spec.store_id],
                            "reads": [spec.reads_from, profile.stores[spec.store_id]]}
            for spec in update_stores.STORES}


# ------------------------------------------------------------------- signing


def sign(key_file: Path, installer: Path, *, security_floor: str, minimum_version: str | None = None,
         rollback: bool = False, notes: str = "", root: Path = ROOT) -> Path:
    installer = Path(os.path.abspath(installer))
    version = product_version(root)
    expected_name = f"qronos-desktop_{version}_x64-setup.exe"
    if installer.name != expected_name:
        raise SigningError(f"The installer must be {expected_name} for product version {version}.")
    if local_update._unsafe_file(installer) or not installer.is_file():
        raise SigningError("The installer is missing, a link or not a plain file.")
    for value in (security_floor, minimum_version):
        if value is not None:
            local_update.parse_version(value)
    if len(notes) > 2000:
        raise SigningError("Release notes are limited to 2000 characters.")
    size, digest = local_update._hash_package(installer)
    payload = {"schema": 2, "product": local_update.PRODUCT, "version": version,
               "architecture": local_update.ARCHITECTURE, "target": local_update.TARGET,
               "package": {"fileName": installer.name, "bytes": size, "sha256": digest},
               "dataStores": release_data_stores(), "compatibilityEpoch": update_stores.COMPATIBILITY_EPOCH,
               "minimumVersion": minimum_version, "securityFloor": security_floor, "rollback": rollback,
               "notes": notes}
    record = _read_key_record(key_file)
    secret = _unlock(record)
    try:
        envelope = local_update.build_envelope(payload, [bytes(secret)])
    finally:
        secret[:] = bytes(len(secret))
    # Checked as Qronos will check it, with only this public key trusted.
    public = base64.b64decode(record["publicKey"])
    keys = {record["keyId"]: local_update.TrustedKey(record["keyId"], public, record["owner"], False)}
    payload_bytes, signer = local_update._authenticate(envelope, keys)
    if signer != record["keyId"] or json.loads(payload_bytes) != payload:
        raise SigningError("The signed manifest did not verify; nothing was written.")
    manifest = installer.with_name(installer.name + local_update.MANIFEST_SUFFIX)
    try:
        handle = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    except FileExistsError:
        raise SigningError(f"{manifest.name} already exists; remove it deliberately before signing again.") from None
    with os.fdopen(handle, "wb") as stream:
        stream.write(envelope)
    return manifest


# ------------------------------------------------------------------ preflight


def _find_signtool() -> Path | None:
    """signtool from the fixed Windows SDK location only; PATH is never searched."""
    windows = Path(os.environ.get("SystemDrive", "C:") + "\\")
    kits = windows / "Program Files (x86)" / "Windows Kits" / "10" / "bin"
    candidates = sorted(kits.glob("*/x64/signtool.exe")) if kits.is_dir() else []
    return candidates[-1] if candidates else None


def _authenticode_gate(root: Path, config: Path, artifact: Path,
                       verify: Callable[..., dict[str, Any]] | None) -> dict[str, Any]:
    """Open only if the built installer verifies NOW (build_support.authenticode_release.verify_signed)."""
    from build_support import authenticode_release  # build tooling; imported only when asked

    try:
        loaded = authenticode_release.load_config(config)
        result = (verify or authenticode_release.verify_signed)(loaded, artifact, root=root)
    except (authenticode_release.AuthenticodeError, SigningError, UpdateError, OSError, ValueError) as error:
        return {"gate": "authenticode", "open": False,
                "detail": f"The installer {artifact.name} did not verify now: {error} A provenance file, a thumbprint "
                          "or a signtool binary is not used as evidence; a self-signed certificate is never a "
                          "substitute."}
    return {"gate": "authenticode", "open": True,
            "detail": f"{result['artifact']} (sha256 {result['sha256']}) verified now: signtool verify /pa /all /tw "
                      f"exit 0 and WinVerifyTrust, signer {result['signer'].get('subject')} "
                      f"{result['signer'].get('thumbprintSha1')}; every signature ends in a reviewed, pinned public "
                      "root (config/authenticode_public_roots.json). Revocation was not checked offline."}


def preflight(root: Path = ROOT, *, signtool: Callable[[], Path | None] = _find_signtool,
              certificate: str | None = None, authenticode_config: Path | None = None,
              artifact: Path | None = None,
              authenticode_verify: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """The release gates, each open or closed with the exact action that would open it.

    The Authenticode gate is checked only against a built installer given with
    its Authenticode config, by verifying that file now; otherwise it is closed.
    """
    gates: list[dict[str, Any]] = []
    try:
        version = product_version(root)
        gates.append({"gate": "version_authority", "open": True, "detail": f"product version {version}"})
    except (SigningError, UpdateError, OSError, KeyError, ValueError) as error:
        gates.append({"gate": "version_authority", "open": False, "detail": str(error)})
    try:
        keys = local_update.load_trusted_keys(root / "config" / "update_trusted_keys.json")
        active = [k for k in keys.values() if not k.revoked]
        gates.append({"gate": "owner_update_key", "open": bool(active),
                      "detail": f"{len(active)} active trusted entr{'y' if len(active) == 1 else 'ies'} listed "
                                "(only the public entries are counted; custody of the private key file is not "
                                "checked here)" if active else
                      "config/update_trusted_keys.json lists no owner key. After review: run create-key on the "
                      "owner's account, then add the printed public entry in a reviewed change."})
    except UpdateError as error:
        gates.append({"gate": "owner_update_key", "open": False, "detail": str(error)})
    if authenticode_config is not None and artifact is not None:
        gates.append(_authenticode_gate(root, Path(authenticode_config), Path(artifact), authenticode_verify))
        return {"ok": all(g["open"] for g in gates), "gates": gates}
    tool = signtool()
    configured = bool(certificate or os.environ.get("QRONOS_AUTHENTICODE_THUMBPRINT", "").strip())
    # Closed unless a built installer is verified now (above). A thumbprint setting or a signtool
    # binary is not evidence.
    gates.append({"gate": "authenticode", "open": False, "detail": (
        "Closed: no signed installer was given to verify (preflight --authenticode-config CONFIG --artifact EXE). "
        f"Detected: {'signtool at the fixed Windows SDK path' if tool else 'no signtool at the fixed Windows SDK path'}; "
        f"{'a thumbprint setting (not used as evidence)' if configured else 'no certificate setting'}. "
        "Needed: an Authenticode code-signing certificate from a public certificate authority (an external, paid "
        "step for the owner), then build_support.authenticode_release sign on the exact built installer (time "
        "stamp, signtool verify /pa /all, code-signing usage, every chain ending in a reviewed public root pinned "
        "in config/authenticode_public_roots.json, which ships empty; not self-signed). An enterprise or private "
        "root is never public trust, and a self-signed certificate is never a substitute.")})
    return {"ok": all(g["open"] for g in gates), "gates": gates}


# ------------------------------------------------------------------------ CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-key")
    create.add_argument("--key-file", required=True, type=Path)
    create.add_argument("--owner", required=True)
    show = commands.add_parser("public-entry")
    show.add_argument("--key-file", required=True, type=Path)
    signing = commands.add_parser("sign")
    signing.add_argument("--key-file", required=True, type=Path)
    signing.add_argument("--installer", required=True, type=Path)
    signing.add_argument("--security-floor", required=True)
    signing.add_argument("--minimum-version")
    signing.add_argument("--rollback", action="store_true")
    signing.add_argument("--notes-file", type=Path)
    checks = commands.add_parser("preflight")
    checks.add_argument("--authenticode-config", type=Path)
    checks.add_argument("--artifact", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "create-key":
            result: dict[str, Any] = {"ok": True, "trustedKeyEntry": create_key(arguments.key_file, arguments.owner)}
        elif arguments.command == "public-entry":
            result = {"ok": True, "trustedKeyEntry": public_entry(arguments.key_file)}
        elif arguments.command == "sign":
            notes = arguments.notes_file.read_text(encoding="utf-8") if arguments.notes_file else ""
            manifest = sign(arguments.key_file, arguments.installer, security_floor=arguments.security_floor,
                            minimum_version=arguments.minimum_version, rollback=arguments.rollback, notes=notes)
            result = {"ok": True, "manifest": str(manifest)}
        else:
            if (arguments.authenticode_config is None) != (arguments.artifact is None):
                raise SigningError("Give both --authenticode-config and --artifact, or neither.")
            result = preflight(authenticode_config=arguments.authenticode_config, artifact=arguments.artifact)
    except (SigningError, UpdateError, OSError) as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
