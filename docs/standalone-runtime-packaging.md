# Standalone Windows runtime and installer

The installer includes a narrowly dispatched `qronos-runtime.exe`. Installed
builds use that bundled executable for the bridge, settings, permissions,
Library, memory and audio services. They do not discover Python or a source
checkout from the working directory, PATH or a build-machine path. Debug
builds retain the source-checkout development path.

## Build on Windows

Install CPython 3.14.8 for Windows x64 from the official python.org
installer, Node.js 24 and the Rust/MSVC prerequisites described in the
existing desktop documentation. Release builds of the backend accept only
CPython 3.14.8 with the exact audited files of that installer. Any other
version, including a later 3.14 release or a portable/embeddable package, is
refused before the build starts (see "CPython components" below). CPython
3.13.13 is also audited and stays usable for local development bundles, but
only with the explicit `--allow-audited-interpreter` option. Such a bundle's
manifest records that it was not built with the packaging target.

Open PowerShell in the Qronos repository folder. These commands create a
separate build environment, install the pinned dependencies, build the
backend and exercise its services with a new temporary profile:

```powershell
py -3.14 -m venv .venv-build
.\.venv-build\Scripts\python.exe --version
.\.venv-build\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
Set-Location desktop
npm ci --no-audit --no-fund
Set-Location ..
.\.venv-build\Scripts\python.exe -m build_support.build_runtime --destination runtime/package
.\.venv-build\Scripts\python.exe -m build_support.smoke_runtime --executable runtime/package/qronos-runtime/qronos-runtime.exe --profile runtime/smoke-profile
```

The `--version` line must print `Python 3.14.8`. If `py -3.14` selects another
3.14 release, use the full path of the 3.14.8 `python.exe` instead.

Before building the installer for the first time, prepare the pinned WebView2
prerequisite and 7-Zip on the build machine as described in
`docs/offline-windows-installer.md`. That one-time preparation is a
development download on the build machine. The finished installer carries
its own copy of WebView2, runs it only when WebView2 is missing, and downloads
nothing on the user's machine. Then build the NSIS installer:

```powershell
.\.venv-build\Scripts\python.exe -m build_support.build_desktop --backend runtime/package/qronos-runtime
```

Successful smoke output has `"ok": true` and reports bridge
startup/ping/shutdown as passed. The smoke test does not open a microphone,
camera or application, and does not load a model. It isolates settings,
history, permissions, storage and the user profile from the developer's data.
That isolated profile deliberately has no models or voice assets, so the smoke
output lists them as missing; this is expected and does not describe a
provisioned machine. The desktop's bounded service transport, and an optional test that exercises
a trusted frozen bundle through it, are described in
`bounded-desktop-service-ipc.md`.

The installer is written under `desktop/src-tauri/target/release/bundle/nsis`
unless `CARGO_TARGET_DIR` selects another build directory. Build logs and
generated runtime resources stay under ignored directories. For another
build, choose a new empty destination and smoke-profile directory; the
builder refuses to replace an existing runtime bundle silently.

The runtime manifest records every bundled file's SHA-256 and size. The
installer builder verifies the complete manifest before packaging and maps
the directory into `qronos-runtime/` using a generated Tauri resource overlay.
This detects stale, missing and accidentally modified build files. It does
not replace code signing or authenticate a maliciously rewritten manifest.

## Third-party notices in the backend

After PyInstaller finishes, and before the runtime manifest is written, the
builder adds a `third-party-notices/` folder to the bundle. It reads the
frozen executable's module archive and the `_internal` files, and maps each
one to the installed package that provides it. It then copies the license,
NOTICE and similar files that each of those packages ships. This includes
texts that package metadata does not declare, such as ONNX Runtime's
`ThirdPartyNotices.txt` and SciPy's license for its bundled libraries. The
build interpreter's CPython `LICENSE.txt` and PyInstaller's license are
copied as well. Packages that are installed in the build environment but not
bundled are not collected, and nothing is downloaded. `inventory.json` lists
each bundled distribution with its version, declared license, frozen
modules/files, native libraries and copied texts, using bundle-relative paths
only. The manifest covers these files like any other bundled file.

The build stops if a frozen module or file cannot be attributed to a package,
CPython, PyInstaller, Microsoft's C/C++ runtime or Qronos. It also stops if a
bundled package has no non-empty license text. AUTHORS, NOTICE, COPYRIGHT and
README files are copied as auxiliary notices but do not count as license
text, and neither does an empty or label-only LICENSE. No review record in
`build_support/runtime_notice_reviews.json` can replace missing license text.

Some wheels carry native binaries without the upstream license text. For
these, the missing text is kept in `build_support/runtime_notice_texts/`. It
is pinned to an exact upstream commit, public URL and SHA-256. The record also
lists the SHA-256 of every bundled binary it covers. The build checks that
each covered binary has the same hash in the package's RECORD, in the
installed package and in the bundle. It also checks that every bundled native
file of that package is covered and that each vendored text still has its
pinned hash. A record that matches no bundled package version stops the
build. Changing a text or upgrading the package therefore needs a new review.

The only such record today is for sounddevice 0.5.6. Its x64 PortAudio DLL is
byte-identical to the one in the published wheel and in the
`spatialaudio/portaudio-binaries` commit pinned by sounddevice's 0.5.6 tag.
That repository's workflow builds PortAudio v19.7.0
(commit `147dd722548358763a8b649b3e4b41dfffbcfbb6`). The vendored texts are
PortAudio's `LICENSE.txt` from that commit and the copyright lines copied
verbatim from the source files compiled into the DLL. The review record lists
the remaining open points: the statically linked Microsoft C runtime, a
Wine-derived constant with an informal BSD permission, and the fact that the
binary was not rebuilt independently.

### CPython components

CPython's `LICENSE.txt` does not contain every third-party text that a
Windows build compiles in or ships beside it. Examples are zlib or zlib-ng,
Expat, mpdecimal, HACL*, mimalloc, libb2, libtommath and the Zstandard library,
SQLite's public-domain dedication, and CPython's own `Doc/license.rst`
acknowledgements. `build_support/cpython_notice_registry.json` describes each
audited interpreter build:

- **Identity.** Version, platform and build tag (for example
  `tags/v3.13.13:01104ce`), the full source commit and the SHA-256 of its
  `LICENSE.txt`.
- **Native files.** Every `.dll` and `.pyd` that the official python.org
  installer puts in the interpreter folder or `DLLs/`, with its size and
  SHA-256, and the components compiled into it.
- **Data trees.** For Tcl/Tk 8.6, the file count and digest of the `_tcl_data`,
  `_tk_data` and `tcl8` folders PyInstaller copies from the interpreter's `tcl/`
  tree. Tcl/Tk 9 (CPython 3.14) keeps its script library inside `tcl90.dll`
  and `tcl9tk90.dll`, so those records have no data trees and the DLLs carry
  the Tcl and Tk components.
- **Module-bound components.** Components bound to frozen modules rather than
  files, such as `ctypes.macholib`, which PyInstaller collects through
  `ctypes.util`.
- **Components.** Each component pins byte-exact upstream texts in
  `build_support/cpython_notice_texts/` and gives each text a role:
  - `license-text`: the licence itself.
  - `terms-excerpt`: a verbatim source comment that carries the terms, such as
    SQLite's dedication or the HACL* MIT header.
  - `auxiliary`: README, AUTHORS or other attribution files.

  A component also records the exact copyright lines (which must appear in its
  own texts), its scope and any unresolved point.

The audited builds are CPython 3.13.13, 3.14.7 and 3.14.8 for win-amd64, in the
layout of the official python.org installer. That is also the layout
`actions/setup-python` installs. For these builds, the registry's hashes were
read from the official `core`, `exe`, `lib` and `tcltk` MSIs. Each text records
the verified sources it came from:

- a `cpython-source-deps` archive whose SHA-256 equals CPython's
  `Misc/externals.spdx.json`;
- a CPython source tarball verified by its python.org SHA-256 and Sigstore
  identity;
- an upstream repository file at a full commit;
- for the Unicode License, which has no versioned URL, the retrieval date.

At build time the collector checks the following before anything is written:

- The build interpreter's version, platform and build tag must be one of the
  audited records.
- The interpreter's `LICENSE.txt` and installed native files must have the
  audited bytes.
- Every bundled CPython `.dll`/`.pyd` must equal the audited file with the same
  name.
- Every Tcl/Tk data folder must match its audited tree.
- Any other CPython folder must contain no native code.

The texts of every component bound to the bundled files and modules are then
copied to `third-party-notices/python/cpython-<version>/components/`. They are
listed in `inventory.json` with path, size, SHA-256, role and sources, and the
runtime manifest seals them with the rest of the bundle.

Anything else stops the build with the exact item that did not match:

- an unknown interpreter, including a portable or embeddable package or a
  later 3.14 release;
- changed bytes;
- an unknown binary;
- a Tcl/Tk data folder without an audited tree.

The registry also names the packaging target:

- **Target:** CPython 3.14.8 win-amd64 (`tags/v3.14.8:8e6e75d`) from the
  official python.org installer. `build_runtime` checks the interpreter before
  PyInstaller starts. It refuses any build that is not audited, or whose files
  differ from the audited bytes. Each refusal starts with
  `Unsupported CPython build:`, which separates a support failure from the
  rights review described below.
- **Other audited builds:** a development bundle can use another audited build,
  such as 3.13.13, only with the explicit `--allow-audited-interpreter` option.
- **Manifest record:** `runtime-manifest.json` records the interpreter's
  version, build tag, source commit, audited record and whether it was the
  packaging target. The bundle's `inventory.json` records the same support
  status. It also marks the rights review as `owner-review-required`, because
  matching an audited build is not a licence or redistribution clearance.

To support a new interpreter build, audit it the same way and add a reviewed
record. The development-only assembler that produced the current records and
texts is kept outside the repository with the scratch evidence. The registry
loader itself refuses records that are inconsistent or unused, and texts whose
bytes changed. The registry only applies to Windows frozen packaging. Linux
unit tests exercise it with synthetic fixtures and never need a Windows
interpreter.

### Recorded rather than resolved

Some items are recorded rather than resolved:

- Microsoft runtime files (`vcruntime140*.dll`, `msvcp140*.dll`,
  `vcomp140.dll`, `ucrtbase.dll`, `api-ms-win-*.dll`) are listed with the
  package that delivered them and marked `review-required`. The inventory notes
  whether a bundled `vcruntime140*.dll` is byte-identical to the audited
  interpreter's own copy. That is an identity fact, not a redistribution right.
  Microsoft's distributable-code terms are not reproduced and remain an owner
  gate.
- Each CPython component's `unresolved` note is repeated in `inventory.json`
  and `README.txt`. These notes cover:
  - Zstandard's "BSD OR GPLv2" choice;
  - HACL*'s MIT headers next to its Apache-2.0 repository licence;
  - mimalloc's licence file, which comes from upstream because CPython omits it;
  - macholib's "MIT or PSF" statement;
  - the Unicode Character Database notice that CPython 3.13 does not document.

  The build ships every text involved and makes none of these choices.
- Qronos's own modules and configuration are listed as project-owned, with an
  unspecified license. Choosing that license is an owner decision.

This is technical notice packaging, not a legal clearance.

## Audio host APIs in the packaged backend

The sounddevice package ships PortAudio builds for 32-bit, x64 and ARM64
Windows, each with and without ASIO, plus a macOS library. The packaged
backend supports x64 Windows only. From that folder it keeps only
`libportaudio64bit.dll` and the upstream `README.md`. That DLL provides the
MME, DirectSound, WDM-KS and WASAPI host APIs, and is the one sounddevice
selects on 64-bit Windows when ASIO is not requested. Qronos's device
selection prefers WASAPI and does not use ASIO. Before PyInstaller starts, the
builder refuses to package on non-x64 Windows. The frozen backend also
removes an inherited `SD_ENABLE_ASIO` variable from its own process before
any audio code loads, so it cannot select an ASIO build. Audio devices that
are available only through ASIO are therefore not supported by the packaged
backend. Running from source, and the user's environment settings, are
unchanged. As before, sounddevice first looks for a PortAudio library on the
system search path and uses the bundled DLL only if none is found.

## Installed data and optional local assets

Writable settings, logs, memory, cache and temporary audio use the operating
system's local data directory, with a `Qronos` subdirectory (normally
`%LOCALAPPDATA%/Qronos` on Windows). Runtime code and default brain configuration
remain inside the installed bundle. Valid existing Library/history location
preferences still apply. Generated artifacts normally use `Documents/Qronos`;
when that location overlaps the program checkout or another protected path,
the validated default moves to `Documents/Qronos Files` and reports the change.
Existing files are not migrated automatically.

Storage-location changes are serialised with an operating-system lock that
older Qronos builds do not use. Older and newer versions running at the same
time therefore do not share a guaranteed lock: close every older Qronos
instance before installing this build or changing storage locations.

Approved native voice binaries and local models are provisioned separately
under `RuntimeAssets` in that local data directory, preserving the existing
`runtime/` and `models/` subdirectories. Expand **Local asset folder** in the
desktop's local setup report to read the effective path. An absolute
`QRONOS_ASSET_ROOT` can select an existing asset directory. No
development-machine models, private data or credentials are automatically
copied into an installer.

To inspect missing assets, run the installed backend's `doctor` service.
`doctor --models` also inspects names from the local Ollama server with a
bounded read-only request. It never starts Ollama, pulls or loads models.
Asset presence alone does not demonstrate model quality or hardware support.
At run time, a new GPU model load is deferred when the free video memory
cannot be confirmed to fit it. Qronos may already have unloaded its other
configured models while preparing the slot; no inference request is sent and
models outside Qronos's configuration are left untouched.

The desktop's local setup report lists each missing asset and configured model
role, with a read-only refresh. Wake-word provisioning includes the Qronos
head and its external data plus `models/openwakeword/melspectrogram.onnx`,
`embedding_model.onnx`, and `silero_vad.onnx` in that same folder. The loader
uses these explicit paths; it does not download package-default weights. The
three feature models are the official openWakeWord v0.5.1 release files that
the pinned `openwakeword` 0.6.0 package declares; the Qronos head is
project-specific and must be supplied separately.

The source build remains useful without these assets for text/UI setup, but
voice, wake-word and vision acceptance requires the corresponding approved
assets. Wake-word training/data quality, model redistribution licenses,
installer signing, updates and clean-machine/live hardware acceptance remain
release requirements. This build is not a signed public release.

### Reviewed runtime asset identities

`build_support/runtime_assets.json` lists every asset the setup report checks (and the whisper DLLs beside
them).

- A **verified** entry carries the size and SHA-256 its official publisher publishes. That is the Hugging Face
  LFS object id, or for the openWakeWord v0.5.1 release files, the official release file re-downloaded and hashed.
- An **unverified** entry carries no hash at all, because no published identity matches the local file.
- A **missing** entry is configured but absent.
- Each entry states its licence position. "owner-review-required" marks terms this project has not settled. Note
  that openWakeWord's README places its pre-trained models under CC BY-NC-SA 4.0.

Check an asset folder, read only (nothing is opened beyond hashing, followed, started or downloaded):

```powershell
.\.venv-build\Scripts\python.exe -m build_support.runtime_assets --root "$env:LOCALAPPDATA\Qronos\RuntimeAssets"
```

Each entry is reported as verified, mismatch, unverified, missing or refused. Refused means the file is not a
regular file, or the file, the root or a folder between them is a link, or the file Windows opened is not the
intended one. A registry path must be plainly relative: no drive, root, UNC or device prefix, `..`, stream or
reserved name.
`--require-verified` exits 1 unless every entry is present and verified; that is the release gate for assets.

State on 2026-10-12:
- **Verified (6):**
  - the three openWakeWord feature models;
  - `ggml-large-v3-turbo-q5_0.bin`;
  - `ggml-silero-v6.2.0.bin`;
  - `chatterbox-s3gen-q8_0.gguf`.
- **Unverified (5):**
  - the whisper.cpp binaries: a local 1.9.3-dev build tuned to the build machine's CPU, so not portable;
  - `crispasr.exe`;
  - the Persian `t3-fa-q4_k.gguf`: most likely a local quantization of the published f16 model.
- **Missing (3):**
  - the project's wake-word head and its data;
  - `whisper-server.exe`.
