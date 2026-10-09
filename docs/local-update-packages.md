# Authenticated local update packages

Qronos never downloads updates. The person installing an update gets it either
through the visible, approved browser or on removable media. They then choose
two local files:

- **the installer:** `qronos-desktop_<version>_x64-setup.exe`;
- **its signed manifest:** `qronos-desktop_<version>_x64-setup.exe.qronos-update.json`.

`core/local_update.py` checks them entirely offline before anything is
installed. Signatures are Ed25519, verified by `core/ed25519.py`, a strict RFC
8032 implementation tested against the RFC's own test vectors. No network
library is imported.

`core/update_service.py` adds the product lifecycle around it (request,
approval with the approval secret, launch, start-up confirmation, rollback with
data snapshots); see below. No owner key exists yet, so today every package is
refused; see the owner steps below.

## What a signed manifest binds

The manifest's payload is signed byte-for-byte. It must name:

| Field | Rule |
| --- | --- |
| `product` | `com.qronos.agent` |
| `architecture`, `target` | `x64`, `x86_64-pc-windows-msvc` |
| `version` | a semantic version |
| `package` | the installer's file name, size and SHA-256. The chosen file must match exactly. |
| `dataSchema` (schema 1), or `dataStores` and `compatibilityEpoch` (schema 2) | the data formats it writes and reads; see "Data compatibility" |
| `minimumVersion` | the oldest installed version it may upgrade from, or `null` |
| `securityFloor` | versions below this may never be installed again |
| `rollback` | `true` only for a package deliberately signed as a rollback |

Unknown or missing fields are refused.

## Keys

`config/update_trusted_keys.json` lists the owner's public keys. It ships empty,
and while it is empty every package is refused with `no_trusted_key`. The
following are also refused:
- a signature by an unknown key (`untrusted_signature`);
- a signature by a key marked `revoked` (`revoked_key`);
- a payload changed after signing.

One trusted signature among several is enough. That allows a key to be rotated
by signing with both the old and the new key for a while.

Each key entry records:
- `keyId`: the first 32 hex digits of the SHA-256 of the 32-byte public key;
- `publicKey`: base64;
- `owner`;
- `revoked`.

The following key entries are refused:
- a `keyId` that does not match its key;
- a duplicate;
- a value that is not a canonical encoding of a point in the prime-order
  subgroup. That covers the identity point, the other small-order points and
  mixed-order points; with the identity point as a key, an "unsigned" payload
  would verify.

Verification checks the key again. It also refuses a signature whose `R` is
non-canonical or of small order, or whose `S` is not below the group order.

**The repository never contains a private key, and no test key is ever
committed.** Tests generate throwaway keys in memory.

## Version and data rules

The rules are checked against the running Qronos version, its data schema and
the security floor recorded by earlier updates:

- **Newer version:** an upgrade. The installed version must be at least
  `minimumVersion`, and the package must not write an older data format.
- **Same version:** a repair.
- **Older version:** refused unless the package is signed with
  `rollback: true`.
- **Below the security floor:** any version under the higher of the recorded
  floor and the package's own floor is refused.
- **Data compatibility:** the installed data format must lie within the
  package's `reads` range. A rollback cannot leave data that the older version
  cannot read.

## The staging store

`LocalUpdateStore(<Qronos data root>/Updates)` keeps a state file and verified
copies, in its own folder only.

1. **`stage(manifest, installer, keys, version, data_schema)`**
   - verifies everything;
   - copies the installer into `Updates/staged/<sha256>.exe`, re-hashing while
     it copies;
   - keeps the manifest beside it.
   - A refused package changes nothing.
2. **`handoff(...)`**
   - re-verifies the staged copy and the key file at that moment;
   - returns the installer path for the user-approved install step. That step
     launches the installer; this module does not.
3. **`confirm(version)`**, at the next start:
   - When the running version equals the staged one, it records the update in
     the history, raises the security floor, and keeps that package as
     "previous".
   - Otherwise it reports "not applied" and keeps the staged copy.
4. **`discard()`** deletes only the store's own copies, by the names it recorded.

The store refuses:
- an update folder, staging folder or any folder above them that is a link or
  junction;
- a state file that names files the store did not create, or that is not
  strictly typed and bounded (at most 256 KiB, 50 history entries). Every
  field's type is checked first: `schema` must be the integer 1, not `true`
  or `1.0`, and a list or object where a string belongs is refused. The file
  is read with a hard bound of 256 KiB + 1 byte, not after a size check, and
  only through a handle to the same plain, single-link file that was checked;
- any destination file, state file or lock file that is a link, is not a
  regular file or has another hard link. Such a file is never written
  through.

How the store writes and serialises changes:
- Every write goes to an exclusively created temporary file with an
  unpredictable name in the store's own folder, and is published with an
  atomic rename. A failed write leaves no temporary file behind.
- Changes are serialised across processes by an exclusive lock on
  `update.lock`. A second process waiting longer than the timeout gets `busy`.
  Reading the state takes the same lock: on Windows a file that is open
  cannot be replaced, so a reader must not hold it during another process's
  save. Reading a store that does not exist yet creates nothing.
- If another program (not Qronos) has a destination file open, the rename
  is retried for up to two seconds and then refused with `busy`. Any other
  write error is refused with `store`. Either way nothing changes, the
  temporary file is removed and the caller gets an `UpdateError`, never a
  raw operating-system error and never a silent success.
- If the security floor changes while a package is being checked, staging
  stops with `busy` rather than using the stale floor.

It never deletes anything else. User data, the Library and models are outside
its folder.

## Data compatibility: payload schema 2

Qronos keeps many stores, each versioned on its own; there is no global data
version. The product accepts only payload **schema 2**, which replaces
`dataSchema` with:

| Field | Rule |
| --- | --- |
| `dataStores` | for each store id: the format it `writes` and the range `[oldest, newest]` it `reads` |
| `compatibilityEpoch` | must equal the running build's epoch; moving between epochs is refused (`data_epoch`) |

The running build's side comes from `core/update_stores.py`, a closed registry:

- Every store with its format version. Each version is compared with the store
  module's own constant by a test, so they cannot drift apart.
- SQLite stores pin a fingerprint of their SQL layout (`ddl_sha256`): changing
  a layout without bumping its version fails a test. A test also fails if any
  module opens a SQLite file the registry does not list.
- Which stores are a single fixed file in the data root and can therefore be
  kept as a snapshot. SQLite stores are never copied raw.

Rules (`data_schema` unless noted):

- Every store the running build has must be listed, and its installed format
  must lie within `reads`.
- An upgrade may add new stores but never lowers a store's format.
- A repair (same version) must describe exactly the running build's formats.
- A rollback must not name stores the running build does not have.
- An upgrade that changes the format of a store that cannot be kept as a
  snapshot is refused (`unsupported_migration`), because a later rollback
  could not bring that data back.
- Schema 1 (a single number) is refused by the product (`unsupported_schema`).

The updater never runs migrations. Forward migrations stay in each store's own
reviewed code in the new build.

## The product lifecycle (`core/update_service.py`)

The desktop reaches it through the `update` backend service
(`core/update_cli.py`: operation in argv, one JSON object on stdin, one JSON
object out; every refusal is `{"ok": false, "code": ..., "error": ...}`).

The running version is never taken from a request; one that names
`runningVersion` is refused. It comes from `QRONOS_APP_VERSION`, which the
desktop binary sets from its own compiled package information when it starts
the service. In a source checkout it comes from `tauri.conf.json`. A packaged
backend without it refuses (`version_unknown`).

1. **status**: the running version, whether an owner key is configured, the
   phase, the staged package, the outcome of the last update, snapshots and a
   bounded event history (100 entries).
2. **discover** `{folder}`: installer and manifest pairs directly in a folder
   the person chose (not recursive, links refused). Nothing is opened.
3. **verify** / **request** `{installer, manifest}`: every check, offline,
   before anything is copied. `request` then asks the permission gate
   (`security.gate`, category `INSTALL_SOFTWARE`) for exactly this version,
   kind and SHA-256. A refusing policy refuses. Otherwise an approval secret
   must be configured (updates always need the TYPED_SECRET level, even if the
   policy was relaxed). It returns the offer and a random single-use
   `approvalToken` (10 minutes); only its SHA-256 is stored.
4. **approve** `{approvalToken, secret}`: verifies the approval secret (with
   the store's persisted lockout), re-verifies the files and checks they are
   still exactly the package, signer and paths that were requested. Then it
   consumes the token, stages the copy, and returns a single-use `launchToken`
   (15 minutes). A wrong secret changes nothing; the request stays usable until
   it expires. A missing or unknown token is refused before the secret is even
   checked.
5. **launch** `{launchToken}`:
   - It proves the runtime is stopped by creating the named mutexes the runtime
     bridge, supervisor and sidecars hold while they run. If any exists, it
     refuses with `runtime_running`. While they are held, none of those
     processes can start. A caller saying "stopped" is not evidence.
   - It also holds `Local\Qronos.UpdateInProgress.v1` for the whole launch.
     Other backend services (settings, permissions, library and so on) do not
     hold the runtime mutexes. Once Runtime wires `update_in_progress()` into
     the backend entry point, they refuse to start while the update holds it.
     Until then, the desktop must not start them during a launch.
   - It claims the launch in the lifecycle file, so a crash is recovered at the
     next start.
   - Before an upgrade it keeps a bounded, hashed snapshot of the snapshot-able
     stores whose format changes.
   - It re-verifies the staged copy, holds it open so nobody can write, rename or
     delete it, and re-hashes it through that handle.
   - For a rollback that needs older data, it first saves the current files as
     another snapshot, then restores. A restore that fails part-way puts the
     current files back.
   - Only then does it start exactly the staged installer, with no arguments.
     The desktop exits; the installer's own install-root and owned-payload
     guards apply.
6. **startup_check** (at every start, before stores open):
   - It records `applied` (the running version is the target; any store whose
     format differs from the signed one is reported as `dataMismatch`) or
     `not_applied` (cancelled, failed or crashed installer). The staged copy is
     kept for a retry.
   - It puts back data restored by a launch that crashed before the installer
     started.
   - It removes interrupted snapshot folders. Foreign files are never removed.
7. **discard**: forgets the staged update and its tokens.

Limits:

- A same-version repair counts as applied whether or not the installer
  finished, since the version cannot tell the difference.
- `store.handoff` re-hashes the staged installer while holding the store lock
  (bounded by the 2 GiB package limit).
- Snapshots cover only single-file stores, at most 64 MiB each and 256 MiB in
  total. Two upgrade snapshots are kept.
- Recovery after a crash runs at the next start-up, which must call
  `startup_check` before any store is opened.
- JSON stores have no per-store lock (`JsonStore` relies on atomic replace).
  A snapshot therefore reads one complete version of each file, never a torn
  one. A backend service that was already running when the launch began could
  still write afterwards. If it overwrote restored data with a newer format,
  the older version refuses that store (`StoreTooNew`), and the saved current
  copy lets it be put back. Blocking new services (above) and draining
  in-flight desktop commands (Control) close this window.

## Owner key and signing (`build_support/release_signing.py`)

**Status:** the tool is implemented and tested with synthetic keys only. The
real owner key is created only after the tool has been reviewed and Root gives
the go-ahead.

How the key is kept:

- It is generated on the owner's own Windows account and protected with
  Windows DPAPI for that account.
- It is written to a **new** file **outside every Git checkout**. The tool
  refuses a path inside a checkout, or one reached through a link, and never
  overwrites an existing key.
- The file's access list is then reduced to that account alone. A key file
  whose access was loosened is refused.
- The private key is never printed, logged, passed on the command line or in
  the environment. Only the public trusted-key entry is printed.

Owner procedure (once, after review). Run each command in **Windows
PowerShell**, opened from the Start menu, in the Qronos source folder:

1. Create a private folder for the key, outside the source folder:

   ```powershell
   New-Item -ItemType Directory "$env:USERPROFILE\QronosOwnerKey"
   ```

2. Create the key. Replace `Your Name` with the owner's name:

   ```powershell
   python -m build_support.release_signing create-key --key-file "$env:USERPROFILE\QronosOwnerKey\owner.key" --owner "Your Name"
   ```

   The output is a `trustedKeyEntry`: a key id and a public key only.
3. Add that entry to the `keys` list in `config/update_trusted_keys.json` in a
   reviewed change. Never add the key file itself.
4. Back the key file up to offline media. It can be unlocked only by the same
   Windows account, so a backup is of no use to someone else, but losing the
   account loses the key.

To sign a release, build the installer first, then run this. Replace the two
placeholders: the full path of the built installer, and the oldest version that
may still be installed:

```powershell
python -m build_support.release_signing sign --key-file "$env:USERPROFILE\QronosOwnerKey\owner.key" --installer "<path to qronos-desktop_VERSION_x64-setup.exe>" --security-floor "<oldest allowed version>"
```

- The version comes from the product version authority,
  `desktop/src-tauri/tauri.conf.json`. `desktop/package.json` and `Cargo.toml`
  must agree with it, and so must the installer's file name.
- The data formats come from `core/update_stores.py`.
- The manifest is verified the way Qronos will verify it, then written beside
  the installer, never over an existing one.

`python -m build_support.release_signing preflight` lists the release gates and
exits non-zero while any is closed:

- **owner update key:** closed until the reviewed public entry is added.
- **version authority:** open when the three version fields agree.
- **Authenticode:** closed. No code-signing certificate exists. The owner must
  obtain one from a public certificate authority, which is an external, paid
  step. Then they install the Windows SDK signing tools and set
  `QRONOS_AUTHENTICODE_THUMBPRINT`. A self-signed certificate is not a
  substitute, and nothing here installs certificates or touches trust stores.

Signing in pure Python is not constant-time, so the owner signs only on their
own trusted machine.

## Wiring that other owners do (requested, not done here)

- **Runtime** (`build_support/runtime_entry.py`, `core/runtime_bridge.py`):
  - add the service `"update": "core.update_cli"`;
  - register the start-up hook `core.update_service.startup_check` with
    context `{"appVersion": ...}` (a small, bounded dict that never raises).
    It must run once, before stores open, and its result is emitted as
    `update_status`;
  - in the backend entry point, refuse every service except `update` while
    `core.update_service.update_in_progress()` is true.
- **Control** (desktop):
  - add `Service::Update` (`update`, `core.update_cli`) to
    `src-tauri/src/runtime/backend.rs`;
  - add a Tauri command `update_command(operation, payload)` with the same
    shape as `permissions_command`. Start the service with `QRONOS_APP_VERSION`
    set from the app's own compiled package information, never from the
    frontend;
  - block and drain other backend commands while an update launch is running;
  - add native file and folder pickers;
  - add a real `stopRuntime` that stops the bridge and reports the result, plus
    `restartRuntime` and `exitApp`;
  - mount `src/components/UpdatePanel.tsx` (Persian) in Settings, with
    `src/updateStore.ts` as its client.
- **Elevation:** none. Installs are per user (`installMode: currentUser`).
  A machine-wide configuration is refused rather than routed through a broker.

The unsigned review installers built so far are not owner-signed and must not
be described as such.
