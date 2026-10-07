"""Desktop third-party notice collection (build_support.desktop_notices).

Synthetic Cargo metadata, lock files and package trees under tmp_path prove the
rules; two tests read the real tracked lock files (and, when the offline Cargo
registry is present, the real locked graph) to prove actual coverage. Nothing
is downloaded and no installer is built.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from build_support import build_desktop
from build_support import desktop_notices as notices
from build_support.desktop_notices import (
    Component,
    NoticeError,
    check_coverage,
    npm_components,
    npm_production_keys,
    rust_components,
    write_notices,
)

REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
ROOT_ID = "path+file:///qronos-desktop#0.1.0"
# Captured at import, before the autouse fixture isolates the profile, so the
# real-graph test can read the existing offline registry (read-only).
TOOLCHAIN_HOMES = {
    "CARGO_HOME": os.environ.get("CARGO_HOME") or str(Path.home() / ".cargo"),
    "RUSTUP_HOME": os.environ.get("RUSTUP_HOME") or str(Path.home() / ".rustup"),
}


# ---------------------------------------------------------------- fixtures


def make_crate(base: Path, name: str, version: str, files: dict[str, str], *,
               license: str | None = "MIT", license_file: str | None = None) -> dict:
    directory = base / "registry" / f"{name}-{version}"
    directory.mkdir(parents=True)
    (directory / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return {"id": f"{name} {version}", "name": name, "version": version, "license": license,
            "license_file": license_file, "manifest_path": str(directory / "Cargo.toml"),
            "source": REGISTRY}


def metadata(packages: list[dict], edges: dict[str, list[tuple[str, str | None]]]) -> dict:
    root = {"id": ROOT_ID, "name": "qronos-desktop", "version": "0.1.0", "license": None,
            "license_file": None, "manifest_path": "unused", "source": None}
    ids = [ROOT_ID] + [package["id"] for package in packages]
    nodes = [{"id": node, "deps": [{"pkg": target, "dep_kinds": [{"kind": kind}]}
                                   for target, kind in edges.get(node, [])]} for node in ids]
    return {"packages": [root] + packages, "workspace_members": [ROOT_ID],
            "resolve": {"root": ROOT_ID, "nodes": nodes}}


def component(directory: Path, name: str = "demo", version: str = "1.0.0", *,
              declared: str | None = "MIT", license_file: str | None = None,
              provenance: str = "sum") -> Component:
    return Component("rust", name, version, declared, license_file, REGISTRY, provenance,
                     directory.resolve())


def tree_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def generate(tmp_path: Path, components: list[Component], exceptions: dict | None = None,
             out: str = "out", private: tuple[str, ...] = ()) -> dict:
    exceptions = exceptions or {}
    files = check_coverage(components, exceptions)
    return write_notices(components, files, exceptions, tmp_path / out,
                         notices.WINDOWS_TARGET, private)


# ------------------------------------------------------------ graph scope


def test_the_rust_closure_follows_normal_edges_only(tmp_path):
    crates = {name: make_crate(tmp_path, name, "1.0.0", {"LICENSE": name})
              for name in ("linked", "transitive", "buildonly", "builddep", "devonly")}
    graph = metadata(list(crates.values()), {
        ROOT_ID: [("linked 1.0.0", None), ("buildonly 1.0.0", "build"), ("devonly 1.0.0", "dev")],
        "linked 1.0.0": [("transitive 1.0.0", None)],
        "buildonly 1.0.0": [("builddep 1.0.0", None)],
    })

    names = {item.name for item in rust_components(graph, {})}

    assert names == {"linked", "transitive"}


def test_a_local_path_dependency_is_not_treated_as_third_party(tmp_path):
    local = make_crate(tmp_path, "inhouse", "0.1.0", {"LICENSE": "x"})
    local["source"] = None
    graph = metadata([local], {ROOT_ID: [("inhouse 0.1.0", None)]})

    with pytest.raises(NoticeError, match="local path crate"):
        rust_components(graph, {})


def test_the_real_lock_production_closure_is_the_bundled_frontend():
    lock = json.loads(notices.PACKAGE_LOCK.read_text(encoding="utf-8"))

    keys = npm_production_keys(lock)

    assert keys == [
        "node_modules/@tauri-apps/api",
        "node_modules/@tauri-apps/plugin-opener",
        "node_modules/react",
        "node_modules/react-dom",
        "node_modules/scheduler",
    ]
    # Build tooling is not claimed to ship.
    assert not any(name in key for key in keys for name in ("vite", "typescript", "@tauri-apps/cli"))


def test_the_real_locked_rust_graph_covers_the_linked_crates(monkeypatch):
    for name, value in TOOLCHAIN_HOMES.items():
        monkeypatch.setenv(name, value)
    try:
        graph = notices.cargo_metadata()
    except (NoticeError, OSError):
        pytest.skip("The offline locked Cargo registry is not available on this machine.")

    checksums = notices.cargo_lock_checksums(notices.CARGO_MANIFEST.parent / "Cargo.lock")
    found = {item.name: item for item in rust_components(graph, checksums)}

    for linked in ("tauri", "wry", "tao", "webview2-com", "serde", "tokio", "windows-sys",
                   "tauri-plugin-single-instance"):
        assert linked in found
    assert "tauri-build" not in found  # build-dependency only
    assert all(item.provenance for item in found.values())  # every crate has a lock checksum


def test_a_dev_package_reachable_from_production_is_refused():
    lock = {"lockfileVersion": 3, "packages": {
        "": {"dependencies": {"app": "1"}},
        "node_modules/app": {"version": "1.0.0", "dependencies": {"helper": "1"}},
        "node_modules/helper": {"version": "1.0.0", "dev": True},
    }}

    with pytest.raises(NoticeError, match="locked as dev"):
        npm_production_keys(lock)


def test_nested_npm_resolution_prefers_the_nearest_copy():
    lock = {"lockfileVersion": 3, "packages": {
        "": {"dependencies": {"app": "1"}},
        "node_modules/app": {"version": "1.0.0", "dependencies": {"shared": "2"}},
        "node_modules/app/node_modules/shared": {"version": "2.0.0"},
        "node_modules/shared": {"version": "1.0.0", "dev": True},
    }}

    assert npm_production_keys(lock) == ["node_modules/app", "node_modules/app/node_modules/shared"]


# ----------------------------------------------- preservation and failures


def test_notice_files_and_the_license_file_field_are_preserved(tmp_path):
    crate = make_crate(tmp_path, "keeps", "2.1.0", {
        "LICENSE-MIT": "MIT text", "LICENSE-APACHE": "Apache text", "NOTICE": "Required notice",
        "docs/COPYRIGHT.txt": "Custom terms", "README.md": "not a notice",
    }, license="MIT OR Apache-2.0", license_file="docs/COPYRIGHT.txt")
    item = rust_components(metadata([crate], {ROOT_ID: [("keeps 2.1.0", None)]}),
                           {("keeps", "2.1.0"): "abc"})[0]

    inventory = generate(tmp_path, [item])

    (record,) = inventory["components"]
    assert record["licenseFileField"] == "docs/COPYRIGHT.txt"
    assert record["provenance"] == "abc"
    assert record["noticeFiles"] == [
        "licenses/rust/keeps-2.1.0/LICENSE-APACHE",
        "licenses/rust/keeps-2.1.0/LICENSE-MIT",
        "licenses/rust/keeps-2.1.0/NOTICE",
        "licenses/rust/keeps-2.1.0/docs/COPYRIGHT.txt",
    ]
    out = tmp_path / "out"
    assert (out / "licenses/rust/keeps-2.1.0/NOTICE").read_text(encoding="utf-8") == "Required notice"
    text = (out / notices.NOTICE_TEXT).read_text(encoding="utf-8")
    assert "Required notice" in text and "Custom terms" in text and "not a notice" not in text


@pytest.mark.parametrize(("files", "declared", "missing"), [
    ({}, "MIT", "text"),
    ({"LICENSE": "text"}, None, "expression"),
    ({"LICENSE.spdx": "PackageLicenseDeclared: MIT"}, "MIT", "text"),
])
def test_a_missing_licence_fails_with_the_exact_identity(tmp_path, files, declared, missing):
    directory = tmp_path / "pkg"
    directory.mkdir()
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")

    with pytest.raises(NoticeError, match=rf"demo@1\.0\.0: no licence {missing} \(provenance sum\)"):
        check_coverage([component(directory, declared=declared)], {})


def test_only_an_exact_reviewed_exception_admits_a_missing_text(tmp_path):
    directory = tmp_path / "pkg"
    directory.mkdir()
    item = component(directory)
    exact = {"ecosystem": "rust", "name": "demo", "version": "1.0.0", "provenance": "sum",
             "missing": "text", "reason": "published crate has no licence file", "reviewedBy": "Root"}

    inventory = generate(tmp_path, [item], {item.key: exact})

    record = inventory["components"][0]
    assert record["declaredLicense"] == "MIT"  # unchanged: nothing invented
    assert record["licenceTextPresent"] is False
    assert record["exception"]["reviewedBy"] == "Root"

    with pytest.raises(NoticeError):
        check_coverage([item], {item.key: {**exact, "provenance": "other"}})
    with pytest.raises(NoticeError, match="matches no missing notice"):
        check_coverage([item], {("rust", "demo", "9.9.9"): exact})


def test_the_committed_exception_list_is_empty_and_well_formed():
    assert notices.load_exceptions() == {}


# --------------------------------------------------------- path safety


@pytest.mark.parametrize("declared", ["../outside/LICENSE", "C:/Windows/win.ini", "/etc/passwd",
                                      "\\\\server\\share\\LICENSE", "docs/../../LICENSE"])
def test_a_license_file_outside_the_package_is_refused(tmp_path, declared):
    directory = tmp_path / "pkg"
    directory.mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "LICENSE").write_text("x", encoding="utf-8")
    (directory / "LICENSE").write_text("ok", encoding="utf-8")

    with pytest.raises(NoticeError):
        check_coverage([component(directory, license_file=declared)], {})


def test_a_linked_notice_file_is_refused(tmp_path):
    directory = tmp_path / "pkg"
    directory.mkdir()
    target = tmp_path / "elsewhere.txt"
    target.write_text("x", encoding="utf-8")
    try:
        os.symlink(target, directory / "LICENSE")
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are not permitted for this user.")

    with pytest.raises(NoticeError, match="link"):
        check_coverage([component(directory)], {})


def test_a_junctioned_npm_package_is_refused(tmp_path):
    winapi = pytest.importorskip("_winapi")
    real = tmp_path / "real-react"
    real.mkdir()
    (real / "package.json").write_text('{"name": "react", "version": "19.2.8"}', encoding="utf-8")
    (real / "LICENSE").write_text("MIT", encoding="utf-8")
    modules = tmp_path / "node_modules"
    modules.mkdir()
    winapi.CreateJunction(str(real), str(modules / "react"))
    lock = {"lockfileVersion": 3, "packages": {
        "": {"dependencies": {"react": "19"}},
        "node_modules/react": {"version": "19.2.8", "license": "MIT"},
    }}

    with pytest.raises(NoticeError, match="link"):
        npm_components(lock, modules)


def test_an_installed_package_must_match_the_lock(tmp_path):
    modules = tmp_path / "node_modules"
    (modules / "react").mkdir(parents=True)
    (modules / "react" / "package.json").write_text('{"name": "react", "version": "18.0.0"}',
                                                    encoding="utf-8")
    lock = {"lockfileVersion": 3, "packages": {
        "": {"dependencies": {"react": "19"}},
        "node_modules/react": {"version": "19.2.8", "license": "MIT"},
    }}

    with pytest.raises(NoticeError, match="does not match the locked version"):
        npm_components(lock, modules)


def test_output_carrying_a_private_path_is_refused(tmp_path):
    directory = tmp_path / "pkg"
    directory.mkdir()
    (directory / "LICENSE").write_text(f"built at {tmp_path}", encoding="utf-8")

    with pytest.raises(NoticeError, match="private path"):
        generate(tmp_path, [component(directory)], private=(str(tmp_path),))


def test_a_foreign_non_empty_output_directory_is_not_deleted(tmp_path):
    directory = tmp_path / "pkg"
    directory.mkdir()
    (directory / "LICENSE").write_text("MIT", encoding="utf-8")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "keep.txt").write_text("user file", encoding="utf-8")

    with pytest.raises(NoticeError, match="not created by this tool"):
        generate(tmp_path, [component(directory)])
    assert (tmp_path / "out" / "keep.txt").read_text(encoding="utf-8") == "user file"


# ------------------------------------------------------------ determinism


def test_generation_is_deterministic_and_relative(tmp_path):
    items = []
    for name in ("zeta", "alpha"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "LICENSE").write_text(f"{name} licence\r\nline", encoding="utf-8")
        items.append(component(directory, name=name))

    first = generate(tmp_path, items, out="a")
    second = generate(tmp_path, list(reversed(items)), out="b")
    again = generate(tmp_path, items, out="a")  # regenerating its own output is allowed

    assert first == second == again
    assert tree_digest(tmp_path / "a") == tree_digest(tmp_path / "b")
    assert [record["name"] for record in first["components"]] == ["alpha", "zeta"]
    inventory_text = (tmp_path / "a" / notices.INVENTORY).read_text(encoding="utf-8")
    assert str(tmp_path) not in inventory_text and str(tmp_path).replace("\\", "/") not in inventory_text
    # Licence bytes are copied exactly.
    assert (tmp_path / "a/licenses/rust/alpha-1.0.0/LICENSE").read_bytes() == \
        (tmp_path / "alpha" / "LICENSE").read_bytes()


# --------------------------------------------------------- installer wiring


def test_the_installer_overlay_maps_backend_and_notices(tmp_path, monkeypatch):
    calls: list[str] = []
    backend = tmp_path / "qronos-runtime"
    backend.mkdir()
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    monkeypatch.setattr(build_desktop, "DESKTOP_NOTICES", tmp_path / "runtime/package/desktop-notices")
    monkeypatch.setattr(build_desktop, "verify_manifest", lambda path: calls.append("verify"))
    monkeypatch.setattr(build_desktop.shutil, "which", lambda name: "npm")

    def collect(out_dir):
        calls.append("notices")
        out_dir.mkdir(parents=True)

    def run(command, **kwargs):
        calls.append("tauri")
        configuration = Path(command[command.index("--config") + 1])
        recorded.update(json.loads(configuration.read_text(encoding="utf-8")))
        return subprocess.CompletedProcess(command, 0)

    recorded: dict = {}
    monkeypatch.setattr(build_desktop, "collect_desktop_notices", collect)
    monkeypatch.setattr(build_desktop.subprocess, "run", run)

    build_desktop.build(backend)

    assert calls == ["verify", "notices", "tauri"]
    resources = recorded["bundle"]["resources"]
    assert resources[backend.resolve().as_posix() + "/"] == "qronos-runtime/"
    notices_dir = (tmp_path / "runtime/package/desktop-notices").resolve().as_posix() + "/"
    assert resources[notices_dir] == notices.NOTICE_RESOURCE_TARGET == "third-party-notices/"


def test_incomplete_notices_stop_the_installer_build(tmp_path, monkeypatch):
    backend = tmp_path / "qronos-runtime"
    backend.mkdir()
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    monkeypatch.setattr(build_desktop, "verify_manifest", lambda path: None)
    monkeypatch.setattr(build_desktop.shutil, "which", lambda name: "npm")

    def collect(out_dir):
        raise NoticeError("incomplete")

    def run(command, **kwargs):
        raise AssertionError("tauri build must not start")

    monkeypatch.setattr(build_desktop, "collect_desktop_notices", collect)
    monkeypatch.setattr(build_desktop.subprocess, "run", run)

    with pytest.raises(NoticeError):
        build_desktop.build(backend)
