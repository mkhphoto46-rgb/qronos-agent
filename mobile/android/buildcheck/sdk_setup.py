"""Prepare the Android SDK for the STANDARD Gradle build of mobile/android, in a private folder (no PATH change).

The packages and their official archive pins are in sdk-packages.json beside this file. They are all under the
Android SDK License Agreement. This helper NEVER accepts it: no "y" is piped, and no licences/ file is written. The
owner accepts it, if they decide to, by answering sdkmanager's own prompt (step "licences").

    python mobile/android/buildcheck/sdk_setup.py plan
        Prints the pins and what is present under the SDK root. Touches nothing.
    python mobile/android/buildcheck/sdk_setup.py fetch
        Downloads the pinned archives into the archive cache and checks size + SHA-1 (only once the owner has approved).
    python mobile/android/buildcheck/sdk_setup.py licences --java-home <JDK 17+>
        Unpacks the verified command-line tools into <sdk root>/cmdline-tools/latest and runs
        `sdkmanager --sdk_root=<sdk root> --licenses` interactively: the OWNER reads and answers.
    python mobile/android/buildcheck/sdk_setup.py install --java-home <JDK 17+>
        `sdkmanager --sdk_root=<sdk root> "platforms;android-36" "build-tools;36.0.0"`, then checks that both are
        present at the pinned revisions. sdkmanager refuses this until the licence was accepted.

The build itself (Root names it separately) then runs with ANDROID_HOME set for that one process only:
    ANDROID_HOME=<sdk root> mobile/android/gradlew -p mobile/android :app:assembleDebug
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PINS = json.loads((Path(__file__).with_name("sdk-packages.json")).read_text(encoding="utf-8"))
SDK_ROOT = Path(PINS["sdkRoot"])
CACHE = Path(PINS["archiveCache"])
INSTALL = ("platforms;android-36", "build-tools;36.0.0")


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
                      "licencesFolderPresent": (SDK_ROOT / "licenses").exists()}, indent=1))
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


def _sdkmanager(java_home: Path) -> tuple[list[str], dict[str, str]]:
    if not _verified("cmdline-tools;latest"):
        raise SystemExit("run `fetch` first: the command-line tools archive is missing or not the pinned one")
    tools = SDK_ROOT / "cmdline-tools" / "latest"
    if not (tools / "bin" / "sdkmanager.bat").is_file():
        with zipfile.ZipFile(_archive("cmdline-tools;latest")) as archive:
            for member in archive.namelist():  # the zip holds cmdline-tools/...; it belongs under .../latest
                if not member.startswith("cmdline-tools/") or ".." in Path(member).parts:
                    raise SystemExit(f"unexpected archive entry {member!r}")
            archive.extractall(SDK_ROOT / "cmdline-tools" / "_unpack")
        (SDK_ROOT / "cmdline-tools" / "_unpack" / "cmdline-tools").replace(tools)
        (SDK_ROOT / "cmdline-tools" / "_unpack").rmdir()
    env = dict(os.environ, JAVA_HOME=str(java_home))  # this process only; nothing global
    return [str(tools / "bin" / "sdkmanager.bat"), f"--sdk_root={SDK_ROOT}"], env


def licences(java_home: Path) -> int:
    command, env = _sdkmanager(java_home)
    print("The Android SDK License Agreement follows. Accepting it is the owner's decision; this helper answers nothing.")
    return subprocess.call(command + ["--licenses"], env=env)  # interactive: stdin is the owner's terminal


def install(java_home: Path) -> int:
    command, env = _sdkmanager(java_home)
    code = subprocess.call(command + list(INSTALL), env=env)
    if code:
        return code
    found = {name: (SDK_ROOT.joinpath(*name.split(";")) / "source.properties").is_file() for name in INSTALL}
    print(json.dumps(found))
    return 0 if all(found.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=("plan", "fetch", "licences", "install"))
    parser.add_argument("--java-home", type=Path)
    args = parser.parse_args(argv)
    if args.step in ("licences", "install") and not args.java_home:
        parser.error("--java-home is required for sdkmanager")
    if args.step == "plan":
        return plan()
    if args.step == "fetch":
        return fetch()
    return (licences if args.step == "licences" else install)(args.java_home)


if __name__ == "__main__":
    sys.exit(main())
