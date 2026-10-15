"""Prepare the Android SDK for the STANDARD Gradle build of mobile/android, in a private folder (no PATH change).

The packages and their official archive pins are in sdk-packages.json beside this file. They are under the Android
SDK License Agreement (https://developer.android.com/studio/terms; the pinned text hash is in sdk-packages.json).
Installing requires the explicit flag --accept-android-sdk-license, which only the owner's approval may authorise.
No sdkmanager, Android CLI or other downloaded tool is run, and no licences/ marker is written.

    python mobile/android/buildcheck/sdk_setup.py plan
        Prints the pins and what is present under the SDK root. Touches nothing.
    python mobile/android/buildcheck/sdk_setup.py fetch
        Downloads the pinned archives into the archive cache and checks size + SHA-1.
    python mobile/android/buildcheck/sdk_setup.py licences
        Shows which licence the install accepts and where its terms are. Runs nothing, writes nothing.
    python mobile/android/buildcheck/sdk_setup.py install --accept-android-sdk-license
        Unpacks the VERIFIED platform and build-tools archives directly into platforms/android-36 and
        build-tools/36.0.0. Every entry is checked before anything is written: the expected archive root only, no
        absolute path, no '..', no link. A package already present at its pinned revision is left as it is; one at
        another revision is never overwritten. It then FAILS unless each installed source.properties has exactly one
        Pkg.Revision equal to the pin (revision agreement only, not byte identity of every installed file).

The build itself (Root names it separately) then runs with ANDROID_HOME set for that one process only:
    ANDROID_HOME=<sdk root> mobile/android/gradlew -p mobile/android :app:assembleDebug
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

# subprocess is imported only so a fixture can prove it is never used by install.
import subprocess  # noqa: F401

PINS = json.loads((Path(__file__).with_name("sdk-packages.json")).read_text(encoding="utf-8"))
SDK_ROOT = Path(PINS["sdkRoot"])
CACHE = Path(PINS["archiveCache"])
INSTALL = ("platforms;android-36", "build-tools;36.0.0")
#: The single top-level folder of each official archive (read from the verified archives); it becomes the package path.
ARCHIVE_ROOTS = {"platforms;android-36": "android-36", "build-tools;36.0.0": "android-16"}
LICENCE_FLAG = "--accept-android-sdk-license"


def _archive(name: str) -> Path:
    return CACHE / PINS["packages"][name]["url"].rsplit("/", 1)[1]


def _verified(name: str) -> bool:
    path, pin = _archive(name), PINS["packages"][name]
    if not path.is_file() or path.stat().st_size != pin["size"]:
        return False
    digest = hashlib.sha1()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest() == pin["sha1"]


def plan() -> int:
    print(json.dumps({"sdkRoot": str(SDK_ROOT), "sdkRootExists": SDK_ROOT.exists(), "archiveCache": str(CACHE),
                      "licence": PINS["licence"],
                      "packages": {name: {**pin, "archiveVerified": _verified(name)}
                                   for name, pin in PINS["packages"].items()},
                      "installed": {name: installed_revision(name) for name in INSTALL}}, indent=1))
    return 0


def fetch() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, pin in PINS["packages"].items():
        if _verified(name):
            continue
        partial = _archive(name).with_suffix(".part")
        urllib.request.urlretrieve(pin["url"], partial)  # noqa: S310 - fixed official https URL from the pin file
        partial.replace(_archive(name))
        if not _verified(name):
            _archive(name).unlink()
            raise SystemExit(f"{name}: the download is not the pinned archive (size or SHA-1); removed.")
    print("all archives verified")
    return 0


def licences() -> int:
    licence = PINS["licence"]
    print(f"install {LICENCE_FLAG} accepts the {licence['id']} (Android SDK License Agreement, "
          f"https://developer.android.com/studio/terms; pinned text sha256 {licence['textSha256']}) for: "
          + ", ".join(f"{name} {PINS['packages'][name]['revision']}" for name in INSTALL)
          + ". Pass the flag only with the owner's approval. Nothing was run or written.")
    return 0


class UnsafeArchive(ValueError):
    """An archive entry that would write outside its package, or a link, or the wrong root."""


def _members(archive: zipfile.ZipFile, root: str) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Every file entry as (info, path inside the package), after checking ALL entries; nothing is written."""
    files = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if name.startswith("/") or path.is_absolute() or ":" in name or ".." in path.parts:
            raise UnsafeArchive(f"unsafe entry {info.filename!r}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise UnsafeArchive(f"link entry {info.filename!r}")
        if not path.parts or path.parts[0] != root:
            raise UnsafeArchive(f"entry {info.filename!r} is outside the expected root {root!r}")
        if not info.is_dir() and len(path.parts) > 1:
            files.append((info, PurePosixPath(*path.parts[1:])))
    if PurePosixPath("source.properties") not in {inner for _, inner in files}:
        raise UnsafeArchive("the archive has no source.properties")
    return files


def _check(name: str) -> tuple[str | None, bool]:
    """(failure reason or None, whether it needs unpacking): every check, and nothing written."""
    pin = PINS["packages"][name]
    target = SDK_ROOT.joinpath(*name.split(";"))
    if target.exists() or target.is_symlink():
        found = installed_revision(name)
        if found is not None and _revision(found) == _revision(pin["revision"]):
            return None, False  # already installed at the pinned revision: left as it is
        return f"{target} already exists at revision {found!r}; it is not overwritten", False
    if not _verified(name):
        return f"{_archive(name)} is missing or is not the pinned archive (size or SHA-1)", False
    try:
        with zipfile.ZipFile(_archive(name)) as archive:
            _members(archive, ARCHIVE_ROOTS[name])
    except (UnsafeArchive, zipfile.BadZipFile) as error:
        return str(error), False
    return None, True


def _unpack(name: str) -> None:
    """Install one package from its verified, already checked archive."""
    target = SDK_ROOT.joinpath(*name.split(";"))
    with zipfile.ZipFile(_archive(name)) as archive:
        files = _members(archive, ARCHIVE_ROOTS[name])  # checked again on the same bytes
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=target.parent))
        try:
            for info, inner in files:
                destination = staging.joinpath(*inner.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(destination, "xb") as sink:
                    shutil.copyfileobj(source, sink)
            staging.rename(target)  # the package appears whole, or not at all
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)  # only the staging folder this call created
            raise


def install(accept_licence: bool = False) -> int:
    if not accept_licence:
        print(f"refused: the Android SDK License Agreement is not accepted (pass {LICENCE_FLAG} only with the owner's "
              "approval). Nothing was written.")
        return 2
    checked = {name: _check(name) for name in INSTALL}  # every package is checked before anything is written
    failures = {name: reason for name, (reason, _) in checked.items() if reason is not None}
    if not failures:
        for name, (_, needed) in checked.items():
            if needed:
                _unpack(name)
    # The installed revision must be the pinned one: revision agreement only, not byte identity of every file.
    found = {name: installed_revision(name) for name in INSTALL}
    agreed = {name: found[name] is not None and _revision(found[name]) == _revision(PINS["packages"][name]["revision"])
              for name in INSTALL}
    print(json.dumps({"pinned": {name: PINS["packages"][name]["revision"] for name in INSTALL},
                      "installed": found, "agreed": agreed, "failures": failures}))
    return 0 if all(agreed.values()) and not failures else 1


def _revision(text: str) -> tuple[int, ...] | None:
    """A dotted numeric revision with trailing zero parts dropped ("36" == "36.0.0"); anything else is None."""
    parts = text.strip().split(".")
    if not 1 <= len(parts) <= 4 or not all(part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers)


def installed_revision(name: str) -> str | None:
    """Pkg.Revision of an installed package's source.properties; None if missing, unreadable or malformed."""
    path = SDK_ROOT.joinpath(*name.split(";")) / "source.properties"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    values = [line.split("=", 1)[1].strip() for line in lines if line.strip().startswith("Pkg.Revision=")]
    return values[0] if len(values) == 1 and _revision(values[0]) is not None else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=("plan", "fetch", "licences", "install"))
    parser.add_argument(LICENCE_FLAG, dest="accept", action="store_true",
                        help="install only: accept the pinned Android SDK License Agreement (owner approval needed)")
    args = parser.parse_args(argv)
    if args.step == "plan":
        return plan()
    if args.step == "fetch":
        return fetch()
    if args.step == "licences":
        return licences()
    return install(accept_licence=args.accept)


if __name__ == "__main__":
    sys.exit(main())
