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
- **Nothing to run:** with no selection, or with `--dry-run`, it prints the plan and runs no test. It still reads the
  source head and changed files with `git`.
- **Tools:** `--python` and `--node` must be absolute paths to existing executables. Nothing is installed,
  downloaded or looked up on `PATH`.
- **Isolation:** every child process gets the same environment:
  - `QRONOS_RUN_LIVE_DEVICE_TESTS=0`, `QRONOS_OLLAMA_AUTO_START=0` and `PYTHONDONTWRITEBYTECODE=1`;
  - `QRONOS_DESKTOP_WAKE_BOOTSTRAP` and `QRONOS_PACKAGED_RUNTIME` removed;
  - `LOCALAPPDATA`, `APPDATA`, `USERPROFILE`, `HOME`, `TEMP`/`TMP`, `QRONOS_DATA_ROOT`, `QRONOS_ASSET_ROOT` and
    the npm and XDG caches pointed into the workspace;
  - the store overrides `QRONOS_CONVERSATION_HISTORY_PATH`, `QRONOS_SETTINGS_PATH`, `QRONOS_QUEUE_STATE_PATH`,
    `QRONOS_PERMISSION_POLICY_PATH`, `QRONOS_PERMISSION_AUDIT_PATH`, `QRONOS_APPROVAL_SECRET_PATH` and
    `QRONOS_PAINT_REFERENCE_PATH` pointed at new, empty locations under `<workspace>/state`, so an inherited value
    never reaches real user data.
- **Workspace:** required to execute. It must not exist yet, must be outside the repository, and its name must
  contain `pytest`. Every redirected folder is created before the first child starts. pytest uses
  `<workspace>/basetemp` and no cache provider. The test conftest adds its own per-test profile isolation on top.
- **Commands:** argument lists, never a shell, each with a timeout (`--timeout`, default 900 s, maximum 3600 s). The
  timeout ends the direct child only; processes it started are not tracked.
- **Report:** a JSON report on stdout (and in `--report` if given) with the source head, any changed files, each
  command and its actual exit code. It stops at the first failing step and exits with that step's code: 124 on a
  timeout, 127 when the tool could not be started (with the reason), 2 when a selection, tool or workspace is
  refused.

## What it does not do

- It is a scoped runner, not a sandbox. A selected test runs with your user rights, so audit the source of what you
  select.
- It does not run the app, the installer, native acceptance, models or devices.
- Live-device tests stay skipped.
- Files that execute native installers or write the registry should not be selected
  on the development machine.
- It does not run the Rust tests or builds (`cargo test`, `cargo check`, `tauri build`); those stay separate,
  explicitly named checks.
