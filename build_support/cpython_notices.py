"""Supplemental CPython component notices for the frozen backend (build time only).

CPython's ``LICENSE.txt`` does not carry every third-party text that a Windows
build compiles in or ships beside it (zlib/zlib-ng, Expat, HACL*, mimalloc,
libb2, mpdecimal, libtommath, SQLite's dedication, CPython's own
``Doc/license.rst`` and more). ``cpython_notice_registry.json`` records, for each
audited interpreter build, every native file the official layout installs with
its SHA-256 and the components inside it, and maps each component to byte-exact
upstream texts kept under ``cpython_notice_texts``.

Nothing is inferred from names or SPDX labels: a bundled CPython file is
matched by its exact bytes to an audited interpreter record, and an interpreter,
binary or data tree that is not in the registry stops the build. The registry is
technical notice packaging, not a licence decision; Microsoft runtime files are
recorded for identity only and stay an owner review gate. Nothing is downloaded.
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Iterable, Mapping

REGISTRY_FILE = Path(__file__).with_name("cpython_notice_registry.json")
TEXT_ROOT = Path(__file__).with_name("cpython_notice_texts")
SCHEMA = 1
MAX_TEXT_BYTES = 1024 * 1024

# Text roles. A component needs at least one licence text or verbatim terms
# excerpt; README/AUTHORS-style files are auxiliary and never satisfy that.
LICENSE_TEXT = "license-text"
TERMS_EXCERPT = "terms-excerpt"
AUXILIARY = "auxiliary"
TEXT_KINDS = frozenset({LICENSE_TEXT, TERMS_EXCERPT, AUXILIARY})
_SUBSTANTIVE = frozenset({LICENSE_TEXT, TERMS_EXCERPT})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
_COMPONENT_KEY = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,40}@[A-Za-z0-9][A-Za-z0-9.+-]{0,60}$")
_NATIVE = (".dll", ".pyd")
# Folders PyInstaller fills from a CPython installation's Tcl/Tk tree (Tcl/Tk
# 8.6). Bundling one always requires an audited data tree.
TCL_TK_DATA_ROOTS = frozenset({"_tcl_data", "_tk_data", "tcl8"})

_SOURCE_KEYS = {
    "archive-member": {"kind", "url", "archiveSha256", "member", "pinnedBy"},
    "archive-member-excerpt": {"kind", "url", "archiveSha256", "member", "pinnedBy", "offset", "length", "excerpt"},
    "cpython-source-file": {"kind", "url", "archiveSha256", "member", "verifiedBy", "tagCommit"},
    "cpython-source-excerpt": {"kind", "url", "archiveSha256", "member", "verifiedBy", "tagCommit", "offset", "length",
                               "excerpt", "identicalBlockInFiles"},
    "repository-file": {"kind", "repository", "commit", "path", "url", "ref"},
    "web-document": {"kind", "url", "retrieved", "note"},
}
_COMPONENT_KEYS = {"name", "version", "texts", "attribution", "unresolved", "scope"}
_INTERPRETER_KEYS = {"id", "implementation", "version", "platform", "build", "sourceTag", "sourceCommit", "layout",
                     "provenance", "licenseFile", "coreComponents", "moduleComponents", "files", "dataTrees"}
_TREE_KEYS = {"source", "excludes", "files", "treeSha256", "components"}


class CPythonNoticeError(ValueError):
    """The bundled CPython runtime cannot be matched to audited notices."""


@dataclass(frozen=True)
class Interpreter:
    """Identity of the interpreter that produced the frozen bundle."""

    version: str
    platform: str
    build: str
    base: Path


@dataclass(frozen=True)
class Registry:
    texts: Mapping[str, dict]
    components: Mapping[str, dict]
    interpreters: tuple[dict, ...]
    text_root: Path


@dataclass(frozen=True)
class PlannedText:
    source: Path
    destination: str  # relative to the bundle, POSIX form
    sha256: str
    size: int
    kind: str


@dataclass(frozen=True)
class CPythonPlan:
    interpreter: dict
    inventory: dict
    texts: tuple[PlannedText, ...]


# ------------------------------------------------------------------ helpers


def _fail(message: str) -> None:
    raise CPythonNoticeError(message)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_regular(path: Path) -> None:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    if path.is_symlink() or attributes & 0x400:
        _fail(f"Refusing a link or reparse point: {path.name}")


def _safe_relative(text: object, what: str) -> str:
    if not isinstance(text, str) or not text or "\\" in text or ":" in text or text.startswith("/"):
        _fail(f"Unsafe {what}: {text!r}")
    if any(part in {"", ".", ".."} or any(ord(c) < 32 for c in part) for part in text.split("/")):
        _fail(f"Unsafe {what}: {text!r}")
    return text


def _regular_chain(root: Path, relative: str) -> Path:
    """``root/relative`` with every component refused if it is a link or junction."""
    current = root
    _require_regular(current)
    for part in relative.split("/"):
        current = current / part
        if not current.exists() and not current.is_symlink():
            _fail(f"Missing file: {relative}")
        _require_regular(current)
    return current


def _normalised(text: str) -> str:
    return " ".join(text.split())


def _https(value: object) -> bool:
    return isinstance(value, str) and value.startswith("https://") and len(value) < 400


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


# ------------------------------------------------------------------ registry


def _validate_source(source: object, text_id: str) -> None:
    if not isinstance(source, dict) or source.get("kind") not in _SOURCE_KEYS or set(source) != _SOURCE_KEYS[source["kind"]]:
        _fail(f"Text {text_id} has a source record with missing or unknown fields.")
    kind = source["kind"]
    if not _https(source["url"]):
        _fail(f"Text {text_id} source URL must be https.")
    if "archiveSha256" in source and not (isinstance(source["archiveSha256"], str) and _SHA256.fullmatch(source["archiveSha256"])):
        _fail(f"Text {text_id} must pin its source archive SHA-256.")
    if kind in {"archive-member", "archive-member-excerpt"}:
        if not isinstance(source["pinnedBy"], list) or not source["pinnedBy"] or not all(_nonempty(item) for item in source["pinnedBy"]):
            _fail(f"Text {text_id} must say which CPython build pins its archive.")
    if kind.startswith("cpython-source") and not (isinstance(source["tagCommit"], str) and _COMMIT.fullmatch(source["tagCommit"])):
        _fail(f"Text {text_id} must pin the CPython tag commit.")
    if kind.endswith("excerpt"):
        if type(source["offset"]) is not int or type(source["length"]) is not int or source["offset"] < 0 or source["length"] <= 0:
            _fail(f"Text {text_id} excerpt must record its byte offset and length.")
    if kind == "repository-file":
        if not _https(source["repository"]) or not (isinstance(source["commit"], str) and _COMMIT.fullmatch(source["commit"])):
            _fail(f"Text {text_id} must pin a repository and full commit.")
        if not (source["url"].startswith(source["repository"].rstrip("/") + "/") and source["commit"] in source["url"]):
            _fail(f"Text {text_id} URL must point into its repository at the pinned commit.")
    if kind == "web-document" and not (_nonempty(source["retrieved"]) and _nonempty(source["note"])):
        _fail(f"Text {text_id} from an unversioned web document must record when it was retrieved and why.")


def load_registry(path: Path = REGISTRY_FILE, text_root: Path = TEXT_ROOT) -> Registry:
    """Load and fully validate the registry and every text it pins.

    Unknown fields, unpinned sources, texts whose bytes changed, components
    without a substantive text, attribution lines that do not appear in the
    component's own texts, unused texts or components, and ambiguous
    interpreter or file records all fail.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CPythonNoticeError(f"The CPython notice registry cannot be read: {error}") from error
    if not isinstance(raw, dict) or set(raw) != {"schema", "comment", "texts", "components", "interpreters"} or raw["schema"] != SCHEMA:
        _fail("The CPython notice registry has an unsupported shape or schema.")
    texts, components, interpreters = raw["texts"], raw["components"], raw["interpreters"]
    if not isinstance(texts, dict) or not isinstance(components, dict) or not isinstance(interpreters, list) or not interpreters:
        _fail("The CPython notice registry has an unsupported shape.")

    data: dict[str, bytes] = {}
    for text_id, record in texts.items():
        _safe_relative(text_id, "text path")
        if not isinstance(record, dict) or set(record) != {"size", "sha256", "sources"}:
            _fail(f"Text {text_id} must record exactly size, sha256 and sources.")
        if type(record["size"]) is not int or not (0 < record["size"] <= MAX_TEXT_BYTES):
            _fail(f"Text {text_id} has an invalid size.")
        if not (isinstance(record["sha256"], str) and _SHA256.fullmatch(record["sha256"])):
            _fail(f"Text {text_id} must record its lowercase SHA-256.")
        if not isinstance(record["sources"], list) or not record["sources"]:
            _fail(f"Text {text_id} must record at least one verified source.")
        for source in record["sources"]:
            _validate_source(source, text_id)
        file = _regular_chain(text_root, text_id)
        if not file.is_file():
            _fail(f"Text {text_id} is not a regular file.")
        content = file.read_bytes()
        if len(content) != record["size"] or _sha256_bytes(content) != record["sha256"]:
            _fail(f"Text {text_id} does not match its pinned size and SHA-256.")
        data[text_id] = content

    used_texts: set[str] = set()
    for key, record in components.items():
        if not isinstance(key, str) or not _COMPONENT_KEY.fullmatch(key):
            _fail(f"Invalid component key: {key!r}")
        if not isinstance(record, dict) or not {"name", "version", "texts", "attribution"} <= set(record) <= _COMPONENT_KEYS:
            _fail(f"Component {key} has missing or unknown fields.")
        if not _nonempty(record["name"]) or not _nonempty(record["version"]):
            _fail(f"Component {key} must name itself and its version.")
        for optional in ("unresolved", "scope"):
            if optional in record and not _nonempty(record[optional]):
                _fail(f"Component {key} has an empty {optional} note.")
        refs = record["texts"]
        if not isinstance(refs, list) or not refs:
            _fail(f"Component {key} lists no texts.")
        seen: set[str] = set()
        basenames: set[str] = set()
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"text", "kind"} or ref["kind"] not in TEXT_KINDS:
                _fail(f"Component {key} has an invalid text reference.")
            if ref["text"] not in texts or ref["text"] in seen:
                _fail(f"Component {key} references an unknown or repeated text: {ref['text']!r}")
            seen.add(ref["text"])
            basename = PurePosixPath(ref["text"]).name.casefold()
            if basename in basenames:
                _fail(f"Component {key} has two texts named {basename}; their notice files would collide.")
            basenames.add(basename)
        if not any(ref["kind"] in _SUBSTANTIVE for ref in refs):
            _fail(f"Component {key} has only auxiliary texts; a licence text or terms excerpt is required.")
        own = _normalised("\n".join(data[ref["text"]].decode("utf-8", errors="replace") for ref in refs))
        attribution = record["attribution"]
        if not isinstance(attribution, list) or not all(_nonempty(line) for line in attribution):
            _fail(f"Component {key} attribution must be a list of lines.")
        for line in attribution:
            if _normalised(line) not in own:
                _fail(f"Component {key} attribution does not appear verbatim in its texts: {line!r}")
        used_texts.update(seen)
    if set(texts) - used_texts:
        _fail(f"Texts no component uses: {sorted(set(texts) - used_texts)}")

    identities: set[tuple[str, str, str]] = set()
    ids: set[str] = set()
    used_components: set[str] = set()
    for record in interpreters:
        used_components |= _validate_interpreter(record, components)
        identity = (record["version"], record["platform"], record["build"])
        if identity in identities or record["id"] in ids:
            _fail(f"Duplicate interpreter record: {record['id']}")
        identities.add(identity)
        ids.add(record["id"])
    if set(components) - used_components:
        _fail(f"Components no interpreter uses: {sorted(set(components) - used_components)}")
    extra = sorted(
        path.relative_to(text_root).as_posix()
        for path in text_root.rglob("*")
        if path.is_file() and path.name != ".gitattributes" and path.relative_to(text_root).as_posix() not in texts
    )
    if extra:
        _fail(f"Unregistered files in the CPython text folder: {extra}")
    return Registry(texts=texts, components=components, interpreters=tuple(interpreters), text_root=text_root)


def _validate_interpreter(record: object, components: Mapping[str, dict]) -> set[str]:
    if not isinstance(record, dict) or set(record) != _INTERPRETER_KEYS:
        _fail("An interpreter record has missing or unknown fields.")
    name = record["id"]
    for key in ("id", "implementation", "platform", "build", "sourceTag", "layout", "provenance"):
        if not _nonempty(record[key]):
            _fail(f"Interpreter {name!r} is missing {key}.")
    if not (isinstance(record["version"], str) and _VERSION.fullmatch(record["version"])):
        _fail(f"Interpreter {name} has an invalid version.")
    if not (isinstance(record["sourceCommit"], str) and _COMMIT.fullmatch(record["sourceCommit"])):
        _fail(f"Interpreter {name} must pin its full source commit.")
    if record["implementation"] != "CPython" or not record["build"].startswith(f"tags/v{record['version']}:") \
            or not record["sourceCommit"].startswith(record["build"].rsplit(":", 1)[1]):
        _fail(f"Interpreter {name} build tag does not match its version and commit.")
    licence = record["licenseFile"]
    if not isinstance(licence, dict) or set(licence) != {"path", "size", "sha256"} or licence["path"] != "LICENSE.txt" \
            or type(licence["size"]) is not int or not (isinstance(licence["sha256"], str) and _SHA256.fullmatch(licence["sha256"])):
        _fail(f"Interpreter {name} must pin its LICENSE.txt.")
    used: set[str] = set()

    def keys(value: object, where: str) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(item, str) and item in components for item in value) \
                or len(set(value)) != len(value):
            _fail(f"Interpreter {name} {where} names unknown or repeated components.")
        used.update(value)
        return value

    if not keys(record["coreComponents"], "coreComponents"):
        _fail(f"Interpreter {name} must name its core components.")
    modules = record["moduleComponents"]
    if not isinstance(modules, dict):
        _fail(f"Interpreter {name} moduleComponents must be an object.")
    for module, value in modules.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", module):
            _fail(f"Interpreter {name} has an invalid module name {module!r}.")
        keys(value, f"module {module}")
    files = record["files"]
    if not isinstance(files, dict) or not files:
        _fail(f"Interpreter {name} lists no files.")
    names: set[str] = set()
    core_dll = f"python{record['version'].split('.')[0]}{record['version'].split('.')[1]}.dll"
    for path, entry in files.items():
        _safe_relative(path, "interpreter file path")
        if path.count("/") > 1 or ("/" in path and not path.startswith("DLLs/")) or not path.lower().endswith(_NATIVE):
            _fail(f"Interpreter {name} file {path} must be a top-level or DLLs native file.")
        base = PurePosixPath(path).name.casefold()
        if base in names:
            _fail(f"Interpreter {name} lists two files named {base}; bundled files would be ambiguous.")
        names.add(base)
        if not isinstance(entry, dict) or type(entry.get("size")) is not int \
                or not (isinstance(entry.get("sha256"), str) and _SHA256.fullmatch(entry["sha256"])):
            _fail(f"Interpreter {name} file {path} must record size and SHA-256.")
        if set(entry) == {"size", "sha256", "microsoftRuntime"}:
            if entry["microsoftRuntime"] is not True:
                _fail(f"Interpreter {name} file {path} has an invalid Microsoft runtime flag.")
        elif set(entry) == {"size", "sha256", "components"}:
            keys(entry["components"], f"file {path}")
        else:
            _fail(f"Interpreter {name} file {path} must list its components or be a Microsoft runtime file.")
    if core_dll not in names:
        _fail(f"Interpreter {name} must list its core DLL {core_dll}.")
    trees = record["dataTrees"]
    if not isinstance(trees, dict):
        _fail(f"Interpreter {name} dataTrees must be an object.")
    for root, tree in trees.items():
        _safe_relative(root, "data tree name")
        if "/" in root or not isinstance(tree, dict) or set(tree) != _TREE_KEYS:
            _fail(f"Interpreter {name} data tree {root} has missing or unknown fields.")
        _safe_relative(tree["source"], "data tree source")
        if not isinstance(tree["excludes"], list) or not all(_nonempty(item) for item in tree["excludes"]):
            _fail(f"Interpreter {name} data tree {root} has invalid excludes.")
        if type(tree["files"]) is not int or tree["files"] <= 0 or not (isinstance(tree["treeSha256"], str) and _SHA256.fullmatch(tree["treeSha256"])):
            _fail(f"Interpreter {name} data tree {root} must pin its file count and digest.")
        if not keys(tree["components"], f"data tree {root}"):
            _fail(f"Interpreter {name} data tree {root} names no components.")
    return used


# ---------------------------------------------------------------- matching


def find_interpreter(registry: Registry, interpreter: Interpreter) -> dict:
    """The audited record for exactly this version, platform and build tag."""
    for record in registry.interpreters:
        if (record["version"], record["platform"], record["build"]) == (interpreter.version, interpreter.platform, interpreter.build):
            return record
    audited = ", ".join(f"{r['version']} {r['platform']} ({r['build']})" for r in registry.interpreters)
    _fail(
        f"CPython {interpreter.version} {interpreter.platform} ({interpreter.build}) is not an audited interpreter, "
        f"so its bundled components and notices cannot be determined. Audited builds: {audited}. "
        "Add a reviewed registry record before packaging with it."
    )
    raise AssertionError  # unreachable


def verify_interpreter(record: dict, base: Path) -> None:
    """The build interpreter's own files must be the audited bytes."""
    licence = base / "LICENSE.txt"
    if not licence.is_file():
        _fail("The build interpreter has no LICENSE.txt.")
    _require_regular(licence)
    if _sha256_file(licence) != record["licenseFile"]["sha256"]:
        _fail(f"The build interpreter's LICENSE.txt differs from audited {record['id']}.")
    core = f"python{record['version'].split('.')[0]}{record['version'].split('.')[1]}.dll"
    for path, entry in record["files"].items():
        installed = base.joinpath(*path.split("/"))
        if not installed.exists() and not installed.is_symlink():
            if PurePosixPath(path).name == core:
                _fail(f"The build interpreter has no {core}.")
            continue  # an optional feature that is not installed
        _require_regular(installed)
        if not installed.is_file() or _sha256_file(installed) != entry["sha256"]:
            _fail(f"Build interpreter file {path} differs from audited {record['id']}.")


def tree_digest(directory: Path, excludes: Iterable[str] = ()) -> tuple[int, str]:
    """File count and digest of ``relative\\tsize\\tsha256`` lines (links refused)."""
    patterns = list(excludes)
    _require_regular(directory)
    lines: list[str] = []
    for current, subdirectories, names in os.walk(directory, followlinks=False):
        here = Path(current)
        for name in subdirectories:
            _require_regular(here / name)
        for name in names:
            path = here / name
            _require_regular(path)
            relative = path.relative_to(directory).as_posix()
            if any(fnmatch.fnmatch(part, pattern) for part in relative.split("/") for pattern in patterns):
                continue
            lines.append(f"{relative}\t{path.stat().st_size}\t{_sha256_file(path)}\n")
    lines.sort()
    return len(lines), _sha256_bytes("".join(lines).encode("utf-8"))


def _component_folder(key: str) -> str:
    """Short folder name: ``name-version``, a commit version cut to 12 characters."""
    name, version = key.split("@", 1)
    if _COMMIT.fullmatch(version):
        version = version[:12]
    return f"{name}-{version}"


def plan_cpython_notices(
    registry: Registry,
    interpreter: Interpreter,
    *,
    bundle_internal: Path,
    cpython_roots: Iterable[str],
    archive_names: Iterable[str],
    notice_root: str,
) -> CPythonPlan:
    """Match every bundled CPython root to the audited record and choose its texts.

    ``cpython_roots`` are the ``_internal`` top-level names (and archive module
    names) already attributed to CPython. Native files must equal an audited
    file of this interpreter; data folders must equal an audited data tree;
    other folders may hold no native code. Every component found is copied
    with all of its texts.
    """
    record = find_interpreter(registry, interpreter)
    verify_interpreter(record, interpreter.base)
    by_name = {PurePosixPath(path).name.casefold(): (path, entry) for path, entry in record["files"].items()}
    selected: dict[str, set[str]] = {key: {"CPython runtime"} for key in record["coreComponents"]}
    binaries: list[dict] = []
    trees: list[dict] = []
    python_roots: list[str] = []
    for root in sorted(set(cpython_roots)):
        path = bundle_internal / root
        if not path.exists() and not path.is_symlink():
            continue  # an archive (PYZ/CArchive) module name, covered by the core texts
        _require_regular(path)
        if path.is_file() and root.lower().endswith(_NATIVE):
            match = by_name.get(root.casefold())
            if match is None:
                _fail(f"Bundled CPython binary {root} is not part of audited {record['id']}.")
            audited_path, entry = match
            if "microsoftRuntime" in entry:
                _fail(f"Bundled {root} is a Microsoft runtime file and must not be attributed to CPython.")
            if path.stat().st_size != entry["size"] or _sha256_file(path) != entry["sha256"]:
                _fail(f"Bundled CPython binary {root} differs from audited {record['id']} ({audited_path}).")
            binaries.append({"path": root, "interpreterPath": audited_path, "sha256": entry["sha256"],
                             "components": list(entry["components"])})
            for key in entry["components"]:
                selected.setdefault(key, set()).add(root)
        elif path.is_dir() and root in record["dataTrees"]:
            tree = record["dataTrees"][root]
            count, digest = tree_digest(path)
            if (count, digest) != (tree["files"], tree["treeSha256"]):
                _fail(
                    f"Bundled data folder {root} ({count} files) does not match audited {record['id']} "
                    f"{tree['source']} ({tree['files']} files)."
                )
            trees.append({"path": root, "interpreterPath": tree["source"], "files": count, "treeSha256": digest,
                          "components": list(tree["components"])})
            for key in tree["components"]:
                selected.setdefault(key, set()).add(root)
        elif path.is_dir() and root in TCL_TK_DATA_ROOTS:
            _fail(f"Bundled Tcl/Tk data folder {root} is not an audited data tree of {record['id']}.")
        elif path.is_dir():
            native = [
                name for _, _, names in os.walk(path, followlinks=False) for name in names
                if name.lower().endswith(_NATIVE + (".so", ".exe"))
            ]
            if native:
                _fail(f"Bundled CPython folder {root} contains native code that no audited record covers.")
            python_roots.append(root)
        else:
            python_roots.append(root)  # e.g. base_library.zip: Python code under the core texts
    names = set(archive_names)
    for module, keys in record["moduleComponents"].items():
        if any(name == module or name.startswith(module + ".") for name in names):
            for key in keys:
                selected.setdefault(key, set()).add(f"module {module}")

    planned: list[PlannedText] = []
    components: list[dict] = []
    base = f"{notice_root}/python/cpython-{record['version']}/components"
    for key in sorted(selected):
        component = registry.components[key]
        copies = []
        for ref in component["texts"]:
            text = registry.texts[ref["text"]]
            source = _regular_chain(registry.text_root, ref["text"])
            destination = f"{base}/{_component_folder(key)}/{PurePosixPath(ref['text']).name}"
            planned.append(PlannedText(source, destination, text["sha256"], text["size"], ref["kind"]))
            copies.append({"path": destination, "size": text["size"], "sha256": text["sha256"], "kind": ref["kind"],
                           "sources": text["sources"]})
        item = {
            "id": key,
            "name": component["name"],
            "version": component["version"],
            "boundTo": sorted(selected[key]),
            "texts": copies,
            "attribution": list(component["attribution"]),
        }
        for optional in ("scope", "unresolved"):
            if optional in component:
                item[optional] = component[optional]
        components.append(item)
    inventory = {
        "interpreter": {key: record[key] for key in ("id", "version", "platform", "build", "sourceTag", "sourceCommit", "layout", "provenance")},
        "binaries": binaries,
        "dataTrees": trees,
        "pythonRoots": sorted(python_roots),
        "components": components,
        "unresolved": [f"{item['id']}: {item['unresolved']}" for item in components if "unresolved" in item],
    }
    return CPythonPlan(interpreter=record, inventory=inventory, texts=tuple(sorted(planned, key=lambda t: t.destination)))


def microsoft_identity(record: dict, bundled: Path) -> str | None:
    """Audited interpreter path whose bytes equal a bundled Microsoft runtime file, if any."""
    for path, entry in record["files"].items():
        if entry.get("microsoftRuntime") and PurePosixPath(path).name.casefold() == bundled.name.casefold():
            if bundled.is_file() and _sha256_file(bundled) == entry["sha256"]:
                return path
    return None
