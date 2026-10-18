"""Qronos's own Apache-2.0 LICENSE and NOTICE (build_support/project_licence.py) and how the desktop notices
carry them. Fixtures write only to tmp_path; the repository's LICENSE, NOTICE and .gitattributes are only read.
The runtime and installer sides are covered in test_runtime_notices.py and test_desktop_installer.py."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from build_support import desktop_notices as notices
from build_support import project_licence

ROOT = Path(__file__).resolve().parent.parent
OFFICIAL_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"  # apache.org LICENSE-2.0.txt


def test_the_repository_licence_is_the_official_apache_text_and_notice_states_its_scope():
    licence = (ROOT / "LICENSE").read_bytes()
    assert hashlib.sha256(licence).hexdigest() == OFFICIAL_SHA256 == project_licence.LICENSE_SHA256
    assert len(licence) == project_licence.LICENSE_BYTES and b"\r" not in licence
    assert licence.startswith(b"\n                                 Apache License\n                           Version 2.0")
    found = project_licence.read()
    assert set(found) == {"LICENSE", "NOTICE"}
    notice = found["NOTICE"].decode("utf-8")
    for excluded in ("desktop/src-tauri/installer-template/", "cpython_notice_texts/",
                     "desktop_notice_upstream/", "runtime_notice_texts/", "build_support/runtime_assets.json",
                     "CC BY-NC-SA 4.0"):
        assert excluded in notice, excluded


def test_checkouts_keep_the_exact_bytes():
    attributes = subprocess.run(["git", "check-attr", "text", "--", "LICENSE", "NOTICE"], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout.splitlines()
    assert attributes == ["LICENSE: text: unset", "NOTICE: text: unset"]


@pytest.mark.parametrize(("name", "data", "message"), [
    ("LICENSE", b"MIT License\n", "official Apache License 2.0 text"),
    ("LICENSE", (ROOT / "LICENSE").read_bytes().replace(b"\n", b"\r\n"), "official Apache License 2.0 text"),
    ("LICENSE", (ROOT / "LICENSE").read_bytes() + b"\n", "official Apache License 2.0 text"),
    ("NOTICE", b"", "must state"),
    ("NOTICE", b"Qronos is licensed under the Apache License, Version 2.0.\n", "must state"),
    ("NOTICE", b"\xff\xfe", "not UTF-8"),
    ("COPYING", b"x", "not a project licence file"),
])
def test_anything_but_the_exact_licence_and_a_scoped_notice_is_refused(name, data, message):
    with pytest.raises(project_licence.ProjectLicenceError, match=message):
        project_licence.check(name, data)


def test_a_distributed_folder_must_hold_exactly_the_two_files(tmp_path):
    folder = tmp_path / "qronos"
    folder.mkdir()
    for name, data in project_licence.read().items():
        (folder / name).write_bytes(data)
    assert project_licence.verify_folder(folder)["LICENSE"]["sha256"] == OFFICIAL_SHA256
    (folder / "EXTRA").write_bytes(b"x")
    with pytest.raises(project_licence.ProjectLicenceError, match="not exactly"):
        project_licence.verify_folder(folder)
    (folder / "EXTRA").unlink()
    (folder / "NOTICE").unlink()
    with pytest.raises(project_licence.ProjectLicenceError, match="not exactly"):
        project_licence.verify_folder(folder)


def one_component(tmp_path: Path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "LICENSE").write_text("Permission is hereby granted, free of charge, to any person.\n"
                                     "THE SOFTWARE IS PROVIDED AS IS.\n", encoding="utf-8")
    component = notices.Component("rust", "demo", "1.0.0", "MIT", None,
                                  "registry+https://github.com/rust-lang/crates.io-index", "sum", package.resolve())
    return [component], notices.check_coverage([component], {})


def test_desktop_notices_carry_the_project_licence_in_their_manifest(tmp_path):
    components, files = one_component(tmp_path)
    out = tmp_path / "notices"
    inventory = notices.write_notices(components, files, {}, out, notices.WINDOWS_TARGET, (),
                                      project=project_licence.read())
    assert (out / "qronos" / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    assert (out / "qronos" / "NOTICE").read_bytes() == (ROOT / "NOTICE").read_bytes()
    manifest = notices.verify_notice_output(out)
    listed = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    assert listed["qronos/LICENSE"] == OFFICIAL_SHA256
    assert inventory["projectOwned"]["license"] == "Apache-2.0"
    assert [entry["path"] for entry in inventory["projectOwned"]["files"]] == ["qronos/LICENSE", "qronos/NOTICE"]
    assert json.loads((out / notices.INVENTORY).read_text(encoding="utf-8"))["projectOwned"] == inventory["projectOwned"]
    header = (out / notices.NOTICE_TEXT).read_text(encoding="utf-8").split("-" * 72, 1)[0]
    assert "licensed under the Apache License, Version 2.0 (qronos/LICENSE and qronos/NOTICE)" in header
    assert "keep their own licences and are not relicensed" in header


@pytest.mark.parametrize(("project", "message"), [
    ({"LICENSE": b"edited", "NOTICE": (ROOT / "NOTICE").read_bytes()}, "official Apache License 2.0 text"),
    ({"LICENSE": (ROOT / "LICENSE").read_bytes()}, "must be exactly"),
    ({}, "must be exactly"),
])
def test_desktop_notices_with_a_wrong_project_licence_write_nothing(tmp_path, project, message):
    components, files = one_component(tmp_path)
    out = tmp_path / "notices"
    with pytest.raises(notices.NoticeError, match=message):
        notices.write_notices(components, files, {}, out, notices.WINDOWS_TARGET, (), project=project)
    assert not out.exists()


def test_the_readmes_state_the_adopted_licence_and_its_scope():
    english = (ROOT / "README.en.md").read_text(encoding="utf-8")
    persian = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "No licence has been chosen yet." not in english
    assert "Apache License 2.0" in english and "NOTICE" in english
    assert "Apache License 2.0" in persian and "NOTICE" in persian
