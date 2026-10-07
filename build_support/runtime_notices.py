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

from dataclasses import dataclass, field
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
NOTICE_DIRECTORY = "third-party-notices"
INVENTORY_NAME = "inventory.json"
README_NAME = "README.txt"
REVIEWS_FILE = Path(__file__).with_name("runtime_notice_reviews.json")
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
# license text. Bundling one needs a reviewed entry for its exact version.
NATIVE_REVIEW_REQUIRED = frozenset({"sounddevice"})

# The sounddevice wheel ships PortAudio for several platforms. The packaged
# x64 backend keeps only the default-host-API x64 build (MME, DirectSound,
# WDM/KS, WASAPI); the ASIO, 32-bit and ARM64 builds are removed.
PORTAUDIO_DIRECTORY = ("_sounddevice_data", "portaudio-binaries")
PORTAUDIO_KEEP = "libportaudio64bit.dll"
_PORTAUDIO_DLL = re.compile(r"^libportaudio[0-9a-z_-]*\.dll$", re.IGNORECASE)

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


@dataclass(frozen=True)
class NoticeCopy:
    source: Path
    destination: str  # relative to the bundle, POSIX form


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
        name = (metadata.get("Name") or "").strip()
        if not name:
            continue
        canonical = _canonical(name)
        if canonical in result:
            raise NoticeError(f"Distribution {name} is installed more than once in the build environment.")
        files = tuple(PurePosixPath(str(item).replace("\\", "/")) for item in (dist.files or ()))
        base = Path(dist.locate_file("")).resolve()
        record = _Distribution(canonical, str(dist.version), base, files, metadata)
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


def _source_inside(base: Path, relative: str) -> Path:
    candidate = base.joinpath(*relative.split("/"))
    if not candidate.exists() and not candidate.is_symlink():
        raise NoticeError(f"Notice file is missing: {relative}")
    _require_regular(candidate)
    resolved = candidate.resolve()
    if base != resolved and base not in resolved.parents:
        raise NoticeError(f"Notice source escapes its distribution: {relative}")
    if not resolved.is_file():
        raise NoticeError(f"Notice file is missing: {relative}")
    if resolved.stat().st_size > MAX_NOTICE_BYTES:
        raise NoticeError(f"Notice file is unexpectedly large: {relative}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_reviews(path: Path = REVIEWS_FILE) -> dict[tuple[str, str], dict]:
    """Reviewed, exact name/version records for notices metadata cannot provide.

    Each record names its provenance and either supplemental notice files the
    wheel ships outside its license metadata, or the reason a distribution
    without any notice file is accepted. Unresolved terms stay recorded.
    """
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    result: dict[tuple[str, str], dict] = {}
    for entry in raw.get("reviews", []):
        if not all(isinstance(entry.get(key), str) and entry[key].strip() for key in ("name", "version", "provenance")):
            raise NoticeError("A notice review must record name, version and provenance.")
        notices = entry.get("supplementalNotices", [])
        if not isinstance(notices, list) or not all(isinstance(item, str) for item in notices):
            raise NoticeError("Supplemental notices must be a list of relative paths.")
        if not notices and not (isinstance(entry.get("noNoticeReason"), str) and entry["noNoticeReason"].strip()):
            raise NoticeError("A review without supplemental notices must explain why none exist.")
        key = (_canonical(entry["name"]), entry["version"])
        if key in result:
            raise NoticeError(f"Duplicate notice review for {entry['name']} {entry['version']}.")
        result[key] = entry
    return result


# ------------------------------------------------------------------ pruning


def prune_portaudio(bundle: Path) -> tuple[str, ...]:
    """Remove every bundled PortAudio DLL except the x64 non-ASIO build.

    Returns the removed paths relative to the bundle. Other files in the
    directory (its README attribution, for example) are preserved.
    """
    directory = bundle.joinpath("_internal", *PORTAUDIO_DIRECTORY)
    if not directory.is_dir():
        raise NoticeError("The bundle has no sounddevice PortAudio directory.")
    _require_regular(directory)
    entries = sorted(directory.iterdir(), key=lambda item: item.name)
    for entry in entries:
        _require_regular(entry)
    if not any(entry.name == PORTAUDIO_KEEP and entry.is_file() for entry in entries):
        raise NoticeError(f"The bundle is missing {PORTAUDIO_KEEP}.")
    removed: list[str] = []
    for entry in entries:
        if entry.name != PORTAUDIO_KEEP and entry.is_file() and _PORTAUDIO_DLL.match(entry.name):
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
) -> NoticePlan:
    """Attribute every frozen piece and choose the notice texts to copy (read-only)."""
    reviews = dict(reviews or {})
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
        notices = _notice_files(dist)
        if name in NATIVE_REVIEW_REQUIRED and review is None:
            missing.append(f"{dist.name} {dist.version} (native binaries need a reviewed provenance entry)")
            continue
        supplemental = [PurePosixPath(item) for item in (review or {}).get("supplementalNotices", [])]
        if not notices and not supplemental and review is None:
            missing.append(f"{dist.name} {dist.version}")
            continue
        destination_root = PurePosixPath(NOTICE_DIRECTORY, "python", f"{dist.name}-{dist.version}")
        copied: list[str] = []
        for notice in sorted(set(notices) | set(supplemental)):
            relative = _safe_relative(notice)
            if notice in supplemental and notice not in dist.files:
                raise NoticeError(f"Reviewed notice {relative} is not part of {dist.name} {dist.version}.")
            destination = (destination_root / relative).as_posix()
            copies.append(NoticeCopy(_source_inside(dist.base, relative), destination))
            copied.append(destination)
        native = sorted(
            relative
            for root in represented[name]
            for relative in contents.internal_roots.get(root, ())
            if relative.lower().endswith(".dll")
        )
        entry = {
            "name": dist.name,
            "version": dist.version,
            "declaredLicense": _declared_license(dist.metadata),
            "frozenRoots": sorted(represented[name]),
            "nativeLibraries": native,
            "notices": sorted(copied),
            "review": None,
        }
        if review is not None:
            entry["review"] = {
                key: review[key]
                for key in ("provenance", "unresolved", "noNoticeReason")
                if isinstance(review.get(key), str) and review[key].strip()
            }
        entries.append(entry)
    if missing:
        raise NoticeError(
            "Bundled third-party distributions lack license/notice coverage and have no "
            f"reviewed entry: {missing}"
        )

    license_text = python_base / "LICENSE.txt"
    if not license_text.is_file():
        raise NoticeError("The CPython LICENSE.txt of the build interpreter was not found.")
    _require_regular(license_text)
    cpython_destination = f"{NOTICE_DIRECTORY}/python/cpython-{python_version}/LICENSE.txt"
    copies.append(NoticeCopy(license_text.resolve(), cpython_destination))
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
    destinations = [copy.destination for copy in copies]
    if len(destinations) != len(set(destinations)):
        raise NoticeError("Two notices would be copied to the same destination.")
    return NoticePlan(inventory=inventory, copies=tuple(sorted(copies, key=lambda c: c.destination)))


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
    for copy in plan.copies:
        relative = _safe_relative(PurePosixPath(copy.destination))
        if not relative.startswith(f"{NOTICE_DIRECTORY}/"):
            raise NoticeError(f"Notice destination outside the notice directory: {relative}")
        destination = root.joinpath(*relative.split("/"))
        if destination.exists():
            raise NoticeError(f"Duplicate notice destination: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        _require_regular(copy.source)
        shutil.copyfile(copy.source, destination)
    target.mkdir(parents=True, exist_ok=True)
    (target / INVENTORY_NAME).write_text(
        json.dumps(plan.inventory, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (target / README_NAME).write_text(_readme(plan.inventory), encoding="utf-8", newline="\n")
    return target


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
