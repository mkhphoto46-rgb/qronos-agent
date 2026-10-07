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

## Installed data and optional local assets

Writable settings, logs, memory, cache and temporary audio use
`%LOCALAPPDATA%/Qronos`. Runtime code and default brain configuration remain
inside the installed bundle. Existing Library/history location preferences
still apply; generated user artifacts default to `Documents/Qronos`.

Approved native voice binaries and local models are provisioned separately
under `%LOCALAPPDATA%/Qronos/RuntimeAssets`, preserving the existing `runtime/`
and `models/` subdirectories. An absolute `QRONOS_ASSET_ROOT` can select an
existing asset directory. No development-machine models, private data or
credentials are automatically copied into an installer.

To inspect missing assets, run the installed backend's `doctor` service.
`doctor --models` also inspects names from the local Ollama server with a
bounded read-only request. It never starts Ollama, pulls or loads models.
Asset presence alone does not demonstrate model quality or hardware support.

The desktop's local setup report lists each missing asset and configured model
role, with a read-only refresh. Wake-word provisioning includes the Qronos
head and its external data plus `models/openwakeword/melspectrogram.onnx`,
`embedding_model.onnx`, and `silero_vad.onnx` in that same folder. The loader
uses these explicit paths; it does not download package-default weights.

The source build remains useful without these assets for text/UI setup, but
voice, wake-word and vision acceptance requires the corresponding approved
assets. Wake-word training/data quality, model redistribution licenses,
installer signing, updates and clean-machine/live hardware acceptance remain
release requirements. This build is not a signed public release.
