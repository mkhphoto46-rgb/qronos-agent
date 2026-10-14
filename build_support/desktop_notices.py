"""Collect third-party notices for the Qronos desktop application (NSIS installer).

The inventory is a conservative technical scope, not proof of what the linker
put into the final executable:

* Rust: the locked Windows normal-dependency closure from
  ``cargo metadata --offline --locked`` for the installer target, following
  NORMAL dependency edges from the desktop crate. Proc-macro crates reached
  through normal edges are included; build-only and dev-only dependencies are
  excluded. Build scripts can still generate or copy code and native libraries
  into the build; such outputs may carry notice duties that package metadata
  does not show.
* npm: the production closure of ``desktop/package-lock.json`` - packages
  reachable from the root ``dependencies`` through dependency/optional/peer
  edges, never ``dev`` (React, react-dom, scheduler and the Tauri JS packages).

For every covered package the package-local LICENSE*/LICENCE*/COPYING*/
UNLICENSE*/NOTICE*/COPYRIGHT* files and any declared Cargo ``license-file``
are copied byte-for-byte from the exact local source. Only a substantive
licence file counts as licence text: NOTICE/COPYRIGHT files, ``.spdx``
metadata and empty, whitespace-only or label-only (e.g. just ``MIT``) licence
files are copied as auxiliary files but never count.

A published package that ships no licence text can be completed only from
``build_support/desktop_notice_upstream``: small upstream files vendored
byte-for-byte, pinned by ``registry.json`` to the exact package (ecosystem,
name, version, locked checksum/integrity, declared licence and hashes of local
identity files) and, per file, to an immutable source URL, revision, source
path and SHA-256. Every pin is verified offline here; a missing, mismatched,
duplicate, stale or unsafe entry fails.

A covered package without a declared licence expression or without licence
text (package-local or verified upstream) fails the collection, unless an
exception for that exact ecosystem/name/version/provenance is listed in
``build_support/desktop_notice_exceptions.json`` for review. Neither file
supplies or invents a licence grant.

Every identity and every destination is validated before the output exists.
The output must be a new or empty directory with no link or junction on its
path; files are created exclusively, so nothing is ever deleted or overwritten.

Output (deterministic; no timestamps, no absolute paths):
    THIRD_PARTY_NOTICES-desktop.txt
    third-party-inventory-desktop.json   per-file role, size and SHA-256
    third-party-manifest-desktop.json    size and SHA-256 of every other file
    licenses/rust/<crate>/<version>/<files>
    licenses/npm/[@scope/]<package>/<version>/<files>
    licenses/<...>/<version>/upstream/<vendored files>
    qronos/LICENSE, qronos/NOTICE        Qronos's own licence (Apache-2.0, the pinned official
                                         text) and NOTICE, from ``project_licence``

This is technical notice packaging, not legal clearance for redistribution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

from build_support import project_licence

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
CARGO_MANIFEST = DESKTOP / "src-tauri" / "Cargo.toml"
PACKAGE_LOCK = DESKTOP / "package-lock.json"
EXCEPTIONS_PATH = ROOT / "build_support" / "desktop_notice_exceptions.json"
UPSTREAM_REGISTRY = ROOT / "build_support" / "desktop_notice_upstream" / "registry.json"

#: The installer is built for this target; cargo metadata filters to it.
WINDOWS_TARGET = "x86_64-pc-windows-msvc"

#: Where the notices land inside the installed application.
NOTICE_RESOURCE_TARGET = "third-party-notices/"

NOTICE_TEXT = "THIRD_PARTY_NOTICES-desktop.txt"
INVENTORY = "third-party-inventory-desktop.json"
MANIFEST = "third-party-manifest-desktop.json"

CRATES_IO_SOURCES = frozenset({"registry+https://github.com/rust-lang/crates.io-index",
                               "sparse+https://index.crates.io/"})
NPM_REGISTRY = "https://registry.npmjs.org/"
#: A registry ownerReview value starting with this marks terms the product owner must still review.
OWNER_REVIEW_REQUIRED = "owner-review-required"

SCOPE_LIMITS = (
    "Rust coverage is the locked normal-dependency closure for the target, including proc-macro crates "
    "reached through normal edges and excluding build-only and dev-only dependencies. It is conservative "
    "and is not proof of what the linker included.",
    "Build scripts can generate or copy code and native libraries into the build; such outputs may carry "
    "notice duties that package metadata does not show.",
    "Source download URLs and checksums identify the exact published sources. They are provenance, not a "
    "source-availability offer or legal approval.",
    "This is a technical notice collection, not legal advice or redistribution clearance.",
)

_COLLECTED_NAME = re.compile(r"(licen[cs]e|copying|notice|unlicense|copyright)", re.IGNORECASE)
_LICENCE_NAME = re.compile(r"(licen[cs]e|copying|unlicense)", re.IGNORECASE)
_AUXILIARY_NAME = re.compile(r"(authors|notice|copyright|readme|contributors|credits)", re.IGNORECASE)
_LABEL_ONLY = re.compile(r"(spdx-license-identifier:\s*)?[a-z0-9.+\-()/ ]+", re.IGNORECASE | re.ASCII)
_MAX_LABEL_CHARS = 80
_MAX_NOTICE_BYTES = 2 * 1024 * 1024
_CRATE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", re.ASCII)
_NPM_NAME = re.compile(r"(@[a-z0-9~-][a-z0-9._~-]*/)?[a-z0-9~-][a-z0-9._~-]*", re.ASCII)
_NUMERIC = r"(?:0|[1-9][0-9]*)"
_PRERELEASE_ID = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
#: Semantic Versioning 2.0 (Cargo and npm): prerelease and build metadata are kept.
_SEMVER = re.compile(
    rf"{_NUMERIC}\.{_NUMERIC}\.{_NUMERIC}"
    rf"(?:-{_PRERELEASE_ID}(?:\.{_PRERELEASE_ID})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", re.ASCII)
_MAX_VERSION_CHARS = 128
_SEGMENT_FORBIDDEN = re.compile(r'[<>:"|?*\\/\x00-\x1f\x7f]')
_RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)),
                             *(f"lpt{n}" for n in range(1, 10))})
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: Identity files may be large binaries (e.g. a statically linked library).
_MAX_IDENTITY_BYTES = 64 * 1024 * 1024
_UPSTREAM_ROLES = frozenset({"licence", "designated-licence", "copyright", "notice", "authors",
                             "embedded-licence", "embedded-notice"})
#: Upstream roles that count as licence text for the package itself.
_LICENCE_ROLES = frozenset({"licence", "designated-licence"})
_ENTRY_FIELDS = frozenset({"ecosystem", "name", "version", "provenance", "declaredLicense",
                           "upstream", "identity", "texts", "note", "ownerReview", "evidence"})
_SOURCE_FIELDS = frozenset({"repository", "revision", "packagePath", "pinnedBy"})
_IDENTITY_FIELDS = frozenset({"path", "sha256"})
_TEXT_FIELDS = frozenset({"file", "sha256", "role", "repository", "revision", "sourcePath", "url"})


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
        """One directory level per name segment and one for the version: no two identities share it."""
        return "/".join(("licenses", self.ecosystem, *self.name.split("/"), self.version))


@dataclass(frozen=True)
class NoticeFile:
    """One file to copy: where it goes, its exact bytes and what it is."""

    path: str
    source: Path
    data: bytes = field(repr=False)
    role: str
    origin: str = "package"
    upstream: dict[str, Any] | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


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


def _portable_segment(segment: str) -> bool:
    """A single file or directory name that means the same thing on Windows and POSIX."""
    return (segment not in ("", ".", "..") and not _SEGMENT_FORBIDDEN.search(segment)
            and not segment.endswith((".", " ")) and len(segment.encode("utf-8")) <= 255
            and segment.split(".")[0].rstrip().lower() not in _RESERVED_NAMES)


def _relative_parts(value: str) -> tuple[str, ...]:
    """A declared relative path, refused if absolute, escaping or not portable."""
    if not value or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive \
            or PurePosixPath(value).is_absolute() or value.startswith(("\\", "/")):
        raise NoticeError(f"Notice path {value!r} must be relative to its package.")
    parts = tuple(part for part in re.split(r"[\\/]", value) if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise NoticeError(f"Notice path {value!r} escapes its package.")
    if not all(_portable_segment(part) for part in parts):
        raise NoticeError(f"Notice path {value!r} is not a portable relative path.")
    return parts


def _contained_file(directory: Path, parts: tuple[str, ...], limit: int = _MAX_NOTICE_BYTES) -> Path:
    candidate = directory
    for part in parts:
        candidate = candidate / part
        if _is_link(candidate):
            raise NoticeError(f"Notice path {'/'.join(parts)!r} is missing, is a link or passes through one.")
    resolved = candidate.resolve(strict=True)
    if directory not in resolved.parents or not resolved.is_file():
        raise NoticeError(f"Notice path {'/'.join(parts)!r} is not a file inside its package.")
    if resolved.stat().st_size > limit:
        raise NoticeError(f"Notice file {'/'.join(parts)!r} is larger than {limit} bytes.")
    return resolved


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package_directory(path: Path) -> Path:
    if _is_link(path) or not path.is_dir():
        raise NoticeError("A covered package directory is missing or is a link.")
    return path.resolve(strict=True)


def identity_problem(component: Component) -> str | None:
    """Why this identity cannot name a notice folder, if it cannot."""
    if component.ecosystem == "rust":
        valid_name = bool(_CRATE_NAME.fullmatch(component.name))
    elif component.ecosystem == "npm":
        valid_name = len(component.name) <= 214 and bool(_NPM_NAME.fullmatch(component.name))
    else:
        return f"unknown ecosystem {component.ecosystem!r}"
    if not valid_name:
        return f"unexpected {component.ecosystem} package name {component.name!r}"
    if len(component.version) > _MAX_VERSION_CHARS or not _SEMVER.fullmatch(component.version):
        return f"{component.name}: version {component.version!r} is not a plain semantic version"
    if not all(_portable_segment(part) for part in component.folder.split("/")):
        return f"{component.name}@{component.version} does not map to a portable notice folder"
    return None


# ------------------------------------------------------------ notice files


def _is_substantive(data: bytes) -> bool:
    """Real licence wording, not an empty file or a bare licence label."""
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return False
    if "\n" not in text and len(text) <= _MAX_LABEL_CHARS and _LABEL_ONLY.fullmatch(text):
        return False
    return True


def _classify(name: str, data: bytes, declared: bool) -> str:
    base = PurePosixPath(name).name
    if base.lower().endswith(".spdx"):
        return "spdx-metadata"
    if _AUXILIARY_NAME.match(base):
        return "auxiliary"  # authorship/notice metadata never counts, even as a declared license-file
    if declared or _LICENCE_NAME.match(base):
        return "licence" if _is_substantive(data) else "licence-label"
    return "auxiliary"


def notice_files(component: Component) -> list[NoticeFile]:
    """The package's own notice files with their bytes and roles, sorted, without duplicates."""
    directory = component.directory
    found: dict[Path, tuple[str, bool]] = {}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if _COLLECTED_NAME.match(entry.name):
            if _is_link(entry):
                raise NoticeError(f"{component.name}@{component.version}: notice {entry.name!r} is a link.")
            if entry.is_file():
                found[_contained_file(directory, _relative_parts(entry.name))] = (entry.name, False)
    if component.license_file:
        parts = _relative_parts(component.license_file)
        path = _contained_file(directory, parts)
        name = found.get(path, ("/".join(parts), True))[0]
        found[path] = (name, True)
    files = []
    for path, (name, declared) in found.items():
        data = path.read_bytes()
        files.append(NoticeFile(name, path, data, _classify(name, data, declared)))
    return sorted(files, key=lambda item: item.path)


def _has_licence_text(files: list[NoticeFile]) -> bool:
    return any(item.role == "licence" for item in files)


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
    for package_id in sorted(seen - members):
        package = packages[package_id]
        name, version = package["name"], package["version"]
        source = package.get("source")
        identity = Component("rust", name, version, None, None, source or "", None, Path())
        problem = identity_problem(identity)
        if problem:
            raise NoticeError(f"Unexpected crate identity: {problem}.")
        if not source:
            raise NoticeError(f"{name}@{version} is a local path crate; first-party code is not a third-party notice.")
        components.append(replace(
            identity,
            declared_license=package.get("license") or None,
            license_file=package.get("license_file") or None,
            provenance=checksums.get((name, version)),
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
        identity = Component("npm", name, str(entry.get("version", "")), None, None, "", None, Path())
        problem = identity_problem(identity)
        if problem:
            raise NoticeError(f"Unexpected npm identity in the lock: {problem}.")
        parts = _relative_parts(key[len("node_modules/"):])
        directory = root
        for part in parts:
            directory = directory / part
            if _is_link(directory):
                raise NoticeError(f"Installed package {name!r} is or passes through a link.")
        directory = _package_directory(directory)
        if root not in directory.parents:
            raise NoticeError(f"Installed package {name!r} is outside node_modules.")
        installed = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        if installed.get("version") != identity.version or installed.get("name") != name:
            raise NoticeError(f"Installed {name} does not match the locked version {identity.version}.")
        components.append(replace(
            identity,
            declared_license=entry.get("license") or installed.get("license") or None,
            source=entry.get("resolved") or "npm-lock-entry",
            provenance=entry.get("integrity"),
            directory=directory,
        ))
    return components


def source_download(component: Component) -> dict[str, str] | None:
    """The immutable published-source URL and its pinned checksum, where established."""
    if component.ecosystem == "rust" and component.source in CRATES_IO_SOURCES and component.provenance:
        return {"url": f"https://static.crates.io/crates/{component.name}/"
                       f"{component.name}-{component.version}.crate",
                "sha256": component.provenance}
    if component.ecosystem == "npm" and component.source.startswith(NPM_REGISTRY) and component.provenance:
        return {"url": component.source, "integrity": component.provenance}
    return None


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


# ------------------------------------------------- verified upstream texts


@dataclass(frozen=True)
class UpstreamNotice:
    """Vendored upstream texts pinned to one exact package; hashes already verified."""

    entry: dict[str, Any]
    texts: tuple[tuple[dict[str, Any], Path], ...]


def _fields(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise NoticeError(f"{label}: expected exactly the fields {sorted(expected)}.")
    if any(not isinstance(item, (str, list, dict)) or not item for item in value.values()):
        raise NoticeError(f"{label}: every field must be a non-empty value.")
    return value


def _vendored_files(texts_root: Path) -> set[Path]:
    """Every file under the vendored tree; links anywhere in it are refused."""
    found: set[Path] = set()
    for current, directories, names in os.walk(texts_root, followlinks=False):
        for name in sorted(directories) + sorted(names):
            path = Path(current) / name
            if _is_link(path):
                raise NoticeError(f"Vendored upstream path {path.relative_to(texts_root).as_posix()!r} is a link.")
            if name in names:
                found.add(path.resolve(strict=True))
    return found


def load_upstream(path: Path = UPSTREAM_REGISTRY) -> dict[tuple[str, str, str], UpstreamNotice]:
    """Verified upstream notice entries keyed by exact package identity."""
    if not path.exists():
        return {}
    if _is_link(path):
        raise NoticeError("The upstream notice registry must not be a link.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("packages"), list):
        raise NoticeError("The upstream notice registry needs schema 1 and a packages list.")
    texts_root = _package_directory(path.parent / "texts") if data["packages"] else None
    registry: dict[tuple[str, str, str], UpstreamNotice] = {}
    referenced: set[Path] = set()
    for entry in data["packages"]:
        label = "upstream entry"
        entry = _fields(entry, _ENTRY_FIELDS, label)
        label = f"upstream entry {entry['ecosystem']}/{entry['name']}@{entry['version']}"
        if any(not isinstance(entry[name], str) for name in
               ("ecosystem", "name", "version", "provenance", "declaredLicense", "note", "ownerReview")):
            raise NoticeError(f"{label}: identity fields must be text.")
        if not isinstance(entry["evidence"], list) or \
                not all(isinstance(item, str) and item for item in entry["evidence"]):
            raise NoticeError(f"{label}: evidence must be a list of non-empty statements.")
        key = (entry["ecosystem"], entry["name"], entry["version"])
        problem = identity_problem(Component(*key, None, None, "", None, Path()))
        if problem:
            raise NoticeError(f"{label}: {problem}.")
        if key in registry:
            raise NoticeError(f"{label}: duplicate entry.")
        source = _fields(entry["upstream"], _SOURCE_FIELDS, f"{label} upstream")
        if not source["repository"].startswith("https://"):
            raise NoticeError(f"{label}: the upstream repository must be an https URL.")
        identity_paths: set[tuple[str, ...]] = set()
        for item in entry["identity"] if isinstance(entry["identity"], list) else [None]:
            item = _fields(item, _IDENTITY_FIELDS, f"{label} identity")
            parts = _relative_parts(item["path"])
            if not _SHA256.fullmatch(item["sha256"]) or parts in identity_paths:
                raise NoticeError(f"{label}: identity {item['path']!r} needs one lower-case SHA-256.")
            identity_paths.add(parts)
        texts: list[tuple[dict[str, Any], Path]] = []
        for text in entry["texts"] if isinstance(entry["texts"], list) else [None]:
            text = _fields(text, _TEXT_FIELDS, f"{label} text")
            if text["role"] not in _UPSTREAM_ROLES or not _SHA256.fullmatch(text["sha256"]):
                raise NoticeError(f"{label}: text {text['file']!r} needs a known role and a SHA-256.")
            if not text["url"].startswith("https://") or text["revision"] not in text["url"] \
                    or not text["repository"].startswith("https://"):
                raise NoticeError(f"{label}: text {text['file']!r} needs an immutable https URL "
                                  "that names its revision.")
            file_parts, source_parts = _relative_parts(text["file"]), _relative_parts(text["sourcePath"])
            if len(file_parts) <= len(source_parts) or file_parts[-len(source_parts):] != source_parts:
                raise NoticeError(f"{label}: vendored file {text['file']!r} does not mirror its sourcePath.")
            vendored = _contained_file(texts_root, file_parts)
            if any(vendored == other for _, other in texts):
                raise NoticeError(f"{label}: vendored file {text['file']!r} is listed twice.")
            content = vendored.read_bytes()
            if hashlib.sha256(content).hexdigest() != text["sha256"]:
                raise NoticeError(f"{label}: vendored file {text['file']!r} does not match its pinned SHA-256.")
            if text["role"] in _LICENCE_ROLES and not _is_substantive(content):
                raise NoticeError(f"{label}: vendored licence {text['file']!r} is empty or only a label.")
            texts.append((text, vendored))
            referenced.add(vendored)
        if not any(text["role"] in _LICENCE_ROLES for text, _ in texts):
            raise NoticeError(f"{label}: no vendored licence text.")
        registry[key] = UpstreamNotice(entry, tuple(sorted(texts, key=lambda pair: pair[0]["file"])))
    if texts_root is not None:
        stale = sorted(path.relative_to(texts_root).as_posix()
                       for path in _vendored_files(texts_root) - referenced)
        if stale:
            raise NoticeError("Vendored upstream files are not referenced by the registry: " + ", ".join(stale))
    return registry


def _upstream_problem(component: Component, found: list[NoticeFile], notice: UpstreamNotice) -> str | None:
    """Why a verified upstream entry cannot stand in for this exact package, if it cannot."""
    entry = notice.entry
    if _has_licence_text(found):
        return "ships its own licence text, so its upstream entry is stale"
    if entry["provenance"] != component.provenance:
        return f"upstream entry provenance {entry['provenance']} does not match the lock {component.provenance}"
    if entry["declaredLicense"] != component.declared_license:
        return (f"upstream entry declares {entry['declaredLicense']!r} but the package declares "
                f"{component.declared_license!r}")
    for item in entry["identity"]:
        try:
            local = _contained_file(component.directory, _relative_parts(item["path"]), _MAX_IDENTITY_BYTES)
        except (NoticeError, OSError):
            return f"identity file {item['path']!r} is missing or unsafe in the installed package"
        if _sha256(local) != item["sha256"]:
            return f"identity file {item['path']!r} does not match the package the upstream text was verified for"
    return None


def _unique_components(components: list[Component]) -> list[Component]:
    by_key: dict[tuple[str, str, str], Component] = {}
    for component in components:
        problem = identity_problem(component)
        if problem:
            raise NoticeError(f"Unsafe package identity: {problem}.")
        if component.key in by_key:
            raise NoticeError(f"{component.ecosystem} {component.name}@{component.version} is listed more than "
                              "once; duplicate package identities are refused.")
        by_key[component.key] = component
    return [by_key[key] for key in sorted(by_key)]


def check_coverage(
    components: list[Component],
    exceptions: dict[tuple[str, str, str], dict[str, Any]],
    upstream: dict[tuple[str, str, str], UpstreamNotice] | None = None,
) -> dict[tuple[str, str, str], list[NoticeFile]]:
    """Every covered package has an expression and text (own or verified upstream), or an exact exception."""
    upstream = upstream or {}
    files: dict[tuple[str, str, str], list[NoticeFile]] = {}
    problems: list[str] = []
    used: set[tuple[str, str, str]] = set()
    for component in _unique_components(components):
        found = notice_files(component)
        files[component.key] = found
        notice = upstream.get(component.key)
        problem = None if notice is None else _upstream_problem(component, found, notice)
        if problem:
            problems.append(f"{component.ecosystem} {component.name}@{component.version}: {problem}")
        missing = []
        if not component.declared_license:
            missing.append("expression")
        if not _has_licence_text(found) and (notice is None or problem):
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
    problems += [f"upstream entry {e}/{n}@{v} matches no covered package"
                 for e, n, v in sorted(set(upstream) - set(files))]
    if problems:
        raise NoticeError("Desktop third-party notices are incomplete:\n  " + "\n  ".join(problems))
    return files


# ---------------------------------------------------------------- output


def _plan(
    components: list[Component],
    files: dict[tuple[str, str, str], list[NoticeFile]],
    upstream: dict[tuple[str, str, str], UpstreamNotice],
) -> list[tuple[Component, list[NoticeFile]]]:
    """Every file to write, with its final relative destination; nothing is touched yet."""
    planned = []
    for component in _unique_components(components):
        if component.key not in files:
            raise NoticeError(f"{component.name}@{component.version} was not checked for coverage.")
        entries = [replace(item, path=f"{component.folder}/{item.path}") for item in files[component.key]]
        notice = upstream.get(component.key)
        for meta, source in () if notice is None else notice.texts:
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != meta["sha256"]:
                raise NoticeError(f"Vendored upstream file {meta['file']!r} changed after it was verified.")
            entries.append(NoticeFile(f"{component.folder}/upstream/{meta['file']}", source, data,
                                      meta["role"], "upstream", meta))
        planned.append((component, entries))
    _check_destinations([item.path for _, entries in planned for item in entries] + [INVENTORY, NOTICE_TEXT, MANIFEST])
    return planned


def _check_destinations(paths: list[str]) -> None:
    """Portable, unique even when case is ignored, and no file is also a directory."""
    folded: dict[str, str] = {}
    for path in paths:
        if not all(_portable_segment(part) for part in path.split("/")):
            raise NoticeError(f"Notice destination {path!r} is not a portable relative path.")
        if path.casefold() in folded:
            raise NoticeError(f"Notice destinations {folded[path.casefold()]!r} and {path!r} collide.")
        folded[path.casefold()] = path
    prefixes = {"/".join(path.split("/")[:depth]).casefold()
                for path in paths for depth in range(1, path.count("/") + 1)}
    clashes = sorted(set(folded) & prefixes)
    if clashes:
        raise NoticeError(f"Notice destination {folded[clashes[0]]!r} is both a file and a directory.")


def _private_needles(private_prefixes: tuple[str, ...]) -> set[str]:
    needles = set()
    for prefix in private_prefixes:
        if prefix:
            for variant in (prefix, prefix.replace("\\", "/"), prefix.replace("/", "\\"),
                            prefix.replace("/", "\\").replace("\\", "\\\\")):
                needles.add(variant.lower())
    return needles


def _refuse_private_content(contents: dict[str, bytes], private_prefixes: tuple[str, ...]) -> None:
    needles = _private_needles(private_prefixes)
    for name in sorted(contents):
        lowered = contents[name].decode("utf-8", errors="ignore").lower()
        if any(needle in lowered for needle in needles):
            raise NoticeError(f"Notice output {name!r} would contain a private path; nothing was written.")


def _prepare_output(out_dir: Path) -> Path:
    """A new or empty plain directory with no link or junction anywhere on its path. Never deletes."""
    if any(part == ".." for part in out_dir.parts):
        raise NoticeError("The notice output path must not contain '..'.")
    absolute = out_dir if out_dir.is_absolute() else Path.cwd() / out_dir
    for candidate in (absolute, *absolute.parents):
        if os.path.lexists(candidate) and _is_link(candidate):
            raise NoticeError("The notice output or one of its parent directories is a link or junction.")
    if os.path.lexists(absolute):
        if not absolute.is_dir():
            raise NoticeError("The notice output exists and is not a directory.")
        if any(absolute.iterdir()):
            raise NoticeError("The notice output directory is not empty. Nothing was deleted; "
                              "use a new directory for every build.")
    else:
        absolute.mkdir(parents=True)
    return absolute


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as handle:  # never replaces an existing file
        handle.write(data)
    if path.read_bytes() != data:
        raise NoticeError(f"Notice file {path.name!r} did not read back byte-for-byte.")


def write_notices(
    components: list[Component],
    files: dict[tuple[str, str, str], list[NoticeFile]],
    exceptions: dict[tuple[str, str, str], dict[str, Any]],
    out_dir: Path,
    target: str,
    private_prefixes: tuple[str, ...],
    upstream: dict[tuple[str, str, str], UpstreamNotice] | None = None,
    project: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """``project`` is Qronos's own LICENSE and NOTICE (``project_licence.read()``); a build always passes it."""
    upstream = upstream or {}
    planned = _plan(components, files, upstream)
    project_files = {}
    for name, data in sorted((project or {}).items()):
        try:
            project_files[f"{project_licence.FOLDER}/{name}"] = (data, project_licence.check(name, data))
        except project_licence.ProjectLicenceError as error:
            raise NoticeError(f"Qronos's own licence cannot be shipped: {error}") from None
    if project is not None and sorted(project) != sorted(project_licence.FILES):
        raise NoticeError(f"Qronos's own licence files must be exactly {list(project_licence.FILES)}.")
    records = []
    text = [
        "Qronos desktop application - third-party notices",
        "=" * 49,
        f"Target: {target}.",
        *([f"Qronos itself: the source owned by the Qronos project is licensed under the Apache License, "
           f"Version 2.0 ({' and '.join(sorted(project_files))}). The components below keep their own licences "
           "and are not relicensed."] if project_files else []),
        *SCOPE_LIMITS,
        "npm coverage is the production closure of desktop/package-lock.json (no dev dependencies).",
        "Each section names the package, its declared licence expression as published, its source, its "
        "locked checksum/integrity and its published-source download where established, then every copied "
        "file with its role, size and SHA-256. Only files with the role 'licence' or 'designated-licence' "
        "count as licence text; the others are preserved as auxiliary notices. Where the published package "
        "ships no licence text, verified upstream files from its pinned source revision follow.",
        "",
    ]
    owner_review = []
    for component, entries in planned:
        exception = exceptions.get(component.key)
        notice = upstream.get(component.key)
        own_text = _has_licence_text([item for item in entries if item.origin == "package"])
        if notice is not None and notice.entry["ownerReview"].startswith(OWNER_REVIEW_REQUIRED):
            owner_review.append(f"{component.ecosystem}/{component.name}@{component.version}")
        download = source_download(component)
        records.append({
            "ecosystem": component.ecosystem,
            "name": component.name,
            "version": component.version,
            "declaredLicense": component.declared_license,
            "licenseFileField": component.license_file,
            "source": component.source,
            "provenance": component.provenance,
            "sourceDownload": download,
            "folder": component.folder,
            "notices": [{
                "path": item.path, "role": item.role, "origin": item.origin,
                "bytes": len(item.data), "sha256": item.sha256,
                **({} if item.upstream is None else {name: item.upstream[name] for name in
                                                     ("url", "repository", "revision", "sourcePath")}),
            } for item in entries],
            "licenceTextPresent": own_text,
            "licenceTextSource": ("package" if own_text else "verified-upstream" if notice
                                  else "reviewed-exception"),
            "upstream": None if notice is None else {
                name: notice.entry[name] for name in ("upstream", "note", "ownerReview", "evidence")},
            "exception": None if exception is None else {
                "missing": exception["missing"], "reason": exception["reason"],
                "reviewedBy": exception["reviewedBy"]},
        })
        text += ["-" * 72, f"{component.name} {component.version} ({component.ecosystem})",
                 f"Declared licence: {component.declared_license or 'NOT DECLARED'}",
                 f"Source: {component.source}", f"Provenance: {component.provenance}"]
        if download:
            text.append(f"Published source: {download['url']}")
        if component.license_file:
            text.append(f"license-file field: {component.license_file}")
        if exception:
            text.append(f"Reviewed exception (missing {exception['missing']}): {exception['reason']}")
        if notice is not None:
            source = notice.entry["upstream"]
            text += [f"Verified upstream notices: {source['repository']} at {source['revision']} "
                     f"({source['packagePath']}). {notice.entry['note']}",
                     f"Owner review: {notice.entry['ownerReview']}"]
        for item in entries:
            origin = "" if item.upstream is None else f" from {item.upstream['url']}"
            text += ["", f"--- {item.path} ({item.role}, {len(item.data)} bytes, SHA-256 {item.sha256}){origin} ---",
                     item.data.decode("utf-8", errors="replace").rstrip("\n")]
        text.append("")
    inventory = {
        "schema": 3,
        "target": target,
        "scope": {
            "rust": ("locked normal-dependency closure for the target, including normal-edge proc-macro "
                     "dependencies, excluding build/dev-only dependencies; conservative, not proof of linkage"),
            "npm": "package-lock production closure (no dev)",
            "limits": list(SCOPE_LIMITS),
        },
        "counts": {
            "rust": sum(1 for record in records if record["ecosystem"] == "rust"),
            "npm": sum(1 for record in records if record["ecosystem"] == "npm"),
            "noticeFiles": sum(len(record["notices"]) for record in records),
        },
        "ownerReviewRequired": owner_review,
        "components": records,
    }
    if project_files:
        inventory["projectOwned"] = {"license": project_licence.SPDX,
                                     "files": [{"path": path, **evidence}
                                               for path, (_, evidence) in sorted(project_files.items())]}
    contents = {item.path: item.data for _, entries in planned for item in entries}
    contents.update({path: data for path, (data, _) in project_files.items()})
    _check_destinations(sorted(contents) + [INVENTORY, NOTICE_TEXT, MANIFEST])
    contents[INVENTORY] = (json.dumps(inventory, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode()
    contents[NOTICE_TEXT] = ("\n".join(text) + "\n").encode("utf-8")
    contents[MANIFEST] = (json.dumps({
        "schema": 1,
        "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in sorted(contents.items())],
    }, indent=1, sort_keys=True) + "\n").encode()
    _refuse_private_content(contents, private_prefixes)

    # Everything is validated; only now does the output directory come into being.
    out_dir = _prepare_output(out_dir)
    sources = {item.path: item for _, entries in planned for item in entries}
    for name in sorted(contents):
        item = sources.get(name)
        if item is not None and (_is_link(item.source) or _sha256(item.source) != item.sha256):
            raise NoticeError(f"Notice source for {name!r} changed after it was checked.")
        _write_new(out_dir.joinpath(*name.split("/")), contents[name])
    return inventory


def verify_notice_output(out_dir: Path) -> dict[str, Any]:
    """The output holds exactly the manifest's files, byte-for-byte, and no links."""
    if _is_link(out_dir) or not out_dir.is_dir():
        raise NoticeError("The notice output is missing or is a link.")
    manifest = json.loads((out_dir / MANIFEST).read_text(encoding="utf-8"))
    expected = {entry["path"]: entry for entry in manifest["files"]}
    found = set()
    for current, directories, names in os.walk(out_dir, followlinks=False):
        for name in directories + names:
            path = Path(current) / name
            if _is_link(path):
                raise NoticeError(f"Notice output contains a link: {path.relative_to(out_dir).as_posix()!r}.")
            if name in names:
                found.add(path.relative_to(out_dir).as_posix())
    if found != set(expected) | {MANIFEST}:
        raise NoticeError("Notice output files differ from its manifest: "
                          + ", ".join(sorted(found ^ (set(expected) | {MANIFEST}))))
    for name, entry in sorted(expected.items()):
        data = out_dir.joinpath(*name.split("/")).read_bytes()
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise NoticeError(f"Notice output file {name!r} does not match its manifest.")
    return manifest


def collect_desktop_notices(
    out_dir: Path,
    *,
    node_modules: Path | None = None,
    target: str = WINDOWS_TARGET,
    exceptions_path: Path = EXCEPTIONS_PATH,
    upstream_path: Path = UPSTREAM_REGISTRY,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    if target != WINDOWS_TARGET:
        raise NoticeError(f"Desktop notices are collected for {WINDOWS_TARGET} only.")
    metadata = cargo_metadata(CARGO_MANIFEST, target, runner)
    components = rust_components(metadata, cargo_lock_checksums(CARGO_MANIFEST.parent / "Cargo.lock"))
    lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    node_root = node_modules if node_modules is not None else DESKTOP / "node_modules"
    components += npm_components(lock, node_root)
    exceptions = load_exceptions(exceptions_path)
    upstream = load_upstream(upstream_path)
    files = check_coverage(components, exceptions, upstream)
    private = (str(ROOT), str(Path.home()), str(node_root.resolve()),
               *{str(component.directory.parent) for component in components if component.ecosystem == "rust"})
    try:
        project = project_licence.read(ROOT)
    except project_licence.ProjectLicenceError as error:
        raise NoticeError(f"Qronos's own licence cannot be shipped: {error}") from None
    return write_notices(components, files, exceptions, out_dir, target, private, upstream, project=project)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="New or empty directory to generate into.")
    parser.add_argument("--node-modules", type=Path, default=None,
                        help="Installed node_modules to read (default: desktop/node_modules).")
    parser.add_argument("--exceptions", type=Path, default=EXCEPTIONS_PATH)
    parser.add_argument("--upstream", type=Path, default=UPSTREAM_REGISTRY,
                        help="Verified upstream notice registry (default: the committed one).")
    arguments = parser.parse_args(argv)
    try:
        inventory = collect_desktop_notices(arguments.out, node_modules=arguments.node_modules,
                                            exceptions_path=arguments.exceptions,
                                            upstream_path=arguments.upstream)
        verify_notice_output(arguments.out)
    except NoticeError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(inventory["counts"], sort_keys=True))
    for item in inventory["ownerReviewRequired"]:
        print(f"owner review required: {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
