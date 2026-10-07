"""Collect third-party notices for the Qronos desktop application (NSIS installer).

Covers what the desktop executable and its web frontend redistribute:

* Rust: the locked dependency graph from ``cargo metadata --offline --locked``
  for the Windows target, following NORMAL dependency edges from the desktop
  crate. Build-only and dev-only edges are excluded (their code is not linked
  into the executable). Proc-macro crates reached through normal edges stay
  in, so the set is deliberately conservative (over-inclusive).
* npm: the production closure of ``desktop/package-lock.json`` - packages
  reachable from the root ``dependencies`` through dependency/optional/peer
  edges, never ``dev``. These are the packages the frontend bundle can contain
  (React, react-dom, scheduler and the Tauri JS packages).

For every covered package the package-local LICENSE*/LICENCE*/COPYING*/
NOTICE*/UNLICENSE*/COPYRIGHT* files and any declared Cargo ``license-file``
are copied from the exact local source (the Cargo registry source of the
locked version; the installed node_modules package whose version matches the
lock). Files ending in ``.spdx`` are copied but are SPDX metadata, not licence
text.

A covered package without a declared licence expression or without any
licence text fails the collection, unless an exception for that exact
ecosystem/name/version/provenance is listed in
``build_support/desktop_notice_exceptions.json`` for review. An exception
records why; it never supplies or invents a licence grant.

Output (deterministic; no timestamps, no absolute paths):
    THIRD_PARTY_NOTICES-desktop.txt
    third-party-inventory-desktop.json
    licenses/rust/<crate>-<version>/<files>
    licenses/npm/<package>-<version>/<files>

This is technical notice packaging, not legal clearance for redistribution.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
CARGO_MANIFEST = DESKTOP / "src-tauri" / "Cargo.toml"
PACKAGE_LOCK = DESKTOP / "package-lock.json"
EXCEPTIONS_PATH = ROOT / "build_support" / "desktop_notice_exceptions.json"

#: The installer is built for this target; cargo metadata filters to it.
WINDOWS_TARGET = "x86_64-pc-windows-msvc"

#: Where the notices land inside the installed application.
NOTICE_RESOURCE_TARGET = "third-party-notices/"

NOTICE_TEXT = "THIRD_PARTY_NOTICES-desktop.txt"
INVENTORY = "third-party-inventory-desktop.json"
OWNER_MARKER = ".qronos-desktop-notices"

_NOTICE_NAME = re.compile(r"^(licen[cs]e|copying|notice|unlicense|copyright)", re.IGNORECASE)
_METADATA_ONLY_SUFFIXES = (".spdx",)
_MAX_NOTICE_BYTES = 2 * 1024 * 1024
_CRATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_VERSION = re.compile(r"^[0-9A-Za-z.+-]+$")
_NPM_NAME = re.compile(r"^(@[a-z0-9~-][a-z0-9._~-]*/)?[a-z0-9~-][a-z0-9._~-]*$")


class NoticeError(RuntimeError):
    """The notice set cannot be produced honestly."""


@dataclass(frozen=True)
class Component:
    ecosystem: str
    name: str
    version: str
    declared_license: str | None
    license_file: str | None
    source: str
    provenance: str | None
    directory: Path = field(compare=False)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.ecosystem, self.name, self.version)

    @property
    def folder(self) -> str:
        safe = self.name.replace("@", "").replace("/", "__")
        return f"licenses/{self.ecosystem}/{safe}-{self.version}"


# ----------------------------------------------------------------- safety


def _is_link(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _relative_parts(value: str) -> tuple[str, ...]:
    """A declared relative path, refused if absolute or escaping."""
    if not value or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive \
            or PurePosixPath(value).is_absolute() or value.startswith(("\\", "/")):
        raise NoticeError(f"Notice path {value!r} must be relative to its package.")
    parts = tuple(part for part in re.split(r"[\\/]", value) if part not in ("", "."))
    if not parts or any(part == ".." or ":" in part for part in parts):
        raise NoticeError(f"Notice path {value!r} escapes its package.")
    return parts


def _contained_file(directory: Path, parts: tuple[str, ...]) -> Path:
    candidate = directory
    for part in parts:
        candidate = candidate / part
        if _is_link(candidate):
            raise NoticeError(f"Notice path {'/'.join(parts)!r} is or passes through a link.")
    resolved = candidate.resolve(strict=True)
    if directory not in resolved.parents or not resolved.is_file():
        raise NoticeError(f"Notice path {'/'.join(parts)!r} is not a file inside its package.")
    if resolved.stat().st_size > _MAX_NOTICE_BYTES:
        raise NoticeError(f"Notice file {'/'.join(parts)!r} is larger than {_MAX_NOTICE_BYTES} bytes.")
    return resolved


def _package_directory(path: Path) -> Path:
    if _is_link(path) or not path.is_dir():
        raise NoticeError("A covered package directory is missing or is a link.")
    return path.resolve(strict=True)


def notice_files(component: Component) -> list[tuple[str, Path]]:
    """(relative name, file) pairs for one package, sorted, without duplicates."""
    directory = component.directory
    found: dict[Path, str] = {}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if _NOTICE_NAME.match(entry.name):
            if _is_link(entry):
                raise NoticeError(f"{component.name}@{component.version}: notice {entry.name!r} is a link.")
            if entry.is_file():
                found.setdefault(_contained_file(directory, (entry.name,)), entry.name)
    if component.license_file:
        parts = _relative_parts(component.license_file)
        found.setdefault(_contained_file(directory, parts), "/".join(parts))
    return sorted(((name, path) for path, name in found.items()), key=lambda item: item[0])


def _has_licence_text(files: list[tuple[str, Path]]) -> bool:
    return any(not name.lower().endswith(_METADATA_ONLY_SUFFIXES) for name, _ in files)


# ---------------------------------------------------------------- graphs


def cargo_metadata(
    manifest: Path = CARGO_MANIFEST,
    target: str = WINDOWS_TARGET,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Locked, offline metadata for one target. Never updates or downloads."""
    result = runner(
        ["cargo", "metadata", "--offline", "--locked", "--format-version", "1",
         "--filter-platform", target, "--manifest-path", str(manifest)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode != 0:
        raise NoticeError("cargo metadata --offline --locked failed; the locked registry sources are required.")
    return json.loads(result.stdout)


def cargo_lock_checksums(lock_path: Path) -> dict[tuple[str, str], str]:
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    return {
        (package["name"], package["version"]): package["checksum"]
        for package in data.get("package", [])
        if package.get("checksum")
    }


def rust_components(metadata: dict[str, Any], checksums: dict[tuple[str, str], str]) -> list[Component]:
    """The normal-dependency closure of the desktop crate, excluding itself."""
    packages = {package["id"]: package for package in metadata["packages"]}
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    root = metadata["resolve"]["root"]
    members = set(metadata.get("workspace_members", ())) | {root}
    seen: set[str] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        for dependency in nodes[current]["deps"]:
            normal = any(kind.get("kind") is None for kind in dependency.get("dep_kinds", ()))
            if normal and dependency["pkg"] not in seen:
                seen.add(dependency["pkg"])
                stack.append(dependency["pkg"])
    components = []
    for package_id in seen - members:
        package = packages[package_id]
        name, version = package["name"], package["version"]
        if not _CRATE_NAME.match(name) or not _VERSION.match(version):
            raise NoticeError(f"Unexpected crate identity {name!r} {version!r}.")
        source = package.get("source")
        if not source:
            raise NoticeError(f"{name}@{version} is a local path crate; first-party code is not a third-party notice.")
        components.append(Component(
            ecosystem="rust", name=name, version=version,
            declared_license=package.get("license") or None,
            license_file=package.get("license_file") or None,
            source=source, provenance=checksums.get((name, version)),
            directory=_package_directory(Path(package["manifest_path"]).parent),
        ))
    return components


def npm_production_keys(lock: dict[str, Any]) -> list[str]:
    """Lock entries reachable from the root dependencies without dev edges."""
    packages = lock.get("packages")
    if lock.get("lockfileVersion") not in (2, 3) or not isinstance(packages, dict):
        raise NoticeError("package-lock.json must be lockfileVersion 2 or 3 with a packages map.")

    def resolve(owner: str, name: str) -> str | None:
        base = owner
        while True:
            candidate = f"{base}/node_modules/{name}" if base else f"node_modules/{name}"
            if candidate in packages:
                return candidate
            if not base:
                return None
            base = base.rsplit("/node_modules/", 1)[0] if "/node_modules/" in base else ""

    root = packages.get("", {})
    queue = [("", name, False) for name in sorted(root.get("dependencies", {}))]
    queue += [("", name, True) for name in sorted(root.get("optionalDependencies", {}))]
    seen: set[str] = set()
    while queue:
        owner, name, optional = queue.pop()
        key = resolve(owner, name)
        if key is None:
            if optional:
                continue
            raise NoticeError(f"Production dependency {name!r} is not in the lock.")
        if key in seen:
            continue
        entry = packages[key]
        if entry.get("dev"):
            raise NoticeError(f"{key} is reachable from production dependencies but locked as dev.")
        seen.add(key)
        for edge, is_optional in (("dependencies", False), ("optionalDependencies", True),
                                  ("peerDependencies", False)):
            for child in sorted(entry.get(edge, {})):
                peer_optional = (entry.get("peerDependenciesMeta", {}).get(child, {}).get("optional")
                                 if edge == "peerDependencies" else False)
                queue.append((key, child, is_optional or bool(peer_optional)))
    return sorted(seen)


def npm_components(lock: dict[str, Any], node_modules: Path) -> list[Component]:
    root = _package_directory(node_modules)
    components = []
    for key in npm_production_keys(lock):
        entry = lock["packages"][key]
        name = key.rsplit("node_modules/", 1)[1]
        if not _NPM_NAME.match(name):
            raise NoticeError(f"Unexpected npm package name {name!r}.")
        relative = key[len("node_modules/"):]
        parts = _relative_parts(relative)
        directory = root
        for part in parts:
            directory = directory / part
            if _is_link(directory):
                raise NoticeError(f"Installed package {name!r} is or passes through a link.")
        directory = _package_directory(directory)
        if root not in directory.parents:
            raise NoticeError(f"Installed package {name!r} is outside node_modules.")
        installed = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        if installed.get("version") != entry.get("version") or installed.get("name") != name:
            raise NoticeError(f"Installed {name} does not match the locked version {entry.get('version')}.")
        components.append(Component(
            ecosystem="npm", name=name, version=entry["version"],
            declared_license=entry.get("license") or installed.get("license") or None,
            license_file=None,
            source=entry.get("resolved") or "npm-lock-entry",
            provenance=entry.get("integrity"),
            directory=directory,
        ))
    return components


# ------------------------------------------------------------ exceptions


def load_exceptions(path: Path = EXCEPTIONS_PATH) -> dict[tuple[str, str, str], dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    exceptions = {}
    for entry in data.get("exceptions", []):
        required = ("ecosystem", "name", "version", "provenance", "missing", "reason", "reviewedBy")
        if any(not entry.get(key) for key in required) or entry["missing"] not in ("text", "expression"):
            raise NoticeError("Every notice exception needs ecosystem, name, version, provenance, "
                              "missing (text|expression), reason and reviewedBy.")
        exceptions[(entry["ecosystem"], entry["name"], entry["version"])] = entry
    return exceptions


def check_coverage(
    components: list[Component],
    exceptions: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[tuple[str, str, str], list[tuple[str, Path]]]:
    """Every covered package has an expression and text, or an exact exception."""
    files: dict[tuple[str, str, str], list[tuple[str, Path]]] = {}
    problems: list[str] = []
    used: set[tuple[str, str, str]] = set()
    for component in sorted(components, key=lambda item: item.key):
        found = notice_files(component)
        files[component.key] = found
        missing = []
        if not component.declared_license:
            missing.append("expression")
        if not _has_licence_text(found):
            missing.append("text")
        for kind in missing:
            exception = exceptions.get(component.key)
            if (exception and exception["missing"] == kind
                    and exception["provenance"] == component.provenance):
                used.add(component.key)
                continue
            problems.append(f"{component.ecosystem} {component.name}@{component.version}: "
                            f"no licence {kind} (provenance {component.provenance})")
    stale = sorted(set(exceptions) - used)
    problems += [f"exception {e}/{n}@{v} matches no missing notice" for e, n, v in stale]
    if problems:
        raise NoticeError("Desktop third-party notices are incomplete:\n  " + "\n  ".join(problems))
    return files


# ---------------------------------------------------------------- output


def _prepare_output(out_dir: Path) -> None:
    if out_dir.exists():
        if _is_link(out_dir) or not out_dir.is_dir():
            raise NoticeError("The notice output must be a plain directory.")
        if any(out_dir.iterdir()) and not (out_dir / OWNER_MARKER).is_file():
            raise NoticeError("The notice output directory is not empty and was not created by this tool.")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    (out_dir / OWNER_MARKER).write_text("generated by build_support.desktop_notices\n", encoding="utf-8")


def write_notices(
    components: list[Component],
    files: dict[tuple[str, str, str], list[tuple[str, Path]]],
    exceptions: dict[tuple[str, str, str], dict[str, Any]],
    out_dir: Path,
    target: str,
    private_prefixes: tuple[str, ...],
) -> dict[str, Any]:
    _prepare_output(out_dir)
    records = []
    text = [
        "Qronos desktop application - third-party notices",
        "=" * 49,
        f"Rust: normal-dependency closure of the desktop crate for {target}, from Cargo.lock "
        "(offline, locked). Build-only and dev-only crates excluded; proc-macro crates included.",
        "npm: production closure of desktop/package-lock.json (no dev dependencies).",
        "Each section names the package, its declared licence expression as published, its source "
        "and its locked checksum/integrity, followed by the package's own notice files.",
        "This file is a technical notice collection, not legal advice or redistribution clearance.",
        "",
    ]
    for component in sorted(components, key=lambda item: item.key):
        found = files[component.key]
        exception = exceptions.get(component.key)
        relative = []
        for name, path in found:
            destination = out_dir / component.folder / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())
            relative.append(f"{component.folder}/{name}")
        record = {
            "ecosystem": component.ecosystem,
            "name": component.name,
            "version": component.version,
            "declaredLicense": component.declared_license,
            "licenseFileField": component.license_file,
            "source": component.source,
            "provenance": component.provenance,
            "noticeFiles": relative,
            "licenceTextPresent": _has_licence_text(found),
            "exception": None if exception is None else {
                "missing": exception["missing"], "reason": exception["reason"],
                "reviewedBy": exception["reviewedBy"]},
        }
        records.append(record)
        text += ["-" * 72, f"{component.name} {component.version} ({component.ecosystem})",
                 f"Declared licence: {component.declared_license or 'NOT DECLARED'}",
                 f"Source: {component.source}", f"Provenance: {component.provenance}"]
        if component.license_file:
            text.append(f"license-file field: {component.license_file}")
        if exception:
            text.append(f"Reviewed exception (missing {exception['missing']}): {exception['reason']}")
        for name, path in found:
            text += ["", f"--- {name} ---", path.read_bytes().decode("utf-8", errors="replace").rstrip("\n")]
        text.append("")
    inventory = {
        "schema": 1,
        "target": target,
        "scope": {
            "rust": "normal-dependency closure (proc-macros included; build/dev-only excluded)",
            "npm": "package-lock production closure (no dev)",
        },
        "counts": {
            "rust": sum(1 for record in records if record["ecosystem"] == "rust"),
            "npm": sum(1 for record in records if record["ecosystem"] == "npm"),
        },
        "components": records,
    }
    (out_dir / INVENTORY).write_text(
        json.dumps(inventory, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    (out_dir / NOTICE_TEXT).write_text("\n".join(text) + "\n", encoding="utf-8", newline="\n")
    _refuse_private_paths(out_dir, private_prefixes)
    return inventory


def _refuse_private_paths(out_dir: Path, private_prefixes: tuple[str, ...]) -> None:
    needles = set()
    for prefix in private_prefixes:
        if prefix:
            for variant in (prefix, prefix.replace("\\", "/"), prefix.replace("/", "\\")):
                needles.add(variant.lower())
    for path in sorted(out_dir.rglob("*")):
        if path.is_file():
            content = path.read_bytes().decode("utf-8", errors="ignore").lower()
            for needle in needles:
                if needle and needle in content:
                    raise NoticeError(f"Generated notice output contains a private path ({path.name}).")


def collect_desktop_notices(
    out_dir: Path,
    *,
    node_modules: Path | None = None,
    target: str = WINDOWS_TARGET,
    exceptions_path: Path = EXCEPTIONS_PATH,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    metadata = cargo_metadata(CARGO_MANIFEST, target, runner)
    components = rust_components(metadata, cargo_lock_checksums(CARGO_MANIFEST.parent / "Cargo.lock"))
    lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    node_root = node_modules if node_modules is not None else DESKTOP / "node_modules"
    components += npm_components(lock, node_root)
    exceptions = load_exceptions(exceptions_path)
    files = check_coverage(components, exceptions)
    private = (str(ROOT), str(Path.home()), str(node_root.resolve()),
               *{str(component.directory.parent) for component in components if component.ecosystem == "rust"})
    return write_notices(components, files, exceptions, out_dir, target, private)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="Directory to (re)generate.")
    parser.add_argument("--node-modules", type=Path, default=None,
                        help="Installed node_modules to read (default: desktop/node_modules).")
    parser.add_argument("--target", default=WINDOWS_TARGET)
    parser.add_argument("--exceptions", type=Path, default=EXCEPTIONS_PATH)
    arguments = parser.parse_args(argv)
    try:
        inventory = collect_desktop_notices(arguments.out, node_modules=arguments.node_modules,
                                            target=arguments.target, exceptions_path=arguments.exceptions)
    except NoticeError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(inventory["counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
