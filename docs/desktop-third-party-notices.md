# Desktop installer third-party notices

This covers the Tauri desktop installer only. Notices for the standalone Python
backend belong to the backend packaging work.

This is technical notice packaging. It is not legal advice and does not approve
redistribution. The licence for Qronos itself is outside this document.

## What is collected

`python -m build_support.desktop_notices --out <dir>` builds the notice set from
the locked sources already on the build machine. It does not download anything.

The scope is a conservative technical inventory. It does not prove that every
listed crate is linked into the final executable.

- **Rust:** runs `cargo metadata --offline --locked --filter-platform
  x86_64-pc-windows-msvc` against `desktop/src-tauri`.
  - Included: the locked Windows normal-dependency closure of the desktop crate,
    including proc-macro crates reached through normal edges.
  - Excluded: build-only and dev-only dependencies, such as `tauri-build`.
  - Provenance for each crate is its `Cargo.lock` checksum.
- **npm:** the production closure of `desktop/package-lock.json`, resolved the
  way Node does. Packages locked as `dev` are never included.
  - Each installed `node_modules` package must match its locked name and version.
  - Provenance is the lock `integrity` value.
  - Today the closure is `@tauri-apps/api`, `@tauri-apps/plugin-opener`, `react`,
    `react-dom` and `scheduler`.

For each package, the collector copies the following exactly from the package's
own source directory:

- top-level `LICENSE*`, `LICENCE*`, `COPYING*`, `NOTICE*`, `UNLICENSE*` and
  `COPYRIGHT*` files;
- Cargo's `license-file`, wherever it is in the package.

`.spdx` files are metadata, not licence text.

Output, all relative and deterministic:

- `THIRD_PARTY_NOTICES-desktop.txt`: every component and its full notice text.
- `third-party-inventory-desktop.json`: for each component, the name, version,
  declared licence, `license-file` field, source, provenance, notice locations,
  where its licence text came from (`package` or `verified-upstream`), upstream
  pins and any reviewed exception.
- `licenses/<ecosystem>/<package>-<version>/...`: the copied files, with
  verified upstream files under `upstream/`.

## Verified upstream texts

Some published packages ship no licence text. For these, the text comes from
`build_support/desktop_notice_upstream/`:

- `texts/` holds the small upstream files, byte-for-byte. Its `.gitattributes`
  disables line-ending conversion so the hashes survive checkout.
- `registry.json` has one entry per exact package:
  - ecosystem, name and version;
  - the locked checksum or integrity value;
  - the declared licence;
  - the pinned source repository, revision and package path, and how they were
    established;
  - SHA-256 hashes of local identity files, such as `.cargo_vcs_info.json`,
    `Cargo.toml.orig` and `package.json`;
  - for each vendored file: its role, immutable URL, revision, source path and
    SHA-256.

The collector checks all of this offline on every build. It never fetches
anything. Fetching was a one-off preparation step: anyone can repeat it by
downloading each recorded URL and comparing SHA-256 values.

The registry is not a waiver list:
- Unknown fields are rejected.
- Every entry needs at least one licence-role text.
- Each file must mirror its upstream source path and match its hash.
- Every file under `texts/` must be referenced.

## Failure rules

Any of the following stops the build:

- a component with no licence expression or no licence text, unless it has
  verified upstream text or an exact reviewed exception;
- a stale exception or upstream entry. That includes an entry for a package that
  now ships its own licence text, and an exception for a package that already
  has verified upstream text;
- an upstream entry whose provenance, declared licence or identity-file hash
  does not match the installed package;
- a duplicate entry, a duplicate or unreferenced vendored file, a corrupted
  vendored file, or a vendored path that is absolute, escapes, is linked or does
  not mirror its source path;
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

`build_support/build_desktop.py` runs the collector into
`runtime/package/desktop-notices`, which is ignored by Git, before it writes the
generated Tauri configuration. That overlay maps the directory to
`third-party-notices/` next to `qronos-runtime/`. `tauri.conf.json` is unchanged.
A collection failure stops the build before Tauri starts.

## Current coverage (offline dry run, 2026-10-07)

The real locked inventory has 254 Rust crates and 5 npm packages:
- 247 ship their own licence text;
- 12 use verified upstream text;
- none uses an exception.

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
| rust | unic-char-property, unic-char-range, unic-common, unic-ucd-version | 0.9.0 | MIT/Apache-2.0 | repository `COPYRIGHT.md`, `LICENSE-MIT`, `LICENSE-APACHE` |
| rust | unic-ucd-ident | 0.9.0 | MIT/Apache-2.0 | the same files at its own packaged commit |
| rust | webview2-com, webview2-com-macros | 0.38.2, 0.8.1 | MIT | repository `LICENSE` |
| rust | webview2-com-sys | 0.38.2 | MIT | repository `LICENSE`, plus Microsoft's `LICENSE.txt` and `NOTICE.txt`; see below |

Points for review:

- **`selectors`:** the exact stylo source revision has no licence file. Its
  files carry the MPL-2.0 Exhibit A notice, which points to
  https://mozilla.org/MPL/2.0/.
  - The vendored text is Mozilla's own file from the `mozilla/bedrock` site
    repository, pinned by commit.
  - When retrieved, it was byte-identical to
    https://www.mozilla.org/media/MPL/2.0/index.txt.
  - This is designated text, not a file from the dependency's own repository.
- **`webview2-com-sys`:** on MSVC, this crate statically links Microsoft's
  `WebView2LoaderStatic.lib`.
  - The packaged x64 copy, pinned by hash as an identity file, is byte-identical
    to the one in `Microsoft.Web.WebView2` 1.0.3650.58.
  - That package's SHA-512 equals the nuget.org catalog `packageHash`.
  - The package's `LICENSE.txt` and `NOTICE.txt` are included for this embedded
    component. They are Microsoft's terms, not the crate's MIT.
- **UNIC:** `COPYRIGHT.md` refers to the repository's `AUTHORS` list. That list
  contains contributor names and e-mail addresses, so it is not vendored (data
  minimisation). It stays available at the pinned revision.

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
  The statically linked WebView2 loader is covered above.
- **Embedded binaries in other crates:** the inventory follows declared package
  metadata. It does not inspect other crates for embedded third-party binaries.
