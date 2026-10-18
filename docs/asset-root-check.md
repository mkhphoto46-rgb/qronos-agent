# Checking where Qronos finds its models

A development checkout reads its models and runtime assets from the checkout itself (`models/` and `runtime/`).
An installed Qronos reads them from one asset root:

1. `QRONOS_ASSET_ROOT` in the running process, if it is set and not blank;
2. otherwise your saved Windows user value of `QRONOS_ASSET_ROOT` (read from `HKCU\Environment`), so a Qronos started
   from an older Explorer or terminal still finds it;
3. otherwise the default, `%LOCALAPPDATA%\Qronos\RuntimeAssets`.

A development checkout uses that same asset root only when `QRONOS_DEV_USE_ASSET_ROOT=1` is set, so a machine-wide
`QRONOS_ASSET_ROOT` never silently swaps the checkout's models. A configured value must be an absolute path; a
relative one is refused with an error instead of being ignored. Development always keeps its data, logs and settings
in the checkout.

## Check it (no microphone, no model run)

From the development checkout:

```
.venv\Scripts\python.exe -m core.runtime_preflight
```

- `assetRoot` shows the folder Qronos will use.
- `features.wakeWord.assetsPresent` must be `true`; `missing` lists any wake file not found there.

In the installed app the same `assetRoot` appears in the setup check. If it is not the folder you expect, set the user
variable (for example in Windows "Edit environment variables for your account") and start Qronos again.

This shows where the files are found; it does not prove that the wake word detects your voice.
