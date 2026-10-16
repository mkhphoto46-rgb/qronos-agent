# Local validation

Qronos is validated locally; hosted CI is not required. `build_support/local_validate.py` runs only the checks you
name.

```
python -m build_support.local_validate --help
python -m build_support.local_validate --dry-run tests/test_x.py --frontend
python -m build_support.local_validate --python C:/path/.venv/Scripts/python.exe \
    --workspace C:/path/work/pytest-local-1 tests/test_x.py tests/test_y.py::test_z
python -m build_support.local_validate --node "C:/Program Files/nodejs/node.exe" \
    --workspace C:/path/work/pytest-local-2 --frontend
```

## What it does

- **Selections:** explicit `tests/**/test_*.py` files or node IDs. Directories, options and paths outside `tests/`
  are refused, so there is no full-suite default.
- **Frontend (optional):** `--frontend` runs `node --test desktop/tests/*.test.mjs`, the same files as `npm test`.
  It needs an existing `desktop/node_modules`.
- **Nothing to run:** with no selection, or with `--dry-run`, it prints the plan and runs nothing.
- **Tools:** `--python` and `--node` must be absolute paths to existing executables. Nothing is installed,
  downloaded or looked up on `PATH`.
- **Isolation:** every child process gets the same environment:
  - `QRONOS_RUN_LIVE_DEVICE_TESTS=0`, `QRONOS_OLLAMA_AUTO_START=0` and `PYTHONDONTWRITEBYTECODE=1`;
  - `QRONOS_DESKTOP_WAKE_BOOTSTRAP` and `QRONOS_PACKAGED_RUNTIME` removed;
  - `LOCALAPPDATA`, `APPDATA`, `USERPROFILE`, `HOME`, `TEMP`/`TMP`, `QRONOS_DATA_ROOT`, `QRONOS_ASSET_ROOT` and the
    npm cache pointed into the workspace.
- **Workspace:** it must not exist yet, must be outside the repository, and its name must contain `pytest`. pytest
  uses `<workspace>/basetemp` and no cache provider. The test conftest adds its own per-test profile isolation on top.
- **Commands:** argument lists, never a shell, each with a timeout (`--timeout`, default 900 s, maximum 3600 s).
- **Report:** a JSON report on stdout (and in `--report` if given) with the source head, any changed files, each
  command and its actual exit code. The process exits with the first non-zero step exit: 124 on a timeout, 2 when
  a selection or tool is refused.

## What it does not do

- It does not run the app, the installer, native acceptance, models or devices.
- Live-device tests stay skipped.
- Files that execute native installers or write the registry (see the A103/A104 records) should not be selected
  on the development machine.
- It does not run the Rust tests or builds (`cargo test`, `cargo check`, `tauri build`); those stay separate,
  explicitly named checks.
