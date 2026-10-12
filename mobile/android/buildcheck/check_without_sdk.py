"""
Compile and package the Android app without the Android SDK, as evidence.

The Android SDK's platform and build-tools packages are distributed under the
Android SDK licence, which has to be accepted; this project does not accept
it on anyone's behalf. Every tool used here is published under an
open-source licence with no acceptance step:

    android-all   org.robolectric:android-all:16-robolectric-13921718
                  (Maven Central, Apache 2.0) - Android 16 / API 36, built
                  from AOSP source; the platform API to compile against
    aapt2         com.android.tools.build:aapt2:9.4.1-15978811 (Google Maven,
                  Apache 2.0) - the resource compiler AGP 9.4.1 itself uses
    d8            com.android.tools:r8:9.4.28 (Google Maven, BSD-3-Clause)
    kotlinc, JDK  Kotlin 2.4.21 (Apache 2.0), any JDK 17+
    bctls/bcprov/bcutil, org.json   as in linkcore/build.gradle.kts

It does what AGP would do for a debug build, by hand: compile and link the
resources (injecting the namespace, SDK levels and version from
app/build.gradle.kts), generate R, compile linkcore and the app's Kotlin
against the platform API, dex everything for minSdk 26, and put the dex into
the linked resource package. The result is an UNSIGNED APK: proof that the
sources, resources and manifest build against Android 16. It is not signed,
not installed and not distributed, and an unsigned APK cannot be installed.

    python mobile/android/buildcheck/check_without_sdk.py --jdk <jdk home>
        --kotlinc <kotlinc home> --aapt2 <aapt2 executable>
        --android-all <jar> --r8 <jar> --maven <dir with the five jars>
        --out <empty build directory>

Run from the repository root. Prints the APK's badging and its SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

APP = Path("mobile/android/app/src/main")
CORE = Path("mobile/android/linkcore/src/main/kotlin")

NAMESPACE = "app.qronos.remote"
MIN_SDK, TARGET_SDK = "26", "36"
VERSION_CODE, VERSION_NAME = "1", "0.1.0"

JARS = (
    "bctls-jdk18on-1.86.2.jar",
    "bcprov-jdk18on-1.86.jar",
    "bcutil-jdk18on-1.86.jar",
)
JSON_JAR = "json-20260814.jar"


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    completed = subprocess.run(command, capture_output=True, text=True, **kwargs)

    if completed.returncode != 0:
        sys.stderr.write(completed.stdout + completed.stderr)
        raise SystemExit(f"failed: {Path(command[0]).name}")

    return completed


def kotlinc(home: Path, jdk: Path, classpath: list[Path], out: Path, sources: list[Path]) -> None:
    # An argument file: kotlinc's Windows launcher splits ';' in a classpath.
    # Forward slashes, because kotlinc reads backslashes in it as escapes.
    args = out.parent / f"{out.name}.args"
    joined = os.pathsep.join(path.resolve().as_posix() for path in classpath)
    lines = ["-jvm-target", "17", "-cp", f'"{joined}"', "-d", f'"{out.resolve().as_posix()}"']
    lines += [source.as_posix() for source in sources]
    args.write_text("\n".join(lines), encoding="utf-8")

    launcher = home / "bin" / ("kotlinc.bat" if os.name == "nt" else "kotlinc")
    run([str(launcher), f"@{args}"], env={**os.environ, "JAVA_HOME": str(jdk)}, shell=False)


def jar(jdk: Path, source: Path, target: Path) -> None:
    run([str(jdk / "bin" / "jar"), "cf", str(target), "-C", str(source), "."])


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("jdk", "kotlinc", "aapt2", "android-all", "r8", "maven", "out"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    options = parser.parse_args()

    out = options.out
    out.mkdir(parents=True, exist_ok=True)

    if any(out.iterdir()):
        raise SystemExit(f"{out} must be empty")

    framework = options.android_all
    maven = options.maven
    jdk = options.jdk

    # 1. Resources, as AGP links them.
    manifest = (APP / "AndroidManifest.xml").read_text(encoding="utf-8").replace(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">',
        f'<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="{NAMESPACE}">',
        1,
    )
    (out / "AndroidManifest.xml").write_text(manifest, encoding="utf-8")
    run([str(options.aapt2), "compile", "--dir", str(APP / "res"), "-o", str(out / "res.zip")])
    (out / "gen").mkdir()
    run([
        str(options.aapt2), "link", "-I", str(framework),
        "--manifest", str(out / "AndroidManifest.xml"), "--java", str(out / "gen"),
        "--min-sdk-version", MIN_SDK, "--target-sdk-version", TARGET_SDK,
        "--version-code", VERSION_CODE, "--version-name", VERSION_NAME,
        "-o", str(out / "resources.ap_"), str(out / "res.zip"),
    ])

    # 2. R, linkcore, then the app, against the platform API.
    (out / "r").mkdir()
    r_java = next((out / "gen").rglob("R.java"))
    run([str(jdk / "bin" / "javac"), "-nowarn", "-d", str(out / "r"), "-cp", str(framework), str(r_java)])

    deps = [maven / name for name in JARS]
    (out / "linkcore").mkdir()
    kotlinc(options.kotlinc, jdk, deps + [maven / JSON_JAR], out / "linkcore", sorted(CORE.rglob("*.kt")))
    (out / "app").mkdir()
    kotlinc(
        options.kotlinc, jdk,
        [framework, out / "r", out / "linkcore"] + deps,
        out / "app",
        sorted((APP / "kotlin").rglob("*.kt")),
    )

    # 3. Dex for minSdk 26. org.json is not packaged: Android provides it.
    for name in ("app", "r", "linkcore"):
        jar(jdk, out / name, out / f"{name}.jar")

    (out / "dex").mkdir()
    run([
        str(jdk / "bin" / "java"), "-cp", str(options.r8), "com.android.tools.r8.D8",
        "--release", "--min-api", MIN_SDK, "--lib", str(framework), "--lib", str(jdk),
        "--output", str(out / "dex"),
        str(out / "app.jar"), str(out / "r.jar"), str(out / "linkcore.jar"),
        str(options.kotlinc / "lib" / "kotlin-stdlib.jar"),
        *map(str, deps),
    ])

    # 4. Dex into the linked resource package: an unsigned APK.
    apk = out / "qronos-remote-unsigned.apk"
    shutil.copyfile(out / "resources.ap_", apk)

    with zipfile.ZipFile(apk, "a") as archive:
        for dex in sorted((out / "dex").glob("classes*.dex")):
            archive.write(dex, dex.name, compress_type=zipfile.ZIP_STORED)

    badging = run([str(options.aapt2), "dump", "badging", str(apk)]).stdout
    print(badging.strip())
    print("sha256", hashlib.sha256(apk.read_bytes()).hexdigest(), apk.name, apk.stat().st_size)
    print("UNSIGNED - build evidence only; not signed, installed or distributed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
