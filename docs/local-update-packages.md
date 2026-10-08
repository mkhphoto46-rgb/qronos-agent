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

This is software that is ready to be wired in. No owner key exists yet, so today
it accepts no package at all; see the owner steps below.

## What a signed manifest binds

The manifest's payload is signed byte-for-byte. It must name:

| Field | Rule |
| --- | --- |
| `product` | `com.qronos.agent` |
| `architecture`, `target` | `x64`, `x86_64-pc-windows-msvc` |
| `version` | a semantic version |
| `package` | the installer's file name, size and SHA-256. The chosen file must match exactly. |
| `dataSchema` | the data format version it writes, and the range `[oldest, newest]` it can read |
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
  strictly typed and bounded (at most 256 KiB, 50 history entries);
- any destination file, state file or lock file that is a link, is not a
  regular file or has another hard link. Such a file is never written
  through.

How the store writes and serialises changes:
- Every write goes to an exclusively created temporary file with an
  unpredictable name in the store's own folder, and is published with an
  atomic rename. A failed write leaves no temporary file behind.
- Changes are serialised across processes by an exclusive lock on
  `update.lock`. A second process waiting longer than the timeout gets `busy`.
- If the security floor changes while a package is being checked, staging
  stops with `busy` rather than using the stale floor.

It never deletes anything else. User data, the Library and models are outside
its folder.

## Still needed before this is live

These are wiring requests to the owners of shared files. None of them is done
here.

- **UI and approval (workflow worker):**
  - a "Choose update package" flow using a file picker;
  - an approval prompt that shows the version, update kind and signer key id;
  - launching the staged installer after approval;
  - calling `confirm()` at start-up.
- **Data schema version:** Qronos needs one authoritative data-schema number,
  which the UI passes as `data_schema`.
- **Store location:** `CONFIG.paths.data / "Updates"` from `core/config.py`.
- **Owner signing (external gate):**
  - The owner creates an Ed25519 key pair on a trusted, preferably offline
    machine.
  - The owner adds only the public key to `config/update_trusted_keys.json`, in
    a reviewed change.
  - The owner signs each release's payload; `build_envelope` in
    `core/local_update.py` produces the envelope format.
  - Signing in pure Python is not constant-time, so the owner should sign only
    on a trusted machine.
  - The unsigned review installers built today are not owner-signed and must
    not be described as such.
