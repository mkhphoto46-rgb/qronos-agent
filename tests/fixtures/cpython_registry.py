"""
A small, self-consistent CPython notice registry for tests.

The committed registry describes real python.org builds, so a test that wants
to change one byte, drop a text or invent an unaudited interpreter needs its
own copy. ``RegistryBuilder`` writes synthetic upstream texts and a registry
whose interpreter record pins the exact bytes of a fake interpreter folder, so
every check in ``build_support.cpython_notices`` runs against real files.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

VERSION = "3.13.13"
PLATFORM = "win-amd64"
BUILD = "tags/v3.13.13:01104ce"
COMMIT = "01104ce1beb3135c2e0c01ec835b994c1f55a1c0"

TEXTS = {
    "zlib/LICENSE": b"Synthetic zlib licence.\n (C) 1995-2024 Example Authors\nPermission is granted for tests.\n",
    "zlib/README": b"Synthetic zlib readme naming Example Authors.\n",
    "bzip2/LICENSE": b"Synthetic bzip2 licence, copyright (C) 1996-2019 Example Seward.\n",
    "sqlite/sqlite3.h.leading-comment.txt": b"/* The author disclaims copyright to this synthetic source code. */",
    "cpython/Doc/license.rst": b"Synthetic incorporated-software acknowledgements.\n",
    "tcl/license.terms": b"This synthetic software is copyrighted by Example Regents and other parties.\n",
    "macholib/README.ctypes": b"Synthetic: may be distributed under the MIT or PSF open source licenses.\n",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive_source(member: str) -> dict:
    return {
        "kind": "archive-member",
        "url": "https://github.com/python/cpython-source-deps/archive/refs/tags/example-1.0.tar.gz",
        "archiveSha256": "a" * 64,
        "member": f"example-1.0/{member}",
        "pinnedBy": [f"CPython {VERSION} Misc/externals.spdx.json @ {COMMIT}"],
    }


class RegistryBuilder:
    """Writes texts under ``root/texts`` and returns registry dictionaries."""

    def __init__(self, root: Path, interpreter: Path):
        self.root = root
        self.text_root = root / "texts"
        self.interpreter = interpreter
        for text_id, data in TEXTS.items():
            path = self.text_root / text_id
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def text_record(self, text_id: str) -> dict:
        data = (self.text_root / text_id).read_bytes()
        return {"size": len(data), "sha256": _sha(data), "sources": [archive_source(text_id)]}

    def file_record(self, relative: str, components: list[str] | None) -> dict:
        data = (self.interpreter / relative).read_bytes()
        record = {"size": len(data), "sha256": _sha(data)}
        if components is None:
            record["microsoftRuntime"] = True
        else:
            record["components"] = components
        return record

    def raw(self, files: dict[str, list[str] | None], data_trees: dict | None = None) -> dict:
        licence = (self.interpreter / "LICENSE.txt").read_bytes()
        return {
            "schema": 1,
            "comment": "Synthetic registry for tests.",
            "texts": {text_id: self.text_record(text_id) for text_id in TEXTS},
            "components": {
                "zlib@1.3.1": {"name": "zlib", "version": "1.3.1", "attribution": ["(C) 1995-2024 Example Authors"],
                               "texts": [{"text": "zlib/LICENSE", "kind": "license-text"},
                                         {"text": "zlib/README", "kind": "auxiliary"}]},
                "bzip2@1.0.8": {"name": "bzip2", "version": "1.0.8", "attribution": ["copyright (C) 1996-2019 Example Seward."],
                                "texts": [{"text": "bzip2/LICENSE", "kind": "license-text"}]},
                "sqlite@3.50.4": {"name": "SQLite", "version": "3.50.4", "attribution": [],
                                  "texts": [{"text": "sqlite/sqlite3.h.leading-comment.txt", "kind": "terms-excerpt"}]},
                "cpython-incorporated@3.13.13": {"name": "Incorporated software", "version": VERSION, "attribution": [],
                                                 "texts": [{"text": "cpython/Doc/license.rst", "kind": "license-text"}]},
                "tcl@8.6.15": {"name": "Tcl", "version": "8.6.15", "attribution": [],
                               "texts": [{"text": "tcl/license.terms", "kind": "license-text"}]},
                "macholib@3.13.13": {"name": "macholib", "version": "1.0", "attribution": [], "unresolved": "MIT or PSF choice not made.",
                                     "texts": [{"text": "macholib/README.ctypes", "kind": "terms-excerpt"}]},
            },
            "interpreters": [{
                "id": "synthetic-3.13.13",
                "implementation": "CPython",
                "version": VERSION,
                "platform": PLATFORM,
                "build": BUILD,
                "sourceTag": f"v{VERSION}",
                "sourceCommit": COMMIT,
                "layout": "synthetic installer layout",
                "provenance": "test fixture",
                "licenseFile": {"path": "LICENSE.txt", "size": len(licence), "sha256": _sha(licence)},
                "coreComponents": ["cpython-incorporated@3.13.13"],
                "moduleComponents": {"ctypes.macholib": ["macholib@3.13.13"]},
                "files": {path: self.file_record(path, components) for path, components in files.items()},
                "dataTrees": data_trees or {},
            }],
        }

    def write(self, raw: dict) -> Path:
        path = self.root / "registry.json"
        path.write_text(json.dumps(raw, indent=1), encoding="utf-8")
        return path

    def variant(self, raw: dict) -> dict:
        return copy.deepcopy(raw)
