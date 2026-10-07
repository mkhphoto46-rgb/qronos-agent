from __future__ import annotations

from email.message import Message
import json
import os
from pathlib import Path, PurePosixPath

import pytest

from build_support import runtime_notices
from build_support.runtime_notices import (
    NoticeError,
    frozen_contents,
    load_reviews,
    plan_runtime_notices,
    prune_portaudio,
    write_runtime_notices,
)


class FakeDistribution:
    """Just the importlib.metadata surface the collector reads."""

    def __init__(self, site: Path, name: str, version: str, files: dict[str, str], metadata: dict[str, object]):
        self._site = site
        self.version = version
        self.files = [PurePosixPath(relative) for relative in files]
        self.metadata = Message()
        self.metadata["Name"] = name
        self.metadata["Version"] = version
        for key, value in metadata.items():
            for item in value if isinstance(value, list) else [value]:
                self.metadata[key] = item
        for relative, text in files.items():
            path = site / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def locate_file(self, path):
        return self._site / path


def link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Directory links are unavailable here.")
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


class Fixture:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.site = tmp_path / "site-packages"
        self.python = tmp_path / "python"
        self.bundle = tmp_path / "bundle"
        self.internal = self.bundle / "_internal"
        (self.python / "DLLs").mkdir(parents=True)
        (self.python / "LICENSE.txt").write_text("Python license text\nbzip2 and libffi terms\n", encoding="utf-8")
        (self.python / "python313.dll").write_bytes(b"python core")
        (self.python / "DLLs" / "_bz2.pyd").write_bytes(b"bz2 extension")
        self.internal.mkdir(parents=True)
        for relative, data in {
            "python313.dll": b"python core",
            "_bz2.pyd": b"bz2 extension",
            "_ssl.pyd": b"stdlib extension",
            "base_library.zip": b"stdlib",
            "vcruntime140.dll": b"microsoft runtime",
            "config/brains.json": b"{}",
            "examplepkg/_speedups.pyd": b"compiled",
            "examplepkg/native.dll": b"native",
        }.items():
            self.add_bundle_file(relative, data)
        self.archive = ["core", "core.config", "examplepkg", "examplepkg.core", "json", "pyimod01_archive",
                        "pyi_rth_inspect", "runtime_entry"]
        self.distributions = [
            self.install("examplepkg", "1.0", {
                "examplepkg/__init__.py": "code",
                "examplepkg/_speedups.pyd": "compiled",
                "examplepkg/native.dll": "native",
                "examplepkg/NOTICE": "Example NOTICE text",
                "examplepkg-1.0.dist-info/METADATA": "metadata",
                "examplepkg-1.0.dist-info/licenses/LICENSE.txt": "Example license text",
            }, {"License-Expression": "MIT", "License-File": "LICENSE.txt"}),
            self.install("pyinstaller", "6.0", {
                "PyInstaller/__init__.py": "code",
                "pyinstaller-6.0.dist-info/licenses/COPYING.txt": "PyInstaller license text",
            }, {"License": "GPLv2-or-later with a special exception"}),
            self.install("unrelated", "2.0", {
                "unrelated/__init__.py": "code",
                "unrelated-2.0.dist-info/LICENSE": "Must not be collected",
            }, {"License-Expression": "BSD-3-Clause"}),
        ]

    def install(self, name, version, files, metadata):
        return FakeDistribution(self.site, name, version, files, metadata)

    def add_bundle_file(self, relative: str, data: bytes = b"data") -> None:
        path = self.internal / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def plan(self, **overrides):
        options = {
            "distributions": self.distributions,
            "python_base": self.python,
            "python_version": "3.13.13",
            "stdlib_names": {"json", "_ssl", "_bz2"},
            "reviews": {},
            "bundle_internal": self.internal,
        }
        options.update(overrides)
        return plan_runtime_notices(frozen_contents(self.bundle, self.archive), **options)


@pytest.fixture
def fixture(tmp_path):
    return Fixture(tmp_path)


def entry(plan, name):
    return next(item for item in plan.inventory["distributions"] if item["name"] == name)


def test_inventory_maps_only_bundled_distributions_and_copies_their_notices(fixture):
    plan = fixture.plan()
    assert [item["name"] for item in plan.inventory["distributions"]] == ["examplepkg", "pyinstaller"]
    example = entry(plan, "examplepkg")
    assert example["version"] == "1.0"
    assert example["declaredLicense"]["expression"] == "MIT"
    assert example["frozenRoots"] == ["examplepkg"]
    assert example["nativeLibraries"] == ["examplepkg/native.dll"]
    assert example["notices"] == [
        "third-party-notices/python/examplepkg-1.0/examplepkg-1.0.dist-info/licenses/LICENSE.txt",
        "third-party-notices/python/examplepkg-1.0/examplepkg/NOTICE",
    ]
    assert "bootloader (inside the runtime executable)" in entry(plan, "pyinstaller")["frozenRoots"]
    assert all("unrelated" not in copy.destination for copy in plan.copies)

    target = write_runtime_notices(plan, fixture.bundle)
    copied = fixture.bundle / "third-party-notices/python/examplepkg-1.0/examplepkg/NOTICE"
    assert copied.read_text(encoding="utf-8") == "Example NOTICE text"
    assert (target / "python/cpython-3.13.13/LICENSE.txt").read_text(encoding="utf-8").startswith("Python license text")
    assert (target / "python/pyinstaller-6.0/pyinstaller-6.0.dist-info/licenses/COPYING.txt").is_file()
    assert {"README.txt", "inventory.json", "python"} == {item.name for item in target.iterdir()}


def test_inventory_separates_cpython_microsoft_and_project_owned_pieces(fixture):
    inventory = fixture.plan().inventory
    assert inventory["cpython"]["frozenRoots"] == ["_bz2.pyd", "_ssl.pyd", "base_library.zip", "json", "python313.dll"]
    assert inventory["cpython"]["notices"] == ["third-party-notices/python/cpython-3.13.13/LICENSE.txt"]
    assert inventory["cpython"]["bundledComponents"] == [
        {"component": "bzip2", "files": ["_bz2.pyd"], "namedInLicenseText": True},
    ]
    assert inventory["microsoftRuntime"]["files"] == [{"path": "vcruntime140.dll", "deliveredBy": "bundle top level"}]
    assert inventory["microsoftRuntime"]["status"] == "review-required"
    assert inventory["projectOwned"]["frozenRoots"] == ["config", "core", "runtime_entry"]
    assert inventory["projectOwned"]["license"] == "unspecified"


def test_unnamed_cpython_component_is_left_for_review(fixture):
    (fixture.python / "DLLs" / "sqlite3.dll").write_bytes(b"sqlite")
    fixture.add_bundle_file("sqlite3.dll", b"sqlite")
    plan = fixture.plan()
    assert {"component": "SQLite", "files": ["sqlite3.dll"], "namedInLicenseText": False} in plan.inventory["cpython"]["bundledComponents"]
    write_runtime_notices(plan, fixture.root / "out")
    readme = (fixture.root / "out/third-party-notices/README.txt").read_text(encoding="utf-8")
    assert "review required" in readme and "SQLite" in readme


def test_microsoft_runtime_inside_a_package_is_flagged_with_its_owner(fixture):
    fixture.distributions[0].files.append(PurePosixPath("examplepkg/msvcp140-0123456789abcdef.dll"))
    fixture.add_bundle_file("examplepkg/msvcp140-0123456789abcdef.dll")
    files = fixture.plan().inventory["microsoftRuntime"]["files"]
    assert {"path": "examplepkg/msvcp140-0123456789abcdef.dll", "deliveredBy": "examplepkg"} in files


def test_inventory_is_deterministic_and_has_no_workstation_paths(fixture):
    first = write_runtime_notices(fixture.plan(), fixture.root / "first")
    second = write_runtime_notices(fixture.plan(), fixture.root / "second")
    for name in ("inventory.json", "README.txt"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    text = (first / "inventory.json").read_text(encoding="utf-8")
    assert str(fixture.root) not in text
    assert str(fixture.root.as_posix()) not in text
    assert "\\" not in text
    assert json.loads(text)["schemaVersion"] == 1


def test_missing_license_coverage_fails_the_build(fixture):
    fixture.distributions[0] = fixture.install("examplepkg", "1.0", {
        "examplepkg/__init__.py": "code",
        "examplepkg/_speedups.pyd": "compiled",
        "examplepkg/native.dll": "native",
    }, {"License-Expression": "MIT"})
    with pytest.raises(NoticeError, match="examplepkg 1.0"):
        fixture.plan()


def test_reviewed_exception_must_match_the_exact_version(fixture):
    fixture.distributions[0] = fixture.install("examplepkg", "1.0", {
        "examplepkg/__init__.py": "code",
    }, {})
    reviews = {("examplepkg", "0.9"): {"provenance": "older release", "noNoticeReason": "reviewed"}}
    with pytest.raises(NoticeError, match="examplepkg 1.0"):
        fixture.plan(reviews=reviews)
    reviews = {("examplepkg", "1.0"): {"provenance": "Wheel from the package index", "noNoticeReason": "Reviewed by the owner"}}
    review = entry(fixture.plan(reviews=reviews), "examplepkg")["review"]
    assert review == {"provenance": "Wheel from the package index", "noNoticeReason": "Reviewed by the owner"}


def test_unattributed_frozen_file_fails_the_build(fixture):
    fixture.add_bundle_file("mystery.dll")
    with pytest.raises(NoticeError, match="mystery.dll"):
        fixture.plan()


def test_unattributed_frozen_module_fails_the_build(fixture):
    fixture.archive.append("vendored_without_metadata.helpers")
    with pytest.raises(NoticeError, match="vendored_without_metadata"):
        fixture.plan()


def test_bundled_metadata_must_match_the_build_environment(fixture):
    fixture.add_bundle_file("examplepkg-9.9.dist-info/METADATA")
    with pytest.raises(NoticeError, match="does not match"):
        fixture.plan()


def test_frozen_name_claimed_by_two_distributions_fails(fixture):
    fixture.distributions.append(fixture.install("impostor", "1.0", {
        "examplepkg/other.py": "code",
        "impostor-1.0.dist-info/LICENSE": "text",
    }, {}))
    with pytest.raises(NoticeError, match="more than one distribution"):
        fixture.plan()


def test_pyinstaller_pieces_require_pyinstaller_metadata(fixture):
    fixture.distributions = [item for item in fixture.distributions if item.metadata["Name"] != "pyinstaller"]
    with pytest.raises(NoticeError, match="PyInstaller"):
        fixture.plan()


def test_missing_cpython_license_fails(fixture):
    (fixture.python / "LICENSE.txt").unlink()
    with pytest.raises(NoticeError, match="CPython LICENSE"):
        fixture.plan()


def test_notice_reached_through_a_link_outside_the_distribution_is_refused(fixture):
    outside = fixture.root / "outside"
    outside.mkdir()
    (outside / "LICENSE").write_text("private file outside the distribution", encoding="utf-8")
    link_directory(fixture.site / "linkedpkg", outside)
    fixture.distributions.append(FakeDistribution(fixture.site, "linkedpkg", "1.0", {}, {}))
    fixture.distributions[-1].files = [PurePosixPath("linkedpkg/__init__.py"), PurePosixPath("linkedpkg/LICENSE")]
    fixture.archive.append("linkedpkg")
    with pytest.raises(NoticeError, match="escapes"):
        fixture.plan()


def test_link_inside_the_frozen_bundle_is_refused(fixture):
    outside = fixture.root / "outside"
    outside.mkdir()
    link_directory(fixture.internal / "linked", outside)
    with pytest.raises(NoticeError, match="link or reparse"):
        frozen_contents(fixture.bundle, fixture.archive)


def test_paths_outside_the_distribution_are_never_read(fixture):
    fixture.distributions[0].files.append(PurePosixPath("../../outside/LICENSE"))
    plan = fixture.plan()
    assert all(".." not in copy.destination for copy in plan.copies)
    reviews = {("examplepkg", "1.0"): {"provenance": "test", "supplementalNotices": ["../outside/LICENSE"]}}
    with pytest.raises(NoticeError, match="Unsafe"):
        fixture.plan(reviews=reviews)


def test_existing_bundle_notices_are_preserved_and_never_overwritten(fixture):
    fixture.add_bundle_file("examplepkg-1.0.dist-info/licenses/LICENSE.txt", b"Example license text")
    plan = fixture.plan()
    assert "examplepkg-1.0.dist-info" in entry(plan, "examplepkg")["frozenRoots"]
    write_runtime_notices(plan, fixture.bundle)
    assert (fixture.internal / "examplepkg-1.0.dist-info/licenses/LICENSE.txt").read_bytes() == b"Example license text"
    with pytest.raises(NoticeError, match="already exist"):
        write_runtime_notices(plan, fixture.bundle)


def test_native_package_needs_a_reviewed_provenance_record(fixture):
    fixture.distributions.append(fixture.install("sounddevice", "0.5.6", {
        "sounddevice.py": "code",
        "_sounddevice_data/portaudio-binaries/README.md": "PortAudio attribution",
        "_sounddevice_data/portaudio-binaries/libportaudio64bit.dll": "native",
        "sounddevice-0.5.6.dist-info/licenses/LICENSE": "sounddevice license",
    }, {"License-Expression": "MIT"}))
    fixture.add_bundle_file("_sounddevice_data/portaudio-binaries/libportaudio64bit.dll")
    fixture.archive.append("sounddevice")
    with pytest.raises(NoticeError, match="sounddevice 0.5.6"):
        fixture.plan()
    review = {
        "provenance": "Shipped inside the sounddevice wheel.",
        "supplementalNotices": ["_sounddevice_data/portaudio-binaries/README.md"],
        "unresolved": "The upstream license text is not shipped.",
    }
    plan = fixture.plan(reviews={("sounddevice", "0.5.6"): review}, pruned=["_internal/_sounddevice_data/portaudio-binaries/libportaudio64bit-asio.dll"])
    sounddevice = entry(plan, "sounddevice")
    assert "third-party-notices/python/sounddevice-0.5.6/_sounddevice_data/portaudio-binaries/README.md" in sounddevice["notices"]
    assert sounddevice["review"]["unresolved"] == "The upstream license text is not shipped."
    write_runtime_notices(plan, fixture.root / "out")
    readme = (fixture.root / "out/third-party-notices/README.txt").read_text(encoding="utf-8")
    assert "The upstream license text is not shipped." in readme
    assert "libportaudio64bit-asio.dll" in readme


def test_reviewed_notice_must_belong_to_the_distribution(fixture):
    review = {("examplepkg", "1.0"): {"provenance": "test", "supplementalNotices": ["examplepkg/README.md"]}}
    (fixture.site / "examplepkg/README.md").write_text("not in RECORD", encoding="utf-8")
    with pytest.raises(NoticeError, match="not part of"):
        fixture.plan(reviews=review)


def test_review_file_requires_exact_identity_and_provenance(tmp_path):
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps({"reviews": [{"name": "Sound_Device", "version": "1.0", "provenance": "wheel", "supplementalNotices": ["README.md"]}]}), encoding="utf-8")
    assert set(load_reviews(path)) == {("sound-device", "1.0")}
    for invalid in (
        {"name": "x", "version": "1.0", "supplementalNotices": ["README.md"]},
        {"name": "x", "version": "1.0", "provenance": "wheel"},
        {"name": "x", "provenance": "wheel", "noNoticeReason": "reviewed"},
    ):
        path.write_text(json.dumps({"reviews": [invalid]}), encoding="utf-8")
        with pytest.raises(NoticeError):
            load_reviews(path)
    assert load_reviews(tmp_path / "absent.json") == {}


def test_committed_reviews_record_exact_versions_and_provenance():
    reviews = load_reviews()
    assert reviews, "the sounddevice PortAudio review record is expected"
    for (name, version), review in reviews.items():
        assert name and version and review["provenance"].strip()


def portaudio_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    directory = bundle.joinpath("_internal", *runtime_notices.PORTAUDIO_DIRECTORY)
    directory.mkdir(parents=True)
    for name in ("libportaudio32bit-asio.dll", "libportaudio32bit.dll", "libportaudio64bit-asio.dll",
                 "libportaudio64bit.dll", "libportaudioarm64-asio.dll", "libportaudioarm64.dll",
                 "libportaudio.dylib", "README.md"):
        (directory / name).write_bytes(name.encode())
    return bundle


def test_prune_keeps_only_the_x64_non_asio_portaudio_dll(tmp_path):
    bundle = portaudio_bundle(tmp_path)
    removed = prune_portaudio(bundle)
    assert removed == tuple(
        f"_internal/_sounddevice_data/portaudio-binaries/{name}"
        for name in ("libportaudio32bit-asio.dll", "libportaudio32bit.dll", "libportaudio64bit-asio.dll",
                     "libportaudioarm64-asio.dll", "libportaudioarm64.dll")
    )
    remaining = sorted(item.name for item in bundle.joinpath("_internal", *runtime_notices.PORTAUDIO_DIRECTORY).iterdir())
    assert remaining == ["README.md", "libportaudio.dylib", "libportaudio64bit.dll"]


def test_prune_refuses_a_bundle_without_the_supported_dll(tmp_path):
    bundle = portaudio_bundle(tmp_path)
    directory = bundle.joinpath("_internal", *runtime_notices.PORTAUDIO_DIRECTORY)
    (directory / "libportaudio64bit.dll").unlink()
    with pytest.raises(NoticeError, match="libportaudio64bit.dll"):
        prune_portaudio(bundle)
    assert (directory / "libportaudio64bit-asio.dll").exists()


def test_prune_refuses_links_without_removing_anything(tmp_path):
    bundle = portaudio_bundle(tmp_path)
    directory = bundle.joinpath("_internal", *runtime_notices.PORTAUDIO_DIRECTORY)
    outside = tmp_path / "outside"
    outside.mkdir()
    link_directory(directory / "linked", outside)
    with pytest.raises(NoticeError, match="link or reparse"):
        prune_portaudio(bundle)
    assert (directory / "libportaudio64bit-asio.dll").exists()
