from __future__ import annotations

import base64
from email.message import Message
import hashlib
import importlib.metadata
from importlib.metadata import FileHash, PackagePath
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import sysconfig

import pytest

from build_support import cpython_notices, runtime_notices
from build_support.runtime_notices import (
    NoticeError,
    frozen_contents,
    load_reviews,
    plan_runtime_notices,
    prune_portaudio,
    write_runtime_notices,
)
from tests.fixtures.cpython_registry import BUILD, PLATFORM, RegistryBuilder


class FakeDistribution:
    """Just the importlib.metadata surface the collector reads."""

    def __init__(self, site: Path, name: str, version: str, files: dict[str, str], metadata: dict[str, object]):
        self._site = site
        self.version = version
        self.files = []
        for relative, text in files.items():
            entry = PackagePath(relative)
            digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode("utf-8")).digest()).rstrip(b"=").decode()
            entry.hash = FileHash(f"sha256={digest}")
            self.files.append(entry)
        self.metadata = Message()
        self.metadata["Name"] = name
        self.metadata["Version"] = version
        for key, value in metadata.items():
            for item in value if isinstance(value, list) else [value]:
                self.metadata[key] = item
        for relative, text in files.items():
            path = site / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))

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
        (self.python / "DLLs" / "_ssl.pyd").write_bytes(b"stdlib extension")
        (self.python / "DLLs" / "sqlite3.dll").write_bytes(b"sqlite")
        (self.python / "vcruntime140.dll").write_bytes(b"microsoft runtime")
        (self.python / "tcl" / "tcl8.6").mkdir(parents=True)
        (self.python / "tcl" / "tcl8.6" / "init.tcl").write_bytes(b"# tcl init\n")
        (self.python / "tcl" / "tcl8.6" / "license.terms").write_bytes(b"tcl terms\n")
        self.registry_builder = RegistryBuilder(tmp_path / "registry", self.python)
        count, digest = cpython_notices.tree_digest(self.python / "tcl" / "tcl8.6")
        self.registry_raw = self.registry_builder.raw(
            {
                "python313.dll": ["zlib@1.3.1"],
                "DLLs/_bz2.pyd": ["bzip2@1.0.8"],
                "DLLs/_ssl.pyd": [],
                "DLLs/sqlite3.dll": ["sqlite@3.50.4"],
                "vcruntime140.dll": None,
            },
            {"_tcl_data": {"source": "tcl/tcl8.6", "excludes": ["demos"], "files": count, "treeSha256": digest,
                           "components": ["tcl@8.6.15"]}},
        )
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
                "examplepkg-1.0.dist-info/licenses/LICENSE.txt": "Example license text\n\nPermission is granted for this synthetic fixture only.\n",
            }, {"License-Expression": "MIT", "License-File": "LICENSE.txt"}),
            self.install("pyinstaller", "6.0", {
                "PyInstaller/__init__.py": "code",
                "pyinstaller-6.0.dist-info/licenses/COPYING.txt": "PyInstaller license text\n\nPermission is granted for this synthetic fixture only.\n",
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

    def registry(self, raw=None):
        path = self.registry_builder.write(raw or self.registry_raw)
        return cpython_notices.load_registry(path, self.registry_builder.text_root)

    def plan(self, **overrides):
        options = {
            "distributions": self.distributions,
            "python_base": self.python,
            "python_version": "3.13.13",
            "python_platform": PLATFORM,
            "python_build": BUILD,
            "stdlib_names": {"json", "_ssl", "_bz2", "ctypes", "unicodedata"},
            "reviews": {},
            "bundle_internal": self.internal,
        }
        if "cpython_registry" not in overrides:
            options["cpython_registry"] = self.registry()
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
    cpython = inventory["cpython"]
    assert cpython["frozenRoots"] == ["_bz2.pyd", "_ssl.pyd", "base_library.zip", "json", "python313.dll"]
    assert cpython["interpreter"]["build"] == BUILD and cpython["interpreter"]["id"] == "synthetic-3.13.13"
    assert [(item["path"], item["interpreterPath"], item["components"]) for item in cpython["binaries"]] == [
        ("_bz2.pyd", "DLLs/_bz2.pyd", ["bzip2@1.0.8"]),
        ("_ssl.pyd", "DLLs/_ssl.pyd", []),
        ("python313.dll", "python313.dll", ["zlib@1.3.1"]),
    ]
    assert {item["id"]: item["boundTo"] for item in cpython["components"]} == {
        "bzip2@1.0.8": ["_bz2.pyd"],
        "cpython-incorporated@3.13.13": ["CPython runtime"],
        "zlib@1.3.1": ["python313.dll"],
    }
    assert cpython["pythonRoots"] == ["base_library.zip"]
    assert cpython["support"] == {"status": "audited-build", "record": "synthetic-3.13.13", "packagingTarget": True}
    assert cpython["rightsReview"]["status"] == "owner-review-required"
    assert "not a licence or redistribution clearance" in cpython["rightsReview"]["note"]
    assert "third-party-notices/python/cpython-3.13.13/LICENSE.txt" in cpython["notices"]
    assert "third-party-notices/python/cpython-3.13.13/components/zlib-1.3.1/LICENSE" in cpython["notices"]
    assert inventory["microsoftRuntime"]["files"] == [{
        "path": "vcruntime140.dll", "deliveredBy": "bundle top level", "identicalToAuditedInterpreterFile": "vcruntime140.dll",
    }]
    assert inventory["microsoftRuntime"]["status"] == "review-required"
    assert inventory["projectOwned"]["frozenRoots"] == ["config", "core", "runtime_entry"]
    assert inventory["projectOwned"]["license"] == "unspecified"


def test_bundled_cpython_binary_copies_every_text_of_its_audited_components(fixture):
    fixture.add_bundle_file("sqlite3.dll", b"sqlite")
    plan = fixture.plan()
    sqlite = next(item for item in plan.inventory["cpython"]["components"] if item["id"] == "sqlite@3.50.4")
    assert sqlite["boundTo"] == ["sqlite3.dll"] and sqlite["attribution"] == []
    assert [text["kind"] for text in sqlite["texts"]] == ["terms-excerpt"]
    zlib = next(item for item in plan.inventory["cpython"]["components"] if item["id"] == "zlib@1.3.1")
    assert [(PurePosixPath(t["path"]).name, t["kind"]) for t in zlib["texts"]] == [("LICENSE", "license-text"), ("README", "auxiliary")]
    target = write_runtime_notices(plan, fixture.root / "out")
    components = target / "python/cpython-3.13.13/components"
    for name, text_id in (("sqlite-3.50.4/sqlite3.h.leading-comment.txt", "sqlite/sqlite3.h.leading-comment.txt"),
                          ("zlib-1.3.1/README", "zlib/README"),
                          ("cpython-incorporated-3.13.13/license.rst", "cpython/Doc/license.rst")):
        assert (components / name).read_bytes() == (fixture.registry_builder.text_root / text_id).read_bytes()
    readme = (target / "README.txt").read_text(encoding="utf-8")
    assert "SQLite 3.50.4" in readme and BUILD in readme


def test_unaudited_interpreter_build_fails_closed(fixture):
    for options in ({"python_version": "3.13.14"}, {"python_build": "tags/v3.13.13:deadbee"}, {"python_platform": "linux-x86_64"}):
        with pytest.raises(NoticeError, match="Unsupported CPython build: CPython .* is not an audited interpreter"):
            fixture.plan(**options)


def test_build_interpreter_with_changed_bytes_fails(fixture):
    (fixture.python / "DLLs" / "_ssl.pyd").write_bytes(b"patched extension")
    with pytest.raises(NoticeError, match=r"Unsupported CPython build: build interpreter file DLLs/_ssl.pyd differs"):
        fixture.plan()


def test_changed_interpreter_license_fails(fixture):
    (fixture.python / "LICENSE.txt").write_text("Different license text\n", encoding="utf-8")
    with pytest.raises(NoticeError, match="Unsupported CPython build: .*LICENSE.txt differs"):
        fixture.plan()


def test_bundled_cpython_binary_with_other_bytes_fails(fixture):
    fixture.add_bundle_file("_bz2.pyd", b"rebuilt bz2 extension")
    with pytest.raises(NoticeError, match=r"Unsupported CPython build: bundled CPython binary _bz2.pyd differs"):
        fixture.plan()


def test_bundled_cpython_binary_missing_from_the_audit_fails(fixture):
    fixture.add_bundle_file("unicodedata.pyd", b"unicode data")
    with pytest.raises(NoticeError, match="Unsupported CPython build: bundled CPython binary unicodedata.pyd is not part of audited"):
        fixture.plan()


def test_tcl_data_matching_the_audited_tree_binds_tcl(fixture):
    fixture.add_bundle_file("_tcl_data/init.tcl", b"# tcl init\n")
    fixture.add_bundle_file("_tcl_data/license.terms", b"tcl terms\n")
    trees = fixture.plan().inventory["cpython"]["dataTrees"]
    assert [(tree["path"], tree["interpreterPath"], tree["files"], tree["components"]) for tree in trees] == [
        ("_tcl_data", "tcl/tcl8.6", 2, ["tcl@8.6.15"]),
    ]


@pytest.mark.parametrize("change", ["edited", "extra", "missing"])
def test_tcl_data_differing_from_the_audited_tree_fails(fixture, change):
    fixture.add_bundle_file("_tcl_data/init.tcl", b"# edited\n" if change == "edited" else b"# tcl init\n")
    if change != "missing":
        fixture.add_bundle_file("_tcl_data/license.terms", b"tcl terms\n")
    if change == "extra":
        fixture.add_bundle_file("_tcl_data/extra.tcl", b"# extra\n")
    with pytest.raises(NoticeError, match="Unsupported CPython build: bundled data folder _tcl_data"):
        fixture.plan()


def test_tcl_data_without_an_audited_tree_fails(fixture):
    raw = fixture.registry_builder.variant(fixture.registry_raw)
    raw["interpreters"][0]["dataTrees"] = {}
    raw["components"].pop("tcl@8.6.15")
    raw["texts"].pop("tcl/license.terms")
    (fixture.registry_builder.text_root / "tcl/license.terms").unlink()
    fixture.add_bundle_file("_tcl_data/init.tcl", b"# tcl init\n")
    with pytest.raises(NoticeError, match="_tcl_data is not an audited data tree"):
        fixture.plan(cpython_registry=fixture.registry(raw))


def test_stdlib_folder_with_native_code_fails_and_without_is_python_code(fixture):
    fixture.add_bundle_file("ctypes/helper.py", b"print('x')")
    assert "ctypes" in fixture.plan().inventory["cpython"]["pythonRoots"]
    fixture.add_bundle_file("ctypes/native.pyd", b"native")
    with pytest.raises(NoticeError, match="ctypes contains native code"):
        fixture.plan()


def test_module_bound_component_follows_the_frozen_archive(fixture):
    assert "macholib@3.13.13" not in {item["id"] for item in fixture.plan().inventory["cpython"]["components"]}
    fixture.archive += ["ctypes", "ctypes.macholib", "ctypes.macholib.dyld"]
    components = {item["id"]: item for item in fixture.plan().inventory["cpython"]["components"]}
    assert components["macholib@3.13.13"]["boundTo"] == ["module ctypes.macholib"]
    assert components["macholib@3.13.13"]["unresolved"]
    assert any("macholib@3.13.13" in line for line in fixture.plan().inventory["cpython"]["unresolved"])


def test_microsoft_runtime_differing_from_the_interpreter_copy_is_recorded(fixture):
    fixture.add_bundle_file("vcruntime140.dll", b"system32 runtime")
    files = fixture.plan().inventory["microsoftRuntime"]["files"]
    assert files == [{"path": "vcruntime140.dll", "deliveredBy": "bundle top level", "identicalToAuditedInterpreterFile": None}]


def test_distribution_notice_colliding_with_cpython_notices_fails(fixture):
    fixture.distributions.append(fixture.install("cpython", "3.13.13", {
        "cpython/__init__.py": "code",
        "LICENSE.txt": "A colliding license text\n\nPermission is granted for this synthetic fixture only.\n",
    }, {"License-Expression": "MIT"}))
    fixture.archive.append("cpython")
    with pytest.raises(NoticeError, match="destination"):
        fixture.plan()


def test_microsoft_runtime_inside_a_package_is_flagged_with_its_owner(fixture):
    fixture.distributions[0].files.append(PurePosixPath("examplepkg/msvcp140-0123456789abcdef.dll"))
    fixture.add_bundle_file("examplepkg/msvcp140-0123456789abcdef.dll")
    files = fixture.plan().inventory["microsoftRuntime"]["files"]
    assert {"path": "examplepkg/msvcp140-0123456789abcdef.dll", "deliveredBy": "examplepkg",
            "identicalToAuditedInterpreterFile": None} in files


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
    # Even an exact-identity review cannot stand in for absent license text.
    reviews = {("examplepkg", "1.0"): {"provenance": "Wheel from the package index", "noNoticeReason": "Reviewed by the owner"}}
    with pytest.raises(NoticeError, match="examplepkg 1.0 \\(no non-empty license text\\)"):
        fixture.plan(reviews=reviews)


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
    with pytest.raises(NoticeError, match="link or reparse"):
        fixture.plan()


def test_notice_behind_a_junction_that_stays_inside_the_distribution_is_refused(fixture):
    real = fixture.site / "examplepkg" / "legal"
    real.mkdir()
    (real / "LICENSE").write_text("Inside license\n\nStill inside the distribution.\n", encoding="utf-8")
    link_directory(fixture.site / "examplepkg" / "alias", real)
    fixture.distributions[0].files.append(PurePosixPath("examplepkg/alias/LICENSE"))
    with pytest.raises(NoticeError, match="link or reparse"):
        fixture.plan()


@pytest.mark.parametrize("name", ["AUTHORS", "NOTICE", "COPYRIGHT", "AUTHORS.rst"])
def test_auxiliary_notice_alone_is_not_license_text(fixture, name):
    # Even when metadata declares the attribution file as its License-File.
    fixture.distributions[0] = fixture.install("examplepkg", "1.0", {
        "examplepkg/__init__.py": "code",
        "examplepkg/_speedups.pyd": "compiled",
        "examplepkg/native.dll": "native",
        f"examplepkg-1.0.dist-info/{name}": "Jane Example and contributors\nSee upstream for terms.\n",
    }, {"License-Expression": "MIT", "License-File": name})
    with pytest.raises(NoticeError, match="examplepkg 1.0 \\(no non-empty license text\\)"):
        fixture.plan()


@pytest.mark.parametrize("text", ["", "   \n\t\n", "MIT", "SPDX-License-Identifier: Apache-2.0 OR MIT\n"])
def test_empty_or_label_only_license_is_not_license_text(fixture, text):
    fixture.distributions[0] = fixture.install("examplepkg", "1.0", {
        "examplepkg/__init__.py": "code",
        "examplepkg/_speedups.pyd": "compiled",
        "examplepkg/native.dll": "native",
        "examplepkg-1.0.dist-info/licenses/LICENSE": text,
    }, {"License-Expression": "MIT"})
    with pytest.raises(NoticeError, match="no non-empty license text"):
        fixture.plan()


def test_declared_unusual_license_file_counts_and_auxiliary_notices_are_kept(fixture):
    fixture.distributions[0] = fixture.install("examplepkg", "1.0", {
        "examplepkg/__init__.py": "code",
        "examplepkg/_speedups.pyd": "compiled",
        "examplepkg/native.dll": "native",
        "examplepkg-1.0.dist-info/licenses/TERMS.md": "Example terms\n\nRedistribution is permitted.\n",
        "examplepkg-1.0.dist-info/NOTICE": "Example NOTICE\n",
    }, {"License-File": "TERMS.md"})
    plan = fixture.plan()
    files = {item["path"].rsplit("/", 1)[1]: item for item in entry(plan, "examplepkg")["noticeFiles"]}
    assert files["TERMS.md"]["kind"] == "license-text"
    assert files["NOTICE"]["kind"] == "auxiliary"
    assert files["NOTICE"]["sha256"] == hashlib.sha256(b"Example NOTICE\n").hexdigest()
    assert files["NOTICE"]["size"] == len(b"Example NOTICE\n")
    write_runtime_notices(plan, fixture.root / "out")
    for item in files.values():
        assert hashlib.sha256((fixture.root / "out" / item["path"]).read_bytes()).hexdigest() == item["sha256"]


def test_loaded_sounddevice_review_with_only_auxiliary_text_fails(fixture, tmp_path):
    fixture.distributions.append(fixture.install("sounddevice", "0.5.6", {
        "sounddevice.py": "code",
        "_sounddevice_data/portaudio-binaries/README.md": "PortAudio attribution, MIT License\n",
        PORTAUDIO_DLL: "native x64 build",
        "sounddevice-0.5.6.dist-info/AUTHORS": "Sound authors\n",
    }, {"License-Expression": "MIT"}))
    fixture.add_bundle_file(PORTAUDIO_DLL, b"native x64 build")
    fixture.archive.append("sounddevice")
    texts = tmp_path / "texts"
    (texts / "portaudio").mkdir(parents=True)
    (texts / "portaudio/README-COPY.md").write_text("PortAudio attribution only\n", encoding="utf-8", newline="")
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps({"reviews": [{
        "name": "sounddevice", "version": "0.5.6", "provenance": "wheel",
        "supplementalNotices": ["_sounddevice_data/portaudio-binaries/README.md"],
        "vendoredNotices": [vendored_record(
            file="portaudio/README-COPY.md",
            sha256=sha256_text("PortAudio attribution only\n"),
            covers=[{"path": PORTAUDIO_DLL, "sha256": sha256_text("native x64 build")}],
        )],
    }]}), encoding="utf-8")
    reviews = load_reviews(path)
    with pytest.raises(NoticeError, match="sounddevice 0.5.6 \\(no non-empty license text\\)"):
        fixture.plan(reviews=reviews, vendored_root=texts)


def test_case_alias_notices_collide_before_any_output(fixture):
    fixture.distributions[0].files.append(PackagePath("examplepkg/notice"))
    alias = fixture.site / "examplepkg/notice"
    if not alias.exists():
        # Case-sensitive file systems hold a second, distinct file; on Windows
        # the same file is simply listed under two names.
        alias.write_text("lower-case alias\n", encoding="utf-8")
    with pytest.raises(NoticeError, match="ignoring case"):
        fixture.plan()
    assert not (fixture.root / "out").exists()


def test_file_and_folder_destination_collision_is_refused():
    with pytest.raises(NoticeError, match="also used as a folder"):
        runtime_notices._check_destinations([
            "third-party-notices/python/a-1.0/LICENSE",
            "third-party-notices/python/a-1.0/license/COPYING",
        ])


@pytest.mark.parametrize("name, version", [
    ("/examplepkg", "1.0"), ("example/pkg", "1.0"), ("C:pkg", "1.0"), ("pkg\\x", "1.0"),
    ("examplepkg", "1.0/../../escape"), ("examplepkg", "..\\1.0"), ("examplepkg", "C:1.0"),
    ("examplepkg", "1.0\x00"), ("examplepkg", "1.0\n"), ("examplepkg", ".."),
])
def test_malicious_metadata_after_a_valid_record_fails_before_output(fixture, name, version):
    # "aaa" sorts before the malicious record, and its notices must not be written.
    fixture.distributions.insert(0, fixture.install("aaa", "1.0", {
        "aaa/__init__.py": "code", "aaa-1.0.dist-info/LICENSE": "A license\n\nText.\n",
    }, {}))
    fixture.archive.append("aaa")
    fixture.distributions.append(FakeDistribution(fixture.site, "placeholder", "1.0", {}, {}))
    fixture.distributions[-1].metadata.replace_header("Name", name)
    fixture.distributions[-1].version = version
    out = fixture.root / "out"
    with pytest.raises(NoticeError, match="unsafe name or version"):
        write_runtime_notices(fixture.plan(), out)
    assert not out.exists()


@pytest.mark.parametrize("version", ["1!2.0", "2.0rc1", "2.0.post3", "2.0.dev4", "2.0+local.7", "1!2.0a1.post2+abc"])
def test_valid_pep440_versions_are_accepted_safely(fixture, version):
    fixture.distributions[0] = fixture.install("examplepkg", version, {
        "examplepkg/__init__.py": "code",
        "examplepkg/_speedups.pyd": "compiled",
        "examplepkg/native.dll": "native",
        "examplepkg/LICENSE": "Example License\n\nPermission granted.\n",
    }, {})
    plan = fixture.plan()
    assert entry(plan, "examplepkg")["notices"] == [f"third-party-notices/python/examplepkg-{version}/examplepkg/LICENSE"]
    write_runtime_notices(plan, fixture.root / "out")
    assert (fixture.root / "out" / f"third-party-notices/python/examplepkg-{version}/examplepkg/LICENSE").is_file()


@pytest.mark.parametrize("private", ["D:\\BuildAgent\\work", "D:/BuildAgent/work", "/srv/buildagent/work"])
def test_private_builder_path_in_metadata_fails_without_echoing_it(fixture, private):
    fixture.distributions[0].metadata["License"] = f"See {private}\\LICENSE for terms"
    with pytest.raises(NoticeError) as raised:
        fixture.plan(private_roots=[private])
    assert "builder path" in str(raised.value)
    assert "builder\\work" not in str(raised.value) and "builder/work" not in str(raised.value)


def test_private_builder_path_in_notice_text_fails_and_normal_urls_pass(fixture):
    (fixture.site / "examplepkg/NOTICE").write_text("Built in D:\\BuildAgent\\work\\pkg\n", encoding="utf-8")
    with pytest.raises(NoticeError, match="examplepkg/NOTICE contains a builder path"):
        fixture.plan(private_roots=["D:\\BuildAgent\\work"])
    (fixture.site / "examplepkg/NOTICE").write_text("Upstream: https://example.invalid/project\n", encoding="utf-8")
    fixture.distributions[0] = FakeDistribution(fixture.site, "examplepkg", "1.0", {
        "examplepkg/__init__.py": "code",
        "examplepkg/_speedups.pyd": "compiled",
        "examplepkg/native.dll": "native",
        "examplepkg/NOTICE": "Upstream: https://example.invalid/project\n",
        "examplepkg-1.0.dist-info/licenses/LICENSE.txt": "Example License\n\nGranted.\n",
    }, {"License": "https://example.invalid/license"})
    fixture.plan(private_roots=["D:\\BuildAgent\\work"])


def test_source_changed_after_planning_is_refused_before_output(fixture):
    plan = fixture.plan()
    (fixture.site / "examplepkg/NOTICE").write_text("Changed after planning\n", encoding="utf-8")
    with pytest.raises(NoticeError, match="changed after planning"):
        write_runtime_notices(plan, fixture.root / "out")
    assert not (fixture.root / "out" / "third-party-notices").exists()


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
    fixture.add_bundle_file("examplepkg-1.0.dist-info/licenses/LICENSE.txt", b"Example license text\n\nPermission is granted for this synthetic fixture only.\n")
    plan = fixture.plan()
    assert "examplepkg-1.0.dist-info" in entry(plan, "examplepkg")["frozenRoots"]
    write_runtime_notices(plan, fixture.bundle)
    assert (fixture.internal / "examplepkg-1.0.dist-info/licenses/LICENSE.txt").read_bytes() == b"Example license text\n\nPermission is granted for this synthetic fixture only.\n"
    with pytest.raises(NoticeError, match="already exist"):
        write_runtime_notices(plan, fixture.bundle)


PORTAUDIO_DLL = "_sounddevice_data/portaudio-binaries/libportaudio64bit.dll"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def add_sounddevice(fixture: Fixture, *, dll: str = "native x64 build") -> tuple[dict, Path]:
    """A synthetic sounddevice wheel, its bundled DLL and a pinned upstream text."""
    fixture.distributions.append(fixture.install("sounddevice", "0.5.6", {
        "sounddevice.py": "code",
        "_sounddevice_data/portaudio-binaries/README.md": "PortAudio attribution",
        PORTAUDIO_DLL: dll,
        "sounddevice-0.5.6.dist-info/licenses/LICENSE": "sounddevice license\n\nPermission is granted for this synthetic fixture only.\n",
    }, {"License-Expression": "MIT"}))
    fixture.add_bundle_file(PORTAUDIO_DLL, dll.encode("utf-8"))
    fixture.archive.append("sounddevice")
    texts = fixture.root / "vendored"
    (texts / "portaudio").mkdir(parents=True)
    (texts / "portaudio/LICENSE.txt").write_text("PortAudio upstream license text\n\nPermission is granted for this synthetic fixture only.\n", encoding="utf-8", newline="")
    review = {
        "provenance": "Shipped inside the sounddevice wheel.",
        "supplementalNotices": ["_sounddevice_data/portaudio-binaries/README.md"],
        "vendoredNotices": [{
            "component": "PortAudio",
            "version": "v19.7.0",
            "sourceRepository": "https://example.invalid/portaudio",
            "sourceCommit": COMMIT,
            "sourceUrl": f"https://example.invalid/portaudio/blob/{COMMIT}/LICENSE.txt",
            "file": "portaudio/LICENSE.txt",
            "sha256": sha256_text("PortAudio upstream license text\n\nPermission is granted for this synthetic fixture only.\n"),
            "covers": [{"path": PORTAUDIO_DLL, "sha256": sha256_text(dll)}],
        }],
        "binaryProvenance": {"wheelSha256": "f" * 64},
        "unresolved": "Statically linked runtime terms need review.",
    }
    return review, texts


def plan_sounddevice(fixture: Fixture, review: dict, texts: Path, **overrides):
    return fixture.plan(reviews={("sounddevice", "0.5.6"): review}, vendored_root=texts, **overrides)


def test_native_package_copies_its_pinned_upstream_notice(fixture):
    review, texts = add_sounddevice(fixture)
    plan = plan_sounddevice(fixture, review, texts, pruned=["_internal/_sounddevice_data/portaudio-binaries/libportaudio64bit-asio.dll"])
    sounddevice = entry(plan, "sounddevice")
    upstream = "third-party-notices/python/sounddevice-0.5.6/upstream/portaudio/LICENSE.txt"
    assert upstream in sounddevice["notices"]
    assert "third-party-notices/python/sounddevice-0.5.6/_sounddevice_data/portaudio-binaries/README.md" in sounddevice["notices"]
    vendored = sounddevice["review"]["vendoredNotices"]
    assert vendored == [{
        "component": "PortAudio",
        "version": "v19.7.0",
        "sourceRepository": "https://example.invalid/portaudio",
        "sourceCommit": COMMIT,
        "sourceUrl": f"https://example.invalid/portaudio/blob/{COMMIT}/LICENSE.txt",
        "notice": upstream,
        "sha256": sha256_text("PortAudio upstream license text\n\nPermission is granted for this synthetic fixture only.\n"),
        "covers": [{"path": PORTAUDIO_DLL, "sha256": sha256_text("native x64 build")}],
    }]
    assert sounddevice["review"]["binaryProvenance"] == {"wheelSha256": "f" * 64}
    write_runtime_notices(plan, fixture.root / "out")
    out = fixture.root / "out"
    assert (out / upstream).read_text(encoding="utf-8") == "PortAudio upstream license text\n\nPermission is granted for this synthetic fixture only.\n"
    readme = (out / "third-party-notices/README.txt").read_text(encoding="utf-8")
    assert f"https://example.invalid/portaudio/blob/{COMMIT}/LICENSE.txt" in readme
    assert "Statically linked runtime terms need review." in readme
    assert "libportaudio64bit-asio.dll" in readme
    assert str(fixture.root) not in (out / "third-party-notices/inventory.json").read_text(encoding="utf-8")


def test_native_package_without_a_pinned_upstream_notice_fails(fixture):
    review, texts = add_sounddevice(fixture)
    with pytest.raises(NoticeError, match="sounddevice 0.5.6"):
        fixture.plan(vendored_root=texts)
    del review["vendoredNotices"]
    with pytest.raises(NoticeError, match="pinned upstream notice"):
        plan_sounddevice(fixture, review, texts)


def test_changed_vendored_text_fails(fixture):
    review, texts = add_sounddevice(fixture)
    (texts / "portaudio/LICENSE.txt").write_text("PortAudio upstream license text, edited\n", encoding="utf-8")
    with pytest.raises(NoticeError, match="pinned SHA-256"):
        plan_sounddevice(fixture, review, texts)


def test_missing_vendored_text_fails(fixture):
    review, texts = add_sounddevice(fixture)
    (texts / "portaudio/LICENSE.txt").unlink()
    with pytest.raises(NoticeError, match="missing"):
        plan_sounddevice(fixture, review, texts)


@pytest.mark.parametrize("unsafe", ["../outside/LICENSE.txt", "C:/outside/LICENSE.txt", "portaudio\\LICENSE.txt", "/LICENSE.txt"])
def test_unsafe_vendored_path_is_refused(fixture, unsafe):
    review, texts = add_sounddevice(fixture)
    review["vendoredNotices"][0]["file"] = unsafe
    with pytest.raises(NoticeError, match="Unsafe"):
        plan_sounddevice(fixture, review, texts)


def test_vendored_text_reached_through_a_link_is_refused(fixture):
    review, texts = add_sounddevice(fixture)
    outside = fixture.root / "outside"
    outside.mkdir()
    (outside / "LICENSE.txt").write_text("PortAudio upstream license text\n\nPermission is granted for this synthetic fixture only.\n", encoding="utf-8")
    link_directory(texts / "linked", outside)
    review["vendoredNotices"][0]["file"] = "linked/LICENSE.txt"
    with pytest.raises(NoticeError, match="link or reparse"):
        plan_sounddevice(fixture, review, texts)


def test_bundled_binary_must_be_the_reviewed_one(fixture):
    review, texts = add_sounddevice(fixture)
    fixture.add_bundle_file(PORTAUDIO_DLL, b"a different build")
    with pytest.raises(NoticeError, match="Bundled file differs"):
        plan_sounddevice(fixture, review, texts)


def test_installed_binary_must_match_its_record_and_the_review(fixture):
    review, texts = add_sounddevice(fixture)
    (fixture.site / PORTAUDIO_DLL).write_text("replaced after installation", encoding="utf-8")
    with pytest.raises(NoticeError, match="Installed file differs"):
        plan_sounddevice(fixture, review, texts)


def test_review_for_a_different_binary_fails(fixture):
    review, texts = add_sounddevice(fixture)
    review["vendoredNotices"][0]["covers"][0]["sha256"] = sha256_text("another release")
    with pytest.raises(NoticeError, match="RECORD hash differs"):
        plan_sounddevice(fixture, review, texts)


def test_reviewed_binary_missing_from_the_bundle_fails(fixture):
    review, texts = add_sounddevice(fixture)
    (fixture.internal / PORTAUDIO_DLL).unlink()
    fixture.add_bundle_file("_sounddevice_data/portaudio-binaries/README.md")
    with pytest.raises(NoticeError, match="not in the bundle"):
        plan_sounddevice(fixture, review, texts)


def test_uncovered_bundled_native_file_fails(fixture):
    review, texts = add_sounddevice(fixture)
    asio = "_sounddevice_data/portaudio-binaries/libportaudio64bit-asio.dll"
    fixture.distributions[-1].files.append(PurePosixPath(asio))
    fixture.add_bundle_file(asio, b"asio build")
    with pytest.raises(NoticeError, match="not covered"):
        plan_sounddevice(fixture, review, texts)


def test_stale_or_mismatched_review_records_fail(fixture):
    review, texts = add_sounddevice(fixture)
    with pytest.raises(NoticeError, match="sounddevice 0.5.6"):
        fixture.plan(reviews={("sounddevice", "0.5.5"): review}, vendored_root=texts)
    stale = {("sounddevice", "0.5.6"): review, ("unrelated", "2.0"): {"provenance": "old", "noNoticeReason": "old"}}
    with pytest.raises(NoticeError, match="stale.*unrelated 2.0"):
        fixture.plan(reviews=stale, vendored_root=texts)


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


def vendored_record(**changes):
    record = {
        "component": "PortAudio",
        "version": "v19.7.0",
        "sourceRepository": "https://example.invalid/portaudio",
        "sourceCommit": COMMIT,
        "sourceUrl": f"https://example.invalid/portaudio/blob/{COMMIT}/LICENSE.txt",
        "file": "portaudio/LICENSE.txt",
        "sha256": "a" * 64,
        "covers": [{"path": PORTAUDIO_DLL, "sha256": "b" * 64}],
    }
    record.update(changes)
    return record


@pytest.mark.parametrize("record", [
    vendored_record(sourceCommit="0123456"),
    vendored_record(sourceUrl="https://example.invalid/portaudio/blob/main/LICENSE.txt"),
    vendored_record(sourceUrl=f"https://elsewhere.invalid/blob/{COMMIT}/LICENSE.txt"),
    vendored_record(sourceRepository="http://example.invalid/portaudio",
                    sourceUrl=f"http://example.invalid/portaudio/blob/{COMMIT}/LICENSE.txt"),
    vendored_record(sha256="A" * 64),
    vendored_record(file="../LICENSE.txt"),
    vendored_record(covers=[]),
    vendored_record(covers=[{"path": PORTAUDIO_DLL}]),
    vendored_record(covers=[{"path": "../outside.dll", "sha256": "b" * 64}]),
    vendored_record(license="MIT"),
    {key: value for key, value in vendored_record().items() if key != "sourceCommit"},
])
def test_review_file_refuses_incomplete_or_unpinned_vendored_notices(tmp_path, record):
    path = tmp_path / "reviews.json"
    review = {"name": "sounddevice", "version": "0.5.6", "provenance": "wheel", "vendoredNotices": [record]}
    path.write_text(json.dumps({"reviews": [review]}), encoding="utf-8")
    with pytest.raises(NoticeError):
        load_reviews(path)
    review["vendoredNotices"] = [vendored_record()]
    path.write_text(json.dumps({"reviews": [review]}), encoding="utf-8")
    assert set(load_reviews(path)) == {("sounddevice", "0.5.6")}


def test_review_file_refuses_unknown_fields(tmp_path):
    path = tmp_path / "reviews.json"
    review = {"name": "x", "version": "1.0", "provenance": "wheel", "noNoticeReason": "reviewed", "approved": "yes"}
    path.write_text(json.dumps({"reviews": [review]}), encoding="utf-8")
    with pytest.raises(NoticeError, match="unknown fields"):
        load_reviews(path)


def test_committed_reviews_pin_vendored_texts_that_match_their_hashes():
    reviews = load_reviews()
    assert set(reviews) == {("sounddevice", "0.5.6"), ("uharfbuzz", "0.56.3")}
    review = reviews[("sounddevice", "0.5.6")]
    vendored = review["vendoredNotices"]
    assert {item["file"] for item in vendored} == {
        "portaudio-v19.7.0/LICENSE.txt", "portaudio-v19.7.0/COPYRIGHT-NOTICES.txt",
    }
    for item in vendored:
        runtime_notices._vendored_source(runtime_notices.VENDORED_TEXTS, item["file"], item["sha256"])
        assert item["sourceCommit"] in item["sourceUrl"]
        assert [cover["path"] for cover in item["covers"]] == [PORTAUDIO_DLL]
    text = (runtime_notices.VENDORED_TEXTS / "portaudio-v19.7.0/LICENSE.txt").read_text(encoding="ascii")
    assert "Copyright (c) 1999-2006 Ross Bencina and Phil Burk" in text
    assert "Permission is hereby granted, free of charge" in text


def test_the_harfbuzz_review_pins_its_upstream_text_and_the_extension_module():
    review = load_reviews()[("uharfbuzz", "0.56.3")]
    (item,) = review["vendoredNotices"]
    runtime_notices._vendored_source(runtime_notices.VENDORED_TEXTS, item["file"], item["sha256"])
    assert item["component"] == "HarfBuzz" and item["sourceCommit"] in item["sourceUrl"]
    assert item["covers"] == [{"path": "uharfbuzz/_harfbuzz.pyd",
                               "sha256": "2ab785a40370050976c5ca1c26c2274b682290b047f2427a7a4c6fc83b7715c8"}]
    text = (runtime_notices.VENDORED_TEXTS / item["file"]).read_text(encoding="utf-8")
    assert '"Old MIT" license' in text and "Permission is hereby granted" in text
    assert "uharfbuzz" in runtime_notices.NATIVE_REVIEW_REQUIRED
    try:
        dist = importlib.metadata.distribution("uharfbuzz")
    except importlib.metadata.PackageNotFoundError:
        return
    record = runtime_notices._installed_distributions([dist])[0]
    runtime_notices._check_covered_binary(record, item["covers"][0], None)


def _font_bundle(tmp_path: Path) -> tuple[Path, runtime_notices.FrozenContents]:
    source = Path(__file__).resolve().parents[1] / "qronos_media" / "fonts"
    internal = tmp_path / "bundle" / "_internal"
    target = internal / "qronos_media" / "fonts"
    target.mkdir(parents=True)
    registry = json.loads((source / "fonts.json").read_text(encoding="utf-8"))
    for name in [entry["file"] for entry in registry["files"]] + ["fonts.json"]:
        (target / name).write_bytes((source / name).read_bytes())
    files = tuple(sorted(f"qronos_media/fonts/{p.name}" for p in target.iterdir()) + ["qronos_media/reports.pyc"])
    return internal, runtime_notices.FrozenContents(frozenset(), {"qronos_media": files})


def test_bundled_pdf_fonts_are_attributed_to_their_pinned_registry_with_the_ofl_text(tmp_path):
    internal, contents = _font_bundle(tmp_path)
    entry, copies = runtime_notices._plan_fonts(contents, internal)
    assert entry["family"] == "Vazirmatn" and entry["version"] == "33.003" and entry["licence"] == "OFL-1.1"
    assert entry["notice"] == "third-party-notices/fonts/Vazirmatn-33.003/OFL.txt"
    assert {f["path"] for f in entry["files"]} == {"qronos_media/fonts/Vazirmatn-Regular.ttf",
                                                   "qronos_media/fonts/Vazirmatn-Bold.ttf", "qronos_media/fonts/OFL.txt"}
    assert [copy.kind for copy in copies] == ["license-text"]
    assert runtime_notices._plan_fonts(runtime_notices.FrozenContents(frozenset(), {}), internal) == (None, [])


@pytest.mark.parametrize("change", ["tampered", "unlisted"])
def test_a_bundled_font_that_is_not_the_pinned_one_stops_the_build(tmp_path, change):
    internal, contents = _font_bundle(tmp_path)
    fonts = internal / "qronos_media" / "fonts"
    if change == "tampered":
        data = bytearray((fonts / "Vazirmatn-Bold.ttf").read_bytes())
        data[200] ^= 0x01
        (fonts / "Vazirmatn-Bold.ttf").write_bytes(bytes(data))
    else:
        (fonts / "Other.ttf").write_bytes(b"not pinned")
        contents = runtime_notices.FrozenContents(
            frozenset(), {"qronos_media": contents.internal_roots["qronos_media"] + ("qronos_media/fonts/Other.ttf",)})
    with pytest.raises(NoticeError, match="font"):
        runtime_notices._plan_fonts(contents, internal)


def test_committed_review_matches_the_installed_wheel_when_present():
    try:
        dist = importlib.metadata.distribution("sounddevice")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("sounddevice is not installed here.")
    reviews = load_reviews()
    if ("sounddevice", dist.version) not in reviews:
        pytest.fail(f"sounddevice {dist.version} is installed but has no notice review; review the new wheel.")
    review = reviews[("sounddevice", dist.version)]
    record = runtime_notices._installed_distributions([dist])[0]
    for item in review["vendoredNotices"]:
        for cover in item["covers"]:
            runtime_notices._check_covered_binary(record, cover, None)


@pytest.mark.skipif(sysconfig.get_platform() != "win-amd64", reason="Checks the x64 Windows library choice.")
def test_sounddevice_selects_the_retained_dll_on_x64_windows():
    spec = importlib.util.find_spec("sounddevice")
    if spec is None or spec.origin is None:
        pytest.skip("sounddevice is not installed here.")
    # Read the selection code without importing sounddevice, which would load
    # PortAudio and enumerate audio devices.
    source = Path(spec.origin).read_text(encoding="utf-8")
    assert "_platform_suffix = _platform.architecture()[0]" in source
    assert "if 'SD_ENABLE_ASIO' in _os.environ:" in source
    assert "_libname = 'libportaudio' + _platform_suffix + '.dll'" in source
    assert "libportaudio" + platform.architecture()[0] + ".dll" == runtime_notices.PORTAUDIO_KEEP


def portaudio_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    directory = bundle.joinpath("_internal", *runtime_notices.PORTAUDIO_DIRECTORY)
    directory.mkdir(parents=True)
    for name in ("libportaudio32bit-asio.dll", "libportaudio32bit.dll", "libportaudio64bit-asio.dll",
                 "libportaudio64bit.dll", "libportaudioarm64-asio.dll", "libportaudioarm64.dll",
                 "libportaudio.dylib", "README.md"):
        (directory / name).write_bytes(name.encode())
    return bundle


def test_prune_keeps_only_the_x64_non_asio_portaudio_dll_and_readme(tmp_path):
    bundle = portaudio_bundle(tmp_path)
    removed = prune_portaudio(bundle)
    assert removed == tuple(
        f"_internal/_sounddevice_data/portaudio-binaries/{name}"
        for name in ("libportaudio.dylib", "libportaudio32bit-asio.dll", "libportaudio32bit.dll",
                     "libportaudio64bit-asio.dll", "libportaudioarm64-asio.dll", "libportaudioarm64.dll")
    )
    directory = bundle.joinpath("_internal", *runtime_notices.PORTAUDIO_DIRECTORY)
    assert sorted(item.name for item in directory.iterdir()) == ["README.md", "libportaudio64bit.dll"]
    assert (directory / "libportaudio64bit.dll").read_bytes() == b"libportaudio64bit.dll"
    assert (directory / "README.md").read_bytes() == b"README.md"


@pytest.mark.parametrize("missing", ["libportaudio64bit.dll", "README.md"])
def test_prune_refuses_a_bundle_without_the_supported_dll_or_readme(tmp_path, missing):
    bundle = portaudio_bundle(tmp_path)
    directory = bundle.joinpath("_internal", *runtime_notices.PORTAUDIO_DIRECTORY)
    (directory / missing).unlink()
    with pytest.raises(NoticeError, match="missing"):
        prune_portaudio(bundle)
    assert (directory / "libportaudio64bit-asio.dll").exists()


@pytest.mark.parametrize("unexpected", ["portaudio_x64.dll", "notes.txt", "subfolder"])
def test_prune_refuses_unexpected_entries_without_removing_anything(tmp_path, unexpected):
    bundle = portaudio_bundle(tmp_path)
    directory = bundle.joinpath("_internal", *runtime_notices.PORTAUDIO_DIRECTORY)
    if unexpected == "subfolder":
        (directory / unexpected).mkdir()
    else:
        (directory / unexpected).write_bytes(b"unexpected")
    with pytest.raises(NoticeError, match="Unexpected"):
        prune_portaudio(bundle)
    assert (directory / "libportaudio64bit-asio.dll").exists()
    assert (directory / "libportaudio.dylib").exists()


def test_prune_refuses_links_without_removing_anything(tmp_path):
    bundle = portaudio_bundle(tmp_path)
    directory = bundle.joinpath("_internal", *runtime_notices.PORTAUDIO_DIRECTORY)
    outside = tmp_path / "outside"
    outside.mkdir()
    link_directory(directory / "linked", outside)
    with pytest.raises(NoticeError, match="link or reparse"):
        prune_portaudio(bundle)
    assert (directory / "libportaudio64bit-asio.dll").exists()


def test_output_below_a_parent_junction_fails_before_any_write(fixture):
    real = fixture.root / "real-output"
    real.mkdir()
    link_directory(fixture.root / "linked-parent", real)
    plan = fixture.plan()
    with pytest.raises(NoticeError, match="link or reparse"):
        write_runtime_notices(plan, fixture.root / "linked-parent" / "bundle")
    assert list(real.iterdir()) == []
