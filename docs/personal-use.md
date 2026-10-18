# Personal use: setup and first run

Qronos is private and for personal use. Ordinary installation and startup do not need Windows code signing, hosted CI
or a public release. Every step below is an **owner action**; none of them has been performed or accepted yet.

## What to install

| Artifact | Location | Built from |
|---|---|---|
| Windows installer (UNSIGNED) | `E:/qronos-delivery/personal-2026-10-17-2835768f/qronos-desktop_0.1.0_x64-setup.exe` (318,611,167 B, sha256 `ac441a6d…e85b`, built 2026-10-16 UTC) | `2835768f`, Rust 1.98.1 |
| Android phone app (DEBUG-signed) | `E:/qronos-delivery/ac-2026-10-16/qronos-remote-0.1.0-debug.apk` (4,022,723 B, sha256 `d0f006b4…9af8`) | `53dd571` |

Source equivalence, per component:
- **Windows product:** this installer is a fresh backend and desktop build of `2835768f`. It includes the voice-failure
  fix (A111/A112): a failed voice request shows its own Persian notice, re-checks the setup, and offers one explicit
  retry. A static peer accepted it (A113): the packed runtime bridge equals its source, and the voice notice is in the
  desktop assets.
- **Phone app:** `mobile/` is unchanged from `53dd571` through `2835768f`, so the existing APK still applies.
- **Previous installer:** `E:/qronos-release-pinned-2026-10-16/delivery-31fc2adc` (`96647219…1ef9`) is kept, but it
  lacks the voice-failure fix.

## Install on Windows

1. Run the setup file as your normal user. It installs per user.
2. Because the installer is unsigned, Windows SmartScreen may warn about an unknown publisher (the wording can
   differ). If it does, allow it only for this exact file.
3. The bundled Visual C++ runtime may ask for administrator approval (UAC). WebView2 is bundled as well.

## Models and assets

- **Speech, wake word, voice, image search and ffmpeg** are runtime assets, not part of the installer. An installed
  Qronos reads them from `QRONOS_ASSET_ROOT`, which is set for your user to `E:/qronos-runtime-assets`. A
  development checkout uses `models/` and `runtime/` in the repository, and uses the asset root only when
  `QRONOS_DEV_USE_ASSET_ROOT=1` is set; see `docs/asset-root-check.md`.
- All 34 registry files are present there: 30 verified against a published identity and 4 unverified (the custom
  wake head and two voice files, which are owner-supplied). "Present" means available, not measured for quality.
  - Check them read-only with `python -m build_support.runtime_assets --root E:/qronos-runtime-assets`.
- **Language models** come from Ollama on `127.0.0.1:11434`, using the tags in `config/brains.json`: fast
  `hf.co/unsloth/gemma-3n-E4B-it-GGUF:Q5_K_M`, heavy `gpt-oss:20b` and vision `qwen3-vl:4b-instruct`. Memory uses
  `bge-m3` through Qronos's own CPU sidecar on port 11435. These models are already in the Ollama store named by your
  user `OLLAMA_MODELS` setting. Nothing needs downloading.
- Qronos starts Ollama by itself when it needs it. If you prefer to start it yourself, run `ollama serve` (a future
  live step for you; it has not been run).

## Use

- Start **Qronos** from the Start menu. The status line shows connecting, then ready; if startup fails it offers one
  retry.
- Logs are under `%LOCALAPPDATA%\Qronos\Logs`, and your data is under `%LOCALAPPDATA%\Qronos`.

## Pair a phone (home network only)

1. Install the APK on the phone. Android asks you to allow installing from this source.
2. On the PC, open Settings → **اتصال گوشی** and turn on **اجازهٔ اتصال گوشی‌های جفت‌شده در این شبکه**.
3. Press **جفت کردن گوشی جدید**. The PC shows a QR code. Scan it with the phone's camera within two minutes: it opens the `qronos://pair` link
   in the Qronos app's pairing screen (the app has no scanner of its own). Then press **وصل کن**. The
   PC and phone must be on the same Wi-Fi.
4. To revoke a phone: on the PC, under **دستگاه‌های جفت‌شده**, press **لغو دسترسی** and confirm. On the phone,
   **قطع اتصال این گوشی** only clears the phone's own copy; revoke on the PC as well.

Access from outside the home network (**دسترسی از بیرون خانه**) is off by default and is **not available**: it needs
a relay host that has not been deployed, plus phone acceptance.

## Not yet checked

- A real install, launch and uninstall on this PC.
- Live wake word, speech recognition, voice output (the voice needs a Vulkan-capable GPU), the language models and
  the phone over Wi-Fi.
- Signed updates stay closed: an update must carry a valid signature from the owner's key, and that is not bypassed.

Development checks run locally; see `docs/local-validation.md`.
