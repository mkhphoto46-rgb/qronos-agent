# Standalone Windows runtime and installer

The installer includes a narrowly dispatched `qronos-runtime.exe`. Installed
builds use that bundled executable for the bridge, settings, permissions,
Library, memory and audio services. They do not discover Python or a source
checkout from the working directory, PATH or a build-machine path. Debug
builds retain the source-checkout development path.

## Build on Windows

Install Python 3.14, Node.js 24 and the Rust/MSVC prerequisites described in
the existing desktop documentation. Open PowerShell in the Qronos repository
folder. These commands create a separate build environment, install the
pinned dependencies, build the backend, exercise its services with a new
temporary profile, and then build the NSIS installer:

```powershell
py -3.14 -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
Set-Location desktop
npm ci --no-audit --no-fund
Set-Location ..
.\.venv-build\Scripts\python.exe -m build_support.build_runtime --destination runtime/package
.\.venv-build\Scripts\python.exe -m build_support.smoke_runtime --executable runtime/package/qronos-runtime/qronos-runtime.exe --profile runtime/smoke-profile
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
bundled package ships no license or notice text. An exception must be
recorded for the exact package name and version, with its provenance, in
`build_support/runtime_notice_reviews.json`. The same applies to native
binaries whose wheel lacks the upstream license text. The file currently holds
one record: the PortAudio DLL in sounddevice 0.5.6, whose wheel names the MIT
License but does not include its text.

Some items are recorded rather than resolved:

- Microsoft runtime files (`vcruntime140*.dll`, `msvcp140*.dll`,
  `vcomp140.dll`, `ucrtbase.dll`, `api-ms-win-*.dll`) are listed with the
  package that delivered them and marked `review-required`. Microsoft's
  distributable-code terms are not reproduced.
- CPython's `LICENSE.txt` is copied unchanged. For each bundled CPython
  component (such as OpenSSL, SQLite, Tcl/Tk, zlib, XZ, Expat and mpdecimal)
  the inventory records whether that text names it. Components it does not
  name need review.
- Qronos's own modules and configuration are listed as project-owned, with an
  unspecified license. Choosing that license is an owner decision.

This is technical notice packaging, not a legal clearance.

## Audio host APIs in the packaged backend

The sounddevice package ships PortAudio builds for 32-bit, x64 and ARM64
Windows, each with and without ASIO. The packaged backend supports x64
Windows only. It keeps only `libportaudio64bit.dll`, which provides the MME,
DirectSound, WDM-KS and WASAPI host APIs. Qronos's device selection prefers
WASAPI and does not use ASIO. The builder refuses to package on non-x64
Windows. The frozen backend also removes an inherited `SD_ENABLE_ASIO`
variable from its own process before any audio code loads, so it cannot
select an ASIO build. Audio devices that are available only through ASIO are
therefore not supported by the packaged backend. Running from source, and the
user's environment settings, are unchanged. As before, sounddevice first
looks for a PortAudio library on the system search path and uses the bundled
DLL only if none is found.

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
