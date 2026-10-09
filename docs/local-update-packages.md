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
data snapshots); see below. One reviewed owner key is trusted (see "Keys"), but
the update launch stays refused until its wiring is reviewed, so no update can
be installed today.

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

`config/update_trusted_keys.json` lists the owner's public keys. While it is
empty, every package is refused with `no_trusted_key`.

It lists exactly one key: the owner key created on 2026-10-09.

| Field | Value |
| --- | --- |
| `keyId` | `b0566b68ca6ee2887a8db62b3aff00e8` |
| `publicKey` | `JvjHULywOhnnREKqgUZmYXXqYHXpKrq9s6yYnXlq8bY=` |
| `owner` | `Qronos Owner` |
| `revoked` | `false` |

Provenance:
- The entry is the public `trustedKeyEntry` printed by `create-key` from the
  reviewed `b76a904` signer, on the owner's own Windows account, under Root's
  authorization.
- The public output was saved as
  `work/qronos-owner-update-key-public-2026-10-09.json` (sha256
  `e1a545b98d694c83ec1b3e03116e3647a89c8fe2ec0ae0cddf0af2324e53776d`).
- The entry was copied from that file. The change that added it did not read
  the private key file.
- The key id matches the public key, and the key is a canonical prime-order
  Ed25519 point.

Listing the key does not activate anything:
- The update launch stays refused (`launch_unavailable`) until its wiring is
  reviewed.
- A package can verify only if it is signed by this key.
- No package has been signed with it yet.

The following are also refused:
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

**Activation status: launching an installer is REFUSED by default**
(`launch_unavailable`). It opens only when the dependencies in
`LAUNCH_DEPENDENCIES` exist and are independently accepted together with this
code: the runtime's own instance ownership, store admission in every
store-opening process, the awaited start-up barrier, and the desktop's drain of
its writers. No request field, flag or frontend value can open it. The exact
cross-owner contract is in `work/opus-production-installer-contract-round5.txt`
(coordination file, outside the repository).

The desktop reaches the backend through the `update` service
(`core/update_cli.py`: operation in argv, one JSON object on stdin, one JSON
object out; every refusal is `{"ok": false, "code": ..., "error": ...}`). The
running version is never taken from a request; one that names `runningVersion`
is refused. It comes from `QRONOS_APP_VERSION`, which the desktop binary sets
from its own compiled package information, or from `tauri.conf.json` in a
source checkout. A packaged backend without it refuses (`version_unknown`).

1. **status** (read-only):
   - the running version and whether an owner key is configured;
   - whether launching is available;
   - the phase, the staged package and the last outcome;
   - whether an update is in progress or a recovery is pending;
   - snapshots and a bounded event history.
2. **discover** `{folder}`: installer and manifest pairs directly in a folder
   the person chose (not recursive, links refused). Nothing is opened.
3. **verify** / **request** `{installer, manifest}`:
   - every check runs offline, before anything is copied;
   - `request` then asks the permission gate (`security.gate`, category
     `INSTALL_SOFTWARE`) for exactly this version, kind, installer SHA-256,
     manifest SHA-256 and signer;
   - updates always need the approval secret (the TYPED_SECRET level), so one
     must be configured;
   - it returns the offer and a single-use `approvalToken` (10 minutes); only its
     SHA-256 is stored.
4. **approve** `{approvalToken, secret}`:
   - the token is checked first;
   - the gate is asked again, so a policy changed meanwhile is honoured;
   - the secret is verified (the approval store keeps its lockout);
   - the files are re-verified and compared with the exact binding, including
     the manifest digest and the signer;
   - only then is the token consumed, the copy staged and a single-use
     `launchToken` (15 minutes) returned;
   - the approval is written to the action audit trail, with no token or secret.
5. **launch** `{launchToken}`, when enabled:
   - It takes the **exclusive store admission** (`core/store_admission.py`). This
     is granted only while no process holds a shared writer admission, and while
     it is held every new writer is refused, atomically, with no check-to-open gap.
   - It owns the runtime, supervisor, sidecar and update mutexes: each is
     created, then wait-acquired, on a holder thread.
   - The gate is asked a third time.
   - It claims the launch, snapshots what an upgrade changes, re-verifies the
     staged installer and manifest, holds the installer open, re-hashes it, and
     starts only that file.
   - The installer inherits the update mutex. The launching process answers the
     desktop at once, then stays alive holding everything until the installer
     exits: it is the installer's lifetime guard.
   - Completion is audited.
6. **The start-up barrier**, `recover_before_stores` (mutating, exclusive,
   awaited; Runtime calls it in-process before any store opens, never in the
   post-ready hook):
   - It refuses while an update is in progress. Liveness is the update mutex,
     never a timer.
   - It recovers an interrupted launch.
   - It resolves a handed-off update:
     - **applied** only on a version change;
     - **unconfirmed** for a same-version repair, which stays staged until
       there is reviewed evidence (an installer receipt, not built yet) or the
       person discards it;
     - **not applied** otherwise. A rollback that did not apply gets its pinned
       safety snapshot back, under the exclusive admission.
   - A failure is a typed refusal (`recovery_failed`, `recovery_pending`), and
     the process must stop without opening stores.
   - `startup_check` is the separate read-only status for after `runtime_ready`.
7. **discard**: forgets the staged update and its tokens. It refuses while a
   launch is alive or a recovery is pending.

Data rules for snapshots:

- An upgrade that changes the format of any of the following is refused
  (`unsupported_migration`):
  - a SQLite store;
  - a security registry (paired devices, permissions), because an old copy
    would undo a revocation or a tightening;
  - a store moved out of the data folder by an environment variable.
- A store that did not exist before the upgrade is recorded as absent. A
  rollback that would have to delete today's file is refused.
- Snapshots that an unresolved launch, handoff, request or grant refers to are
  never pruned.

Limits:

- A same-version repair cannot be confirmed until a reviewed installer receipt
  exists.
- `store.handoff` re-hashes the staged installer while holding the store lock
  (bounded by the 2 GiB package limit).
- Snapshots cover only single-file stores, at most 64 MiB each and 256 MiB in
  total. Two upgrade snapshots are kept, plus pinned ones.
- If the launching process (the lifetime guard) dies, its exclusive admission
  ends with it, because a byte-range lock belongs to its process. The update
  mutex still exists in the installer's inherited handle, and every writer
  admission checks that fence AFTER taking its shared lock. So no writer is
  admitted until the installer ends. The updater creates the fence only while
  it holds the exclusive admission, which makes the order atomic.

## Owner key and signing (`build_support/release_signing.py`)

**Status:** the key-creation part of the tool was independently reviewed at
`b76a904` (work/opus-production-media-review-signing-round5.txt) and accepted
for owner-key creation only. The real owner key was then created once, on
2026-10-09, from that exact source, on the owner's account, under Root's
authorization. The tool's tests use synthetic keys only. Public release
signing, Authenticode and update activation are not accepted.

How the key is kept:

- It is generated on the owner's own Windows account and protected with
  Windows DPAPI for that account.
- It is written to a **new** file **outside every Git checkout**. The tool
  refuses a path inside a checkout, or one reached through a link, and never
  overwrites an existing key.
- The file is created (CREATE_NEW) with an access list that admits that
  account alone, set in the same call that creates it, so it is never broader.
  Every later use re-checks, through the open handle: the owner-only access
  list, a plain single-link file, and that the handle's final path is exactly
  the intended path (no folder on the way was swapped for a link). A key file
  whose access was loosened is refused.
- If creating the key fails and the half-written file cannot be removed, the
  tool says so and names the file, so it can be deleted by hand.
- The private key is never printed, logged, passed on the command line or in
  the environment. Only the public trusted-key entry is printed.
- `public-entry` proves custody before it prints anything: it unlocks the key
  file with DPAPI on the current account, and the unlocked key must produce
  exactly the public key the file lists (a strong Ed25519 key). The unlocked
  key is wiped before the entry is printed. An edited file whose listed public
  key is not its own prints nothing.

Where the key can be created and used (anything else is refused, never
worked around):

- **Account:** the owner's own ordinary Windows account, in a normal
  (non-elevated) PowerShell. Only local or domain user accounts
  (`S-1-5-21-...`) are accepted; a Microsoft Entra ID-only account
  (`S-1-12-1-...`), a service account or another user is refused.
- **Path:** a new, plain, local folder given as a full long path. It must be
  outside every Git checkout (a `.git` file or folder above it counts). It must
  not be synchronised (OneDrive or similar) or shared, and nothing on the way
  may be a junction or link. A path in 8.3 short form, or reached through a
  `SUBST` or mapped drive, is refused because the opened file's final path
  differs from the path given.

Limits, stated plainly:

- DPAPI protects the key against other Windows accounts and against copies
  taken off the machine. It does NOT protect against software running as the
  owner's own account, which can ask DPAPI to unlock it just as this tool does.
  Create and use the key only on an account and machine the owner trusts.
- Wiping the decrypted key from memory is best effort. The tool zeroes the
  buffers it controls (the ctypes copies and the byte array), but Python makes
  its own copies, which it cannot erase.

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
   reviewed change. Never add the key file itself. (Done for the 2026-10-09
   key; see "Keys" above.)
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

- **owner update key:** open when `config/update_trusted_keys.json` lists at
  least one active (not revoked) entry. It counts the public entries only; it
  does not check the custody of any private key file.
- **version authority:** open when the three version fields agree.
- **Authenticode:** closed unless a signed installer is given and verifies
  NOW: `preflight --authenticode-config CONFIG --artifact EXE`. Without them,
  or without a public-CA certificate (today), it stays closed. A thumbprint
  setting (`QRONOS_AUTHENTICODE_THUMBPRINT`), a `signtool` binary or a
  provenance file is reported but is never evidence. A self-signed certificate
  is never a substitute.

### Authenticode signing step (`build_support/authenticode_release.py`)

Build tooling on the owner's release machine; never part of Qronos and never a
Qronos network route. It needs two things the project does not have yet:

- a code-signing certificate from a public certificate authority (an external,
  paid step for the owner), installed in the owner's own certificate store by
  the owner;
- the Windows SDK `signtool.exe`.

Its only input is one JSON config file. No environment variable is read and
`PATH` is never searched.

```json
{"schema": 1,
 "signtool": "C:\\Program Files (x86)\\Windows Kits\\10\\bin\\10.0.26100.0\\x64\\signtool.exe",
 "certificate": "C:\\Users\\<owner>\\QronosOwnerKey\\codesign.cer",
 "thumbprintSha1": "<40 hex digits>",
 "timestampUrl": "https://<the CA's RFC 3161 time-stamp service>"}
```

`check --config CONFIG` signs nothing. `sign` and `verify` refuse unless all of
the following hold.

- **signtool:** under `Windows Kits\10\bin\<10.0.x.y>\x64` of Program
  Files (x86) as Windows reports it (not `%SystemDrive%`), with no link on the
  way, and with a valid Microsoft signature.
- **Certificate:** the file is the public certificate only (no `.pfx` or
  private key), equals the configured thumbprint, and is valid now. It has the
  Code Signing usage and is not self-signed. Its chain, built offline, passes
  the Authenticode chain policy to a root in the local machine's root stores. A
  root that only the current user added is refused. An administrator-installed
  enterprise root would also pass, so the provenance lists the whole chain for
  the human release review.
- **Installer:** `qronos-desktop_<version>_x64-setup.exe` for the version
  authority. Its unsigned SHA-256 matches `installer-provenance.json` from
  `build_desktop`, which must be attributable to a clean commit, the one
  checked out now.

`sign --config CONFIG --artifact EXE --build-provenance installer-provenance.json`
then runs:

1. `signtool sign /fd sha256 /sha1 <thumbprint> /tr <timestampUrl> /td sha256`
2. `signtool verify /pa /all /tw /v`, which must exit 0. Exit 2, a warning such
   as a missing time stamp, is a failure.
3. An offline WinVerifyTrust check, whose signer must be the configured
   certificate.

It then writes `EXE.authenticode.json`, never over an existing one. That file
binds:

- the build provenance and the source commit;
- the unsigned and signed SHA-256;
- the signtool path, hash and signer;
- the certificate and its chain;
- the time-stamp service and the verify results.

Sign the update manifest (`release_signing sign`) after this, over the signed
bytes.

Limits:

- Revocation is not checked offline by this tool. `signtool verify` on the
  build machine may contact revocation and time-stamp services; that is the
  build machine's network use, not Qronos's.
- Nothing installs certificates or writes any certificate store.
- The tests use synthetic runners and certificates; no real signing has been
  done.

Signing in pure Python is not constant-time, so the owner signs only on their
own trusted machine.

## Wiring that other owners do (requested, not done here)

The closed contract is `work/opus-production-installer-contract-round5.txt`. In
short:

- **Runtime:**
  - the bridge and supervisor OWN their instance mutexes;
  - every store-opening process calls `recover_before_stores` first, then
    `core.store_admission.admit_writer`, then opens stores;
  - backend services refuse while `update_in_progress()`;
  - `SERVICES["update"]`;
  - only the read-only `startup_check` may use the post-ready hook seam.
- **Control:**
  - `Service::Update` and `update_command` with `QRONOS_APP_VERSION` from the
    binary;
  - the stop result derived from the actual process exit;
  - every other backend command blocked and drained during a launch;
  - read the launch's single answer line without waiting for the process to
    exit;
  - restart the runtime only on a typed proved-no-start refusal;
  - mount `UpdatePanel` after acceptance.
- **Elevation:** none. Installs are per user (`installMode: currentUser`).

The unsigned review installers built so far are not owner-signed and must not
be described as such.
