"""Install-root guard and owned-payload removal, exercised through the real NSIS hook code.

Each test compiles a throwaway probe from installer-hooks/qronos-install-root.nsh
(through webview2-offline.nsh, as the installer includes it) and runs it against
disposable fixture folders under tmp_path. The probe stands the fixture folders
in for the user's profile, AppData and Documents (QRONOS_PROBE_* defines), so no
real user folder is read or changed. It is not the Qronos installer: it installs
nothing, runs no other installer and touches no registry value.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from build_support import desktop_prerequisites as prerequisites
from build_support import install_manifest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="NSIS probes run on Windows only.")

_LOCAL = os.environ.get("LOCALAPPDATA", "")
MAKENSIS = Path(_LOCAL, "tauri", "NSIS", "makensis.exe") if _LOCAL else None


class Fixture:
    """A disposable profile with Qronos data in its usual places."""

    def __init__(self, tmp_path: Path) -> None:
        self.base = tmp_path
        self.profile = tmp_path / "profile"
        self.documents = self.profile / "Documents"
        self.local = self.profile / "AppData" / "Local"
        self.roaming = self.profile / "AppData" / "Roaming"
        for folder in (self.local / "Qronos", self.documents / "Qronos" / "Library", self.roaming,
                       self.local / "Programs"):
            folder.mkdir(parents=True, exist_ok=True)
        (self.local / "Qronos" / "settings.json").write_text("{}", encoding="utf-8")
        (self.documents / "Qronos" / "Library" / "report.md").write_text("user document", encoding="utf-8")
        self.count = 0

    def probe(self, body: list[str]) -> str:
        if MAKENSIS is None or not MAKENSIS.is_file():
            pytest.skip("Tauri's NSIS compiler is not installed on this machine.")
        self.count += 1
        work = self.base / f"probe{self.count}"
        work.mkdir()
        payload = work / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
        payload.write_bytes(b"never run")
        result = work / "result.txt"
        script = work / "probe.nsi"
        script.write_text("\n".join((
            "Unicode true", 'Name "probe"', f'OutFile "{work / "probe.exe"}"', "RequestExecutionLevel user",
            "SilentInstall silent",
            f'!define QRONOS_PROBE_PROFILE "{self.profile}"', f'!define QRONOS_PROBE_LOCALAPPDATA "{self.local}"',
            f'!define QRONOS_PROBE_APPDATA "{self.roaming}"', f'!define QRONOS_PROBE_DOCUMENTS "{self.documents}"',
            f'!define QRONOS_WEBVIEW2_INSTALLER "{payload}"', '!define QRONOS_WEBVIEW2_VERSION "1.2.3.4"',
            f'!include "{prerequisites.HOOK_PATH}"',
            "Section", *body,
            f'  FileOpen $R7 "{result}" w', '  FileWriteUTF16LE $R7 "$QronosMessage"', "  FileClose $R7",
            "SectionEnd", "")), encoding="utf-8-sig")  # with a BOM NSIS reads the script as UTF-8
        compiled = subprocess.run([str(MAKENSIS), "/V1", str(script)], capture_output=True, text=True, check=False)
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
        subprocess.run([str(work / "probe.exe")], check=False, timeout=180)
        return result.read_bytes().decode("utf-16-le") or "ok"

    def snapshot(self) -> dict[str, bytes | None]:
        """Every path and file content under the fixture's apps and profile folders."""
        return {path.relative_to(self.base).as_posix(): (path.read_bytes() if path.is_file() else None)
                for root in (self.base / "apps", self.profile) if root.exists() for path in root.rglob("*")}

    def check(self, install_dir: str) -> str:
        return self.probe([f'  StrCpy $INSTDIR "{install_dir}"', "  Call QronosCheckInstallRoot"])

    def remove(self, install_dir: Path) -> str:
        return self.probe([f'  StrCpy $INSTDIR "{install_dir}"', "  Call QronosRemoveOwnedPayload"])


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def junction(target: Path, link: Path) -> None:
    winapi = pytest.importorskip("_winapi")
    winapi.CreateJunction(str(target), str(link))


def owned_install(folder: Path, files: list[str]) -> None:
    """A previous Qronos installation: the listed files plus its ownership manifest."""
    for name in files:
        path = folder / name.replace("\\", os.sep)
        path.parent.mkdir(parents=True, exist_ok=True)
        if name != install_manifest.FILE_NAME:
            path.write_bytes(b"old " + name.encode())
    (folder / install_manifest.FILE_NAME).write_bytes(install_manifest.render(files))


OLD_FILES = ["qronos-desktop.exe", "uninstall.exe", install_manifest.FILE_NAME,
             "qronos-runtime/qronos-runtime.exe", "qronos-runtime/runtime-manifest.json",
             "qronos-runtime/_internal/python313.dll", "qronos-runtime/_internal/removed_in_new_version.pyd",
             "third-party-notices/THIRD_PARTY_NOTICES-desktop.txt"]


# --------------------------------------------------------- install root


def test_a_new_folder_of_its_own_is_accepted(fx):
    assert fx.check(str(fx.local / "Programs" / "qronos-desktop")) == "ok"


def test_an_empty_existing_folder_is_accepted(fx):
    (fx.base / "apps" / "Qronos Desktop").mkdir(parents=True)

    assert fx.check(str(fx.base / "apps" / "Qronos Desktop")) == "ok"


@pytest.mark.parametrize("where", [
    "local/Qronos", "local/Qronos/app", "local/qronos", "documents/Qronos", "documents/Qronos/Library",
    "documents/Qronos Files/x", "local/com.qronos.agent", "roaming/com.qronos.agent/x", "profile/.ollama/models",
])
def test_user_data_folders_and_folders_inside_them_are_refused(fx, where):
    first, _, rest = where.partition("/")
    target = {"local": fx.local, "documents": fx.documents, "roaming": fx.roaming, "profile": fx.profile}[first] / rest

    message = fx.check(str(target))

    assert message.startswith("The chosen folder overlaps"), message
    assert (fx.local / "Qronos" / "settings.json").read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize("alias", ["Qronos.", "Qronos ", "Qronos. . ", "QRONOS"])
def test_windows_aliases_of_the_data_folder_are_refused(fx, alias):
    assert fx.check(str(fx.local) + "\\" + alias).startswith("The chosen folder overlaps")


def test_an_8_3_short_name_of_the_data_folder_is_refused(fx):
    long_name = fx.local / "Qronos"
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(fx.profile), buffer, 1024)
    if not buffer.value or buffer.value.casefold() == str(fx.profile).casefold():
        pytest.skip("8.3 short names are not generated on this volume.")
    short = buffer.value + str(long_name)[len(str(fx.profile)):]

    assert fx.check(short).startswith("The chosen folder overlaps")


def test_a_folder_containing_user_data_is_refused(fx):
    assert fx.check(str(fx.base)).startswith("The chosen folder overlaps")  # contains the fixture profile


@pytest.mark.parametrize("where", ["profile", "documents", "local", "roaming", "local/Programs"])
def test_profile_and_system_roots_are_refused(fx, where):
    first, _, rest = where.partition("/")
    target = {"local": fx.local, "documents": fx.documents, "roaming": fx.roaming, "profile": fx.profile}[first]

    message = fx.check(str(target / rest if rest else target))

    assert message.startswith("Qronos cannot be installed directly into"), message


@pytest.mark.parametrize(("path", "message"), [
    ("\\\\server\\share\\Qronos", "Choose a folder on a local drive"),
    ("\\\\?\\C:\\Qronos-probe", "Choose a folder on a local drive"),
    ("C:\\", "drive's top folder"),  # NSIS turns this into "C:", which must not mean the current folder
    ("C:", "drive's top folder"),
    ("C:apps", "Choose a complete folder path"),
])
def test_network_device_and_drive_roots_are_refused(fx, path, message):
    assert message in fx.check(path)


@pytest.mark.parametrize("name", ["CON", "con", "NUL.txt", "aux.log", "COM1", "LPT9", "COM\u00b9", "LPT\u00b3.x",
                                  "CONIN$", "PRN "])
def test_a_device_name_is_refused(fx, name):
    """Windows 11 no longer maps "...\\CON" to \\\\.\\CON in GetFullPathNameW, so the name itself is checked."""
    assert fx.check(str(fx.base / "apps" / name)) == "The chosen folder name is a Windows device name."


@pytest.mark.parametrize("name", ["COM10", "Console", "nullable", "LPT", "auxiliary"])
def test_names_that_only_resemble_devices_are_accepted(fx, name):
    assert fx.check(str(fx.base / "apps" / name)) == "ok"


def test_nsis_strips_a_stream_colon_before_the_guard_sees_it(fx):
    """NSIS removes ':' from $INSTDIR, so an alternate data stream cannot be named; the guard's own
    ':' check is a second layer."""
    assert fx.check(str(fx.base / "apps" / "Qronos:stream")) == "ok"
    assert fx.probe([f'  StrCpy $INSTDIR "{fx.base / "apps" / "Qronos:stream"}"',
                     "  StrCpy $QronosMessage $INSTDIR"]).endswith("\\apps\\Qronosstream")


def test_a_non_empty_folder_that_is_not_qronos_is_refused_and_untouched(fx):
    foreign = fx.base / "apps" / "Tools"
    foreign.mkdir(parents=True)
    (foreign / "notes.txt").write_text("someone's file", encoding="utf-8")

    assert "is not a Qronos installation" in fx.check(str(foreign))
    assert (foreign / "notes.txt").read_text(encoding="utf-8") == "someone's file"


def test_a_forged_manifest_header_is_not_ownership(fx):
    foreign = fx.base / "apps" / "Tools"
    foreign.mkdir(parents=True)
    (foreign / install_manifest.FILE_NAME).write_bytes("QRONOS-INSTALL-MANIFEST 2 other\r\n".encode("utf-16-le"))

    assert "is not a Qronos installation" in fx.check(str(foreign))


def test_an_existing_qronos_installation_is_accepted(fx):
    owned = fx.base / "apps" / "qronos-desktop"
    owned_install(owned, OLD_FILES)

    assert fx.check(str(owned)) == "ok"


def test_a_junctioned_install_folder_is_refused(fx):
    target = fx.base / "elsewhere"
    target.mkdir()
    junction(target, fx.base / "apps-link")

    assert fx.check(str(fx.base / "apps-link")).startswith("The chosen folder is reached through a link or junction")


# ------------------------------------------------- owned-payload removal


def test_an_upgrade_removes_exactly_the_previous_payload(fx):
    owned = fx.base / "apps" / "qronos-desktop"
    owned_install(owned, OLD_FILES)
    (owned / "my-notes.txt").write_text("keep", encoding="utf-8")
    (owned / "qronos-runtime" / "_internal" / "added-by-user.txt").write_text("keep too", encoding="utf-8")

    assert fx.remove(owned) == "ok"

    remaining = sorted(path.relative_to(owned).as_posix() for path in owned.rglob("*"))
    assert remaining == ["my-notes.txt", "qronos-runtime", "qronos-runtime/_internal",
                         "qronos-runtime/_internal/added-by-user.txt"]
    # The old version's extra runtime file is gone, so it cannot linger after the upgrade.
    assert not (owned / "qronos-runtime" / "_internal" / "removed_in_new_version.pyd").exists()


def test_without_a_manifest_nothing_is_removed(fx):
    folder = fx.base / "apps" / "empty-of-manifest"
    folder.mkdir(parents=True)
    (folder / "qronos-desktop.exe").write_bytes(b"MZ")

    assert fx.remove(folder) == "ok"
    assert (folder / "qronos-desktop.exe").exists()


@pytest.mark.parametrize("bad_line", ["F ..\\outside.txt", "F qronos-runtime\\..\\..\\outside.txt", "F C:\\x.txt",
                                      "F \\outside.txt", "F a/b.txt", "F a\\\\b.txt", "X something", "F ",
                                      "F file.txt:stream"])
def test_an_unsafe_manifest_line_stops_before_anything_is_deleted(fx, bad_line):
    owned = fx.base / "apps" / "qronos-desktop"
    owned_install(owned, OLD_FILES)
    (fx.base / "apps" / "outside.txt").write_text("outside", encoding="utf-8")
    manifest = owned / install_manifest.FILE_NAME
    manifest.write_bytes(manifest.read_bytes() + (bad_line + "\r\n").encode("utf-16-le"))
    before = fx.snapshot()

    message = fx.remove(owned)

    assert "file list contains" in message, message
    assert fx.snapshot() == before


def test_a_listed_folder_that_is_a_junction_stops_removal(fx):
    owned = fx.base / "apps" / "qronos-desktop"
    owned_install(owned, ["qronos-desktop.exe", install_manifest.FILE_NAME, "qronos-runtime/qronos-runtime.exe"])
    elsewhere = fx.base / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "qronos-runtime.exe").write_text("not Qronos's", encoding="utf-8")
    for path in sorted((owned / "qronos-runtime").rglob("*"), reverse=True):
        path.unlink()
    (owned / "qronos-runtime").rmdir()
    junction(elsewhere, owned / "qronos-runtime")

    assert "qronos-runtime is a link to another location" in fx.remove(owned)
    assert (elsewhere / "qronos-runtime.exe").read_text(encoding="utf-8") == "not Qronos's"
    assert (owned / "qronos-desktop.exe").exists()  # nothing was deleted


def test_a_file_in_use_stops_removal_and_keeps_the_manifest_for_a_retry(fx):
    owned = fx.base / "apps" / "qronos-desktop"
    owned_install(owned, OLD_FILES)
    with open(owned / "qronos-runtime" / "qronos-runtime.exe", "rb"):  # no FILE_SHARE_DELETE: like a running exe
        message = fx.remove(owned)

    assert message.startswith("The previously installed file qronos-runtime\\qronos-runtime.exe could not be removed")
    assert (owned / install_manifest.FILE_NAME).exists()
    assert (owned / "qronos-runtime" / "qronos-runtime.exe").exists()


def test_a_manifest_with_another_header_is_not_used(fx):
    owned = fx.base / "apps" / "qronos-desktop"
    owned_install(owned, OLD_FILES)
    manifest = owned / install_manifest.FILE_NAME
    manifest.write_bytes(manifest.read_bytes().replace("com.qronos.agent".encode("utf-16-le"),
                                                       "com.other.app".encode("utf-16-le")))

    assert fx.remove(owned) == "The previous installation's file list is not a Qronos file list."
    assert (owned / "qronos-desktop.exe").exists()


# ---------------------------------------------- strict manifest entries (I8)


def unsafe_list(owned: Path, *lines: str) -> None:
    manifest = owned / install_manifest.FILE_NAME
    manifest.write_bytes(manifest.read_bytes() + "".join(line + "\r\n" for line in lines).encode("utf-16-le"))


def test_a_wildcard_entry_deletes_nothing(fx):
    """I8 reproduction: "F foreign-*.bin" used to make Delete remove unlisted files."""
    owned = fx.base / "apps" / "qronos-desktop"
    owned_install(owned, OLD_FILES)
    (owned / "foreign-a.bin").write_bytes(b"not listed")
    (owned / "foreign-b.bin").write_bytes(b"not listed either")
    unsafe_list(owned, "F foreign-*.bin")
    before = fx.snapshot()

    assert "unsafe entry: foreign-*.bin" in fx.remove(owned)
    assert fx.snapshot() == before


def test_a_file_through_an_unlisted_junction_is_refused(fx):
    """I8 reproduction: "F linked\\foreign.bin" without a "D linked" line, where linked is a junction."""
    owned = fx.base / "apps" / "qronos-desktop"
    owned_install(owned, OLD_FILES)
    outside = fx.base / "outside"
    outside.mkdir()
    (outside / "foreign.bin").write_bytes(b"outside the install folder")
    junction(outside, owned / "linked")
    unsafe_list(owned, "F linked\\foreign.bin")
    before = fx.snapshot()

    assert "linked is a link to another location" in fx.remove(owned)
    assert (outside / "foreign.bin").read_bytes() == b"outside the install folder"
    assert fx.snapshot() == before


@pytest.mark.parametrize(("line", "message"), [
    ("F qronos-runtime", "names qronos-runtime as a file, but it is a folder"),
    ("D qronos-desktop.exe", "names qronos-desktop.exe as a folder, but it is a file"),
])
def test_an_entry_of_the_wrong_kind_stops_before_anything_is_deleted(fx, line, message):
    owned = fx.base / "apps" / "qronos-desktop"
    owned_install(owned, OLD_FILES)
    unsafe_list(owned, line)
    before = fx.snapshot()

    assert message in fx.remove(owned)
    assert fx.snapshot() == before


ENTRIES = ["qronos-runtime\\_internal\\a.dll", "third-party-notices\\licenses\\npm\\@tauri-apps\\api\\2.11.1\\LICENSE",
           "a b\\c.txt", "COM10\\x", "con-tools\\x", "x..y", "a\\..\\b", "..\\a", "a\\\\b", "\\a", "a\\", "a:b",
           "a/b", "a*", "a?b", "a<b", "a>b", "a|b", "a\tb", "a.\\b", "a \\b", " a\\b", "CON\\x", "x\\nul.txt",
           "x\\COM1.log", "x\\LPT9", "x\\CONIN$"]


def nsis_entry_result(fx: Fixture, entry: str) -> str:
    escaped = entry.replace("$", "$$").replace("\t", "$\\t")
    return fx.probe([f'  StrCpy $QronosPath "{escaped}"', '  StrCpy $QronosMessage ""',
                     "  Call QronosCheckEntry"])


def python_entry_result(entry: str) -> bool:
    """The builder's own rule for a manifest line (install_manifest.parse)."""
    data = (install_manifest.HEADER + "\r\nF " + entry + "\r\n").encode("utf-16-le")
    try:
        install_manifest.parse(data)
    except install_manifest.InstallManifestError:
        return False
    return True


@pytest.mark.parametrize("entry", ENTRIES)
def test_the_hook_and_the_builder_agree_on_every_entry(fx, entry):
    assert (nsis_entry_result(fx, entry) == "ok") == python_entry_result(entry), entry


# ------------------------------------------- install root through links (I9)


def test_an_install_folder_through_a_junction_into_user_data_is_refused(fx):
    """I9 reproduction: <junction to AppData\\Local>\\Qronos\\app used to be accepted."""
    junction(fx.local, fx.base / "looks-harmless")

    message = fx.check(str(fx.base / "looks-harmless" / "Qronos" / "app"))

    assert message.startswith("The chosen folder is reached through a link or junction"), message
    assert not (fx.local / "Qronos" / "app").exists()


def test_any_junction_on_the_way_is_refused_even_to_a_harmless_place(fx):
    elsewhere = fx.base / "elsewhere"
    elsewhere.mkdir()
    junction(elsewhere, fx.base / "apps-link")

    assert fx.check(str(fx.base / "apps-link" / "qronos-desktop")).startswith(
        "The chosen folder is reached through a link or junction")


def test_a_protected_root_that_is_itself_a_junction_is_matched_by_its_target(fx):
    """Documents redirected through a junction: its real target must count as Documents."""
    real_documents = fx.base / "RealDocs"
    shutil.move(str(fx.documents), str(real_documents))
    junction(real_documents, fx.documents)

    assert fx.check(str(real_documents / "Qronos" / "app")).startswith("The chosen folder overlaps")
    assert fx.check(str(real_documents)).startswith("Qronos cannot be installed directly into")


def test_ordinary_install_folders_still_pass_after_resolution(fx):
    assert fx.check(str(fx.local / "Programs" / "qronos-desktop")) == "ok"
    (fx.base / "apps" / "Qronos Desktop").mkdir(parents=True)
    assert fx.check(str(fx.base / "apps" / "Qronos Desktop")) == "ok"
    assert fx.check(str(fx.base / "apps" / "not-yet" / "created")) == "ok"


def test_removal_refuses_an_install_folder_reached_through_a_junction(fx):
    owned = fx.base / "apps" / "qronos-desktop"
    owned_install(owned, OLD_FILES)
    junction(owned, fx.base / "owned-link")
    before = fx.snapshot()

    assert fx.remove(fx.base / "owned-link") == ("The install folder is reached through a link or junction; "
                                                 "nothing was removed.")
    assert fx.snapshot() == before


# -------------------------------------- the resolver fails closed (I9 follow-up)
#
# Real Windows errors, made in throwaway folders: no flag in the hook simulates them.

UNRESOLVED_INSTALL = "Windows cannot open the chosen folder or the folders on the way to it"
UNRESOLVED_ROOT = "so the chosen folder cannot be checked against it"


@pytest.fixture
def deny_open():
    """deny(folder) denies Everyone only SYNCHRONIZE on a fixture folder, which CreateFileW
    always requests, so opening the folder fails with ERROR_ACCESS_DENIED while its
    attributes stay readable. It returns a function that removes the ACE again; any still
    in place are removed after the test.

    Only SYNCHRONIZE: pytest's folders carry an inherited OWNER RIGHTS ACE (Python's
    mkdir mode 0o700), so a deny-everything ACE could not be removed again without
    elevation. This one leaves READ_CONTROL and WRITE_DAC with the owner.
    """
    locked = []

    def allow(folder: Path) -> None:
        if folder in locked:
            subprocess.run(["icacls", str(folder), "/remove:d", "*S-1-1-0"], check=True, capture_output=True)
            locked.remove(folder)

    def deny(folder: Path):
        subprocess.run(["icacls", str(folder), "/deny", "*S-1-1-0:(S)"], check=True, capture_output=True)
        locked.append(folder)
        return lambda: allow(folder)

    yield deny
    for folder in list(locked):
        allow(folder)


def test_a_folder_on_a_missing_drive_is_refused(fx):
    """No ancestor exists at all, so nothing was resolved: refused, not compared as written."""
    present = {drive[0].upper() for drive in os.listdrives()}
    free = [letter for letter in "QRSTUVWXYZ" if letter not in present]
    if not free:
        pytest.skip("No unused drive letter.")

    message = fx.check(f"{free[0]}:\\apps\\qronos-desktop")

    assert message.startswith(UNRESOLVED_INSTALL), message


def test_a_folder_windows_cannot_open_is_refused(fx, deny_open):
    """GetFileAttributesW succeeds but CreateFileW is denied: the existing part is not resolved."""
    locked = fx.base / "apps" / "locked"
    locked.mkdir(parents=True)
    deny_open(locked)

    assert fx.check(str(locked)).startswith(UNRESOLVED_INSTALL)
    assert fx.check(str(locked / "qronos-desktop")).startswith(UNRESOLVED_INSTALL)
    # A sibling Windows can open is still an ordinary install folder.
    assert fx.check(str(fx.base / "apps" / "qronos-desktop")) == "ok"


def test_a_protected_folder_windows_cannot_open_refuses_every_install(fx, deny_open):
    """Documents\\Qronos cannot be resolved, so no install folder can be checked against it."""
    deny_open(fx.documents / "Qronos")

    message = fx.check(str(fx.base / "apps" / "qronos-desktop"))

    assert UNRESOLVED_ROOT in message and str(fx.documents / "Qronos") in message, message


def test_a_name_windows_rejects_is_not_resolved_as_if_it_were_missing(fx):
    """GetFileAttributesW fails with ERROR_INVALID_NAME, not "not found": the walk must stop, not skip it.

    Probed on QronosCanonical itself: NSIS already strips <>|"*?: from $INSTDIR, so the
    install guard never sees such a name. "apps" exists, so Windows reaches "a<b"
    (below a missing folder the lookup stops earlier with "path not found", and the
    walk correctly continues upwards)."""
    (fx.base / "apps").mkdir()
    path = str(fx.base / "apps" / "a<b" / "qronos-desktop")

    result = fx.probe([f'  StrCpy $QronosPath "{path}"', "  Call QronosCanonical",
                       '  StrCpy $QronosMessage "lexical=$QronosLexical|resolved=$QronosPath"'])

    assert result == f"lexical={path}|resolved=", result


def raw_junction(target: str, link: Path) -> None:
    """A junction to an absolute target of any length (_winapi.CreateJunction stops at MAX_PATH)."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = ctypes.c_void_p
    link.mkdir()
    substitute, printed = ("\\??\\" + target).encode("utf-16-le"), target.encode("utf-16-le")
    data = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printed)) \
        + substitute + b"\0\0" + printed + b"\0\0"
    buffer = struct.pack("<IHH", 0xA0000003, len(data), 0) + data  # IO_REPARSE_TAG_MOUNT_POINT
    # GENERIC_WRITE, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
    handle = kernel.CreateFileW(str(link), 0x40000000, 0, None, 3, 0x02200000, None)
    assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
    try:
        returned = ctypes.c_ulong()
        assert kernel.DeviceIoControl(ctypes.c_void_p(handle), 0x000900A4, buffer, len(buffer), None, 0,
                                      ctypes.byref(returned), None), ctypes.get_last_error()  # FSCTL_SET_REPARSE_POINT
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))


def test_a_final_path_longer_than_nsis_strings_is_refused(fx):
    """GetFinalPathNameByHandleW needs more than NSIS's 1024 characters: the junction must not pass as no link."""
    prefix = "\\\\?\\"
    target = prefix + str(fx.base / "deep")
    os.mkdir(target)
    for _ in range(5):
        target += "\\" + "d" * 200
        os.mkdir(target)
    link = fx.base / "short"
    try:
        raw_junction(target[len(prefix):], link)
        assert os.path.isdir(link)  # the junction opens: only its final path is too long

        message = fx.check(str(link / "qronos-desktop"))

        assert message.startswith(UNRESOLVED_INSTALL), message
    finally:
        if os.path.lexists(link):
            os.rmdir(link)  # removes the junction only
        shutil.rmtree(prefix + str(fx.base / "deep"))


def test_removal_refuses_an_install_folder_windows_cannot_open(fx, deny_open):
    owned = fx.base / "apps" / "qronos-desktop"
    owned_install(owned, OLD_FILES)
    before = fx.snapshot()
    allow = deny_open(owned)

    message = fx.remove(owned)

    allow()
    assert message == "Windows cannot open the install folder or the folders on the way to it; nothing was removed."
    assert fx.snapshot() == before


def test_the_directory_page_runs_the_same_guard():
    hook = (prerequisites.HOOK_PATH.parent / "qronos-install-root.nsh").read_text(encoding="utf-8")
    body = hook[hook.index("Function .onVerifyInstDir"):]
    body = body[:body.index("FunctionEnd")]
    assert "Call QronosCheckInstallRoot" in body and "Abort" in body
