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
| WebView2 caches | `%LOCALAPPDATA%\com.qronos.agent` | No. The pinned template does not offer Tauri's "Delete the application data" choice. The UI keeps no state there (no `localStorage`, `sessionStorage` or IndexedDB). |
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
  - such an identifier, which stock Tauri's "Delete the application data"
    would remove. The pinned template does not offer that choice; the guard
    stays.

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
  past. Links and junctions in the part of the path that exists are resolved
  by Windows (`GetFinalPathNameByHandleW`), for the install folder and for every
  protected folder below, so a protected folder reached or redirected through
  a junction is still recognised. Resolution fails closed. The part that does
  not exist yet is kept as written only when Windows reports it as not found
  below a folder that was resolved. Anything else refuses the install: a folder
  Windows denies access to, a name it rejects, a missing drive, or a final path
  too long for the installer. This applies to the install folder and to every
  protected folder (a network Documents folder is compared as written, since
  the install folder must be a local path without links). The check runs on
  the directory page (so an
  unsafe folder is refused before Tauri creates it) and again before any file
  is installed; a silent install skips that page, so Tauri may already have
  created the empty folder. The installer refuses a folder that:
  - is, sits inside, or contains `%LOCALAPPDATA%\Qronos`;
  - is, sits inside, or contains `Documents\Qronos` or `Documents\Qronos Files`;
  - is, sits inside, or contains the `com.qronos.agent` app-data folders;
  - is, sits inside, or contains `%USERPROFILE%\.ollama`, or the
    `QRONOS_DATA_ROOT` or `OLLAMA_MODELS` folders;
  - is the user profile, Documents, Desktop, AppData, `Programs`, Windows or
    Program Files folder itself;
  - is a drive's top folder, a drive-relative path, a network share or a
    device name such as `CON` or `NUL.txt`;
  - is, or is reached through, any link or junction;
  - already contains files and has no valid Qronos manifest.

  NSIS itself removes `:` from the install path, so alternate data streams
  cannot be named.
- **Replace, never mix.** On an upgrade or reinstall, once Qronos is not
  running, the hook deletes exactly the files the previous installation's
  manifest lists. It then removes listed folders that are left empty, and
  only then are the new files copied. A file that an older version had and the
  new version does not cannot linger. Files the manifest does not list, such
  as a user's own, are never touched.
- **Fail closed.** The whole manifest is checked before anything is deleted.
  The removal stops without deleting anything when:
  - any line is not a plain relative path under the same rules as the build
    (`build_support/install_manifest.check_entry`). That excludes `..`, `:`,
    `/`, the wildcards `*` and `?`, `"`, `<`, `>`, `|`, tab, CR or LF, a drive,
    a leading, trailing or doubled backslash, names Windows would change, and
    device names;
  - the header is not Qronos's;
  - any folder on the way to any listed entry, or the entry itself, is a link
    or junction. This is checked on disk for every entry, whatever folder
    lines the list contains;
  - a listed file is really a folder, or a listed folder is really a file;
  - the install folder itself is reached through a link, or Windows cannot
    open it or a folder on the way to it.

  The list is opened once for both passes, not through a link, and with a
  sharing mode that lets no other program write, rename or delete it while it
  is open. If another program already has it open for writing, nothing is
  removed. So the deletion pass reads exactly the bytes the first pass
  checked. That guarantee comes from the lock, not from reading the list twice.
  The deletion pass still checks the header and every line again (kind, path
  rules and disk) before acting on it, and checks each path on disk right
  before its removal. A path can still change on disk between that last check
  and the removal itself; the checks narrow this window but do not close it.

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
  build has no manifest. Its folder is refused as "not a Qronos installation".
  The installer refuses the "uninstall first" choice and never runs an old
  uninstaller, so remove such a build first with its own uninstaller (Windows
  Settings > Apps).
- The desktop's startup check still confirms only the runtime executable, its
  folder and protocol version, not every file in the runtime manifest.
  Stopping at startup when there are stray runtime files belongs to the
  desktop runtime code, which another worker owns.
- `CheckIfAppIsRunning` from Tauri's template is used before the removal. It is
  absent in the probe harness, so the "Qronos is running" path is covered only
  by a file held open during a probe run, not by a real running app.

## The update fence, hand-over and receipt

The installer's half of the update hand-over in `local-update-packages.md` is
made of three parts:
- `installer-hooks/qronos-update-fence.nsh`;
- the pinned installer template `desktop/src-tauri/installer-template/`
  (`PROVENANCE.md` there lists every change to Tauri 2.9.4's own template);
- the hook order in `webview2-offline.nsh`.

The contracts are `work/opus-production-installer-contract-round10.txt` and
`work/opus-production-installer-contract-round11.txt`, with the round-12
corrections. `build_desktop` refuses Tauri's stock template, verifies the pinned
source, and checks the order in the real generated `installer.nsi`.

**Order of an installation.** Before NSIS runs the Install section, only
`.onInit`, the template's no-op checks and the stock WebView2 section (which
only reads the registry in "skip" mode) run. In the GUI, the pages also run;
the reinstall page never starts a previous uninstaller (see below). The
Install section then runs, in this order:
1. The update fence and the EXCLUSIVE store admission (update hand-over or
   manual install).
2. The install-root check (protected folders and ancestors, links, foreign
   content).
3. An interrupted earlier update: an empty leftover folder is removed,
   anything else refuses (see "Update transaction" below).
4. The previous installation's whole file list, checked against the disk.
5. The WebView2 child, before anything of the previous installation changes.
6. Stopping the running app.
7. The update transaction begins: the previous listed files are SET ASIDE
   (kept byte for byte) and every path of the new payload is claimed.
8. Only then `SetOutPath`, the copy, the uninstaller, the registration and the
   shortcuts, then the commit and, in update mode, the receipt.

On a refusal in steps 1-5, no folder or file under the install folder, the start
menu or the desktop is created, changed or removed (except an empty leftover
`.qronos-update` folder in step 3), nothing is stopped and the installer writes no
registry value. Two things can still exist:
- NSIS's own temporary plug-in folder in `%TEMP%`;
- the admission lock file `<data root>\.qronos-store-admission.lock`, opened
  (`OPEN_ALWAYS`) only when the data folder exists.

**What is held.** Two things:
- the update fence: the named mutex `Local\Qronos.UpdateInProgress.v1`;
- the exclusive store admission: a byte-range lock on the admission file, the
  same lock `core/store_admission.py` uses.

Both handles are non-inheritable and never closed by the script. They last
exactly as long as the installer or uninstaller process, whether it ends
normally, aborts, crashes or is killed. Nothing it starts can keep them.

**Manual install or uninstall.** The fence and the admission must both be free
at once. Otherwise the installer stops with "Another Qronos update or
installation is running" or "Qronos is still running with its data open". An
absent data folder skips only the admission.

**Update mode** (`<installer> /S /QRONOSUPDATE`, started only by the updater):
- `/QRONOSUPDATE` without `/S` is refused. The uninstaller refuses
  `/QRONOSUPDATE`. Update mode never falls back to a manual install.
- The private record on standard input is bounded: 1023 bytes, 30 s.
- The guard must be the installer's real parent:
  - If it can be opened, its creation time must match, and it must exit within
    60 s before anything is installed.
  - If it cannot be opened, only proof that it no longer exists (error 87)
    counts as "gone" (a guard that crashed). Then the fence identity is the
    proof. Access denied or any other failure refuses before the
    acknowledgement (independent review Media15, F15-1).
- The handed fence handle must be another handle to the fence object.
- The receipts folder must be this installer's own
  `<data root>\Updates\receipts`. The data root is derived by the installer.
- The version must be the installer's own.

**Explicit success and the receipt.** In a silent install, NSIS 3.11 lets a
failed `File`, `CreateDirectory`, `WriteUninstaller`, registry write or
`CreateShortcut` continue with only the error flag set, and `${GetSize}` clears
that flag. So:
- every one of those statements, including each iteration of the resource,
  program and deep-link loops and each registry write, is followed
  IMMEDIATELY by `QRONOS_STEP`, which stops the installation;
- `ClearErrors` appears only right after a checked step.

The build refuses a generated script that breaks this. In update mode the
receipt (`<pid>-<ticks>.receipt`, `CREATE_NEW`) is written only after
`QRONOS_INSTALL_COMPLETE`, holding the nonce, the approved package and manifest
SHA-256, the installer's own version and `result=installed`. A stopped
installation leaves a partial install and no receipt. The start-up barrier
then reports the update honestly (unconfirmed or not applied).

**Deletion.** The install-root check and the owned file list come before
EVERY deletion, every app stop and every registration or shortcut change.
- The installer deletes nothing else. The stock deletion of an "old main
  program" named in the registry is removed.
- The uninstaller removes only the listed files and folders, plus
  `uninstall.exe` by its exact name. The stock deletion of the program,
  resources, binaries and resource folders is removed.
- The "Delete the application data" checkbox (a recursive folder removal) is
  not offered.
- The reinstall page never runs a previous uninstaller, a WiX/MSI uninstaller
  or any child. "Uninstall first" and an MSI registration are refused visibly;
  passive mode continues in place.

**Writes outside the Install section, for the record.** Modern UI saves the
chosen language in the registry at the end of a successful installation
(skipped after an abort). In the GUI, the finish page can create the desktop
shortcut and start the app. Both happen in the same installer process, after
the section, while the fence is still held.

**Tests** (inert, silent, current-user probes in temporary folders; never a
real installer, registry, shortcut or user folder):
- `tests/test_update_fence_nsis.py` (the fence file in small probes);
- `tests/test_installer_template.py`, which compiles the WHOLE pinned template
  with Tauri's own compiler. Its rendering is proven equal to Tauri's real
  output. The registry and shell folders are swapped for files in the test
  folder, and every swap is counted. It covers:
  - refusals before any change;
  - the reinstall page;
  - a real failure at the FIRST command of every step group while later
    commands would succeed;
  - a real `UpdateService` hand-over;
  - a guard that cannot be opened;
  - protected roots, links, junctions and missing or malformed file lists with
    foreign files (nothing changed);
  - uninstalls that remove only listed files.

**Limits**
- NSIS releases the fence's ownership when its install thread ends. The fence
  object and the admission lock last until the process exits. The app started
  from the manual finish page can meet a live fence for a moment; Runtime's
  start-up must retry briefly (contract round 10, section 5, R2).
- None of this has run as a real Tauri-built installer on a real machine; see
  below. The default updater launch stays refused.

**Update transaction** (installer-hooks/qronos-update-transaction.nsh). A
failed update leaves the previously installed program usable.
- The installer creates `<install folder>\.qronos-update` after the fence, the
  exclusive admission and the install-root checks. Its journal records, before
  each action takes effect, the exact file identity (volume serial and file
  index) the installer itself observed:
  - each previous listed file it moves aside to `.qronos-update\old`;
  - each new folder it creates;
  - each new file path it claims (created new, never opened if something is
    already there).
  Then come "W" (writes may begin) and "C" (committed).
- **No mixing, no overwriting.** A new payload path that is already occupied
  refuses the update before anything new is written. Unlisted files are
  never moved, overwritten or deleted.
- **Normal failure.** Any failed step, a failed commit, or any other stop after
  the transaction began puts the previous version back. It deletes only new
  files whose current identity is the claimed one, removes only empty created
  folders, and moves each set-aside file back by handle when its identity
  matches and its place is free. In the same run it also restores the Apps &
  features values, the install-location value and the previous Start-menu and
  desktop shortcuts. No receipt is written.
- **Trust.** The running installer keeps its own journal open (shared for
  reading only) from creation to commit or rollback, so no other program can
  change it. Only that run's rollback deletes the new files it claimed.
- **Commit and receipt.** "C" is written only after every step succeeded and
  the installed file list equals the installer's own list (no file written
  without a claim). Then the set-aside files are deleted (each only if it is
  still that file). The update receipt follows the commit. If it cannot be
  written, the new version stays installed and the start-up barrier reports
  the update as unconfirmed, never as confirmed.
- **Interrupted work.** A crash or kill leaves `.qronos-update` behind. Nothing
  in it can be proved to be that installer's own: anyone can write a journal
  header, file identities can be observed and a file list can be copied. So
  the next installer never acts on it.
  - An empty folder is removed.
  - Anything else is refused, with nothing moved or deleted. The message says
    that the previous version's files are kept under `.qronos-update\old` at
    their own paths.
  - Recovery is then a manual step: move those files back or restore a backup,
    or reinstall into an empty folder.
  A journal header alone never makes a folder a Qronos installation: the
  set-aside Qronos file list must be there too. The uninstaller refuses while
  the folder exists.
- **Limits.**
  - Atomicity across power loss is not claimed (each step is one NTFS
    operation, decided from what exists, but this was not tested with power
    loss).
  - Registry values are restored only in the same run, not after a crash.
  - A shortcut the failed run newly created is left; it names the same program
    path.
  - Any interrupted update needs manual recovery (above). So does a commit
    whose clean-up of the kept files failed: the update itself is complete,
    but the leftover folder must be removed by hand before the next update.
  - An installer whose own list missed a file it writes would leave that one
    unclaimed file after a rollback. build_desktop makes both lists from one
    file and verifies the package against it, so a shipped installer has no
    such gap.
  - Tested only with inert fixture installers (tests/test_installer_update_transaction.py),
    never with a real installation.

**Open installer gates** (independent review Media18; nothing here is
activated):
- **O-A, availability: repaired by the update transaction (round 19,
  awaiting independent review).** Before, the previous installation's
  listed files were removed before the WebView2 child and every later
  step, so a failure left no usable program. Now the WebView2 child comes
  first and the previous files are set aside, then restored after any
  failure (see "Update transaction"). The update launch and any release stay
  gated until that repair is independently accepted.
- **O-B, defence in depth.** `verify_generated_order` does not inspect the
  sections that run before the Install section. That they do nothing rests on
  the configuration checks: `check_tauri_config` (WebView2 "skip", no minimum
  WebView2 version) and the empty-define check.
- **O-C, shortcuts.** The updater starts the installer with
  `/S /QRONOSUPDATE` but not `/UPDATE`, so Tauri's update mode stays off. A
  silent Qronos update therefore creates or refreshes the Start-menu and
  desktop shortcuts, including one the person deleted. Whether to pass
  `/UPDATE` (or `/NS`) is an open product decision.

## Updates and rollback

Qronos does not use Tauri's network updater. Updates are new installers that a
person gets through the approved browser or on removable media. They are
checked by the authenticated local update software in `core/local_update.py`,
documented in `local-update-packages.md`:
- an owner-signed manifest binds the version, architecture, product and
  installer hash;
- the version, rollback, security-floor and data-schema rules are enforced;
- a verified copy is staged for the user-approved install step.

It accepts no package until the owner adds a public key.

`build_desktop` keeps refusing `createUpdaterArtifacts` and an `updater` plugin.
In the locked bundler, an updater build switches the installer to the WebView2
download bootstrapper. Tauri's updater would also need an update feed on the
internet, beyond the current offline-first exceptions.

A rollback means installing an older package that the owner signed explicitly
as a rollback, at or above the security floor, that can still read the installed
data format. The installer itself allows a downgrade after a warning. Nothing
rolls user data back automatically.

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
6. Upgrading over an older Qronos in place. Choosing "uninstall first" on the
   reinstall page should be refused visibly, with nothing changed.
   `%LOCALAPPDATA%\Qronos` and the Library should be unchanged afterwards.
7. Uninstalling. No "Delete the application data" choice should be shown.
   `%LOCALAPPDATA%\Qronos` and the Library should remain; WebView2 should
   remain installed.
8. The update fence inside a real Tauri-built installer:
   - a manual install, a reinstall and an uninstall, each while Qronos runs
     and while it is closed;
   - an update hand-over (`/S /QRONOSUPDATE`) and its receipt.
   The default updater launch stays refused until this and the Runtime/Control
   wiring are accepted.

## Owner decisions still open

- **Redistribution:** Microsoft's terms for redistributing the WebView2 Runtime
  and its standalone installer have not been reviewed or accepted.
- **Signing:** the Qronos installer is unsigned. Public release needs an
  Authenticode certificate and a signing step.
- **Keeping the pin current:** the pinned runtime ages. Installed copies update
  themselves when online; fresh offline installs get 154.0.4258.62 until the pin
  is replaced in a reviewed change.
- **Installer size:** the installer grows by about 212 MB.
