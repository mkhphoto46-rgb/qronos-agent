# Desktop installer third-party notices

This covers the Tauri desktop installer only. Notices for the standalone Python
backend belong to the backend packaging work.

This is technical notice packaging. It is not legal advice and does not approve
redistribution. The licence for Qronos itself is outside this document.

## What is collected

`python -m build_support.desktop_notices --out <dir>` builds the notice set from
the locked sources already on the build machine. It does not download anything.

- **Rust:** runs `cargo metadata --offline --locked --filter-platform
  x86_64-pc-windows-msvc` against `desktop/src-tauri`.
  - Included: every crate reachable from the desktop crate through normal
    dependency edges. Proc-macros are included as a conservative choice.
  - Excluded: build-only and dev-only edges, such as `tauri-build`.
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
  declared licence, `license-file` field, source, provenance, notice locations
  and any reviewed exception.
- `licenses/<ecosystem>/<package>-<version>/...`: the copied files.

## Failure rules

Any of the following stops the build:

- a component with no licence expression or no licence text, unless it has an
  exact reviewed exception;
- a stale exception;
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

## Open items (dry run, 2026-10-07)

The real locked graph covers 254 Rust crates and 5 npm packages. These 12 packages
declare a licence but ship no licence text in their published source, so the
installer build fails until each one is resolved:

| Ecosystem | Package | Version | Declared |
| --- | --- | --- | --- |
| npm | @tauri-apps/plugin-opener | 2.5.4 | MIT OR Apache-2.0 (only `LICENSE.spdx`) |
| rust | alloc-stdlib | 0.2.4 | BSD-3-Clause |
| rust | defmt-parser | 1.0.0 | MIT OR Apache-2.0 |
| rust | selectors | 0.36.1 | MPL-2.0 |
| rust | unic-char-property, unic-char-range, unic-common, unic-ucd-ident, unic-ucd-version | 0.9.0 | MIT/Apache-2.0 |
| rust | webview2-com, webview2-com-sys | 0.38.2 | MIT |
| rust | webview2-com-macros | 0.8.1 | MIT |

These declarations also need review:

- **Not valid SPDX:** `MIT/Apache-2.0`, `Apache-2.0 / MIT`, `BSD-3-Clause/MIT`
  and `Unlicense/MIT` are recorded exactly as published.
- **MPL-2.0:** `cssparser`, `cssparser-macros`, `dtoa-short`, `option-ext` and
  `selectors` carry file-level source-availability terms.

## Not covered here

- **WebView2 runtime:** whether the installer works on a clean, offline machine
  with no WebView2 runtime is still a separate gate. The bootstrapper policy is
  unchanged by this work and needs its own deliberate check.
- **Microsoft runtime components:** the Visual C++ runtime and WebView2 itself are
  not crates or npm packages, so they are not in this inventory.
