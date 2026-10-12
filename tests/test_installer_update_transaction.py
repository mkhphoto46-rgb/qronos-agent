"""
The installer's update transaction (installer-hooks/qronos-update-transaction.nsh, Media18/Root O-A): a failed
update leaves the previously installed program usable, byte for byte, with its metadata and shortcuts; an
interrupted one is finished or undone only as far as its journal proves, otherwise refused.

Each test compiles the FULL Qronos template into an inert, silent probe (tests/fixtures/nsis_full_template.py:
fake registry INI, fake Start-menu/desktop folders, private fence, private profile and data root, the WebView2
runtime reported present unless a test says otherwise) and runs it as the current user against a PRIVATE previous
installation planted by the test: its owned files (old bytes), its own install manifest, unlisted private files,
its Apps & features values and its two shortcuts. The installer probe is this test's own process, waited for and
killed only through its own handle. No real installer, registry, shortcut, WebView2, UAC, network or user folder.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

from build_support import install_manifest
from tests.fixtures.nsis_fence_probe import MAKENSIS
import tests.test_installer_template as tt

pytestmark = pytest.mark.skipif(os.name != "nt" or MAKENSIS is None or not MAKENSIS.is_file(),
                                reason="Windows with Tauri's NSIS compiler")

TXN = ".qronos-update"
HEADER = "QRONOS-UPDATE-JOURNAL 1 com.qronos.agent"
OLD = {"qronos-probe.exe": b"MZ OLD main program 0.0.9\n", "uninstall.exe": b"MZ OLD uninstaller 0.0.9\n",
       "res\\one.txt": b"OLD resource one\n", "res\\old-only.txt": b"a file only the old version had\n"}
PRIVATE = {"notes\\private.txt": b"the person's own note\n", "res\\user-added.txt": b"added by the person\n"}
UNINSTKEY = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Qronos Probe"
OLD_VALUES = {"DisplayName": "Qronos Probe", "DisplayVersion": "0.0.9", "MainBinaryName": "qronos-probe.exe",
              "Publisher": "QronosProbe", "UninstallString": "old-uninstall-string", "EstimatedSize": "0x00000007"}
OLD_LNK = {"start": b"OLD start menu shortcut bytes", "desktop": b"OLD desktop shortcut bytes"}
NEW_FILES = ["qronos-probe.exe", "uninstall.exe", "res\\one.txt", "res2\\two.txt", install_manifest.FILE_NAME]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plant_previous(probe, *, webview2: bool = True) -> dict:
    """The private previous installation; returns what 'usable and byte-exact' means for it."""
    from tests.fixtures.nsis_full_template import VCREDIST_KEY
    root = probe.instdir
    for entry, data in {**OLD, **PRIVATE}.items():
        path = root / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    listed = [entry.replace("\\", "/") for entry in OLD] + [install_manifest.FILE_NAME]
    (root / install_manifest.FILE_NAME).write_bytes(install_manifest.render(listed))
    (probe.smprograms / "Qronos Probe.lnk").write_bytes(OLD_LNK["start"])
    (probe.desktop / "Qronos Probe.lnk").write_bytes(OLD_LNK["desktop"])
    # Preserve the newly required prerequisite in the private fake registry.
    # This fixture replaces the initial INI when planting the old application.
    sections = {VCREDIST_KEY: {"Installed": "1", "Major": "14", "Minor": "44", "Bld": "35211"},
                UNINSTKEY: dict(OLD_VALUES)}
    if webview2:
        sections = {tt_webview2_key(): {"pv": "120.0.0.0"}, **sections}
    lines = []
    for section, values in sections.items():
        lines.append(f"[{section}]")
        lines += [f"{key}={value}" for key, value in values.items()]
    probe.registry.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-16"))  # exact CRLF, no translation
    return state(probe)


def tt_webview2_key() -> str:
    from tests.fixtures.nsis_full_template import WEBVIEW2_KEY

    return WEBVIEW2_KEY


def state(probe) -> dict:
    """Everything that makes the installation usable: every file under the install folder with its digest, the
    Apps & features values, and the two shortcuts' bytes."""
    files = {}
    if probe.instdir.exists():
        for path in sorted(probe.instdir.rglob("*")):
            relative = str(path.relative_to(probe.instdir))
            files[relative] = sha(path.read_bytes()) if path.is_file() else "<dir>"
    registry = {}
    text = probe.registry_text()
    section = None
    for line in text.splitlines():
        if line.startswith("["):
            section = line.strip("[]")
        elif section == UNINSTKEY and "=" in line:
            key, _, value = line.partition("=")
            registry[key] = value
    lnk = {name: (folder / "Qronos Probe.lnk").read_bytes() if (folder / "Qronos Probe.lnk").is_file() else None
           for name, folder in (("start", probe.smprograms), ("desktop", probe.desktop))}
    return {"files": files, "registry": registry, "lnk": lnk}


def assert_previous_usable(probe, before: dict, *, registry: bool = True) -> None:
    after = state(probe)
    assert after["files"] == before["files"], ("the previous installation changed",
                                               sorted(set(after["files"].items()) ^ set(before["files"].items())))
    assert not (probe.instdir / TXN).exists()
    for entry, data in {**OLD, **PRIVATE}.items():
        assert (probe.instdir / entry).read_bytes() == data, entry
    if registry:
        for key, value in OLD_VALUES.items():
            assert after["registry"].get(key) == value, (key, after["registry"])
        assert after["lnk"] == {"start": OLD_LNK["start"], "desktop": OLD_LNK["desktop"]}, after["lnk"]


def install(probe, *arguments: str) -> int:
    return tt.run(probe, "/S", *arguments)


def new_payload_installed(probe) -> bool:
    payload = probe.work / "payload"
    expected = {"qronos-probe.exe": payload / "qronos-probe.exe", "res\\one.txt": payload / "one.txt",
                "res2\\two.txt": payload / "two.txt", install_manifest.FILE_NAME: payload / install_manifest.FILE_NAME}
    return all((probe.instdir / entry).read_bytes() == source.read_bytes() for entry, source in expected.items()) \
        and (probe.instdir / "uninstall.exe").is_file()


# ------------------------------------------------------------------------------------- normal failures


def test_a_failed_prerequisite_leaves_the_previous_installation_usable(tmp_path):
    env = tt.Env(tmp_path)
    probe = env.probe("prereq")
    before = plant_previous(probe, webview2=False)  # the embedded stand-in is not a program: ExecWait fails
    code = install(probe)
    assert code != 0, "a failed prerequisite must not end successfully"
    assert "WebView2" in probe.read_result(), probe.read_result()
    assert_previous_usable(probe, before)


def test_a_failed_file_write_after_the_old_files_were_set_aside_restores_them(tmp_path):
    env = tt.Env(tmp_path)
    probe = env.probe("write", extra_defines={"QRONOS_PROBE_LOCK_AFTER_CLAIM": "res\\one.txt"})
    before = plant_previous(probe)
    code = install(probe)
    assert code != 0
    assert probe.read_result() == "step failed: a resource file", probe.read_result()
    assert_previous_usable(probe, before)


def test_something_unlisted_in_a_new_files_way_is_left_untouched_and_the_previous_version_stays(tmp_path):
    env = tt.Env(tmp_path)
    probe = env.probe("foreign")
    plant_previous(probe)
    foreign = probe.instdir / "res2" / "two.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"someone else's file, not listed by Qronos")
    before = state(probe)
    code = install(probe)
    assert code != 0
    assert "left untouched" in probe.read_result(), probe.read_result()
    assert foreign.read_bytes() == b"someone else's file, not listed by Qronos"
    assert_previous_usable(probe, before)


def test_a_failure_after_the_new_files_and_registration_restores_files_metadata_and_shortcuts(tmp_path):
    env = tt.Env(tmp_path)
    probe = env.probe("late", registry_fault='"EstimatedSize"')  # after DisplayVersion etc. were overwritten
    before = plant_previous(probe)
    try:
        code = install(probe)
    finally:
        os.chmod(probe.work / "fault-registry.ini", 0o666)
    assert code != 0
    assert probe.read_result() == "step failed: the size registration", probe.read_result()
    assert_previous_usable(probe, before)


def test_a_failed_shortcut_step_restores_the_previous_start_menu_shortcut(tmp_path):
    env = tt.Env(tmp_path)
    probe = env.probe("lnk")
    before = plant_previous(probe)
    (probe.desktop / "Qronos Probe.lnk").unlink()
    (probe.desktop / "Qronos Probe.lnk" / "inside").mkdir(parents=True)  # the desktop shortcut cannot be written
    before = state(probe)
    code = install(probe)
    assert code != 0
    assert probe.read_result() == "step failed: the desktop shortcut", probe.read_result()
    after = state(probe)
    assert after["files"] == before["files"] and after["lnk"]["start"] == OLD_LNK["start"], after
    assert after["registry"] == before["registry"]


def test_a_kept_file_replaced_during_the_update_is_never_moved_back_and_the_outcome_is_reported(tmp_path):
    """The rollback moves a kept file back only if it is still the file this run recorded."""
    env = tt.Env(tmp_path)
    probe = env.probe("tamper", extra_defines={"QRONOS_PROBE_TAMPER_KEPT": "res\\one.txt",
                                               "QRONOS_PROBE_LOCK_AFTER_CLAIM": "res2\\two.txt"})
    plant_previous(probe)
    code = install(probe)
    assert code != 0
    assert probe.read_result() == "step failed: a resource file", probe.read_result()
    replaced = probe.instdir / TXN / "old" / "res" / "one.txt"
    assert replaced.read_bytes() == b"replaced by another program", "the replacement was moved or deleted"
    assert not (probe.instdir / "res" / "one.txt").exists()
    report = (probe.work / "result.txt.rollback").read_bytes().decode("mbcs", "replace")
    assert "not the file this update recorded" in report, report


def test_a_failed_commit_restores_the_previous_installation_and_writes_no_success(tmp_path):
    env = tt.Env(tmp_path)
    probe = env.probe("commit", extra_defines={"QRONOS_PROBE_COMMIT_FAULT": "1"})
    before = plant_previous(probe)
    code = install(probe)
    assert code != 0
    assert probe.read_result() == "step failed: the update commit", probe.read_result()
    assert "postinstall" not in probe.read_log()
    assert_previous_usable(probe, before)


def test_any_other_abort_after_the_transaction_began_is_rolled_back_too(tmp_path):
    env = tt.Env(tmp_path)
    probe = env.probe("abort", extra_defines={"QPROBE_ABORT_SECOND_APP_CHECK": "1"})
    before = plant_previous(probe)
    code = install(probe)
    assert code != 0
    assert probe.read_log().count("app-check") == 2
    assert_previous_usable(probe, before)


def test_a_successful_update_replaces_exactly_the_owned_payload_and_keeps_private_files(tmp_path):
    env = tt.Env(tmp_path)
    probe = env.probe("ok")
    plant_previous(probe)
    assert install(probe) == 0, probe.read_result()
    assert new_payload_installed(probe)
    assert not (probe.instdir / "res" / "old-only.txt").exists()
    assert not (probe.instdir / TXN).exists()
    for entry, data in PRIVATE.items():
        assert (probe.instdir / entry).read_bytes() == data
    assert state(probe)["registry"]["DisplayVersion"] == tt.VERSION


def test_a_failed_receipt_keeps_the_committed_new_version_and_never_claims_confirmation(tmp_path):
    """Update mode: the receipt comes after the commit. Without it the start-up barrier reports unconfirmed."""
    import _winapi

    env = tt.Env(tmp_path)
    probe = env.probe("receipt")
    plant_previous(probe)
    receipts = env.data / "Updates" / "receipts"
    user = os.environ.get("USERNAME", "")
    denied = subprocess.run(["icacls", str(receipts), "/deny", f"{user}:(WD,AD)"], capture_output=True)
    assert denied.returncode == 0, denied.stdout
    try:
        result = tt.hand_over(env, probe)
    finally:
        subprocess.run(["icacls", str(receipts), "/remove:d", user], capture_output=True)
    assert result["ack"] is True and result["exit"] == _winapi.WAIT_OBJECT_0
    assert env.receipts() == [], "a receipt exists although it could not be written"
    assert new_payload_installed(probe) and not (probe.instdir / TXN).exists()


# ------------------------------------------------------------------------------------- interrupted work


def identity(path: Path) -> str:
    """The identity string the installer journals: signed 32-bit volume serial, file index high and low."""
    info = os.stat(path)

    def signed(value: int) -> int:
        value &= 0xFFFFFFFF
        return value - (1 << 32) if value >= 1 << 31 else value

    return f"{signed(info.st_dev)}:{signed(info.st_ino >> 32)}:{signed(info.st_ino)}"


def journal(probe, lines: list[str], *, last_line_end: bool = True, header: str = HEADER) -> None:
    text = "\r\n".join([header, *lines]) + ("\r\n" if last_line_end else "")
    (probe.instdir / TXN / "journal").write_bytes(text.encode("utf-16-le"))


def set_aside(probe, entry: str) -> str:
    """Moves one planted old file into the transaction folder (as the installer would) -> its journal line."""
    source = probe.instdir / entry
    line = f"O {identity(source)} {entry}"
    target = probe.instdir / TXN / "old" / entry
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    return line


def recover_then_stop(probe) -> int:
    """A run whose WebView2 prerequisite fails right after recovery and validation: what recovery did stays visible."""
    text = probe.registry.read_bytes().decode("utf-16")  # exact bytes, no newline translation
    block = f"[{tt_webview2_key()}]\r\npv=120.0.0.0\r\n"
    assert block in text, text
    probe.registry.write_bytes(text.replace(block, "").encode("utf-16"))
    return install(probe)


@pytest.fixture
def interrupted(tmp_path):
    env = tt.Env(tmp_path)
    probe = env.probe("crash")
    before = plant_previous(probe)
    (probe.instdir / TXN).mkdir()
    return probe, before


def _plant_left_over(probe, case: str) -> dict:
    """Plants what an earlier run (genuine, interrupted) or a forger left; returns the files that must survive."""
    keep: dict = {}
    victim = probe.instdir / "notes" / "private.txt"
    if case == "interrupted_while_setting_aside":
        journal(probe, [set_aside(probe, "qronos-probe.exe"), set_aside(probe, "res\\one.txt"),
                        f"O {identity(probe.instdir / 'uninstall.exe')} uninstall.exe"])
    elif case in ("interrupted_after_writing", "committed_but_unfinished"):
        lines = [set_aside(probe, entry) for entry in OLD] + [set_aside(probe, install_manifest.FILE_NAME)]
        for entry in ("qronos-probe.exe", "res2\\two.txt"):
            path = probe.instdir / entry
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"new bytes the interrupted installer wrote")
            lines.append(f"N {identity(path)} {entry}")
        journal(probe, lines + (["W", "C"] if case == "committed_but_unfinished" else ["W"]))
    elif case == "truncated_last_line":
        journal(probe, [set_aside(probe, "qronos-probe.exe"),
                        f"O {identity(probe.instdir / 'res' / 'one.txt')} res\\one."], last_line_end=False)
    elif case == "forged_new_file_with_a_private_files_real_identity":
        journal(probe, [set_aside(probe, "qronos-probe.exe"), f"N {identity(victim)} notes\\private.txt", "W"])
    elif case == "forged_committed_kept_file_with_a_foreign_files_real_identity":
        foreign = probe.instdir / TXN / "old" / "res" / "someone-elses.txt"  # Root's committed counterpart
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"a foreign file inside a folder that only looks like Qronos's")
        journal(probe, [f"O {identity(foreign)} res\\someone-elses.txt", "W", "C"])
        keep[foreign] = foreign.read_bytes()
    elif case == "malformed_line":
        journal(probe, [set_aside(probe, "qronos-probe.exe"), "N res2\\two.txt", "W"])
    elif case == "foreign_journal_header":
        journal(probe, [set_aside(probe, "qronos-probe.exe"), "W"], header="SOMEONE-ELSES-JOURNAL 1")
    elif case == "no_journal_but_kept_files":
        set_aside(probe, "qronos-probe.exe")
    elif case == "unsafe_entry":
        journal(probe, [set_aside(probe, "qronos-probe.exe"), "K ..\\outside"])
    keep[victim] = victim.read_bytes()
    return keep


LEFT_OVER = ["interrupted_while_setting_aside", "interrupted_after_writing", "committed_but_unfinished",
             "truncated_last_line", "forged_new_file_with_a_private_files_real_identity",
             "forged_committed_kept_file_with_a_foreign_files_real_identity", "malformed_line",
             "foreign_journal_header", "no_journal_but_kept_files", "unsafe_entry"]


@pytest.mark.parametrize("case", LEFT_OVER)
def test_anything_an_earlier_run_left_is_refused_with_nothing_moved_or_deleted(interrupted, case):
    """A transaction folder left by an EARLIER run is untrusted input (headers can be written by anyone, file
    identities are observable, lists can be copied): genuine interrupted states and forgeries alike are refused
    before anything is moved or deleted, and the message says where the kept previous files are."""
    probe, _ = interrupted
    keep = _plant_left_over(probe, case)
    before = state(probe)
    code = recover_then_stop(probe)
    assert code != 0
    assert "cannot prove what" in probe.read_result() and "\\old" in probe.read_result(), probe.read_result()
    assert state(probe)["files"] == before["files"], "a refused recovery changed something"
    for path, data in keep.items():
        assert path.read_bytes() == data, path


def test_an_empty_folder_left_by_an_earlier_run_is_removed(interrupted):
    probe, before = interrupted  # the empty .qronos-update folder only
    recover_then_stop(probe)
    assert "WebView2" in probe.read_result(), probe.read_result()  # recovery passed; the run stopped later
    assert_previous_usable(probe, before)


def test_a_folder_whose_file_list_was_set_aside_is_recognised_and_refused_not_misread(interrupted):
    probe, _ = interrupted
    journal(probe, [set_aside(probe, entry) for entry in OLD] + [set_aside(probe, install_manifest.FILE_NAME)])
    before = state(probe)
    code = recover_then_stop(probe)
    assert code != 0 and "earlier Qronos update was interrupted" in probe.read_result(), probe.read_result()
    assert state(probe)["files"] == before["files"]


def test_a_journal_header_alone_never_makes_a_foreign_folder_a_qronos_installation(tmp_path):
    env = tt.Env(tmp_path)
    probe = env.probe("foreign-root")
    user = probe.instdir / "user-file.txt"
    user.parent.mkdir(parents=True)
    user.write_bytes(b"a file in a folder that is not a Qronos installation")
    (probe.instdir / TXN).mkdir()
    journal(probe, [f"N {identity(user)} user-file.txt", "W"])
    before = state(probe)
    code = install(probe)
    assert code != 0
    assert "not a Qronos installation" in probe.read_result(), probe.read_result()
    assert state(probe)["files"] == before["files"] and user.read_bytes().startswith(b"a file in a folder")


def test_an_incoming_list_with_an_extra_entry_is_never_committed(tmp_path):
    from tests.fixtures.nsis_full_template import payload_manifest_files

    env = tt.Env(tmp_path)
    probe = env.probe("mismatch-extra", owned_manifest_files=payload_manifest_files() + ["res3/extra.txt"])
    before = plant_previous(probe)
    code = install(probe)
    assert code != 0
    assert probe.read_result() == "step failed: the update commit", probe.read_result()
    assert_previous_usable(probe, before)


def test_an_incoming_list_missing_a_written_file_is_never_committed(tmp_path):
    """The installed list differs from the claims, so nothing is committed and the previous files come back. The
    one file written WITHOUT a claim cannot be proved to be this run's and is left in place (limit; build_desktop
    makes both lists from one file and verifies the package against it, so a shipped installer has no such gap)."""
    from tests.fixtures.nsis_full_template import payload_manifest_files

    env = tt.Env(tmp_path)
    probe = env.probe("mismatch-missing",
                      owned_manifest_files=[f for f in payload_manifest_files() if f != "res2/two.txt"])
    before = plant_previous(probe)
    code = install(probe)
    assert code != 0
    assert probe.read_result() == "step failed: the update commit", probe.read_result()
    after = state(probe)
    unclaimed = {k: v for k, v in after["files"].items() if k not in before["files"]}
    assert set(unclaimed) <= {"res2", "res2\\two.txt"}, unclaimed
    assert {k: v for k, v in after["files"].items() if k in before["files"]} == before["files"]
    assert not (probe.instdir / TXN).exists()


def test_the_uninstaller_refuses_while_an_update_is_unfinished(tmp_path):
    env = tt.Env(tmp_path)
    probe = env.probe("uninstall")
    assert install(probe) == 0, probe.read_result()
    (probe.instdir / TXN).mkdir()
    (probe.instdir / TXN / "journal").write_bytes((HEADER + "\r\n").encode("utf-16-le"))
    before = state(probe)
    code = tt._uninstall_into(probe, probe.instdir)
    assert code != 0
    assert "interrupted" in probe.read_result(), probe.read_result()
    assert state(probe)["files"] == before["files"]


# ------------------------------------------------------------------------------------- source facts


def test_the_hooks_name_shortcut_folders_only_where_the_fixture_replaces_them():
    from tests.fixtures import nsis_full_template as full

    hooks = Path(full.HOOKS)
    counts = {name: {var: len(re.findall(re.escape(var) + r"(?![A-Za-z])", (hooks / name).read_text(encoding="utf-8")))
                     for var in ("$SMPROGRAMS", "$DESKTOP")}
              for name in ("webview2-offline.nsh", "qronos-install-root.nsh", "qronos-update-fence.nsh",
                           "qronos-update-transaction.nsh")}
    expected = {name: full.HOOK_FOLDERS.get(name, {"$SMPROGRAMS": 0, "$DESKTOP": 0}) for name in counts}
    assert counts == expected


def test_the_production_wrapper_must_name_the_install_manifest(tmp_path):
    from build_support import desktop_prerequisites as prerequisites

    pin = {"sha256": "0" * 64, "bytes": 1, "runtime": {"version": "1.2.3.4"}}
    wrapper = prerequisites.hook_wrapper(tmp_path / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe", pin,
                                         owned_manifest=tmp_path / "qronos-install-manifest.txt",
                                         vcredist=(tmp_path / "vc_redist.x64.exe", prerequisites.load_vcredist_pin()))
    prerequisites.verify_hook_wrapper(wrapper)
    with pytest.raises(prerequisites.PrerequisiteError):
        prerequisites.verify_hook_wrapper(wrapper.replace("QRONOS_OWNED_MANIFEST", "QRONOS_OTHER"))
    for probe_value in ("QRONOS_PROBE_COMMIT_FAULT", "QRONOS_PROBE_LOCK_AFTER_CLAIM"):
        with pytest.raises(prerequisites.PrerequisiteError):
            prerequisites.verify_hook_wrapper(wrapper + f'!define {probe_value} "1"\n')
