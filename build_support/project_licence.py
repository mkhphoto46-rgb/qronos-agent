"""Qronos's own licence and NOTICE, exactly as every distribution carries them (build time only).

The source owned by the Qronos project is licensed under the Apache License,
Version 2.0. LICENSE at the repository root is the official text, byte for byte
(https://www.apache.org/licenses/LICENSE-2.0.txt). Its SHA-256 is pinned here,
so an edited or replaced licence stops the build instead of shipping. NOTICE
scopes that licence to the project-owned source and names the third-party
material that keeps its own terms; it must say both.

The runtime notices (``runtime_notices``) and the desktop notices
(``desktop_notices``) each carry both files in a ``qronos/`` folder, and the
installer check (``desktop_installer_verify``) requires them in both places.
Nothing here grants, changes or relicenses any third-party licence.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPDX = "Apache-2.0"
LICENSE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
LICENSE_BYTES = 11358
FOLDER = "qronos"
FILES = ("LICENSE", "NOTICE")
MAX_NOTICE_BYTES = 64 * 1024
#: What NOTICE must state: the licence of the project-owned source, and that third-party material is outside it.
NOTICE_STATEMENTS = ("licensed under the Apache License, Version 2.0", "are NOT covered by that licence")


class ProjectLicenceError(ValueError):
    """The project licence or NOTICE is missing, altered or does not say what it must."""


def check(name: str, data: bytes) -> dict[str, object]:
    """Size and SHA-256 of a valid LICENSE or NOTICE; refuses anything else."""
    if name == "LICENSE":
        if len(data) != LICENSE_BYTES or hashlib.sha256(data).hexdigest() != LICENSE_SHA256:
            raise ProjectLicenceError("LICENSE is not the official Apache License 2.0 text (pinned SHA-256).")
    elif name == "NOTICE":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ProjectLicenceError("NOTICE is not UTF-8 text.") from None
        words = " ".join(text.split())  # line wrapping does not matter
        if not 0 < len(data) <= MAX_NOTICE_BYTES or not all(statement in words for statement in NOTICE_STATEMENTS):
            raise ProjectLicenceError("NOTICE must state the Apache-2.0 scope and that third-party material is "
                                      "not covered by it.")
    else:
        raise ProjectLicenceError(f"{name!r} is not a project licence file.")
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _plain(path: Path) -> Path:
    if path.is_symlink() or os.path.isjunction(path) or not path.is_file():
        raise ProjectLicenceError(f"{path.name} is missing or is not a plain file.")
    return path


def sources(root: Path = ROOT) -> dict[str, Path]:
    """The checked LICENSE and NOTICE at the repository root."""
    found = {name: _plain(Path(root) / name) for name in FILES}
    for name, path in found.items():
        check(name, path.read_bytes())
    return found


def read(root: Path = ROOT) -> dict[str, bytes]:
    """The checked bytes of LICENSE and NOTICE at the repository root."""
    data = {name: path.read_bytes() for name, path in sources(root).items()}
    for name, value in data.items():
        check(name, value)  # re-checked: the bytes returned are the bytes validated
    return data


def verify_folder(folder: Path) -> dict[str, dict[str, object]]:
    """A distributed ``qronos/`` folder holds exactly a valid LICENSE and NOTICE; returns their evidence."""
    folder = Path(folder)
    if folder.is_symlink() or os.path.isjunction(folder) or not folder.is_dir():
        raise ProjectLicenceError(f"The project licence folder {folder.name!r} is missing or is a link.")
    names = sorted(entry.name for entry in folder.iterdir())
    if names != sorted(FILES):
        raise ProjectLicenceError(f"The project licence folder holds {names}, not exactly {sorted(FILES)}.")
    return {name: check(name, _plain(folder / name).read_bytes()) for name in FILES}
