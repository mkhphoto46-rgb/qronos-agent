# Personal use: setup and first run

Qronos is private and for personal use. Ordinary installation and startup do not need Windows code signing, hosted CI
or a public release. Every step below is an **owner action**; none of them has been performed or accepted yet.

## What to install

| Artifact | Location | Built from |
|---|---|---|
| Windows installer (UNSIGNED) | `E:/qronos-release-pinned-2026-10-16/delivery-31fc2adc/qronos-desktop_0.1.0_x64-setup.exe` (318,601,298 B, sha256 `96647219…1ef9`) | `31fc2adc`, Rust 1.98.1 |
| Android phone app (DEBUG-signed) | `E:/qronos-delivery/ac-2026-10-16/qronos-remote-0.1.0-debug.apk` (4,022,723 B, sha256 `d0f006b4…9af8`) | `53dd571` |

The current source differs from these builds only in documentation, tests and two source-side tools (the local
validation runner and the asset registry). Neither tool is packaged, so the artifacts above match the current product.

## Install on Windows

1. Run the setup file as your normal user. It installs per user.
2. Windows shows "unknown publisher" (SmartScreen) because the installer is unsigned. Choose **More info → Run
   anyway** only for this exact file.
3. The bundled Visual C++ runtime may ask for administrator approval (UAC). WebView2 is bundled as well.

## Models and assets

- **Speech, wake word, voice, image search and ffmpeg** are runtime assets, not part of the installer. An installed
  Qronos reads them from `QRONOS_ASSET_ROOT`, which is set for your user to `E:/qronos-runtime-assets`. A
  development checkout ignores that variable and uses `models/` and `runtime/` in the repository.
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
3. Press **جفت کردن گوشی جدید**. Scan the QR code with the phone app within two minutes, then press **وصل کن**. The
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
