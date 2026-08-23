# Qronos Project Context

Last verified: 2026-08-23

## Purpose

This document preserves sanitized, project-relevant context recovered from the
user's ChatGPT export and reconciles it with the current local repository.
Raw conversations, personal data, credentials, and unrelated chat history are
intentionally excluded.

## Product Direction

Qronos is intended to become a local-first Windows automation agent with:

- A persistent supervisor and a separate user-session runtime.
- A desktop control center and system-tray experience.
- Local wake-word detection, STT, TTS, and voice-session management.
- Fast and heavy local brains selected according to task and resource pressure.
- Permission-gated control of applications, files, browsers, and devices.
- Resource protection for gaming and creator workloads.
- Optional secure remote control from a phone.
- Future support for printers, 3D printers, and other third-party devices.

## Architectural Decisions Recovered from Chats

### Process boundaries

- A Windows service must not directly own the interactive UI or microphone.
- The supervisor, user runtime, desktop UI, and elevated action broker should be
  separate processes with a narrow authenticated IPC boundary.
- Administrator access must be requested only for the specific operation that
  requires it. UAC must never be bypassed.

### Power and background behavior

- Wake-word listening is expected while Windows is awake, including screen-off
  and power-saving modes when resources permit.
- Traditional sleep, hibernation, and shutdown cannot reliably run a Python
  wake-word detector.
- Wake-from-sleep should use hardware-dependent capabilities or secure remote
  Wake-on-LAN rather than claiming universal voice wake support.

### Safety and permissions

- Read-only actions may be low risk.
- File changes, application execution, installation, uninstallation, remote
  control, and device actions require explicit policy checks.
- Destructive file operations need preview, confirmation, journaling, and a
  recoverable path such as Recycle Bin or quarantine.
- Package installation must resolve an exact package, publisher, source, and
  version before execution.

### Resource policy

- Qronos distinguishes normal, gaming, creator, high-pressure, and critical
  operating conditions.
- Heavy models, vision, and background work may be blocked or unloaded to
  protect CPU, RAM, GPU, VRAM, and temperature limits.
- Simple deterministic commands should avoid invoking an LLM when possible.

## Wake-Word Training History

The recovered Qronos conversations document a long openWakeWord training path
through Google Colab and Molab. Important resolved failures included:

- Python package and import conflicts involving torch, torchvision, and
  torchmetrics.
- Missing openwakeword and ONNX export dependencies.
- Incorrect audio sample rates and RIR directory handling.
- Large ACAV memory-map and validation-memory investigations.
- Finite DataLoader exhaustion that prevented validation history from being
  populated.
- Broken and partially removed augmentation code in train.py.
- A corrupted transform line that needed `x = np.vstack(x)`.
- Optional TFLite conversion failure after successful ONNX export.

The final training run completed all three sequences and exported the ONNX
classifier before optional TFLite conversion failed.

## Current Verified Repository State

- Active development branch: `feature/qronos-wake-word-model`.
- Qronos model files exist locally under `models/wake_word/`:
  - `qronos.onnx`
  - `qronos.onnx.data`
  - `qronos_recommended_settings.json`
- The ONNX model loads successfully with ONNX Runtime on CPU.
- Model input: `[1, 16, 96]`, float tensor.
- Model output: `[1, 1]`, sigmoid score.
- `OpenWakeWordEngine` starts and stops successfully with the real model.
- The configured detection threshold is `0.660167`.
- The complete local test suite passes: 131 tests.
- Wake-word model files are ignored by the current `.gitignore` and therefore
  are not included in Git commits by default.
- The wake-word engine, its tests, and the live test tool are currently
  uncommitted local changes.
- The main application entry point is not yet wired end-to-end to the wake-word
  service, router, orchestrator, STT, or TTS.
- No live microphone detection test has been authorized or completed for this
  trained model.

## Model Quality Risk

The exported model is technically valid but not production-ready. Training
reported:

- Accuracy: approximately 67.4%.
- Recall: approximately 36.7%.
- False positives: approximately 71.5 per hour.

The likely user impact is frequent accidental activation while still missing a
large portion of real Qronos utterances. Raising the threshold may reduce false
positives but will normally reduce recall further. The model should be treated
as experimental until live recordings, hard negatives, threshold calibration,
and a new validation run show acceptable results.

## Recommended Next Order

1. Run an explicitly authorized live microphone test and record raw score
   behavior for positive and negative speech.
2. Decide measurable acceptance targets for recall and false positives per hour.
3. Improve positive diversity and hard-negative coverage, then retrain or
   calibrate the threshold.
4. Connect AudioInput, OpenWakeWordEngine, and VoiceTriggerService in the main
   runtime with safe start, stop, pause, and recovery behavior.
5. Add STT, voice-session management, and TTS after wake-word reliability is
   acceptable.
6. Complete security review, decide how model binaries will be distributed,
   then commit locally.
7. Push or publish only after explicit user authorization.

## Collaboration Requirements

- Explain implementation steps in Persian for a complete beginner.
- Keep code, commands, file names, framework names, and technical terms in
  English when appropriate.
- Provide complete copy-ready code when the user must make a manual change.
- Use evidence-based critical review and identify risks rather than presenting
  optimistic estimates as verified facts.
