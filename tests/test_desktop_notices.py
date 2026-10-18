"""Desktop third-party notice collection (build_support.desktop_notices).

Synthetic Cargo metadata, lock files and package trees under tmp_path prove the
rules; a few tests read the real tracked lock files, the committed upstream
registry and (when the offline Cargo registry is present) the real locked graph
to prove actual coverage. Nothing is downloaded and no installer is built.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

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
# real-graph tests can read the existing offline registry (read-only).
TOOLCHAIN_HOMES = {
    "CARGO_HOME": os.environ.get("CARGO_HOME") or str(Path.home() / ".cargo"),
    "RUSTUP_HOME": os.environ.get("RUSTUP_HOME") or str(Path.home() / ".rustup"),
}
#: Substantive licence wording (a bare label such as "MIT" is not licence text).
LICENCE = ("Permission is hereby granted, free of charge, to any person obtaining a copy\n"
           "of this software. THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND.\n")


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
              provenance: str = "sum", ecosystem: str = "rust", source: str = REGISTRY) -> Component:
    return Component(ecosystem, name, version, declared, license_file, source, provenance,
                     directory.resolve())


def package_dir(tmp_path: Path, name: str = "pkg", files: dict[str, str] | None = None) -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True)
    for relative, content in (files if files is not None else {"LICENSE": LICENCE}).items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return directory


def tree(directory: Path) -> dict[str, bytes]:
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*")) if path.is_file()}


def tree_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for name, data in tree(directory).items():
        digest.update(name.encode())
        digest.update(data)
    return digest.hexdigest()


def generate(tmp_path: Path, components: list[Component], exceptions: dict | None = None,
             out: str = "out", private: tuple[str, ...] = (), upstream: dict | None = None) -> dict:
    exceptions = exceptions or {}
    files = check_coverage(components, exceptions, upstream)
    return write_notices(components, files, exceptions, tmp_path / out,
                         notices.WINDOWS_TARGET, private, upstream)


def junction(target: Path, link: Path) -> None:
    winapi = pytest.importorskip("_winapi")
    winapi.CreateJunction(str(target), str(link))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


UPSTREAM_COMMIT = "0123456789abcdef0123456789abcdef01234567"
UPSTREAM_LICENCE = b"MIT License\r\n\r\nCopyright (c) 2020 Example Holder\r\n"
UPSTREAM_NOTICE = b"Example NOTICE\n"


def upstream_package(tmp_path: Path) -> tuple[Path, Component]:
    """A published package without licence text, plus its identity file."""
    directory = tmp_path / "pkg"
    directory.mkdir()
    (directory / "Cargo.toml.orig").write_bytes(b"[package]\nname = \"demo\"\n")
    return directory, component(directory)


def upstream_registry(tmp_path: Path, **overrides) -> Path:
    """A registry with one entry for component() and its vendored files."""
    base = tmp_path / "upstream"
    texts = base / "texts" / "example__demo-0123456789ab"
    texts.mkdir(parents=True)
    (texts / "LICENSE").write_bytes(UPSTREAM_LICENCE)
    (texts / "NOTICE").write_bytes(UPSTREAM_NOTICE)

    def text(name: str, data: bytes, role: str) -> dict:
        return {"file": f"example__demo-0123456789ab/{name}", "sha256": sha256(data), "role": role,
                "repository": "https://github.com/example/demo", "revision": UPSTREAM_COMMIT,
                "sourcePath": name,
                "url": f"https://raw.githubusercontent.com/example/demo/{UPSTREAM_COMMIT}/{name}"}

    entry = {
        "ecosystem": "rust", "name": "demo", "version": "1.0.0", "provenance": "sum",
        "declaredLicense": "MIT",
        "upstream": {"repository": "https://github.com/example/demo", "revision": UPSTREAM_COMMIT,
                     "packagePath": "demo", "pinnedBy": ".cargo_vcs_info.json git.sha1"},
        "identity": [{"path": "Cargo.toml.orig",
                      "sha256": sha256(b"[package]\nname = \"demo\"\n")}],
        "texts": [text("LICENSE", UPSTREAM_LICENCE, "licence"),
                  text("NOTICE", UPSTREAM_NOTICE, "notice")],
        "note": "Repository-root licence at the exact packaged revision.",
        "ownerReview": "none",
        "evidence": [f".cargo_vcs_info.json git.sha1 = {UPSTREAM_COMMIT}"],
    }
    entry.update(overrides)
    registry = base / "registry.json"
    registry.write_text(json.dumps({"schema": 1, "packages": [entry]}), encoding="utf-8")
    return registry


def rewrite_entry(registry: Path, change) -> None:
    data = json.loads(registry.read_text(encoding="utf-8"))
    change(data["packages"][0])
    registry.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------------------ graph scope


def test_the_rust_closure_follows_normal_edges_only(tmp_path):
    crates = {name: make_crate(tmp_path, name, "1.0.0", {"LICENSE": LICENCE})
              for name in ("normal", "transitive", "buildonly", "builddep", "devonly")}
    graph = metadata(list(crates.values()), {
        ROOT_ID: [("normal 1.0.0", None), ("buildonly 1.0.0", "build"), ("devonly 1.0.0", "dev")],
        "normal 1.0.0": [("transitive 1.0.0", None)],
        "buildonly 1.0.0": [("builddep 1.0.0", None)],
    })

    names = {item.name for item in rust_components(graph, {})}

    assert names == {"normal", "transitive"}


def test_a_local_path_dependency_is_not_treated_as_third_party(tmp_path):
    local = make_crate(tmp_path, "inhouse", "0.1.0", {"LICENSE": LICENCE})
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


def test_the_real_locked_rust_closure_has_the_expected_scope(monkeypatch):
    for name, value in TOOLCHAIN_HOMES.items():
        monkeypatch.setenv(name, value)
    try:
        graph = notices.cargo_metadata()
    except (NoticeError, OSError):
        pytest.skip("The offline locked Cargo registry is not available on this machine.")

    checksums = notices.cargo_lock_checksums(notices.CARGO_MANIFEST.parent / "Cargo.lock")
    found = {item.name: item for item in rust_components(graph, checksums)}

    for expected in ("tauri", "wry", "tao", "webview2-com", "serde", "tokio", "windows-sys",
                     "tauri-plugin-single-instance"):
        assert expected in found
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


# ----------------------------------------------- licence text and roles


def test_licence_and_auxiliary_notices_are_copied_with_distinct_roles(tmp_path):
    crate = make_crate(tmp_path, "keeps", "2.1.0", {
        "LICENSE-MIT": LICENCE, "LICENSE-APACHE": "Apache License\nVersion 2.0, January 2004\n",
        "NOTICE": "Required notice", "README.md": "not a notice",
    }, license="MIT OR Apache-2.0")
    item = rust_components(metadata([crate], {ROOT_ID: [("keeps 2.1.0", None)]}),
                           {("keeps", "2.1.0"): "abc"})[0]

    inventory = generate(tmp_path, [item])

    (record,) = inventory["components"]
    folder = "licenses/rust/keeps/2.1.0"
    assert [(entry["path"], entry["role"]) for entry in record["notices"]] == [
        (f"{folder}/LICENSE-APACHE", "licence"),
        (f"{folder}/LICENSE-MIT", "licence"),
        (f"{folder}/NOTICE", "auxiliary"),
    ]
    out = tmp_path / "out"
    assert (out / folder / "NOTICE").read_bytes() == b"Required notice"
    assert (out / folder / "LICENSE-MIT").read_bytes() == (Path(crate["manifest_path"]).parent / "LICENSE-MIT").read_bytes()
    text = (out / notices.NOTICE_TEXT).read_text(encoding="utf-8")
    assert "Required notice" in text and "not a notice" not in text


def test_an_unusual_declared_license_file_counts_and_keeps_its_field(tmp_path):
    directory = package_dir(tmp_path, files={"legal/TERMS.txt": LICENCE, "COPYRIGHT": "(c) Holder"})

    inventory = generate(tmp_path, [component(directory, license_file="legal/TERMS.txt")])

    (record,) = inventory["components"]
    assert record["licenseFileField"] == "legal/TERMS.txt"
    assert record["licenceTextPresent"] is True
    assert {entry["path"].rsplit("/", 2)[-2:][-1]: entry["role"] for entry in record["notices"]} == {
        "COPYRIGHT": "auxiliary", "TERMS.txt": "licence"}


@pytest.mark.parametrize(("files", "declared", "missing"), [
    ({}, "MIT", "text"),
    ({"LICENSE": LICENCE}, None, "expression"),
    ({"LICENSE.spdx": "PackageLicenseDeclared: MIT"}, "MIT", "text"),
    ({"NOTICE": "This product includes software developed by Example."}, "MIT", "text"),
    ({"COPYRIGHT": "Copyright 2020 Example Holder\nAll rights reserved.\n"}, "MIT", "text"),
    ({"LICENSE": ""}, "MIT", "text"),
    ({"LICENSE": " \r\n\t\n"}, "MIT", "text"),
    ({"LICENSE": "MIT"}, "MIT", "text"),
    ({"LICENSE-MIT": "SPDX-License-Identifier: MIT OR Apache-2.0\n"}, "MIT", "text"),
    ({"COPYING": "(MIT/Apache-2.0)"}, "MIT", "text"),
])
def test_a_missing_licence_fails_with_the_exact_identity(tmp_path, files, declared, missing):
    directory = package_dir(tmp_path, files=files)

    with pytest.raises(NoticeError, match=rf"demo@1\.0\.0: no licence {missing} \(provenance sum\)"):
        check_coverage([component(directory, declared=declared)], {})


@pytest.mark.parametrize(("declared_file", "content"), [
    ("AUTHORS", "Example Author\nA Contributor\n"),
    ("NOTICE", "Example required notice\nAuthors: Example\n"),
    ("COPYRIGHT", "Copyright 2020 Example Holder\nAll rights reserved.\n"),
    ("README.md", "# demo\nLicensed under MIT. See the repository for details.\n"),
    ("docs/AUTHORS.txt", "Example Author <author@example.invalid>\n"),
    ("TERMS", ""),
    ("TERMS", "MIT OR Apache-2.0"),
])
def test_a_declared_license_file_with_only_metadata_or_a_label_does_not_count(tmp_path, declared_file, content):
    directory = package_dir(tmp_path, files={declared_file: content})

    with pytest.raises(NoticeError, match=r"demo@1\.0\.0: no licence text"):
        check_coverage([component(directory, license_file=declared_file)], {})
    (found,) = notices.notice_files(component(directory, license_file=declared_file)),
    assert [item.role for item in found] == (["licence-label"] if declared_file == "TERMS" else ["auxiliary"])


def test_auxiliary_files_stay_collectable_but_never_count(tmp_path):
    directory = package_dir(tmp_path, files={"NOTICE": "Attribution", "LICENSE": "MIT"})
    item = component(directory)
    exception = {"ecosystem": "rust", "name": "demo", "version": "1.0.0", "provenance": "sum",
                 "missing": "text", "reason": "only a label is published", "reviewedBy": "Root"}

    inventory = generate(tmp_path, [item], {item.key: exception})

    (record,) = inventory["components"]
    assert record["licenceTextPresent"] is False
    assert record["licenceTextSource"] == "reviewed-exception"
    assert sorted(entry["role"] for entry in record["notices"]) == ["auxiliary", "licence-label"]
    assert (tmp_path / "out/licenses/rust/demo/1.0.0/NOTICE").read_bytes() == b"Attribution"


def test_only_an_exact_reviewed_exception_admits_a_missing_text(tmp_path):
    directory = package_dir(tmp_path, files={})
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


# ------------------------------------------------ identity and folders


@pytest.mark.parametrize("version", [
    "1.0/../../../../escaped", "1.0.0/..", "..", "1.0.0\\..\\x", "C:\\temp", "C:1.0.0",
    "\\\\server\\share", "1.0.0:stream", "1.0.0\n", "1.0.0\x00", "1.0.0\t", "1.0", "01.0.0",
    "1.0.0-", "1.0.0+", "1.0.0-a..b", " 1.0.0", "1.0.0.",
])
def test_a_malicious_matching_npm_version_is_refused_before_any_output(tmp_path, version):
    modules = tmp_path / "node_modules"
    (modules / "pkg").mkdir(parents=True)
    (modules / "pkg" / "package.json").write_text(json.dumps({"name": "pkg", "version": version}),
                                                  encoding="utf-8")
    (modules / "pkg" / "LICENSE").write_text(LICENCE, encoding="utf-8")
    lock = {"lockfileVersion": 3, "packages": {
        "": {"dependencies": {"pkg": "1"}},
        "node_modules/pkg": {"version": version, "license": "MIT"},
    }}

    with pytest.raises(NoticeError, match="semantic version"):
        npm_components(lock, modules)

    # A component built some other way is refused before the output exists.
    item = component(modules / "pkg", name="pkg", version=version, ecosystem="npm")
    with pytest.raises(NoticeError, match="semantic version"):
        write_notices([item], {item.key: []}, {}, tmp_path / "out", notices.WINDOWS_TARGET, ())
    assert not (tmp_path / "out").exists()
    assert not any(tmp_path.rglob("escaped"))


@pytest.mark.parametrize("version", ["1.0.0-alpha.1", "1.0.0+build.5", "2.0.0-rc.1+sha.5114f85", "0.0.0-0",
                                     "1.0.0-x-y-z.--"])
def test_valid_prerelease_and_build_versions_are_kept(tmp_path, version):
    directory = package_dir(tmp_path)

    inventory = generate(tmp_path, [component(directory, version=version)])

    assert inventory["components"][0]["folder"] == f"licenses/rust/demo/{version}"
    assert (tmp_path / "out/licenses/rust/demo" / version / "LICENSE").is_file()


@pytest.mark.parametrize(("name", "ecosystem"), [
    ("../escape", "npm"), ("@scope/../x", "npm"), ("a/b/c", "npm"), ("con", "npm"), ("Upper", "npm"),
    ("evil/x", "rust"), ("aux", "rust"), ("a:b", "rust"), ("", "rust"),
])
def test_unsafe_package_names_are_refused(tmp_path, name, ecosystem):
    item = component(package_dir(tmp_path), name=name, ecosystem=ecosystem)

    with pytest.raises(NoticeError, match="package name|portable"):
        check_coverage([item], {})


def test_scoped_and_underscore_npm_names_keep_separate_notices(tmp_path):
    scoped = component(package_dir(tmp_path, "scoped", {"LICENSE": LICENCE + "scoped\n"}),
                       name="@foo/bar", ecosystem="npm")
    plain = component(package_dir(tmp_path, "plain", {"LICENSE": LICENCE + "unscoped\n"}),
                      name="foo__bar", ecosystem="npm")

    inventory = generate(tmp_path, [scoped, plain])

    out = tmp_path / "out"
    assert (out / "licenses/npm/@foo/bar/1.0.0/LICENSE").read_text(encoding="utf-8").endswith("scoped\n")
    assert (out / "licenses/npm/foo__bar/1.0.0/LICENSE").read_text(encoding="utf-8").endswith("unscoped\n")
    paths = [entry["path"] for record in inventory["components"] for entry in record["notices"]]
    assert len(paths) == len(set(paths)) == 2


def test_a_case_insensitive_destination_collision_fails_before_output(tmp_path):
    upper = component(package_dir(tmp_path, "upper"), name="Demo")
    lower = component(package_dir(tmp_path, "lower"), name="demo")

    with pytest.raises(NoticeError, match="collide"):
        generate(tmp_path, [upper, lower])
    assert not (tmp_path / "out").exists()


def test_duplicate_package_identities_are_refused(tmp_path):
    first = component(package_dir(tmp_path, "one"), provenance="aaa")
    second = component(package_dir(tmp_path, "two"), provenance="bbb")

    with pytest.raises(NoticeError, match="listed more than once"):
        check_coverage([first, second], {})
    with pytest.raises(NoticeError, match="listed more than once"):
        write_notices([first, first], {first.key: []}, {}, tmp_path / "out", notices.WINDOWS_TARGET, ())
    assert not (tmp_path / "out").exists()


def test_a_file_that_is_also_a_directory_prefix_is_refused():
    with pytest.raises(NoticeError, match="both a file and a directory"):
        notices._check_destinations(["licenses/npm/x/1.0.0/LICENSE", "licenses/npm/x/1.0.0/license/extra"])
    notices._check_destinations(["licenses/npm/x/1.0.0/LICENSE", "licenses/npm/x/1.0.0/upstream/LICENSE"])


# --------------------------------------------------------- path safety


@pytest.mark.parametrize("declared", ["../outside/LICENSE", "C:/Windows/win.ini", "/etc/passwd",
                                      "\\\\server\\share\\LICENSE", "docs/../../LICENSE", "LICENSE:stream"])
def test_a_license_file_outside_the_package_is_refused(tmp_path, declared):
    directory = package_dir(tmp_path)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "LICENSE").write_text(LICENCE, encoding="utf-8")

    with pytest.raises(NoticeError):
        check_coverage([component(directory, license_file=declared)], {})


def test_a_linked_notice_file_is_refused(tmp_path):
    directory = package_dir(tmp_path, files={})
    target = tmp_path / "elsewhere.txt"
    target.write_text(LICENCE, encoding="utf-8")
    try:
        os.symlink(target, directory / "LICENSE")
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are not permitted for this user.")

    with pytest.raises(NoticeError, match="link"):
        check_coverage([component(directory)], {})


def test_a_junctioned_notice_directory_is_refused(tmp_path):
    directory = package_dir(tmp_path, files={})
    elsewhere = package_dir(tmp_path, "elsewhere", {"TERMS": LICENCE})
    junction(elsewhere, directory / "legal")

    with pytest.raises(NoticeError, match="link"):
        check_coverage([component(directory, license_file="legal/TERMS")], {})


def test_a_junctioned_npm_package_is_refused(tmp_path):
    real = tmp_path / "real-react"
    real.mkdir()
    (real / "package.json").write_text('{"name": "react", "version": "19.2.8"}', encoding="utf-8")
    (real / "LICENSE").write_text(LICENCE, encoding="utf-8")
    modules = tmp_path / "node_modules"
    modules.mkdir()
    junction(real, modules / "react")
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


@pytest.mark.parametrize("spelling", ["plain", "forward", "json-escaped"])
def test_output_carrying_a_private_path_is_refused_before_writing(tmp_path, spelling):
    private = str(tmp_path / "builder")
    shown = {"plain": private, "forward": private.replace("\\", "/"),
             "json-escaped": json.dumps(private)[1:-1]}[spelling]
    directory = package_dir(tmp_path, files={"LICENSE": LICENCE + f"built at {shown}\n"})

    with pytest.raises(NoticeError, match="private path; nothing was written") as raised:
        generate(tmp_path, [component(directory)], private=(private,))
    assert private not in str(raised.value)
    assert not (tmp_path / "out").exists()


# ------------------------------------------------- output ownership


def test_a_forged_marker_directory_is_refused_and_preserved(tmp_path):
    directory = package_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / ".qronos-desktop-notices").write_text("generated by build_support.desktop_notices\n", encoding="utf-8")
    (out / "keep.txt").write_text("user file", encoding="utf-8")
    before = tree(out)

    with pytest.raises(NoticeError, match="not empty. Nothing was deleted"):
        generate(tmp_path, [component(directory)])
    assert tree(out) == before


def test_own_previous_output_with_an_extra_file_is_not_regenerated(tmp_path):
    directory = package_dir(tmp_path)
    generate(tmp_path, [component(directory)])
    (tmp_path / "out" / "added-by-user.txt").write_text("keep me", encoding="utf-8")
    before = tree(tmp_path / "out")

    with pytest.raises(NoticeError, match="not empty"):
        generate(tmp_path, [component(directory)])
    assert tree(tmp_path / "out") == before
    with pytest.raises(NoticeError, match="differ from its manifest: added-by-user.txt"):
        notices.verify_notice_output(tmp_path / "out")


def test_a_marker_link_in_the_output_is_refused_and_preserved(tmp_path):
    directory = package_dir(tmp_path)
    precious = package_dir(tmp_path, "precious", {"data.txt": "precious"})
    out = tmp_path / "out"
    out.mkdir()
    junction(precious, out / ".qronos-desktop-notices")

    with pytest.raises(NoticeError, match="not empty"):
        generate(tmp_path, [component(directory)])
    assert (precious / "data.txt").read_text(encoding="utf-8") == "precious"
    assert (out / ".qronos-desktop-notices" / "data.txt").is_file()


@pytest.mark.parametrize("where", ["parent", "itself"])
def test_an_output_behind_a_windows_junction_is_refused(tmp_path, where):
    directory = package_dir(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (real / "keep.txt").write_text("keep", encoding="utf-8")
    junction(real, tmp_path / "linked")
    out = "linked/out" if where == "parent" else "linked"
    if where == "itself":
        (real / "keep.txt").unlink()  # an empty junction target must still be refused

    with pytest.raises(NoticeError, match="link or junction"):
        generate(tmp_path, [component(directory)], out=out)
    assert not (real / "out").exists()
    assert sorted(path.name for path in real.iterdir()) == ([] if where == "itself" else ["keep.txt"])


def test_an_existing_empty_directory_is_used_without_deleting_anything(tmp_path):
    directory = package_dir(tmp_path)
    (tmp_path / "out").mkdir()

    generate(tmp_path, [component(directory)])

    assert (tmp_path / "out/licenses/rust/demo/1.0.0/LICENSE").is_file()


# ------------------------------------------ hashes and revalidation


def test_every_copied_file_has_its_size_and_hash_and_the_manifest_verifies(tmp_path):
    directory = package_dir(tmp_path, files={"LICENSE": LICENCE, "NOTICE": "Attribution\n"})

    inventory = generate(tmp_path, [component(directory)])

    out = tmp_path / "out"
    for entry in inventory["components"][0]["notices"]:
        data = (out / entry["path"]).read_bytes()
        assert entry["bytes"] == len(data) and entry["sha256"] == sha256(data)
    manifest = notices.verify_notice_output(out)
    assert {entry["path"] for entry in manifest["files"]} == set(tree(out)) - {notices.MANIFEST}

    (out / "licenses/rust/demo/1.0.0/NOTICE").write_bytes(b"Tampered\n")
    with pytest.raises(NoticeError, match="does not match its manifest"):
        notices.verify_notice_output(out)


def test_a_source_changed_after_checking_is_refused(tmp_path):
    directory = package_dir(tmp_path)
    item = component(directory)
    files = check_coverage([item], {})
    (directory / "LICENSE").write_text(LICENCE + "edited later\n", encoding="utf-8")

    with pytest.raises(NoticeError, match="changed after it was checked"):
        write_notices([item], files, {}, tmp_path / "out", notices.WINDOWS_TARGET, ())


def test_a_vendored_text_changed_after_loading_is_refused(tmp_path):
    _, item = upstream_package(tmp_path)
    registry = upstream_registry(tmp_path)
    upstream = notices.load_upstream(registry)
    files = check_coverage([item], {}, upstream)
    (registry.parent / "texts/example__demo-0123456789ab/NOTICE").write_bytes(b"swapped\n")

    with pytest.raises(NoticeError, match="changed after it was verified"):
        write_notices([item], files, {}, tmp_path / "out", notices.WINDOWS_TARGET, (), upstream)
    assert not (tmp_path / "out").exists()


def test_published_source_downloads_are_recorded_where_established(tmp_path):
    crate = component(package_dir(tmp_path, "crate"), name="serde", version="1.0.0", provenance="ab" * 32)
    npm = component(package_dir(tmp_path, "npm"), name="react", version="19.2.8", ecosystem="npm",
                    source="https://registry.npmjs.org/react/-/react-19.2.8.tgz", provenance="sha512-x")
    git = component(package_dir(tmp_path, "git"), name="forked", source="git+https://example.com/x#abc")

    records = {record["name"]: record for record in generate(tmp_path, [crate, npm, git])["components"]}

    assert records["serde"]["sourceDownload"] == {
        "url": "https://static.crates.io/crates/serde/serde-1.0.0.crate", "sha256": "ab" * 32}
    assert records["react"]["sourceDownload"] == {
        "url": "https://registry.npmjs.org/react/-/react-19.2.8.tgz", "integrity": "sha512-x"}
    assert records["forked"]["sourceDownload"] is None


# ------------------------------------------------ verified upstream texts


def test_a_pinned_upstream_text_completes_the_exact_package(tmp_path):
    _, item = upstream_package(tmp_path)
    upstream = notices.load_upstream(upstream_registry(tmp_path))

    inventory = generate(tmp_path, [item], upstream=upstream)

    (record,) = inventory["components"]
    assert record["licenceTextPresent"] is False  # the published package itself has none
    assert record["licenceTextSource"] == "verified-upstream"
    assert record["declaredLicense"] == "MIT" and record["exception"] is None
    folder = "licenses/rust/demo/1.0.0/upstream/example__demo-0123456789ab"
    files = [entry for entry in record["notices"] if entry["origin"] == "upstream"]
    assert [entry["path"] for entry in files] == [f"{folder}/LICENSE", f"{folder}/NOTICE"]
    assert files[0]["sha256"] == sha256(UPSTREAM_LICENCE) and files[0]["bytes"] == len(UPSTREAM_LICENCE)
    assert files[0]["revision"] == UPSTREAM_COMMIT and files[0]["role"] == "licence"
    assert record["upstream"]["evidence"] and record["upstream"]["ownerReview"] == "none"
    # Bytes, line endings and the holder line are preserved exactly.
    assert (tmp_path / "out" / folder / "LICENSE").read_bytes() == UPSTREAM_LICENCE
    text = (tmp_path / "out" / notices.NOTICE_TEXT).read_text(encoding="utf-8")
    assert "Copyright (c) 2020 Example Holder" in text and UPSTREAM_COMMIT in text


def test_an_owner_review_requirement_is_surfaced(tmp_path):
    _, item = upstream_package(tmp_path)
    upstream = notices.load_upstream(upstream_registry(tmp_path, ownerReview="owner-review-required: terms"))

    inventory = generate(tmp_path, [item], upstream=upstream)

    assert inventory["ownerReviewRequired"] == ["rust/demo@1.0.0"]
    assert "Owner review: owner-review-required: terms" in \
        (tmp_path / "out" / notices.NOTICE_TEXT).read_text(encoding="utf-8")


def test_a_corrupted_vendored_text_is_refused(tmp_path):
    registry = upstream_registry(tmp_path)
    vendored = registry.parent / "texts/example__demo-0123456789ab/LICENSE"
    vendored.write_bytes(UPSTREAM_LICENCE.replace(b"\r\n", b"\n"))  # e.g. an end-of-line rewrite

    with pytest.raises(NoticeError, match="does not match its pinned SHA-256"):
        notices.load_upstream(registry)


def test_a_label_only_vendored_licence_is_refused(tmp_path):
    registry = upstream_registry(tmp_path)
    vendored = registry.parent / "texts/example__demo-0123456789ab/LICENSE"
    vendored.write_bytes(b"MIT\n")
    rewrite_entry(registry, lambda entry: entry["texts"][0].update(sha256=sha256(b"MIT\n")))

    with pytest.raises(NoticeError, match="empty or only a label"):
        notices.load_upstream(registry)


@pytest.mark.parametrize(("change", "message"), [
    (lambda entry: entry["texts"][0].update(file="../../outside/LICENSE"), "escapes"),
    (lambda entry: entry["texts"][0].update(file="C:/temp/LICENSE"), "must be relative"),
    (lambda entry: entry["texts"][0].update(file="example__demo-0123456789ab/LICENSE:x", sourcePath="LICENSE:x"),
     "portable"),
    (lambda entry: entry["texts"][0].update(sourcePath="COPYING"), "does not mirror"),
    (lambda entry: entry["texts"][0].update(file="example__demo-0123456789ab/MISSING", sourcePath="MISSING"),
     "missing"),
    (lambda entry: entry["identity"][0].update(path="../escape"), "escapes"),
    (lambda entry: entry["texts"][0].update(url="https://example.com/main/LICENSE"), "immutable"),
    (lambda entry: entry["texts"][0].update(role="waiver"), "known role"),
    (lambda entry: entry["texts"].append(dict(entry["texts"][0])), "listed twice"),
    (lambda entry: entry["texts"].pop(0), "no vendored licence text"),
    (lambda entry: entry.update(waiver=True), "expected exactly the fields"),
    (lambda entry: entry.update(identity=[]), "non-empty"),
    (lambda entry: entry.update(evidence=[""]), "evidence"),
    (lambda entry: entry.update(version="1.0/../x"), "semantic version"),
])
def test_an_unsafe_or_malformed_registry_entry_is_refused(tmp_path, change, message):
    registry = upstream_registry(tmp_path)
    rewrite_entry(registry, change)

    with pytest.raises(NoticeError, match=message):
        notices.load_upstream(registry)


def test_duplicate_and_unreferenced_upstream_files_are_refused(tmp_path):
    registry = upstream_registry(tmp_path)
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["packages"].append(data["packages"][0])
    registry.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(NoticeError, match="duplicate entry"):
        notices.load_upstream(registry)

    data["packages"].pop()
    registry.write_text(json.dumps(data), encoding="utf-8")
    (registry.parent / "texts" / "example__demo-0123456789ab" / "COPYING").write_text("x", encoding="utf-8")
    with pytest.raises(NoticeError, match="not referenced by the registry: example__demo-0123456789ab/COPYING"):
        notices.load_upstream(registry)


def test_a_junction_inside_the_vendored_tree_is_refused(tmp_path):
    registry = upstream_registry(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    junction(elsewhere, registry.parent / "texts" / "linked")

    with pytest.raises(NoticeError, match="is a link"):
        notices.load_upstream(registry)


@pytest.mark.parametrize(("change", "message"), [
    ({"provenance": "other-checksum"}, "provenance other-checksum does not match the lock sum"),
    ({"declaredLicense": "Apache-2.0"}, "declares 'Apache-2.0' but the package declares 'MIT'"),
    ({"identity": [{"path": "Cargo.toml.orig", "sha256": "0" * 64}]}, "does not match the package"),
    ({"identity": [{"path": "absent.txt", "sha256": "0" * 64}]}, "missing or unsafe"),
])
def test_an_upstream_entry_for_a_different_package_is_refused(tmp_path, change, message):
    _, item = upstream_package(tmp_path)
    upstream = notices.load_upstream(upstream_registry(tmp_path, **change))

    with pytest.raises(NoticeError, match=message) as raised:
        check_coverage([item], {}, upstream)
    assert "no licence text" in str(raised.value)  # and the gap itself is still reported


def test_a_stale_upstream_entry_is_refused(tmp_path):
    directory, item = upstream_package(tmp_path)
    upstream = notices.load_upstream(upstream_registry(tmp_path))

    with pytest.raises(NoticeError, match="matches no covered package"):
        check_coverage([component(directory, name="other")], {}, upstream)

    (directory / "LICENSE").write_text(LICENCE, encoding="utf-8")
    with pytest.raises(NoticeError, match="ships its own licence text"):
        check_coverage([item], {}, upstream)


def test_a_published_label_only_licence_does_not_make_an_upstream_entry_stale(tmp_path):
    directory, item = upstream_package(tmp_path)
    (directory / "LICENSE").write_text("MIT", encoding="utf-8")
    upstream = notices.load_upstream(upstream_registry(tmp_path))

    inventory = generate(tmp_path, [item], upstream=upstream)

    assert inventory["components"][0]["licenceTextSource"] == "verified-upstream"


def test_an_exception_is_not_needed_and_is_stale_beside_upstream_text(tmp_path):
    _, item = upstream_package(tmp_path)
    upstream = notices.load_upstream(upstream_registry(tmp_path))
    exception = {"ecosystem": "rust", "name": "demo", "version": "1.0.0", "provenance": "sum",
                 "missing": "text", "reason": "r", "reviewedBy": "Root"}

    with pytest.raises(NoticeError, match="matches no missing notice"):
        check_coverage([item], {item.key: exception}, upstream)


COMMITTED_UPSTREAM = {
    ("npm", "@tauri-apps/plugin-opener", "2.5.4"),
    ("rust", "alloc-stdlib", "0.2.4"), ("rust", "defmt-parser", "1.0.0"), ("rust", "selectors", "0.36.1"),
    ("rust", "unic-char-property", "0.9.0"), ("rust", "unic-char-range", "0.9.0"),
    ("rust", "unic-common", "0.9.0"), ("rust", "unic-ucd-ident", "0.9.0"), ("rust", "unic-ucd-version", "0.9.0"),
    ("rust", "webview2-com", "0.38.2"), ("rust", "webview2-com-macros", "0.8.1"),
    ("rust", "webview2-com-sys", "0.38.2"),
}

#: Exact pinned upstream bytes, independent of the registry that also records them.
PINNED_TEXT_SHA256 = {
    "dropbox__rust-alloc-no-stdlib-ae42d22078b9/LICENSE":
        "c0c56f26d9c051cac4d200c34c84e7ae9aaa853e01a982a1df08b09931e518ae",
    "knurling-rs__defmt-4a8cdb44891e/LICENSE-MIT": "2710a622a896bba67356913d4d0492cab5465f61b2ecce6d880aeb483834fb50",
    "knurling-rs__defmt-4a8cdb44891e/LICENSE-APACHE":
        "8173d5c29b4f956d532781d2b86e4e30f83e6b7878dce18c919451d6ba707c90",
    "servo__stylo-635e1a19d029/selectors/lib.rs": "d54c6e13e9e952dac17d209171df8657e3cae93beaddace4906150cdec8d02e9",
    "mozilla__bedrock-a15178c3c7c9/media/MPL/2.0/index.txt":
        "3f3d9e0024b1921b067d6f7f88deb4a60cbe7a78e76c64e3f1d7fc3b779b9d04",
    "open-i18n__rust-unic-5878605364af/AUTHORS": "2748c1a1fba7616917c61d455e8d715ab952671ead63f2026ad0124410596b09",
    "open-i18n__rust-unic-5878605364af/COPYRIGHT.md":
        "f5c342c49f3ac804f3e8e7bb62a8040a44c50d47bb36902b1abd13f66a1adf8b",
    "open-i18n__rust-unic-5878605364af/LICENSE-MIT": "23f18e03dc49df91622fe2a76176497404e46ced8a715d9d2b67a7446571cca3",
    "open-i18n__rust-unic-5878605364af/LICENSE-APACHE":
        "a60eea817514531668d7e00765731449fe14d059d3249e0bc93b36de45f759f2",
    "open-i18n__rust-unic-8a6ce83063d9/AUTHORS": "2748c1a1fba7616917c61d455e8d715ab952671ead63f2026ad0124410596b09",
    "open-i18n__rust-unic-8a6ce83063d9/COPYRIGHT.md":
        "f5c342c49f3ac804f3e8e7bb62a8040a44c50d47bb36902b1abd13f66a1adf8b",
    "open-i18n__rust-unic-8a6ce83063d9/LICENSE-MIT": "23f18e03dc49df91622fe2a76176497404e46ced8a715d9d2b67a7446571cca3",
    "open-i18n__rust-unic-8a6ce83063d9/LICENSE-APACHE":
        "a60eea817514531668d7e00765731449fe14d059d3249e0bc93b36de45f759f2",
    "wravery__webview2-rs-b74dc5e2b394/LICENSE": "0dcf41516e608bbcb6cdc5229feb7b86fe4a643b85e7df251133c93408fdac73",
    "wravery__webview2-rs-dffa41a8a46d/LICENSE": "0dcf41516e608bbcb6cdc5229feb7b86fe4a643b85e7df251133c93408fdac73",
    "nuget__microsoft.web.webview2-1.0.3650.58/LICENSE.txt":
        "0af8f1b807512aae39c2ac1aa4d0cae65cabecb6fd554b8439a5162a0d6eca55",
    "nuget__microsoft.web.webview2-1.0.3650.58/NOTICE.txt":
        "106423785c5b7eba0a8e61d1837f2132e9c828e20ad530f565d981c1df60dd90",
    "tauri-apps__plugins-workspace-5c7668b6bb7c/plugins/opener/LICENSE_MIT":
        "89ff9689dcf9dd53968785d05a26f7898bb169dbfcada8d032b3e68cf0d55607",
    "tauri-apps__plugins-workspace-5c7668b6bb7c/plugins/opener/LICENSE_APACHE-2.0":
        "0cec06e0e55fbc3dc5cee4fca9b607f66cb8f4e4dbcf3b3c013594dd156732e9",
}


def test_the_committed_upstream_registry_verifies_offline():
    upstream = notices.load_upstream()  # every vendored hash and path is checked here

    assert set(upstream) == COMMITTED_UPSTREAM
    vendored = {meta["file"]: (meta, path) for notice in upstream.values() for meta, path in notice.texts}
    assert {name: meta["sha256"] for name, (meta, _) in vendored.items()} == PINNED_TEXT_SHA256
    for name, (meta, path) in vendored.items():
        assert sha256(path.read_bytes()) == PINNED_TEXT_SHA256[name]
        assert meta["url"].startswith(("https://raw.githubusercontent.com/", "https://api.nuget.org/"))
        assert meta["revision"] in meta["url"] and path.stat().st_size < 64 * 1024
    for notice in upstream.values():
        assert len(notice.entry["upstream"]["revision"]) == 40  # a full commit, not a branch
    # Review decisions: UNIC authorship attribution, Microsoft terms flagged, selectors designated text.
    for key in COMMITTED_UPSTREAM:
        if key[1].startswith("unic-"):
            assert "authors" in {meta["role"] for meta, _ in upstream[key].texts}
    assert upstream[("rust", "webview2-com-sys", "0.38.2")].entry["ownerReview"].startswith("owner-review-required")
    assert {meta["role"] for meta, _ in upstream[("rust", "selectors", "0.36.1")].texts} == {
        "notice", "designated-licence"}
    # The committed lock provenance is what the entries pin.
    lock = json.loads(notices.PACKAGE_LOCK.read_text(encoding="utf-8"))
    opener = upstream[("npm", "@tauri-apps/plugin-opener", "2.5.4")]
    assert opener.entry["provenance"] == lock["packages"]["node_modules/@tauri-apps/plugin-opener"]["integrity"]
    checksums = notices.cargo_lock_checksums(notices.CARGO_MANIFEST.parent / "Cargo.lock")
    for (ecosystem, name, version), notice in upstream.items():
        if ecosystem == "rust":
            assert notice.entry["provenance"] == checksums[(name, version)]


def test_the_real_rust_closure_is_complete_with_the_committed_registry(monkeypatch):
    for name, value in TOOLCHAIN_HOMES.items():
        monkeypatch.setenv(name, value)
    try:
        graph = notices.cargo_metadata()
    except (NoticeError, OSError):
        pytest.skip("The offline locked Cargo registry is not available on this machine.")
    checksums = notices.cargo_lock_checksums(notices.CARGO_MANIFEST.parent / "Cargo.lock")
    crates = rust_components(graph, checksums)
    upstream = {key: value for key, value in notices.load_upstream().items() if key[0] == "rust"}

    files = check_coverage(crates, notices.load_exceptions(), upstream)  # identity hashes verified

    without_own_text = {key for key, found in files.items() if not notices._has_licence_text(found)}
    assert without_own_text == set(upstream)


# ------------------------------------------------------------ determinism


def test_generation_is_deterministic_and_relative(tmp_path):
    items = []
    for name in ("zeta", "alpha"):
        directory = package_dir(tmp_path, name, {"LICENSE": f"{name} licence\r\n{LICENCE}"})
        items.append(component(directory, name=name))

    first = generate(tmp_path, items, out="a")
    second = generate(tmp_path, list(reversed(items)), out="b")

    assert first == second
    assert tree_digest(tmp_path / "a") == tree_digest(tmp_path / "b")
    assert [record["name"] for record in first["components"]] == ["alpha", "zeta"]
    inventory_text = (tmp_path / "a" / notices.INVENTORY).read_text(encoding="utf-8")
    assert str(tmp_path) not in inventory_text and str(tmp_path).replace("\\", "/") not in inventory_text
    # Licence bytes are copied exactly.
    assert (tmp_path / "a/licenses/rust/alpha/1.0.0/LICENSE").read_bytes() == \
        (tmp_path / "alpha" / "LICENSE").read_bytes()


# ------------------------------------------------- target binding

# Installer wiring and Windows x64 bundle refusal are covered in test_desktop_installer.py.


def test_the_collector_refuses_another_target(tmp_path):
    with pytest.raises(NoticeError, match="x86_64-pc-windows-msvc only"):
        notices.collect_desktop_notices(tmp_path / "out", target="x86_64-unknown-linux-gnu")
    assert not (tmp_path / "out").exists()
