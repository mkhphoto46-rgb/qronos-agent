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
- An installable standalone Windows application with its own polished UI/UX,
  system tray, activity history, approval history, and undo experience.

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

## Local Brain Model Selection

- Fast Brain uses the official Ollama tag `qwen3:4b-instruct`. The shorter
  `qwen3:4b` tag currently resolves to a thinking-only 2507 build and failed the
  fast-response benchmark, so it is not the configured Fast Brain.
  - Dense model with approximately 4.02 billion parameters.
  - Default Ollama artifact: Q4_K_M, approximately 2.5 GB.
  - Expected to fit on the development machine's 8 GB GPU for fast local use.
- Heavy Brain uses the official Ollama tag `qwen3:14b`.
  - Dense model with approximately 14.8 billion parameters.
  - Default Ollama artifact: Q4_K_M, approximately 9.3 GB.
  - Does not fit fully in the development machine's 8 GB VRAM and therefore
    requires partial CPU/RAM offload.
  - Must remain on-demand and must be blocked during gaming, creator, high,
    and critical resource states.
- Both models are from the Qwen3 family and are published under Apache 2.0.
- Every Heavy Brain request requires a fresh resource and activity check before
  loading, followed by a second check immediately before generation.
- The UI must expose model loading and reasoning states and stream output so a
  resource-aware handoff does not look frozen.

## Permission and Product Architecture

- Qronos uses five permission levels: automatic, voice confirmation, UI
  confirmation, typed Qronos secret, and always denied.
- Voice confirmation is limited to low-risk, visible, reversible operations and
  is not treated as biometric identity.
- High-risk actions require a typed Qronos secret or Windows Hello. Qronos must
  never request or store the user's Windows password.
- Credential access, security bypass, irreversible destruction, hidden
  surveillance, and self-modification of security policy are always denied.
- The LLM may propose structured actions but may not execute them directly.
  Execution belongs to a permission-gated Action Broker.
- Detailed requirements are preserved in `docs/qronos_product_architecture.md`.
- The installed Qronos runtime may not generate, analyze, modify, or execute
  code, scripts, macros, or arbitrary shell instructions under any permission
  level.
- Registry modification, boot or security configuration, raw-disk access,
  backup destruction, and privilege escalation are Level 5 / always denied.
- Malware, exploitation, credential attacks, unauthorized intrusion, vulnerable
  target scanning, hidden services, backdoors, and unauthorized persistence are
  also Level 5 / always denied.
- Resource protection is global: both Fast and Heavy Brain loads are blocked
  under high/critical pressure, and a fresh second check is required immediately
  before generation. Production still needs process-aware continuous monitoring
  so Qronos yields its own resources without reacting to its own VRAM reservation.
- External intrusion response is deterministic and separate from the LLM. It
  rejects, throttles, contains, and finally performs a safe shutdown only for
  high-confidence critical compromise, avoiding an attacker-triggered shutdown
  denial-of-service loop.

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
