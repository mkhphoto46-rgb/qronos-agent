# Offline Windows installer: the WebView2 prerequisite

The Qronos desktop app draws its window with Microsoft Edge WebView2. Windows 11
and most up-to-date Windows 10 machines already have the WebView2 Runtime. A
clean or long-offline Windows 10 machine may not.

Before this change, the installer used Tauri's default `downloadBootstrapper`
mode. On a machine without WebView2, that mode downloads a small Microsoft
bootstrapper during installation, which then downloads the runtime. With no
internet connection, the installation stopped.

The installer now carries the WebView2 Runtime inside it and installs it only
when it is missing.

- **Installing:** never downloads anything.
- **Building:** downloads nothing either, but only after the one-time developer
  preparation below has fetched the WebView2 installer, the Cargo crates and
  Tauri's NSIS tools. The build checks that all of them are already present
  and refuses to start otherwise; it does not quietly fetch them.

This is technical packaging. It does not grant permission to redistribute
Microsoft software, and it does not sign the Qronos installer. Both are owner
decisions, listed at the end.

## Why this approach

These findings come from the locked tools: `@tauri-apps/cli` 2.11.4, which is
built with `tauri-bundler` 2.9.4, and `tauri` 2.11.5.

| Tauri `webviewInstallMode` | What the locked code does | Usable here? |
| --- | --- | --- |
| `downloadBootstrapper` (default) | Downloads Microsoft's bootstrapper at install time; offline installs abort | No |
| `embedBootstrapper` | Embeds the bootstrapper, which still downloads the runtime | No |
| `offlineInstaller` | Contacts Microsoft's moving `fwlink` address on every build and embeds whatever version it finds, with no hash or version pin. The updater variant of an installer silently switches to the download bootstrapper. | No pinned provenance |
| `fixedRuntime` | Ships a fixed runtime copy beside the app. It never receives WebView2 security updates. Per-user installs need extra file permissions for the browser sandbox. | Possible, harder to keep secure |
| `skip` plus a Qronos NSIS hook | Embeds an exact, verified copy of Microsoft's Evergreen Standalone Installer. It runs only when WebView2 is missing. Once installed, Microsoft keeps the runtime updated whenever the machine is online. | **Chosen** |

Microsoft documents the Evergreen Standalone Installer as the way to deploy
WebView2 without internet access:
https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution

## What is pinned

`build_support/webview2_prerequisite.json` records the exact file:

- **File:** `MicrosoftEdgeWebView2RuntimeInstallerX64.exe`, x64 only, for
  `x86_64-pc-windows-msvc`.
- **Size and hash:** 212,358,352 bytes, SHA-256
  `ac22ecdc19c5b88b87f3fa752c00da9541653a8f5c0c5fc4a3b2b6ebe6591f69`.
- **Download address:** Microsoft's own delivery URL for that file. It was
  resolved from the official `fwlink` on 2026-10-08.
- **Runtime version:** the installer's internal offline manifest declares
  WebView2 Runtime **154.0.4258.62** for app ID
  `{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`. The outer file version
  (1.3.275.13) belongs to the Microsoft Edge Update setup program, not to the
  runtime.
- **Signature:** an Authenticode signature by Microsoft Corporation, issued by
  Microsoft Code Signing PCA 2024. `signtool verify /pa` confirmed it on
  2026-10-08.

The large file itself is not stored in Git. Each developer downloads it once
into the ignored `.cache/wv2/` folder.

## One-time preparation

1. Open **PowerShell**.
2. Go to the Qronos repository folder, for example:

   ```powershell
   Set-Location "C:\path\to\Qronos"
   ```

3. Create the cache folder and download the pinned file. This and steps 6 and
   7 are the one-time preparation steps that use the internet:

   ```powershell
   New-Item -ItemType Directory -Force .cache\wv2 | Out-Null
   curl.exe -L -o .cache\wv2\MicrosoftEdgeWebView2RuntimeInstallerX64.exe "https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/18c66e75-0385-4019-be3f-3f4a2bb867d5/MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
   ```

4. Check the file. This runs offline:

   ```powershell
   .\.venv-build\Scripts\python.exe -m build_support.desktop_prerequisites
   ```

   Success prints one line of JSON containing `"authenticode": "valid (WinVerifyTrust)"`
   and the SHA-256 above. Anything else ends with an error message and exit
   code 2. Do not build with that file. If Microsoft no longer serves the pinned
   address, the pin has to be reviewed and replaced; never edit the hash to
   match a different file.

5. Install **7-Zip** from https://www.7-zip.org/ if it is not installed. The
   build uses it only to read the finished installer, not to install anything.
   If `7z.exe` is not in `C:\Program Files\7-Zip`, set the `QRONOS_7ZIP`
   environment variable to its full path.

6. Fetch the Rust crates once. The build runs Cargo with `--offline`, so it
   fails instead of downloading a missing crate:

   ```powershell
   Set-Location desktop\src-tauri
   cargo fetch --locked
   Set-Location ..\..
   ```

7. Prepare Tauri's NSIS tools once. On a machine where it has never built an
   installer, the locked Tauri bundler (`tauri-bundler` 2.9.4, inside
   `@tauri-apps/cli` 2.11.4) downloads two files from GitHub during the build.
   `build_desktop` refuses to start until they are already in place:

   - NSIS 3.11:
     `https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.11/nsis-3.11.zip`
     (SHA-1 `EF7FF767E5CBD9EDD22ADD3A32C9B8F4500BB10D`). Unpack it so that
     `%LOCALAPPDATA%\tauri\NSIS\makensis.exe` exists. The zip contains a
     folder `nsis-3.11`; rename it to `NSIS`.
   - `nsis_tauri_utils.dll` v0.5.3:
     `https://github.com/tauri-apps/nsis-tauri-utils/releases/download/nsis_tauri_utils-v0.5.3/nsis_tauri_utils.dll`
     (SHA-1 `75197FEE3C6A814FE035788D1C34EAD39349B860`), saved as
     `%LOCALAPPDATA%\tauri\NSIS\Plugins\x86-unicode\additional\nsis_tauri_utils.dll`.

   Check the hashes with `Get-FileHash -Algorithm SHA1 <file>` before using the
   files. Any machine that has built a Tauri NSIS installer before already has
   this folder.

   The preflight checks every file the locked bundler requires and the plug-in's
   exact SHA-1. It also stops if `@tauri-apps/cli` is no longer 2.11.4, because
   a newer bundler may have different download rules.

## Building

Follow `standalone-runtime-packaging.md` to build and smoke-test the backend.
Then build the installer, preferably with a short Cargo build folder, because
long folder paths have hit the Windows path-length limit:

```powershell
.\.venv-build\Scripts\python.exe -m build_support.build_desktop --backend runtime/package/qronos-runtime --target-dir C:\qb\t
```

The command prints the installer path and the verified prerequisite when it
succeeds.

`build_support.build_desktop` checks the following, and stops with an error at
the first failure.

**Before building**
- The installer type is NSIS, on Windows x64. MSI and non-Windows bundles are
  refused, because only the NSIS hook carries the offline prerequisite.
- `tauri.conf.json` uses `webviewInstallMode: skip` and the Qronos hook.
- No updater artifacts and no minimum WebView2 version are configured. Either
  would make the installer contact Microsoft.
- The cached WebView2 installer has the pinned size and SHA-256, a valid
  signature by the pinned Microsoft certificate (thumbprint, subject and
  issuer) checked offline, and no link in its path.
- Tauri's NSIS tool folder holds every file the locked bundler needs, with the
  pinned `nsis_tauri_utils.dll` SHA-1, and `@tauri-apps/cli` is still 2.11.4.
  Otherwise Tauri would download NSIS during the build.
- 7-Zip is available and the backend manifest verifies.
- Cargo then runs with `--locked --offline`.

**During the build**
- It creates a new folder under `runtime/package/desktop-builds/` (ignored by
  Git) with the third-party notices, the generated hook wrapper and the Tauri
  overlay. Nothing is deleted or overwritten.

**After building**
- Tauri's generated `installer.nsi` defines no WebView2 download or embed mode
  and no minimum version, and it includes this build's hook.
- The installer contains the WebView2 installer exactly once, at
  `$PLUGINSDIR\MicrosoftEdgeWebView2RuntimeInstallerX64.exe`.
- The installer contains no NSIS download plug-in (`NSISdl.dll`, `INetC.dll`).
  The previous review installer contained `NSISdl.dll`.
- The embedded file, extracted again, has exactly the pinned SHA-256.

The finished installer is moved out of Tauri's output folder into the build's own folder before
any of these checks run. The only way it can sit there unmarked is beside its provenance record
(`installer-provenance.json`), after every check has passed. If anything fails (the Tauri build,
a check, extraction or the provenance record), the installer is renamed `*.UNVERIFIED`. An
installer already lying at Tauri's output path from an earlier or manual build is renamed the
same way before the build starts. Nothing is deleted. Never use a `*.UNVERIFIED` file.

A plain `npm run tauri build` fails on purpose with *"Qronos installers must be
built with build_support.build_desktop"*. It can therefore never produce an
installer without the prerequisite.

## What the installer does

The hook is `desktop/src-tauri/installer-hooks/webview2-offline.nsh`. Before it
copies Qronos files, it reads the WebView2 version from the registry, using
Microsoft's documented keys: per-machine, then per-user. `0.0.0.0` counts as
missing.

- **If WebView2 is present:** nothing happens.
- **If it is missing on an x64 machine:** the hook unpacks the embedded
  installer into the installer's temporary folder and runs it with
  `/silent /install`. It then checks the registry again.
- **If it is missing on another kind of machine:** installation stops with a
  clear message.

A failed or unregistered runtime install stops the Qronos installation; it does
not leave a half-working app. Uninstalling Qronos does not remove WebView2,
because other applications share it.

## Upgrading, uninstalling and user data

These findings come from the locked NSIS template (`tauri-bundler` 2.9.4) and
the Qronos source. None of this has been run on a real machine.

**Where things live**

| What | Where | Removed by the uninstaller? |
| --- | --- | --- |
| Program files (per-user install, Tauri's default) | `%LOCALAPPDATA%\qronos-desktop` | Only the files the installer itself placed there, then the empty folders |
| Settings, history, memory, permissions, approval secret, runtime assets | `%LOCALAPPDATA%\Qronos` (from `core/config.py` and the desktop's `local_data_dir()`) | No |
| Library and other user documents | the folders the user chose, by default under `Documents` | No |
| WebView2 caches | `%LOCALAPPDATA%\com.qronos.agent` | Only when the user ticks "Delete the application data". The UI keeps no state there (no `localStorage`, `sessionStorage` or IndexedDB). |
| WebView2 Runtime | Windows (shared by many apps) | No |

- **Uninstalling keeps Qronos user data.** Removing it is a manual step, which
  privacy-sensitive users need to be told about.
- **The same or an older version:** the installer warns and asks; downgrading
  is possible.
- **Build-time guard:** `build_desktop` refuses a configuration that would put
  user data into a folder the installer or uninstaller manages:
  - a `productName` that is, or that Windows resolves to, "Qronos" (for
    example `Qronos.` or `QRONOS~1`), which would make the per-user install
    folder `%LOCALAPPDATA%\Qronos`;
  - such an identifier, which "Delete the application data" would remove.

**What the installer enforces**

Every installation carries `qronos-install-manifest.txt`. It lists every file
the installer puts in the program folder, plus the folders, deepest first.
`build_desktop` generates it from the runtime and notice manifests. After
building, it checks that the extracted installer holds exactly the listed
files.

The installer hook (`installer-hooks/qronos-install-root.nsh`) then enforces
the following before anything is installed.

- **The install folder must be Qronos's own.** It is compared in its canonical
  Windows form, so trailing dots or spaces and 8.3 short names cannot slip
  past. The installer refuses a folder that:
  - is, sits inside, or contains `%LOCALAPPDATA%\Qronos`;
  - is, sits inside, or contains `Documents\Qronos` or `Documents\Qronos Files`;
  - is, sits inside, or contains the `com.qronos.agent` app-data folders;
  - is, sits inside, or contains `%USERPROFILE%\.ollama`, or the
    `QRONOS_DATA_ROOT` or `OLLAMA_MODELS` folders;
  - is the user profile, Documents, Desktop, AppData, `Programs`, Windows or
    Program Files folder itself;
  - is a drive's top folder, a drive-relative path, a network share or a
    device name such as `CON` or `NUL.txt`;
  - is a junction;
  - already contains files and has no valid Qronos manifest.

  NSIS itself removes `:` from the install path, so alternate data streams
  cannot be named.
- **Replace, never mix.** On an upgrade or reinstall, once Qronos is not
  running, the hook deletes exactly the files the previous installation's
  manifest lists. It then removes listed folders that are left empty, and
  only then are the new files copied. A file that an older version had and the
  new version does not cannot linger. Files the manifest does not list, such
  as a user's own, are never touched.
- **Fail closed.** The removal stops before deleting anything when the manifest:
  - has any line that is not a plain relative path (with `..`, `:`, `/`, a
    drive or a leading backslash);
  - has a header that is not Qronos's;
  - lists a folder that is a junction.

  If a listed file cannot be deleted, for example because Qronos is still
  running, the installation stops and the manifest is kept, so running the
  installer again finishes the job.
- **Uninstalling** runs the same removal after Tauri's own steps, then removes
  the program folder only if it is empty.

These rules are exercised by `tests/test_desktop_install_root.py`. It compiles
the real hook functions into a throwaway probe and runs them against
disposable fixture folders, standing in for the user's profile, AppData and
Documents. It never runs the Qronos installer or reads real user folders.

**Limits**
- An installation from before this manifest existed, such as the 7584 review
  build, has no manifest. Its folder is refused as "not a Qronos installation"
  unless Tauri's "uninstall first" choice has emptied it. Uninstall such a
  build first.
- The desktop's startup check still confirms only the runtime executable, its
  folder and protocol version, not every file in the runtime manifest.
  Stopping at startup when there are stray runtime files belongs to the
  desktop runtime code, which another worker owns.
- `CheckIfAppIsRunning` from Tauri's template is used before the removal. It is
  absent in the probe harness, so the "Qronos is running" path is covered only
  by a file held open during a probe run, not by a real running app.

## Updates and rollback

There is no in-app updater. Updates are new installers, delivered and run by
the user, for example offline from a USB drive.

Adding Tauri's updater is a separate decision that needs:
- an owner-held signing key (Qronos must never generate or store one in the
  repository);
- a signed update feed, which is an internet endpoint beyond the current
  offline-first exceptions;
- a fix for the locked bundler's behaviour: an updater build switches the
  installer to the WebView2 download bootstrapper.

`build_desktop` refuses `createUpdaterArtifacts` and an `updater` plugin until
that design exists.

Rollback is only possible by reinstalling an older signed installer. The
installer allows a downgrade after a warning. Nothing rolls back user data
automatically, and schema migrations of user data are not reversible by
reinstalling.

## Not yet verified on a real machine

These checks need a clean Windows virtual machine, and they wait until live
acceptance testing is allowed:

1. Installing offline on Windows 10 x64 with no WebView2 Runtime. The runtime
   should install, and Qronos should open.
2. The same as a standard (non-administrator) user. Microsoft's package asks for
   administrator rights where it can (`needsadmin="prefers"`); whether a
   per-user install happens silently has not been observed.
3. Installing on a machine that already has WebView2. Nothing should be
   reinstalled.
4. An ARM64 machine without WebView2. The refusal message should appear.
5. A Windows SmartScreen warning is expected while the Qronos installer is
   unsigned.
6. Upgrading over an older Qronos, with both "uninstall first" choices.
   `%LOCALAPPDATA%\Qronos` and the Library should be unchanged afterwards.
7. Uninstalling with and without "Delete the application data".
   `%LOCALAPPDATA%\Qronos` and the Library should remain; WebView2 should
   remain installed.

## Owner decisions still open

- **Redistribution:** Microsoft's terms for redistributing the WebView2 Runtime
  and its standalone installer have not been reviewed or accepted.
- **Signing:** the Qronos installer is unsigned. Public release needs an
  Authenticode certificate and a signing step.
- **Keeping the pin current:** the pinned runtime ages. Installed copies update
  themselves when online; fresh offline installs get 154.0.4258.62 until the pin
  is replaced in a reviewed change.
- **Installer size:** the installer grows by about 212 MB.
