# Qronos desktop

The Tauri shell: a React front end, a Rust back end, and the bridge that starts
the Python voice runtime.

## Recommended IDE setup

- [VS Code](https://code.visualstudio.com/) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer)

## Running it during development

```bash
npm ci
npm run tauri dev
```

`cargo build` on its own is not enough. The debug profile uses `devUrl` from
`tauri.conf.json`, so the window opens against `http://localhost:1420` and shows
nothing at all unless Vite is running. The window looks like a working
application while the front end has never loaded, which is a confusing hour to
spend. Either use `npm run tauri dev`, which starts both, or run `npm run dev`
in one terminal and the binary in another.

The voice runtime is found by walking up from the working directory for a
folder containing both `core/` and `desktop/`, so run the binary from inside
the checkout. An installed copy has no such folder and reports the runtime as
unavailable rather than failing to start.

## Building a release

```bash
npm run tauri build
```

Produces an MSI and an NSIS installer under
`src-tauri/target/release/bundle/`.

### Strip the build machine's paths first

Rust embeds absolute source paths in panic locations. Left alone, a release
carries **324** of them — the cargo registry and the source tree, both under
the building developer's home directory — and they travel to every user inside
the installer.

Measured on this project, setting `RUSTFLAGS` before the build takes that from
324 to 1:

```powershell
$env:RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE\.cargo\registry=[deps] " +
                 "--remap-path-prefix=$PWD\..=[src]"
npm run tauri build
```

```bash
# bash
export RUSTFLAGS="--remap-path-prefix=$HOME/.cargo/registry=[deps] --remap-path-prefix=$(cd .. && pwd)=[src]"
npm run tauri build
```

The paths must be written the way the compiler sees them — backslashes on
Windows. Forward slashes silently match nothing and leave all 324 in place.

This is not in `Cargo.toml` on purpose: the values are absolute and differ per
machine, so a committed copy would be wrong for everybody except whoever wrote
it. Cargo's `trim-paths` would solve that, but it is not stabilised as of Cargo
1.98.

The one remaining path comes from Tauri's own `generate_context!`, which
embeds the crate directory. That is upstream behaviour.

### Still outstanding before a real release

- The installer is unsigned, so Windows will warn on every install.
- `productName` is still `qronos-desktop`, which names the install directory
  and the uninstall entry.
- The Python runtime is not packaged, so an installed copy has a working
  interface and no voice.
