"""Third-party notices for the frozen Qronos backend (build time only).

The PyInstaller bundle redistributes Python packages, native libraries and the
CPython runtime. This module finds what the frozen executable and its
``_internal`` directory actually contain, maps every piece to the installed
distribution metadata (or to CPython, PyInstaller, Microsoft's runtime or
Qronos itself), copies the license/NOTICE texts those distributions ship,
and writes a deterministic inventory beside them.

It is technical notice packaging, not a legal review: declared licenses are
recorded as their packages declare them, nothing is inferred from names, and
anything that cannot be attributed fails the build instead of being waved
through. Qronos's own license is an owner decision and is recorded as
unspecified. Nothing is downloaded.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
NOTICE_DIRECTORY = "third-party-notices"
INVENTORY_NAME = "inventory.json"
README_NAME = "README.txt"
REVIEWS_FILE = Path(__file__).with_name("runtime_notice_reviews.json")
# Upstream license texts vendored for native binaries whose wheel omits them.
# Each one is pinned by SHA-256 in the reviews file; nothing is downloaded.
VENDORED_TEXTS = Path(__file__).with_name("runtime_notice_texts")
MAX_NOTICE_BYTES = 4 * 1024 * 1024

# License-like file names a distribution may ship (LICENSE, COPYING, NOTICE,
# ThirdPartyNotices, AUTHORS, ...). Code and binaries are never notices.
_NOTICE_NAME = re.compile(
    r"^(licen[cs]es?|copying|notices?|thirdpartynotices|third[-_]party[-_]notices|authors?|copyright)"
    r"([._-][^/\\]*)?$",
    re.IGNORECASE,
)
_NOT_NOTICE_SUFFIXES = {
    ".py", ".pyc", ".pyd", ".pyi", ".dll", ".so", ".dylib", ".exe", ".c", ".h",
    ".cpp", ".hpp", ".js", ".json", ".toml", ".cfg", ".typed",
}

# Pieces of the frozen executable that come from PyInstaller itself: its
# bootloader, bootstrap and runtime hooks.
_PYINSTALLER_PREFIXES = ("pyimod", "pyiboot", "pyi_rth_", "_pyi_rth_utils")
PYINSTALLER_DISTRIBUTION = "pyinstaller"

# Qronos's own code and configuration inside the bundle.
PROJECT_ROOTS = frozenset({"core", "security", "build_support", "runtime_entry", "config"})

# Microsoft C/C++ runtime files, also when a wheel renamed them with a hash
# suffix. Their redistribution terms come from Microsoft's distributable-code
# terms, which are not a package notice and are left explicitly for review.
_MICROSOFT_RUNTIME = re.compile(
    r"^(vcruntime140(_1)?|msvcp140(_[0-9a-z]+)?|vcomp140|concrt140|ucrtbase)(-[0-9a-f]{8,})?\.dll$"
    r"|^api-ms-win-[a-z0-9-]+\.dll$",
    re.IGNORECASE,
)

# Tcl/Tk data directories PyInstaller copies from the CPython installation.
_CPYTHON_TCL_TK_DATA = frozenset({"_tcl_data", "_tk_data", "tcl8"})

# Third-party components a Windows CPython build ships, the bundled files that
# carry them and how the component would be named in CPython's LICENSE.txt.
# The inventory records whether the copied text names each bundled component;
# an unnamed one is left for review rather than assumed covered.
_CPYTHON_COMPONENTS = (
    ("OpenSSL", re.compile(r"^(libcrypto|libssl)-[0-9_]+(-x64)?\.dll$", re.I), re.compile(r"openssl", re.I)),
    ("libffi", re.compile(r"^libffi-[0-9]+\.dll$", re.I), re.compile(r"libffi", re.I)),
    ("SQLite", re.compile(r"^sqlite3\.dll$", re.I), re.compile(r"sqlite", re.I)),
    ("Tcl/Tk", re.compile(r"^(tcl|tk)[0-9]+t\.dll$", re.I), re.compile(r"\btcl\b|\btk\b", re.I)),
    ("zlib", re.compile(r"^zlib1?\.dll$", re.I), re.compile(r"\bzlib\b", re.I)),
    ("bzip2", re.compile(r"^_bz2\.pyd$", re.I), re.compile(r"bzip2", re.I)),
    ("XZ (liblzma)", re.compile(r"^_lzma\.pyd$", re.I), re.compile(r"liblzma|\bxz\b", re.I)),
    ("Expat", re.compile(r"^pyexpat\.pyd$", re.I), re.compile(r"\bexpat\b", re.I)),
    ("mpdecimal", re.compile(r"^_decimal\.pyd$", re.I), re.compile(r"mpdecimal|libmpdec", re.I)),
)

# Distributions whose wheels carry native binaries without the upstream
# license text. Bundling one needs a reviewed entry for its exact version
# whose pinned upstream texts cover every bundled native file by SHA-256.
NATIVE_REVIEW_REQUIRED = frozenset({"sounddevice"})

# The sounddevice wheel ships PortAudio for several platforms. The packaged
# x64 backend keeps only the default-host-API x64 build (MME, DirectSound,
# WDM/KS, WASAPI) and the upstream README; the ASIO, 32-bit, ARM64 and macOS
# builds are removed.
PORTAUDIO_DIRECTORY = ("_sounddevice_data", "portaudio-binaries")
PORTAUDIO_KEEP = "libportaudio64bit.dll"
PORTAUDIO_README = "README.md"
_PORTAUDIO_BINARY = re.compile(r"^libportaudio[0-9a-z_-]*\.(dll|dylib)$", re.IGNORECASE)

_DIST_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
_DIST_VERSION = re.compile(r"^(?:[0-9]{1,9}!)?[A-Za-z0-9]{1,40}(?:[._-][A-Za-z0-9]{1,40}){0,20}(?:\+[A-Za-z0-9]{1,40}(?:[._-][A-Za-z0-9]{1,40}){0,20})?$")
# Files whose content can be the license text itself. AUTHORS, NOTICE,
# COPYRIGHT and README files are kept as auxiliary notices but never count
# as license text.
_LICENSE_TEXT_NAME = re.compile(r"^(licen[cs]es?|copying|unlicense)([._-][^/\\]*)?$", re.IGNORECASE)
# Attribution files stay auxiliary even when metadata declares them as a
# License-File.
_AUXILIARY_NAME = re.compile(
    r"^(authors?|notices?|copyright|readme|thirdpartynotices|third[-_]party[-_]notices)([._-][^/\\]*)?$",
    re.IGNORECASE,
)
# A file that only names a license (an SPDX identifier or expression) is not
# its text.
_LICENSE_LABEL_ONLY = re.compile(
    r"^(spdx-license-identifier:\s*)?[A-Za-z0-9.+()\- ]{1,120}$", re.IGNORECASE
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REVIEW_KEYS = {"name", "version", "provenance", "supplementalNotices", "vendoredNotices",
                "binaryProvenance", "unresolved", "noNoticeReason"}
_VENDORED_KEYS = {"component", "version", "sourceRepository", "sourceCommit", "sourceUrl",
                  "file", "sha256", "covers"}

PRODUCT_LICENSE_NOTE = (
    "Qronos's own source and configuration (core, security, the runtime entry point and "
    "config) are owned by the Qronos project. Its public license is an owner decision and is not "
    "chosen by this build."
)
MICROSOFT_NOTE = (
    "Microsoft Visual C++ / Universal C Runtime files. Their redistribution is governed by "
    "Microsoft's distributable-code terms, which are not reproduced here and still need "
    "owner review."
)
CPYTHON_NOTE = (
    "LICENSE.txt is copied exactly as the build interpreter installed it. A bundled "
    "component it does not name (namedInLicenseText false) may still be covered by an "
    "unlabelled text in it or may need its own text; that is unresolved and needs review."
)


class NoticeError(ValueError):
    """The bundle cannot be given complete, attributable notices."""


@dataclass(frozen=True)
class FrozenContents:
    """What the frozen backend actually contains, by top-level name."""

    archive_modules: frozenset[str]  # PYZ and CArchive module/script top-level names
    internal_roots: Mapping[str, tuple[str, ...]]  # first _internal component -> relative files


@dataclass
class _Distribution:
    name: str
    version: str
    base: Path
    files: tuple[PurePosixPath, ...]
    metadata: object
    roots: set[str] = field(default_factory=set)
    record_hashes: dict[PurePosixPath, str] = field(default_factory=dict)  # RECORD path -> "sha256=<b64>"


@dataclass(frozen=True)
class NoticeCopy:
    source: Path
    destination: str  # relative to the bundle, POSIX form
    sha256: str
    size: int
    kind: str  # "license-text" or "auxiliary"

    def record(self) -> dict:
        return {"path": self.destination, "size": self.size, "sha256": self.sha256, "kind": self.kind}


@dataclass(frozen=True)
class NoticePlan:
    inventory: dict
    copies: tuple[NoticeCopy, ...]


# --------------------------------------------------------------- inspection


def _require_regular(path: Path) -> None:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    if path.is_symlink() or attributes & 0x400:
        raise NoticeError(f"Refusing a link or reparse point: {path.name}")


def frozen_contents(bundle: Path, archive_modules: Iterable[str]) -> FrozenContents:
    """Describe the bundle's ``_internal`` tree (links refused) plus archive names."""
    internal = bundle / "_internal"
    _require_regular(bundle)
    if not internal.is_dir():
        raise NoticeError("The frozen bundle has no _internal directory.")
    _require_regular(internal)
    roots: dict[str, list[str]] = {}
    for directory, subdirectories, names in os.walk(internal, followlinks=False):
        current = Path(directory)
        for name in subdirectories:
            _require_regular(current / name)
        for name in names:
            path = current / name
            _require_regular(path)
            relative = path.relative_to(internal).as_posix()
            roots.setdefault(relative.split("/", 1)[0], []).append(relative)
    tops = frozenset(name.split(".", 1)[0] for name in archive_modules if name)
    return FrozenContents(
        archive_modules=tops,
        internal_roots={root: tuple(sorted(files)) for root, files in sorted(roots.items())},
    )


def archive_module_names(executable: Path) -> tuple[str, ...]:
    """Module and script names inside the executable's CArchive and PYZ."""
    from PyInstaller.archive.readers import CArchiveReader  # build-time dependency

    archive = CArchiveReader(str(executable))
    names: list[str] = []
    for name, entry in archive.toc.items():
        code = entry[-1]
        if code in {"m", "M", "s"}:
            names.append(name)
        if code == "z":
            names.extend(archive.open_embedded_archive(name).toc.keys())
    return tuple(sorted(names))


def _module_name(component: str) -> str:
    """Import name a site-packages entry provides (package, module or extension)."""
    for suffix in (".py", ".pyc"):
        if component.endswith(suffix):
            return component[: -len(suffix)]
    if component.endswith((".pyd", ".so")):
        return component.split(".", 1)[0]
    return component


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _installed_distributions(distributions: Iterable[object]) -> list[_Distribution]:
    result: dict[str, _Distribution] = {}
    for dist in distributions:
        metadata = dist.metadata
        name = metadata.get("Name") or ""
        if not name.strip():
            continue
        version = str(dist.version or "")
        # Identities become directory names, so they are validated before any
        # destination is built: PEP 508 names and PEP 440-shaped versions only
        # (epoch "!", local "+", pre/post/dev segments), never separators,
        # drive colons, controls or traversal.
        if not _DIST_NAME.fullmatch(name) or not _DIST_VERSION.fullmatch(version):
            raise NoticeError("An installed distribution has an unsafe name or version in its metadata.")
        canonical = _canonical(name)
        if canonical in result:
            raise NoticeError(f"Distribution {name} is installed more than once in the build environment.")
        entries = list(dist.files or ())
        files = tuple(PurePosixPath(str(item).replace("\\", "/")) for item in entries)
        raw_base = Path(dist.locate_file(""))
        _require_regular(raw_base)
        base = raw_base.resolve()
        record = _Distribution(canonical, version, base, files, metadata)
        for item, path in zip(entries, files):
            digest = getattr(item, "hash", None)
            if digest is not None:
                record.record_hashes[path] = f"{digest.mode}={digest.value}"
        for item in files:
            parts = item.parts
            if not parts or parts[0] == ".." or parts[0].endswith((".dist-info", ".egg-info")):
                continue
            record.roots.add(parts[0])
            record.roots.add(_module_name(parts[0]))
        result[canonical] = record
    return list(result.values())


def _declared_license(metadata: object) -> dict:
    expression = (metadata.get("License-Expression") or "").strip()
    lines = (metadata.get("License") or "").strip().splitlines()
    first_line = lines[0].strip()[:200] if lines else ""
    classifiers = sorted(
        item.split("::", 1)[1].strip()
        for item in (metadata.get_all("Classifier") or [])
        if item.startswith("License ::")
    )
    return {
        "expression": expression or None,
        "licenseField": first_line or None,
        "licenseFieldTruncated": len(lines) > 1 or (bool(lines) and len(lines[0].strip()) > 200),
        "classifiers": classifiers,
    }


def _safe_relative(path: PurePosixPath) -> str:
    text = path.as_posix()
    if path.is_absolute() or not text or ":" in text or "\\" in text:
        raise NoticeError(f"Unsafe notice path: {text}")
    if any(part in {"", ".", ".."} for part in text.split("/")):
        raise NoticeError(f"Unsafe notice path: {text}")
    return text


def _notice_files(dist: _Distribution) -> list[PurePosixPath]:
    declared = {
        PurePosixPath(item.replace("\\", "/")).name.lower()
        for item in (dist.metadata.get_all("License-File") or [])
    }
    found: list[PurePosixPath] = []
    for item in dist.files:
        if not item.parts or item.parts[0] == "..":
            continue
        if item.suffix.lower() in _NOT_NOTICE_SUFFIXES:
            continue
        in_metadata_dir = item.parts[0].endswith(".dist-info")
        if _NOTICE_NAME.match(item.name) or (in_metadata_dir and item.name.lower() in declared):
            found.append(item)
    return sorted(set(found))


def _require_regular_chain(base: Path, relative: str) -> Path:
    """Refuse a link or reparse point at the base or any component below it,
    even one that resolves back inside the base."""
    current = base
    _require_regular(current)
    for part in relative.split("/"):
        current = current / part
        if not current.exists() and not current.is_symlink():
            raise NoticeError(f"Notice file is missing: {relative}")
        _require_regular(current)
    return current


def _source_inside(base: Path, relative: str) -> Path:
    candidate = _require_regular_chain(base, _safe_relative(PurePosixPath(relative)))
    resolved = candidate.resolve()
    if base != resolved and base not in resolved.parents:
        raise NoticeError(f"Notice source escapes its distribution: {relative}")
    if not resolved.is_file():
        raise NoticeError(f"Notice file is missing: {relative}")
    if resolved.stat().st_size > MAX_NOTICE_BYTES:
        raise NoticeError(f"Notice file is unexpectedly large: {relative}")
    return resolved


def _is_license_text(data: bytes) -> bool:
    """Non-empty text that is more than a license label."""
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return False
    return not (len(text.splitlines()) == 1 and _LICENSE_LABEL_ONLY.match(text))


def default_private_roots() -> tuple[str, ...]:
    """Builder-specific absolute locations that must never reach generated output."""
    candidates = [str(Path.home()), sys.prefix, sys.base_prefix, str(Path(__file__).resolve().parent.parent)]
    for variable in ("USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "PIP_CACHE_DIR"):
        if os.environ.get(variable):
            candidates.append(os.environ[variable])
    return tuple(sorted({item for item in candidates if len(item.strip("\\/")) >= 6}))


def _private_markers(roots: Iterable[str]) -> list[str]:
    markers: set[str] = set()
    for root in roots:
        text = root.rstrip("\\/").casefold()
        markers.update({text, text.replace("\\", "/"), text.replace("/", "\\"), text.replace("\\", "\\\\")})
    return sorted(markers)


# Absolute builder-looking paths in generated (not upstream) text.
_GENERIC_PRIVATE_PATH = re.compile(r"(?i)(?<![a-z])[a-z]:[\\/]|\\\\[^\\]|(^|[\s\"'(=])/(home|users|root|tmp)/")


def _contains_private(text: str, markers: Sequence[str]) -> bool:
    folded = text.casefold()
    return any(marker in folded for marker in markers)


def _check_generated(value: object, markers: Sequence[str], where: str = "inventory") -> None:
    """Reject private paths in every decoded scalar of generated output, without echoing them."""
    if isinstance(value, dict):
        for key, item in value.items():
            _check_generated(key, markers, where)
            _check_generated(item, markers, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_generated(item, markers, f"{where}[{index}]")
    elif isinstance(value, str):
        if _contains_private(value, markers) or (_GENERIC_PRIVATE_PATH.search(value) and "://" not in value):
            raise NoticeError(f"Generated notice output contains a builder path at {where}; nothing was written.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_reviews(path: Path = REVIEWS_FILE) -> dict[tuple[str, str], dict]:
    """Reviewed, exact name/version records for notices metadata cannot provide.

    Each record names its provenance and either supplemental notice files the
    wheel ships outside its license metadata, upstream texts vendored under
    ``runtime_notice_texts`` with pinned source and hashes, or the reason a
    distribution without any notice file is accepted. Unresolved terms stay
    recorded. Unknown fields are refused so a typo cannot weaken a check.
    """
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    result: dict[tuple[str, str], dict] = {}
    for entry in raw.get("reviews", []):
        if not isinstance(entry, dict) or set(entry) - _REVIEW_KEYS:
            raise NoticeError("A notice review contains unknown fields.")
        if not all(isinstance(entry.get(key), str) and entry[key].strip() for key in ("name", "version", "provenance")):
            raise NoticeError("A notice review must record name, version and provenance.")
        notices = entry.get("supplementalNotices", [])
        if not isinstance(notices, list) or not all(isinstance(item, str) for item in notices):
            raise NoticeError("Supplemental notices must be a list of relative paths.")
        vendored = entry.get("vendoredNotices", [])
        if not isinstance(vendored, list):
            raise NoticeError("Vendored notices must be a list.")
        for item in vendored:
            _validate_vendored_record(item)
        provenance = entry.get("binaryProvenance", {})
        if not isinstance(provenance, dict) or not all(
            isinstance(key, str) and isinstance(value, str) and value.strip() for key, value in provenance.items()
        ):
            raise NoticeError("Binary provenance must map names to non-empty strings.")
        if not notices and not vendored and not (isinstance(entry.get("noNoticeReason"), str) and entry["noNoticeReason"].strip()):
            raise NoticeError("A review without supplemental notices must explain why none exist.")
        key = (_canonical(entry["name"]), entry["version"])
        if key in result:
            raise NoticeError(f"Duplicate notice review for {entry['name']} {entry['version']}.")
        result[key] = entry
    return result


def _validate_vendored_record(item: object) -> None:
    if not isinstance(item, dict) or set(item) != _VENDORED_KEYS:
        raise NoticeError(f"A vendored notice must record exactly: {sorted(_VENDORED_KEYS)}.")
    for key in ("component", "version", "sourceRepository", "sourceUrl", "file"):
        if not isinstance(item[key], str) or not item[key].strip():
            raise NoticeError(f"A vendored notice is missing {key}.")
    if not isinstance(item["sourceCommit"], str) or not _GIT_COMMIT.fullmatch(item["sourceCommit"]):
        raise NoticeError("A vendored notice must pin a full 40-character source commit.")
    if not item["sourceRepository"].startswith("https://"):
        raise NoticeError("A vendored notice source repository must be an https URL.")
    if not (item["sourceUrl"].startswith(item["sourceRepository"].rstrip("/") + "/")
            and item["sourceCommit"] in item["sourceUrl"]):
        raise NoticeError("A vendored notice URL must point into its repository at the pinned commit.")
    if not isinstance(item["sha256"], str) or not _SHA256.fullmatch(item["sha256"]):
        raise NoticeError("A vendored notice must record its lowercase SHA-256.")
    _safe_relative(PurePosixPath(item["file"]))
    covers = item["covers"]
    if not isinstance(covers, list) or not covers:
        raise NoticeError("A vendored notice must name the bundled files it covers.")
    for cover in covers:
        if (not isinstance(cover, dict) or set(cover) != {"path", "sha256"}
                or not isinstance(cover["path"], str) or not isinstance(cover["sha256"], str)
                or not _SHA256.fullmatch(cover["sha256"])):
            raise NoticeError("A covered file must record exactly its path and lowercase SHA-256.")
        _safe_relative(PurePosixPath(cover["path"]))


def _record_sha256(value: str | None) -> str | None:
    """Hex SHA-256 from a RECORD hash ("sha256=<urlsafe base64>")."""
    if not value or not value.startswith("sha256="):
        return None
    encoded = value.split("=", 1)[1]
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).hex()
    except ValueError:
        return None


def _vendored_source(root: Path, relative: str, expected_sha256: str) -> Path:
    """A pinned upstream text inside ``root``: regular, contained and unchanged."""
    safe = _safe_relative(PurePosixPath(relative))
    if not root.joinpath(*safe.split("/")).exists():
        raise NoticeError(f"Vendored notice is missing: {relative}")
    candidate = _require_regular_chain(root, safe)
    root = root.resolve()
    resolved = candidate.resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise NoticeError(f"Vendored notice escapes its folder: {relative}")
    if resolved.stat().st_size > MAX_NOTICE_BYTES:
        raise NoticeError(f"Vendored notice is unexpectedly large: {relative}")
    if _sha256(resolved) != expected_sha256:
        raise NoticeError(f"Vendored notice does not match its pinned SHA-256: {relative}")
    return resolved


def _check_covered_binary(dist: _Distribution, cover: dict, bundle_internal: Path | None) -> None:
    """The covered file must be the reviewed one: in RECORD, installed and bundled."""
    path = PurePosixPath(cover["path"])
    expected = cover["sha256"]
    label = f"{dist.name} {dist.version}: {cover['path']}"
    if path not in dist.files:
        raise NoticeError(f"Reviewed binary is not part of {label}.")
    if _record_sha256(dist.record_hashes.get(path)) != expected:
        raise NoticeError(f"Installed RECORD hash differs from the reviewed binary for {label}.")
    installed = dist.base.joinpath(*cover["path"].split("/"))
    if not installed.is_file():
        raise NoticeError(f"Installed file is missing for {label}.")
    _require_regular_chain(dist.base, cover["path"])
    resolved = installed.resolve()
    if dist.base not in resolved.parents:
        raise NoticeError(f"Installed file escapes its distribution for {label}.")
    if _sha256(resolved) != expected:
        raise NoticeError(f"Installed file differs from the reviewed binary for {label}.")
    if bundle_internal is not None:
        bundled = bundle_internal.joinpath(*cover["path"].split("/"))
        if not bundled.is_file():
            raise NoticeError(f"Reviewed binary is not in the bundle for {label}.")
        _require_regular_chain(bundle_internal, cover["path"])
        if _sha256(bundled) != expected:
            raise NoticeError(f"Bundled file differs from the reviewed binary for {label}.")


# ------------------------------------------------------------------ pruning


def prune_portaudio(bundle: Path) -> tuple[str, ...]:
    """Keep only the x64 non-ASIO PortAudio DLL and the upstream README.

    Every other PortAudio library (32-bit, ARM64, ASIO and macOS builds) is
    removed. Anything else in the folder is unexpected and stops the build
    before a file is removed. Returns the removed paths relative to the bundle.
    """
    directory = bundle.joinpath("_internal", *PORTAUDIO_DIRECTORY)
    if not directory.is_dir():
        raise NoticeError("The bundle has no sounddevice PortAudio directory.")
    _require_regular(directory)
    entries = sorted(directory.iterdir(), key=lambda item: item.name)
    for entry in entries:
        _require_regular(entry)
    names = {entry.name for entry in entries if entry.is_file()}
    if PORTAUDIO_KEEP not in names or PORTAUDIO_README not in names:
        raise NoticeError(f"The bundle is missing {PORTAUDIO_KEEP} or the PortAudio {PORTAUDIO_README}.")
    unexpected = sorted(
        entry.name for entry in entries
        if not entry.is_file() or not (entry.name in {PORTAUDIO_KEEP, PORTAUDIO_README} or _PORTAUDIO_BINARY.match(entry.name))
    )
    if unexpected:
        raise NoticeError(f"Unexpected entries in the PortAudio folder: {unexpected}")
    removed: list[str] = []
    for entry in entries:
        if entry.name not in {PORTAUDIO_KEEP, PORTAUDIO_README}:
            entry.unlink()
            removed.append(entry.relative_to(bundle).as_posix())
    return tuple(removed)


# --------------------------------------------------------------------- plan


def plan_runtime_notices(
    contents: FrozenContents,
    *,
    distributions: Iterable[object],
    python_base: Path,
    python_version: str,
    stdlib_names: Iterable[str],
    reviews: Mapping[tuple[str, str], dict] | None = None,
    pruned: Sequence[str] = (),
    bundle_internal: Path | None = None,
    vendored_root: Path = VENDORED_TEXTS,
    private_roots: Sequence[str] | None = None,
) -> NoticePlan:
    """Attribute every frozen piece and choose the notice texts to copy (read-only)."""
    reviews = dict(reviews or {})
    used_reviews: set[tuple[str, str]] = set()
    installed = _installed_distributions(distributions)
    by_name = {dist.name: dist for dist in installed}
    stdlib = frozenset(stdlib_names)
    python_base = python_base.resolve()

    evidence = set(contents.archive_modules) | set(contents.internal_roots)
    claims: dict[str, set[str]] = {}
    for dist in installed:
        for item in dist.roots & evidence:
            claims.setdefault(item, set()).add(dist.name)
    # dist-info directories copied into the bundle belong to their distribution.
    for root in contents.internal_roots:
        match = re.fullmatch(r"(.+)-([^-]+)\.dist-info", root)
        if match:
            owner = by_name.get(_canonical(match.group(1)))
            if owner is None or owner.version != match.group(2):
                raise NoticeError(f"Bundled metadata {root} does not match the build environment.")
            claims.setdefault(root, set()).add(owner.name)
    conflicts = sorted(item for item, owners in claims.items() if len(owners) > 1)
    if conflicts:
        raise NoticeError(f"Frozen names claimed by more than one distribution: {conflicts}")

    represented: dict[str, set[str]] = {}
    for item, owners in claims.items():
        represented.setdefault(next(iter(owners)), set()).add(item)

    microsoft_files: dict[str, str] = {}
    for root, files in contents.internal_roots.items():
        owner = next(iter(claims.get(root, {"bundle top level"})))
        for relative in files:
            if _MICROSOFT_RUNTIME.match(PurePosixPath(relative).name):
                microsoft_files[relative] = owner

    cpython_roots: list[str] = []
    project_roots: list[str] = []
    pyinstaller_roots: list[str] = []
    unattributed: list[str] = []
    for item in sorted(evidence - set(claims)):
        if item in PROJECT_ROOTS:
            project_roots.append(item)
        elif item.startswith(_PYINSTALLER_PREFIXES):
            pyinstaller_roots.append(item)
        elif item in microsoft_files:
            continue
        elif item in stdlib or _module_name(item) in stdlib or item == "base_library.zip":
            cpython_roots.append(item)
        elif item in _CPYTHON_TCL_TK_DATA and (python_base / "tcl").is_dir():
            cpython_roots.append(item)
        elif bundle_internal is not None and _matches_cpython_file(bundle_internal / item, python_base):
            cpython_roots.append(item)
        else:
            unattributed.append(item)
    if unattributed:
        raise NoticeError(
            "Frozen files or modules cannot be attributed to an installed distribution, "
            f"CPython, PyInstaller, Microsoft's runtime or Qronos: {unattributed}"
        )
    if pyinstaller_roots:
        if PYINSTALLER_DISTRIBUTION not in by_name:
            raise NoticeError("PyInstaller pieces are bundled but PyInstaller's metadata is not installed.")
        represented.setdefault(PYINSTALLER_DISTRIBUTION, set()).update(
            pyinstaller_roots + ["bootloader (inside the runtime executable)"]
        )

    copies: list[NoticeCopy] = []
    entries: list[dict] = []
    missing: list[str] = []
    for name in sorted(represented):
        dist = by_name[name]
        review = reviews.get((dist.name, dist.version))
        if review is not None:
            used_reviews.add((dist.name, dist.version))
        notices = _notice_files(dist)
        native = sorted(
            relative
            for root in represented[name]
            for relative in contents.internal_roots.get(root, ())
            if relative.lower().endswith(".dll")
        )
        if name in NATIVE_REVIEW_REQUIRED:
            if review is None or not review.get("vendoredNotices"):
                missing.append(f"{dist.name} {dist.version} (native binaries need a reviewed, pinned upstream notice)")
                continue
            covered = {cover["path"] for item in review["vendoredNotices"] for cover in item["covers"]}
            bundled_native = {
                relative
                for root in represented[name]
                for relative in contents.internal_roots.get(root, ())
                if relative.lower().endswith((".dll", ".dylib", ".so"))
                and relative not in microsoft_files
            }
            if bundled_native - covered:
                raise NoticeError(
                    f"Bundled native files of {dist.name} {dist.version} are not covered by its "
                    f"reviewed upstream notices: {sorted(bundled_native - covered)}"
                )
        supplemental = [PurePosixPath(item) for item in (review or {}).get("supplementalNotices", [])]
        vendored = list((review or {}).get("vendoredNotices", []))
        declared = {
            PurePosixPath(item.replace("\\", "/")).name.casefold()
            for item in (dist.metadata.get_all("License-File") or [])
        }
        destination_root = PurePosixPath(NOTICE_DIRECTORY, "python", f"{dist.name}-{dist.version}")
        copied: list[str] = []
        notice_files: list[dict] = []
        for notice in sorted(set(notices) | set(supplemental)):
            relative = _safe_relative(notice)
            if notice in supplemental and notice not in dist.files:
                raise NoticeError(f"Reviewed notice {relative} is not part of {dist.name} {dist.version}.")
            destination = (destination_root / relative).as_posix()
            source = _source_inside(dist.base, relative)
            license_named = bool(_LICENSE_TEXT_NAME.match(notice.name)) or (
                notice.parts[0].endswith(".dist-info") and notice.name.casefold() in declared
                and not _AUXILIARY_NAME.match(notice.name)
            )
            copy = _planned_copy(source, destination, license_named)
            copies.append(copy)
            copied.append(destination)
            notice_files.append(copy.record())
        vendored_entries: list[dict] = []
        for item in vendored:
            _validate_vendored_record(item)
            for cover in item["covers"]:
                _check_covered_binary(dist, cover, bundle_internal)
            source = _vendored_source(vendored_root, item["file"], item["sha256"])
            destination = (destination_root / "upstream" / _safe_relative(PurePosixPath(item["file"]))).as_posix()
            copy = _planned_copy(source, destination, bool(_LICENSE_TEXT_NAME.match(PurePosixPath(item["file"]).name)))
            copies.append(copy)
            copied.append(destination)
            notice_files.append(copy.record())
            vendored_entries.append({
                "component": item["component"],
                "version": item["version"],
                "sourceRepository": item["sourceRepository"],
                "sourceCommit": item["sourceCommit"],
                "sourceUrl": item["sourceUrl"],
                "notice": destination,
                "sha256": item["sha256"],
                "covers": sorted(item["covers"], key=lambda cover: cover["path"]),
            })
        entry = {
            "name": dist.name,
            "version": dist.version,
            "declaredLicense": _declared_license(dist.metadata),
            "frozenRoots": sorted(represented[name]),
            "nativeLibraries": native,
            "notices": sorted(copied),
            "noticeFiles": sorted(notice_files, key=lambda item: item["path"]),
            "licenseTextPresent": any(item["kind"] == "license-text" for item in notice_files),
            "review": None,
        }
        if not entry["licenseTextPresent"]:
            # AUTHORS, NOTICE, COPYRIGHT, README or a label-only/empty LICENSE
            # are attribution, not license text. No review record waives this.
            missing.append(f"{dist.name} {dist.version} (no non-empty license text)")
        if review is not None:
            entry["review"] = {
                key: review[key]
                for key in ("provenance", "unresolved", "noNoticeReason")
                if isinstance(review.get(key), str) and review[key].strip()
            }
            if vendored_entries:
                entry["review"]["vendoredNotices"] = sorted(vendored_entries, key=lambda item: item["notice"])
            if review.get("binaryProvenance"):
                entry["review"]["binaryProvenance"] = dict(sorted(review["binaryProvenance"].items()))
        entries.append(entry)
    if missing:
        raise NoticeError(
            "Bundled third-party distributions lack license/notice coverage and have no "
            f"reviewed entry: {missing}"
        )
    stale = sorted(f"{name} {version}" for name, version in set(reviews) - used_reviews)
    if stale:
        raise NoticeError(
            "Notice review records match no bundled distribution at its installed version "
            f"(stale or mismatched): {stale}"
        )

    license_text = python_base / "LICENSE.txt"
    if not license_text.is_file():
        raise NoticeError("The CPython LICENSE.txt of the build interpreter was not found.")
    if not re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", python_version):
        raise NoticeError("The build interpreter version is not a plain X.Y.Z version.")
    _require_regular(license_text)
    cpython_destination = f"{NOTICE_DIRECTORY}/python/cpython-{python_version}/LICENSE.txt"
    cpython_copy = _planned_copy(license_text.resolve(), cpython_destination, True)
    if cpython_copy.kind != "license-text":
        raise NoticeError("The CPython LICENSE.txt of the build interpreter is empty.")
    copies.append(cpython_copy)
    text = license_text.read_text(encoding="utf-8", errors="replace")
    components = []
    for component, files, named in _CPYTHON_COMPONENTS:
        bundled = sorted(root for root in cpython_roots if files.match(root))
        if bundled:
            components.append({
                "component": component,
                "files": bundled,
                "namedInLicenseText": bool(named.search(text)),
            })

    inventory = {
        "schemaVersion": SCHEMA_VERSION,
        "purpose": (
            "Technical notice packaging for the frozen Qronos backend. Declared licenses are "
            "copied as the packages declare them; this is not a legal clearance."
        ),
        "distributions": entries,
        "cpython": {
            "version": python_version,
            "notices": [cpython_destination],
            "noticeFiles": [cpython_copy.record()],
            "frozenRoots": sorted(cpython_roots),
            "bundledComponents": components,
            "note": CPYTHON_NOTE,
        },
        "microsoftRuntime": {
            "files": [{"path": path, "deliveredBy": owner} for path, owner in sorted(microsoft_files.items())],
            "status": "review-required",
            "note": MICROSOFT_NOTE,
        },
        "projectOwned": {
            "frozenRoots": sorted(project_roots),
            "license": "unspecified",
            "note": PRODUCT_LICENSE_NOTE,
        },
        "prunedBinaries": sorted(pruned),
    }
    _check_destinations([copy.destination for copy in copies])
    markers = _private_markers(default_private_roots() if private_roots is None else private_roots)
    _check_generated(inventory, markers)
    for index, line in enumerate(_readme(inventory).splitlines()):
        _check_generated(line, markers, f"README line {index + 1}")
    for copy in copies:
        # Upstream text is never rewritten; a builder path in it stops the build.
        if _contains_private(copy.source.read_bytes().decode("utf-8", errors="replace"), markers):
            raise NoticeError(f"Notice {copy.destination} contains a builder path; nothing was written.")
    return NoticePlan(inventory=inventory, copies=tuple(sorted(copies, key=lambda c: c.destination)))


def _planned_copy(source: Path, destination: str, license_named: bool) -> "NoticeCopy":
    data = source.read_bytes()
    kind = "license-text" if license_named and _is_license_text(data) else "auxiliary"
    return NoticeCopy(source, destination, hashlib.sha256(data).hexdigest(), len(data), kind)


def _check_destinations(destinations: Sequence[str]) -> None:
    """Every destination is canonical, unique ignoring case, and never also a folder of another."""
    folded: dict[str, str] = {}
    for destination in destinations:
        relative = _safe_relative(PurePosixPath(destination))
        if not relative.startswith(f"{NOTICE_DIRECTORY}/") or any(
            ord(character) < 32 for character in relative
        ):
            raise NoticeError("A notice destination is outside the notice folder or not printable.")
        key = relative.casefold()
        if key in folded:
            raise NoticeError(f"Two notices would share a destination ignoring case: {relative}")
        folded[key] = relative
    for key in folded:
        parts = key.split("/")
        for depth in range(1, len(parts)):
            if "/".join(parts[:depth]) in folded:
                raise NoticeError(f"A notice destination is also used as a folder: {folded[key]}")


def _matches_cpython_file(bundled: Path, python_base: Path) -> bool:
    """True when a bundled top-level file is byte-identical to the interpreter's own."""
    if not bundled.is_file() or bundled.is_symlink():
        return False
    for directory in (python_base, python_base / "DLLs"):
        candidate = directory / bundled.name
        if candidate.is_file() and not candidate.is_symlink():
            if candidate.stat().st_size == bundled.stat().st_size and _sha256(candidate) == _sha256(bundled):
                return True
    return False


# -------------------------------------------------------------------- write


def write_runtime_notices(plan: NoticePlan, root: Path) -> Path:
    """Copy the planned notices and the inventory under ``root/third-party-notices``.

    ``root`` is the bundle directory in a build, or any scratch directory for a
    dry run. An existing notice directory is never overwritten.
    """
    target = root / NOTICE_DIRECTORY
    if target.exists() or target.is_symlink():
        raise NoticeError("Third-party notices already exist in this destination.")
    # Preflight everything before the first folder is created: destinations,
    # the output root and its ancestors, and every source against its planned
    # hash.
    _check_destinations([copy.destination for copy in plan.copies])
    if root.exists() or root.is_symlink():
        _require_regular(root)
    for copy in plan.copies:
        _verified_bytes(copy)
    target.mkdir(parents=True)
    for copy in plan.copies:
        destination = root.joinpath(*copy.destination.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        _require_regular_chain(root, copy.destination.rsplit("/", 1)[0])
        if destination.exists() or destination.is_symlink():
            raise NoticeError(f"Duplicate notice destination: {copy.destination}")
        with destination.open("xb") as stream:
            stream.write(_verified_bytes(copy))
    (target / INVENTORY_NAME).write_text(
        json.dumps(plan.inventory, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (target / README_NAME).write_text(_readme(plan.inventory), encoding="utf-8", newline="\n")
    return target


def _verified_bytes(copy: NoticeCopy) -> bytes:
    """The source bytes, refused if the file changed since planning."""
    _require_regular(copy.source)
    data = copy.source.read_bytes()
    if len(data) != copy.size or hashlib.sha256(data).hexdigest() != copy.sha256:
        raise NoticeError(f"Notice source changed after planning: {copy.destination}")
    return data


def _readme(inventory: dict) -> str:
    lines = [
        "Third-party notices for the Qronos backend runtime",
        "",
        "This folder holds the license and notice texts that the bundled third-party",
        "packages, the CPython runtime and PyInstaller ship. inventory.json lists each",
        "bundled distribution, its version, its declared license and the copied texts.",
        "It is technical notice packaging, not a legal clearance.",
        "",
        "Microsoft runtime files: " + MICROSOFT_NOTE,
        "",
        "Qronos itself: " + PRODUCT_LICENSE_NOTE,
        "",
        "Models, speech binaries and voice assets are not part of this runtime; they are",
        "provisioned separately and carry their own terms.",
    ]
    unnamed = [item["component"] for item in inventory["cpython"]["bundledComponents"] if not item["namedInLicenseText"]]
    if unnamed:
        lines += [
            "",
            "CPython components not named in the copied CPython LICENSE.txt (review required):",
            "  " + ", ".join(unnamed),
        ]
    reviewed = [entry for entry in inventory["distributions"] if entry.get("review")]
    if reviewed:
        lines += ["", "Reviewed provenance records (see inventory.json):"]
        for entry in reviewed:
            lines.append(f"  {entry['name']} {entry['version']}: {entry['review']['provenance']}")
            for item in entry["review"].get("vendoredNotices", []):
                lines.append(
                    f"    Upstream {item['component']} {item['version']} text ({item['sourceUrl']}): {item['notice']}"
                )
            if entry["review"].get("unresolved"):
                lines.append(f"    Unresolved: {entry['review']['unresolved']}")
    if inventory.get("prunedBinaries"):
        lines += ["", "Removed from the bundle (not redistributed):"]
        lines += [f"  {item}" for item in inventory["prunedBinaries"]]
    return "\n".join(lines) + "\n"


def collect_runtime_notices(bundle: Path, *, pruned: Sequence[str] = ()) -> Path:
    """Build-time entry point: inspect the frozen bundle and write its notices."""
    executable = bundle / ("qronos-runtime.exe" if os.name == "nt" else "qronos-runtime")
    contents = frozen_contents(bundle, archive_module_names(executable))
    plan = plan_runtime_notices(
        contents,
        distributions=importlib.metadata.distributions(),
        python_base=Path(sys.base_prefix),
        python_version=".".join(map(str, sys.version_info[:3])),
        stdlib_names=sys.stdlib_module_names,
        reviews=load_reviews(),
        pruned=pruned,
        bundle_internal=bundle / "_internal",
    )
    return write_runtime_notices(plan, bundle)
