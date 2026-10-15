"""sdk_setup.install unpacks ONLY the verified pinned archives into the pinned package paths, never runs sdkmanager,
and accepts the result only when the installed source.properties revisions equal the pins.

Fixture only: tiny real zip files in tmp_path stand in for the archives (their pins are patched to the fixture's
size and SHA-1), the SDK root is a tmp_path folder, and every subprocess entry point raises. No download, no SDK,
no licence. Revision agreement is checked, not byte identity of every installed file."""
from __future__ import annotations

import copy
import hashlib
import stat
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sdk_setup  # noqa: E402

PINNED = {"platforms;android-36": "2", "build-tools;36.0.0": "36.0.0"}
ROOTS = {"platforms;android-36": "android-36", "build-tools;36.0.0": "android-16"}


def props(revision: str) -> str:
    return f"Pkg.Desc=fixture\nPkg.Revision={revision}\n#Pkg.Revision={revision} rc5\n"


def write_zip(path: Path, entries: list[tuple[str, str]], *, symlink: str | None = None) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in entries:
            archive.writestr(name, text)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    """The SDK root and cache in tmp_path; build(name, entries) writes an archive and pins it."""
    pins = copy.deepcopy(sdk_setup.PINS)
    monkeypatch.setattr(sdk_setup, "PINS", pins)
    monkeypatch.setattr(sdk_setup, "SDK_ROOT", tmp_path / "sdk")
    monkeypatch.setattr(sdk_setup, "CACHE", tmp_path / "cache")
    (tmp_path / "cache").mkdir()

    def never(*args, **kwargs):
        raise AssertionError("no subprocess (sdkmanager / Android CLI) may run")

    for name in ("call", "run", "Popen", "check_call", "check_output"):
        monkeypatch.setattr(sdk_setup.subprocess, name, never)

    def build(name: str, entries: list[tuple[str, str]], **options) -> None:
        path = sdk_setup._archive(name)
        write_zip(path, entries, **options)
        data = path.read_bytes()
        pins["packages"][name].update({"size": len(data), "sha1": hashlib.sha1(data).hexdigest()})

    def good(name: str, revision: str | None = None) -> None:
        root = ROOTS[name]
        build(name, [(f"{root}/source.properties", props(revision or PINNED[name])), (f"{root}/bin/tool.txt", "x")])

    return tmp_path / "sdk", build, good


def installed(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def test_the_pins_are_the_committed_selection():
    assert {name: sdk_setup.PINS["packages"][name]["revision"] for name in sdk_setup.INSTALL} == PINNED
    assert sdk_setup.ARCHIVE_ROOTS == ROOTS


def test_the_verified_archives_are_unpacked_into_the_pinned_paths_without_sdkmanager(sdk):
    root, _, good = sdk
    for name in PINNED:
        good(name)
    assert sdk_setup.install(accept_licence=True) == 0
    assert installed(root) == {"platforms/android-36/source.properties", "platforms/android-36/bin/tool.txt",
                               "build-tools/36.0.0/source.properties", "build-tools/36.0.0/bin/tool.txt"}
    assert sdk_setup.install(accept_licence=True) == 0  # already installed at the pins: nothing rewritten


def test_without_the_explicit_licence_acceptance_nothing_is_written(sdk):
    root, _, good = sdk
    for name in PINNED:
        good(name)
    assert sdk_setup.install(accept_licence=False) == 2
    assert installed(root) == set()


@pytest.mark.parametrize("name, revision", [("platforms;android-36", "3"), ("build-tools;36.0.0", "36.1.0")])
def test_an_archive_with_another_revision_fails(sdk, name, revision):
    _, _, good = sdk
    for other in PINNED:
        good(other, revision if other == name else None)
    assert sdk_setup.install(accept_licence=True) == 1


@pytest.mark.parametrize("entries, symlink", [
    ([("android-36/source.properties", props("2")), ("android-36/../escape.txt", "x")], None),   # traversal
    ([("android-36/source.properties", props("2")), ("/abs/escape.txt", "x")], None),             # absolute
    ([("android-36/source.properties", props("2")), ("other/source.properties", "x")], None),     # wrong root
    ([("android-36/source.properties", props("2"))], "android-36/link"),                          # symlink
    ([("android-36/bin/tool.txt", "x")], None),                                                   # no metadata
], ids=["traversal", "absolute", "wrong-root", "symlink", "no-metadata"])
def test_unsafe_or_wrong_archive_content_is_refused_before_any_write(sdk, entries, symlink):
    root, build, good = sdk
    build("platforms;android-36", entries, symlink=symlink)
    good("build-tools;36.0.0")
    assert sdk_setup.install(accept_licence=True) == 1
    assert not (root / "platforms").exists() and not (root.parent / "escape.txt").exists()


def test_an_archive_that_is_not_the_pinned_file_is_refused(sdk):
    root, _, good = sdk
    for name in PINNED:
        good(name)
    sdk_setup.PINS["packages"]["platforms;android-36"]["sha1"] = "0" * 40
    assert sdk_setup.install(accept_licence=True) == 1
    assert installed(root) == set()


def test_an_existing_mismatching_package_is_not_overwritten(sdk):
    root, _, good = sdk
    for name in PINNED:
        good(name)
    existing = root / "platforms" / "android-36"
    existing.mkdir(parents=True)
    (existing / "source.properties").write_text(props("1"), encoding="utf-8")
    assert sdk_setup.install(accept_licence=True) == 1
    assert (existing / "source.properties").read_text(encoding="utf-8") == props("1")
