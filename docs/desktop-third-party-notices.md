# Desktop installer third-party notices

This covers the Tauri desktop installer only. Notices for the standalone Python
backend belong to the backend packaging work. The desktop notices are a separate
installer resource, so the backend manifest does not cover them. They carry
their own hash manifest.

This is technical notice packaging. It is not legal advice and does not approve
redistribution. The licence for Qronos itself is outside this document.

## What is collected

`python -m build_support.desktop_notices --out <new-dir>` builds the notice set
from the locked sources already on the build machine. It does not download
anything.

The scope is a conservative technical inventory, not proof of what the linker
put into the final executable.

- **Rust:** runs `cargo metadata --offline --locked --filter-platform
  x86_64-pc-windows-msvc` against `desktop/src-tauri`.
  - Included: the locked Windows normal-dependency closure of the desktop crate,
    including proc-macro crates reached through normal edges.
  - Excluded: build-only and dev-only dependencies, such as `tauri-build`.
  - Build scripts can still generate or copy code and native libraries into the
    build. Those outputs may carry notice duties that package metadata does not
    show. The statically linked WebView2 loader, covered below, was found by
    inspection, not by this scope.
  - Provenance for each crate is its `Cargo.lock` checksum.
  - The published source is recorded as
    `https://static.crates.io/crates/<name>/<name>-<version>.crate`.
- **npm:** the production closure of `desktop/package-lock.json`, resolved the
  way Node does. Packages locked as `dev` are never included.
  - Each installed `node_modules` package must match its locked name and version.
  - Provenance is the lock `integrity` value, and the published source is the
    lock's registry tarball URL.
  - Today the closure is `@tauri-apps/api`, `@tauri-apps/plugin-opener`, `react`,
    `react-dom` and `scheduler`.

Source URLs and checksums are provenance. They are not a source-availability offer
or legal approval.

For each package, the collector copies the following byte-for-byte from the
package's own source directory:
- top-level `LICENSE*`, `LICENCE*`, `COPYING*`, `UNLICENSE*`, `NOTICE*` and
  `COPYRIGHT*` files;
- Cargo's `license-file`, wherever it is in the package.

Every copied file gets a role:

| Role | Files | Counts as licence text |
| --- | --- | --- |
| `licence` | a `LICENSE`/`LICENCE`/`COPYING`/`UNLICENSE` file, or the declared `license-file`, with real licence wording | yes |
| `licence-label` | such a file that is empty, whitespace-only or only a label (e.g. `MIT`, `SPDX-License-Identifier: MIT`) | no |
| `auxiliary` | `NOTICE`/`COPYRIGHT` files | no |
| `spdx-metadata` | `.spdx` files | no |

Auxiliary files, labels and metadata are still copied and listed. They are never
counted as licence text.

Output, all relative and deterministic:

- `THIRD_PARTY_NOTICES-desktop.txt`: every component, its published source and
  every copied file with its role, size and SHA-256.
- `third-party-inventory-desktop.json`, for each component:
  - name, version and declared licence;
  - the `license-file` field;
  - source, provenance and published-source download;
  - every notice file's path, role, origin, size and SHA-256;
  - where its licence text came from (`package`, `verified-upstream` or
    `reviewed-exception`);
  - upstream pins, evidence and owner review;
  - any reviewed exception.
- `third-party-manifest-desktop.json`: the size and SHA-256 of every other output
  file. `verify_notice_output` checks that the directory holds exactly these
  files, byte-for-byte, with no links.
- `licenses/rust/<crate>/<version>/...` and
  `licenses/npm/[@scope/]<package>/<version>/...`: the copied files.
  - Each name segment and the version is its own directory level, so
    `@foo/bar` and `foo__bar` cannot collide.
  - Verified upstream files are under `upstream/`.
- `qronos/LICENSE` and `qronos/NOTICE`: Qronos's own licence, Apache-2.0
  (`build_support/project_licence.py`). LICENSE must be the official text,
  byte-for-byte (pinned SHA-256). The inventory records both under
  `projectOwned`. The packaged runtime carries the same two files in its
  `third-party-notices/qronos/`, and the installer check requires identical
  copies in both places.
- `artwork/<upstream>-<revision>/...`: the licence texts of the third-party template icons and logos
  (`desktop/src-tauri/icons/`, `desktop/public/`, `desktop/src/assets/`), vendored byte-for-byte from their pinned
  upstream revisions. `build_support/desktop_artwork.json` lists every file under those folders, its exact
  SHA-256 and the upstream file it is identical to. An unlisted, changed or extra file stops the build. The
  notices text has an artwork section and the inventory an `artwork` record. The installer check requires that
  record to name exactly the registered files. This artwork is not Qronos's work and is not covered by its
  Apache-2.0 licence.

## Identities, destinations and the output directory

Everything is checked before the output directory is created:

- **Names:** Cargo crate names and npm names must match their registry rules.
- **Versions:** must be plain Semantic Versioning 2.0, with prerelease and build
  metadata kept. Separators, drives, UNC paths, `:` streams, traversal, control
  characters and Windows device names are refused.
- **Destinations:** every destination must be a portable relative path. All
  destinations must be unique even when case is ignored, and no file may also be
  a directory. Duplicate package identities are refused.
- **Private paths:** generated content containing a private builder path, in
  plain, forward-slash or JSON-escaped form, is refused before anything is
  written.

The output must be a new or empty directory, and neither it nor any parent may
be a link or junction. The collector never deletes or regenerates an existing
non-empty directory. A marker file proves nothing.

Files are created exclusively. Source bytes are re-hashed just before copying,
and each written file is read back.

## Verified upstream texts

Some published packages ship no licence text. For these, the text comes from
`build_support/desktop_notice_upstream/`:

- `texts/` holds the small upstream files, byte-for-byte. Its `.gitattributes`
  disables line-ending and whitespace rewriting so the hashes survive checkout.
- `registry.json` has one entry per exact package:
  - ecosystem, name and version;
  - the locked checksum or integrity value;
  - the declared licence;
  - the pinned source repository, revision and package path, and how they were
    established;
  - SHA-256 hashes of local identity files, such as `.cargo_vcs_info.json`,
    `Cargo.toml.orig` and `package.json`;
  - for each vendored file: its role, immutable URL, revision, source path and
    SHA-256;
  - the evidence statements behind the pins;
  - an `ownerReview` note. A value starting with `owner-review-required` is
    listed in the inventory's `ownerReviewRequired`.

The collector checks all of this offline on every build. It never fetches
anything. Fetching was a one-off preparation step: anyone can repeat it by
downloading each recorded URL and comparing SHA-256 values.

The registry is not a waiver list:
- Unknown fields are rejected.
- Every entry needs at least one substantive licence-role text.
- Each file must mirror its upstream source path and match its hash.
- Every file under `texts/` must be referenced.

## Failure rules

Any of the following stops the build:

- a component with no licence expression or no licence text, unless it has
  verified upstream text or an exact reviewed exception. Auxiliary files, labels
  and `.spdx` metadata never count;
- a stale exception or upstream entry. That includes an entry for a package that
  now ships its own licence text, and an exception for a package that already
  has verified upstream text;
- an upstream entry whose provenance, declared licence or identity-file hash
  does not match the installed package;
- a duplicate entry, a duplicate or unreferenced vendored file, a corrupted or
  label-only vendored licence, or a vendored path that is absolute, escapes, is
  linked, is not portable or does not mirror its source path;
- an unsafe or duplicate package identity, or a colliding destination;
- a non-empty output directory, or a link or junction anywhere on the output
  path;
- a source or vendored file that changes between checking and copying;
- a local path crate;
- a dev package reachable from production;
- a version mismatch;
- a linked or reparse-point notice file;
- a path that escapes the package;
- a private workstation path in the output.

## Exceptions

`build_support/desktop_notice_exceptions.json` is empty. Root must review an entry
before it is added.

Each entry has to match the exact ecosystem, name, version and provenance. It
records why the published source is missing something. It never supplies a
licence.

## Installer wiring

`build_support/build_desktop.py` builds only `nsis` or `msi` installers. It
refuses to start on anything other than Windows x64, or when
`CARGO_BUILD_TARGET` names another target. That way the
`x86_64-pc-windows-msvc` inventory is never attached to another platform's
installer.

Each build then:
1. creates a new directory under `runtime/package/desktop-notices/` (ignored by
   Git);
2. collects the notices into it and verifies the result against its manifest;
3. writes the generated Tauri configuration, which maps that directory to
   `third-party-notices/` next to `qronos-runtime/`.

Earlier build directories are left in place as evidence. `tauri.conf.json` is
unchanged. A collection or verification failure stops the build before Tauri
starts.

## Current coverage (offline dry run, 2026-10-07)

The real locked inventory has 254 Rust crates and 5 npm packages:
- 247 ship their own licence text;
- 12 use verified upstream text;
- none uses an exception.

Every component has a recorded published-source download.

Each of the 12 is pinned to the source revision recorded for it:
- **Rust crates:** the `.cargo_vcs_info.json` commit. The published
  `Cargo.toml.orig` is byte-identical to the manifest at that commit.
- **npm package:** the registry `gitHead`, which equals the matching tag.

| Ecosystem | Package | Version | Declared | Vendored upstream text |
| --- | --- | --- | --- | --- |
| npm | @tauri-apps/plugin-opener | 2.5.4 | MIT OR Apache-2.0 | `plugins/opener/LICENSE_MIT`, `LICENSE_APACHE-2.0` at tag `opener-js-v2.5.4` |
| rust | alloc-stdlib | 0.2.4 | BSD-3-Clause | repository `LICENSE` (Dropbox, Inc.) |
| rust | defmt-parser | 1.0.0 | MIT OR Apache-2.0 | repository `LICENSE-MIT`, `LICENSE-APACHE` |
| rust | selectors | 0.36.1 | MPL-2.0 | `selectors/lib.rs` (MPL Exhibit A notice) and Mozilla's MPL-2.0 text; see below |
| rust | unic-char-property, unic-char-range, unic-common, unic-ucd-version | 0.9.0 | MIT/Apache-2.0 | repository `AUTHORS`, `COPYRIGHT.md`, `LICENSE-MIT`, `LICENSE-APACHE` |
| rust | unic-ucd-ident | 0.9.0 | MIT/Apache-2.0 | the same files at its own packaged commit |
| rust | webview2-com, webview2-com-macros | 0.38.2, 0.8.1 | MIT | repository `LICENSE` |
| rust | webview2-com-sys | 0.38.2 | MIT | repository `LICENSE`, plus Microsoft's `LICENSE.txt` and `NOTICE.txt`; see below |

Root decisions (2026-10-07). These are technical packaging decisions, not
redistribution clearance:

- **`selectors`:** the exact stylo source revision has no licence file. Its
  files carry the MPL-2.0 Exhibit A notice, which points to
  https://mozilla.org/MPL/2.0/.
  - The exact-source notice is kept together with Mozilla's own MPL-2.0 text from
    the `mozilla/bedrock` site repository, pinned by commit.
  - When retrieved, that text was byte-identical to
    https://www.mozilla.org/media/MPL/2.0/index.txt.
- **`webview2-com-sys`:** on MSVC, this crate statically links Microsoft's
  `WebView2LoaderStatic.lib`.
  - The packaged x64 copy, pinned by hash as an identity file, is byte-identical
    to the one in `Microsoft.Web.WebView2` 1.0.3650.58.
  - That package's SHA-512 equals the nuget.org catalog `packageHash`.
  - Its `LICENSE.txt` and `NOTICE.txt` are included. They are independent
    Microsoft terms, marked `owner-review-required`.
- **UNIC:** `COPYRIGHT.md` refers to the repository's `AUTHORS` list. That public
  upstream authorship attribution is reproduced byte-for-byte from the same
  pinned revision.

These declarations also need review:

- **Not valid SPDX:** `MIT/Apache-2.0`, `Apache-2.0 / MIT`, `BSD-3-Clause/MIT`
  and `Unlicense/MIT` are recorded exactly as published.
- **MPL-2.0:** `cssparser`, `cssparser-macros`, `dtoa-short`, `option-ext` and
  `selectors` carry file-level source-availability terms.

## Not covered here

- **WebView2 runtime:** whether the installer works on a clean, offline machine
  with no WebView2 runtime is still a separate gate. The bootstrapper policy is
  unchanged by this work and needs its own deliberate check.
- **Microsoft runtime components:** the Visual C++ runtime and the WebView2
  Runtime are not crates or npm packages, so they are not in this inventory.
- **Embedded binaries in other crates:** the inventory follows declared package
  metadata and does not inspect build-script outputs or embedded binaries in
  other crates.
