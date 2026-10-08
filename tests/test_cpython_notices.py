from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import sys
import sysconfig

import pytest

from build_support import build_runtime, cpython_notices
from build_support.cpython_notices import (
    CPythonNoticeError,
    Interpreter,
    UnsupportedCPythonBuild,
    load_registry,
    packaging_identity,
    plan_cpython_notices,
)
from build_support.runtime_notices import NOTICE_DIRECTORY, NoticeCopy, NoticePlan, write_runtime_notices
from tests.fixtures.cpython_registry import BUILD, PLATFORM, VERSION, RegistryBuilder


def link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Directory links are unavailable here.")
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


class Setup:
    def __init__(self, tmp_path: Path):
        self.python = tmp_path / "python"
        (self.python / "DLLs").mkdir(parents=True)
        (self.python / "LICENSE.txt").write_bytes(b"Python license\n")
        (self.python / "python313.dll").write_bytes(b"core")
        (self.python / "DLLs" / "_bz2.pyd").write_bytes(b"bz2")
        (self.python / "DLLs" / "sqlite3.dll").write_bytes(b"sqlite")
        (self.python / "tcl" / "tcl8.6").mkdir(parents=True)
        (self.python / "tcl" / "tcl8.6" / "init.tcl").write_bytes(b"init")
        self.builder = RegistryBuilder(tmp_path / "registry", self.python)
        count, digest = cpython_notices.tree_digest(self.python / "tcl" / "tcl8.6")
        self.raw = self.builder.raw(
            {"python313.dll": ["zlib@1.3.1"], "DLLs/_bz2.pyd": ["bzip2@1.0.8"], "DLLs/sqlite3.dll": ["sqlite@3.50.4"]},
            {"_tcl_data": {"source": "tcl/tcl8.6", "excludes": [], "files": count, "treeSha256": digest, "components": ["tcl@8.6.15"]}},
        )
        self.internal = tmp_path / "bundle" / "_internal"
        self.internal.mkdir(parents=True)
        (self.internal / "python313.dll").write_bytes(b"core")
        (self.internal / "_bz2.pyd").write_bytes(b"bz2")

    def load(self, raw=None):
        return load_registry(self.builder.write(raw or self.raw), self.builder.text_root)

    def variant(self):
        return self.builder.variant(self.raw)

    def plan(self, registry=None, roots=("python313.dll", "_bz2.pyd"), archive=()):
        return plan_cpython_notices(
            registry or self.load(),
            Interpreter(VERSION, PLATFORM, BUILD, self.python),
            bundle_internal=self.internal,
            cpython_roots=roots,
            archive_names=archive,
            notice_root=NOTICE_DIRECTORY,
        )


@pytest.fixture
def setup(tmp_path):
    return Setup(tmp_path)


# ------------------------------------------------------------ registry checks


def test_valid_registry_loads(setup):
    registry = setup.load()
    assert set(registry.components) >= {"zlib@1.3.1", "sqlite@3.50.4"}
    assert registry.interpreters[0]["version"] == VERSION


def test_component_with_only_auxiliary_text_is_refused(setup):
    raw = setup.variant()
    raw["components"]["zlib@1.3.1"]["texts"][0]["kind"] = "auxiliary"
    with pytest.raises(CPythonNoticeError, match="only auxiliary texts"):
        setup.load(raw)


def test_attribution_must_appear_verbatim_in_the_component_texts(setup):
    raw = setup.variant()
    raw["components"]["zlib@1.3.1"]["attribution"] = ["(C) 1995-2024 Somebody Else"]
    with pytest.raises(CPythonNoticeError, match="does not appear verbatim"):
        setup.load(raw)


def test_changed_text_bytes_are_refused(setup):
    (setup.builder.text_root / "bzip2" / "LICENSE").write_bytes(b"edited licence")
    with pytest.raises(CPythonNoticeError, match="does not match its pinned size and SHA-256"):
        setup.load()


def test_missing_text_is_refused(setup):
    (setup.builder.text_root / "bzip2" / "LICENSE").unlink()
    with pytest.raises(CPythonNoticeError, match="Missing file: bzip2/LICENSE"):
        setup.load()


def test_unregistered_file_in_the_text_folder_is_refused(setup):
    (setup.builder.text_root / "stray.txt").write_bytes(b"not pinned")
    with pytest.raises(CPythonNoticeError, match="Unregistered files"):
        setup.load()


def test_text_reached_through_a_junction_is_refused(setup, tmp_path):
    outside = tmp_path / "outside"
    (outside).mkdir()
    (outside / "LICENSE").write_bytes((setup.builder.text_root / "bzip2" / "LICENSE").read_bytes())
    real = setup.builder.text_root / "bzip2"
    (real / "LICENSE").unlink()
    real.rmdir()
    link_directory(real, outside)
    with pytest.raises(CPythonNoticeError, match="link or reparse point"):
        setup.load()


@pytest.mark.parametrize("text_id", ["../escape.txt", "a/../b", "C:/x", "a\\b", "/abs"])
def test_unsafe_text_paths_are_refused(setup, text_id):
    raw = setup.variant()
    raw["texts"][text_id] = raw["texts"].pop("bzip2/LICENSE")
    raw["components"]["bzip2@1.0.8"]["texts"][0]["text"] = text_id
    with pytest.raises(CPythonNoticeError, match="Unsafe text path"):
        setup.load(raw)


@pytest.mark.parametrize("mutate, message", [
    (lambda raw: raw["components"]["zlib@1.3.1"].update(extra=1), "missing or unknown fields"),
    (lambda raw: raw["texts"]["zlib/LICENSE"]["sources"][0].update(member=None, url="http://insecure"), "https"),
    (lambda raw: raw["texts"]["zlib/LICENSE"]["sources"][0].pop("pinnedBy"), "missing or unknown fields"),
    (lambda raw: raw["texts"]["zlib/LICENSE"].update(sources=[]), "at least one verified source"),
    (lambda raw: raw["interpreters"][0]["files"].update({"DLLs/python313.dll": raw["interpreters"][0]["files"]["python313.dll"]}),
     "two files named"),
    (lambda raw: raw["interpreters"][0].update(sourceCommit="f" * 40), "build tag does not match"),
    (lambda raw: raw["interpreters"].append(dict(raw["interpreters"][0], id="copy")), "Duplicate interpreter"),
    (lambda raw: raw["interpreters"][0]["files"].update({"Lib/x.pyd": {"size": 1, "sha256": "0" * 64, "components": []}}),
     "top-level or DLLs native file"),
    (lambda raw: raw["interpreters"][0]["files"]["python313.dll"].update(components=["missing@1"]), "unknown or repeated components"),
    (lambda raw: raw["components"].update({"spare@1.0": {"name": "x", "version": "1", "attribution": [],
                                                          "texts": [{"text": "zlib/LICENSE", "kind": "license-text"}]}}),
     "no interpreter uses"),
])
def test_malformed_registry_records_are_refused(setup, mutate, message):
    raw = setup.variant()
    mutate(raw)
    with pytest.raises(CPythonNoticeError, match=message):
        setup.load(raw)


def test_repository_source_must_point_at_its_pinned_commit(setup):
    raw = setup.variant()
    raw["texts"]["zlib/LICENSE"]["sources"] = [{
        "kind": "repository-file", "repository": "https://github.com/example/zlib", "commit": "1" * 40,
        "path": "LICENSE", "url": "https://github.com/example/zlib/blob/main/LICENSE", "ref": "v1",
    }]
    with pytest.raises(CPythonNoticeError, match="at the pinned commit"):
        setup.load(raw)


def test_component_texts_with_the_same_file_name_are_refused(setup):
    raw = setup.variant()
    raw["texts"]["other/LICENSE"] = raw["texts"]["bzip2/LICENSE"]
    (setup.builder.text_root / "other").mkdir()
    (setup.builder.text_root / "other" / "LICENSE").write_bytes((setup.builder.text_root / "bzip2" / "LICENSE").read_bytes())
    raw["components"]["zlib@1.3.1"]["texts"].append({"text": "other/LICENSE", "kind": "auxiliary"})
    with pytest.raises(CPythonNoticeError, match="would collide"):
        setup.load(raw)


# ---------------------------------------------------------------- matching


def test_plan_copies_component_texts_bound_to_the_bundled_binaries(setup):
    plan = setup.plan()
    ids = [item["id"] for item in plan.inventory["components"]]
    assert ids == ["bzip2@1.0.8", "cpython-incorporated@3.13.13", "zlib@1.3.1"]
    destinations = [text.destination for text in plan.texts]
    assert destinations == sorted(destinations)
    assert f"{NOTICE_DIRECTORY}/python/cpython-{VERSION}/components/zlib-1.3.1/LICENSE" in destinations
    for text in plan.texts:
        assert hashlib.sha256(text.source.read_bytes()).hexdigest() == text.sha256


def test_unknown_interpreter_lists_the_audited_builds(setup):
    with pytest.raises(UnsupportedCPythonBuild, match=r"3\.14\.9 win-amd64 .*not an audited interpreter.*Audited builds: 3\.13\.13"):
        plan_cpython_notices(setup.load(), Interpreter("3.14.9", PLATFORM, "tags/v3.14.9:abcdef0", setup.python),
                             bundle_internal=setup.internal, cpython_roots=(), archive_names=(), notice_root=NOTICE_DIRECTORY)


def test_missing_core_dll_in_the_build_interpreter_fails(setup):
    (setup.python / "python313.dll").unlink()
    with pytest.raises(CPythonNoticeError, match="no python313.dll"):
        setup.plan()


def test_bundled_binary_reached_through_a_junction_is_refused(setup, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "init.tcl").write_bytes(b"init")
    link_directory(setup.internal / "_tcl_data", outside)
    with pytest.raises(CPythonNoticeError, match="link or reparse point"):
        setup.plan(roots=("python313.dll", "_tcl_data"))


def test_microsoft_file_is_never_attributed_to_cpython(setup):
    (setup.python / "vcruntime140.dll").write_bytes(b"ms")
    raw = setup.builder.raw(
        {"python313.dll": ["zlib@1.3.1"], "DLLs/_bz2.pyd": ["bzip2@1.0.8"], "DLLs/sqlite3.dll": ["sqlite@3.50.4"], "vcruntime140.dll": None},
        setup.raw["interpreters"][0]["dataTrees"],
    )
    (setup.internal / "vcruntime140.dll").write_bytes(b"ms")
    with pytest.raises(CPythonNoticeError, match="Microsoft runtime file"):
        setup.plan(setup.load(raw), roots=("python313.dll", "vcruntime140.dll"))


# ------------------------------------------------------- packaging target


def _second_audited_record(setup: Setup) -> dict:
    """The fixture registry plus a second audited build that is not the target."""
    raw = setup.variant()
    other = dict(raw["interpreters"][0], id="synthetic-3.13.12", version="3.13.12", build="tags/v3.13.12:01104ce")
    raw["interpreters"].append(other)
    return raw


def test_packaging_target_is_recorded_as_the_target(setup):
    identity = packaging_identity(setup.load(), Interpreter(VERSION, PLATFORM, BUILD, setup.python))
    assert identity == {"implementation": "CPython", "version": VERSION, "platform": PLATFORM, "build": BUILD,
                        "sourceCommit": setup.raw["interpreters"][0]["sourceCommit"],
                        "auditedRecord": "synthetic-3.13.13", "packagingTarget": True}


def test_audited_non_target_interpreter_needs_an_explicit_opt_in(setup):
    registry = setup.load(_second_audited_record(setup))
    interpreter = Interpreter("3.13.12", PLATFORM, "tags/v3.13.12:01104ce", setup.python)
    with pytest.raises(UnsupportedCPythonBuild, match="audited but is not the supported packaging target CPython 3.13.13"):
        packaging_identity(registry, interpreter)
    identity = packaging_identity(registry, interpreter, allow_audited=True)
    assert identity["auditedRecord"] == "synthetic-3.13.12" and identity["packagingTarget"] is False


def test_unaudited_interpreter_is_refused_even_with_the_opt_in(setup):
    with pytest.raises(UnsupportedCPythonBuild, match="not an audited interpreter"):
        packaging_identity(setup.load(), Interpreter("3.14.9", PLATFORM, "tags/v3.14.9:abcdef0", setup.python), allow_audited=True)


def test_packaging_target_with_changed_bytes_is_refused(setup):
    (setup.python / "DLLs" / "_bz2.pyd").write_bytes(b"patched")
    with pytest.raises(UnsupportedCPythonBuild, match="DLLs/_bz2.pyd differs"):
        packaging_identity(setup.load(), Interpreter(VERSION, PLATFORM, BUILD, setup.python))


def test_registry_packaging_target_must_be_an_audited_record(setup):
    raw = setup.variant()
    raw["packagingTarget"] = "cpython-3.15.0"
    with pytest.raises(CPythonNoticeError, match="packaging target must name an audited interpreter"):
        setup.load(raw)


def test_unsupported_build_is_distinct_from_the_rights_review(setup):
    plan = setup.plan()
    assert plan.inventory["support"]["status"] == "audited-build"
    assert plan.inventory["rightsReview"]["status"] == "owner-review-required"
    assert issubclass(UnsupportedCPythonBuild, CPythonNoticeError)
    with pytest.raises(CPythonNoticeError) as registry_error:
        setup.load({**setup.variant(), "schema": 2})
    assert not isinstance(registry_error.value, UnsupportedCPythonBuild)


def test_committed_packaging_target_is_the_audited_3_14_8_installer_build():
    registry = load_registry()
    target = next(record for record in registry.interpreters if record["id"] == registry.packaging_target)
    assert (target["version"], target["platform"], target["build"]) == ("3.14.8", "win-amd64", "tags/v3.14.8:8e6e75d")


def test_runtime_manifest_records_the_interpreter_identity(tmp_path):
    bundle = tmp_path / "bundle"
    executable = "qronos-runtime.exe" if os.name == "nt" else "qronos-runtime"
    bundle.mkdir()
    (bundle / executable).write_bytes(b"exe")
    version = ".".join(map(str, sys.version_info[:3]))
    identity = {"implementation": "CPython", "version": version, "platform": "win-amd64", "build": f"tags/v{version}:0000000",
                "sourceCommit": "0" * 40, "auditedRecord": "record", "packagingTarget": False}
    manifest = json.loads(build_runtime.write_manifest(bundle, identity).read_text(encoding="utf-8"))
    assert manifest["python"] == identity and manifest["pythonVersion"] == version
    build_runtime.verify_manifest(bundle)
    for broken in ({**identity, "packagingTarget": "yes"}, {**identity, "version": "0.0.1"}, {**identity, "extra": "x"}):
        raw = dict(manifest, python=broken)
        (bundle / "runtime-manifest.json").write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ValueError, match="interpreter identity"):
            build_runtime.verify_manifest(bundle)


# ---------------------------------------------------- output and manifest


def _write(setup: Setup, root: Path) -> Path:
    plan = setup.plan()
    copies = tuple(NoticeCopy(t.source, t.destination, t.sha256, t.size, t.kind) for t in plan.texts)
    inventory = {"schemaVersion": 1, "distributions": [], "cpython": {"version": VERSION, **plan.inventory},
                 "microsoftRuntime": {"files": []}, "prunedBinaries": []}
    return write_runtime_notices(NoticePlan(inventory=inventory, copies=copies), root)


def test_written_component_texts_are_byte_exact_and_deterministic(setup, tmp_path, monkeypatch):
    monkeypatch.setattr("build_support.runtime_notices._readme", lambda inventory: "readme\n")
    first, second = _write(setup, tmp_path / "one"), _write(setup, tmp_path / "two")
    names = sorted(p.relative_to(first).as_posix() for p in first.rglob("*") if p.is_file())
    assert names == sorted(p.relative_to(second).as_posix() for p in second.rglob("*") if p.is_file())
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes()
    written = first / f"python/cpython-{VERSION}/components/sqlite-3.50.4"
    assert not written.exists()  # sqlite3.dll was not bundled
    assert (first / f"python/cpython-{VERSION}/components/bzip2-1.0.8/LICENSE").read_bytes() == \
        (setup.builder.text_root / "bzip2" / "LICENSE").read_bytes()


def test_component_texts_are_sealed_by_the_runtime_manifest(setup, tmp_path, monkeypatch):
    monkeypatch.setattr("build_support.runtime_notices._readme", lambda inventory: "readme\n")
    bundle = tmp_path / "bundle"
    executable = "qronos-runtime.exe" if os.name == "nt" else "qronos-runtime"
    (bundle / executable).write_bytes(b"exe")
    _write(setup, bundle)
    manifest = json.loads(build_runtime.write_manifest(bundle).read_text(encoding="utf-8"))
    entries = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    text = f"{NOTICE_DIRECTORY}/python/cpython-{VERSION}/components/zlib-1.3.1/LICENSE"
    assert entries[text] == hashlib.sha256((setup.builder.text_root / "zlib" / "LICENSE").read_bytes()).hexdigest()
    build_runtime.verify_manifest(bundle)
    (bundle / text).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="failed integrity verification"):
        build_runtime.verify_manifest(bundle)


# --------------------------------------------------------- committed registry


def test_committed_registry_pins_every_text_and_record():
    registry = load_registry()
    ids = {record["id"] for record in registry.interpreters}
    assert ids == {
        "cpython-3.13.13-win-amd64-python.org-installer",
        "cpython-3.14.7-win-amd64-python.org-installer",
        "cpython-3.14.8-win-amd64-python.org-installer",
    }


def test_committed_registry_binds_the_audited_components_to_the_binaries_that_contain_them():
    registry = load_registry()
    records = {record["version"]: record for record in registry.interpreters}
    core13 = records["3.13.13"]["files"]["python313.dll"]["components"]
    assert {"zlib@1.3.1", "libb2@0.98.1", "mimalloc@2.1.2", "unicode-ucd@15.1.0",
            "hacl-star@bb3d0dc8d9d15a5cd51094d5b69e70aa09005ff0"} <= set(core13)
    assert set(records["3.13.13"]["dataTrees"]) == {"_tcl_data", "_tk_data", "tcl8"}
    for version, ssl, expat in (("3.14.7", "3.5.7", "2.8.2"), ("3.14.8", "3.5.9", "2.8.5")):
        files = records[version]["files"]
        assert "zlib-ng@2.2.4" in files["python314.dll"]["components"]
        assert files["DLLs/_zstd.pyd"]["components"] == ["zstd@1.5.7"]
        assert files["DLLs/libcrypto-3.dll"]["components"] == [f"openssl@{ssl}"]
        assert files["DLLs/_elementtree.pyd"]["components"] == [f"expat@{expat}"]
        assert files["DLLs/libtommath.dll"]["components"] == ["libtommath@tcl-9.0.4"]
        assert records[version]["dataTrees"] == {}  # Tcl 9 keeps its library inside tcl90.dll
        assert files["vcruntime140.dll"] == {"size": 178616, "sha256": "d1f4225df2cd877dbf130d5668a021dce3f94118455ff5ec952061c30afc9ce7",
                                             "microsoftRuntime": True}
    assert "unresolved" in registry.components["zstd@1.5.7"]
    assert registry.components["sqlite@3.50.4"]["attribution"] == []


def test_committed_text_folder_keeps_upstream_bytes_unconverted():
    attributes = (cpython_notices.TEXT_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* -text -whitespace" in attributes
    registry = load_registry()
    crlf = [text_id for text_id in registry.texts if b"\r\n" in (cpython_notices.TEXT_ROOT / text_id).read_bytes()]
    assert crlf == []  # every pinned upstream text uses LF; a CRLF checkout would change the hashes


@pytest.mark.skipif(os.name != "nt", reason="Audited interpreters are Windows builds.")
def test_build_interpreter_matches_its_audited_record_when_it_is_one():
    registry = load_registry()
    interpreter = Interpreter(".".join(map(str, sys.version_info[:3])), sysconfig.get_platform(),
                              platform.python_build()[0], Path(sys.base_prefix))
    try:
        record = cpython_notices.find_interpreter(registry, interpreter)
    except CPythonNoticeError:
        pytest.skip("This interpreter is not an audited build; packaging with it fails closed.")
    cpython_notices.verify_interpreter(record, interpreter.base)
