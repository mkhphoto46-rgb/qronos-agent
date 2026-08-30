<div align="center">

# Qronos · کرونوس

**A local voice assistant for Windows**

Persian-first · Offline · Resource-aware · Permission-controlled

[فارسی](README.md) · **English**

</div>

---

## ⚠️ Project status

Qronos is **under development** and is not yet an installable application.

| | |
|---|---|
| Stage | Prototype |
| Progress against full scope | ~20% |
| Installable release | None |
| Tests on `main` | 1,146, all passing |

These are engineering estimates, not a release date. A tested module is not a finished feature — a feature is finished when it is connected end to end, packaged, secured, and validated on a clean Windows machine.

**Important:** `main` and `feature/mvp-runtime-foundation` have each moved on and neither has been merged into the other. `main` carries the storage manager, the web research layer and the device link. The branch carries speech-to-text, VAD, the voice pipeline and the desktop shell.

---

## What is Qronos?

You say **"Qronos"**. Your PC wakes up, understands you **in Persian**, and does the work on that same machine.

No audio is sent to any server. No paid service is ever required. With the internet unplugged, Qronos still works.

How it differs from what already exists:

- Cloud assistants (Copilot, Alexa and similar) send your voice to a server. Qronos does not.
- Existing local tools (Ollama, LM Studio and similar) are a chat window. Qronos is voice-activated and controls the computer.
- Almost none of them take **local Persian** seriously. Qronos is built for Persian from the start.

---

## Four principles

| Principle | What it means in practice |
|---|---|
| **Voice-first** | Speech is the primary input. Activation always goes through the local wake word |
| **Local-first** | Models, speech recognition and speech synthesis run on your own machine |
| **Resource-aware** | When you're gaming or editing video, Qronos backs off |
| **Permission-controlled** | Nothing that changes state happens without your approval |

Qronos **does not write, analyse or run code**. That is a deliberate boundary, not an unfinished feature.

---

## Capability status

| Capability | Target | Today |
|---|---|---|
| Resource & activity guards | Always yield to the user's workload | ✅ Implemented and tested |
| Permission engine (5 levels) | Hard denial at the top level | ✅ Tested, ❌ not wired to an executor |
| Model lifecycle | Load and unload intelligently | ✅ Implemented and tested |
| Wake word | Always-available local activation | ⚠️ Code ready, **model not ready** |
| Speech-to-text | Persian + English, local | 🔶 whisper.cpp wired in, language set to `auto` |
| Text-to-speech | Natural Persian voice | ❌ Does not exist |
| Fast Brain / Heavy Brain | Quick chat + deeper reasoning | 🔶 Integrated, not benchmarked on real hardware |
| App / file / browser control | Typed actions with undo | ❌ Interfaces and folders only |
| Desktop UI and system tray | Polished Windows experience | 🔶 Tauri shell on branch, not connected to the backend |
| Web research | Answers from the web, no cloud AI | ✅ Implemented and tested, not benchmarked with a real model |
| Storage manager | Never fill the user's disk | ✅ Implemented and tested |
| Device link, PC side | Encrypted phone-to-PC channel | ✅ Implemented and tested, verified on Windows |
| Mobile app (push-to-talk) | Secure remote, not a second cloud | ❌ Does not exist |
| Signed installer and updates | Installable product | ❌ Does not exist |
| Audit log and undo | Failure is never silent | 🔶 The device link has one; everything else is designed, not built |

### About the wake word

The current wake-word model is **not usable**. There is an integrity problem in the training data and CPU training was terminated.

Do not claim wake-word readiness until all of these pass:

1. Regenerate clean artifacts
2. Integrity gate + staged training
3. Validation and ONNX export

What is in the repository today is only the **adapter code** (`core/openwakeword_engine.py`) and its tests. No model files are stored in the repo.

---

## Architecture

```
                        User
                          │
        ┌─────────────────┴─────────────────┐
        │                                   │
    PC microphone                  Mobile app (PTT)
        │                                   │
        └─────────────────┬─────────────────┘
                          ▼
         ┌────────────────────────────────┐
         │        Always-on layer         │
         │  Supervisor · Resource         │
         │  Governor · Health & Recovery  │
         │  CPU wake word                 │
         └────────────────┬───────────────┘
                          ▼
         ┌────────────────────────────────┐
         │        Voice runtime           │
         │  VAD → STT (fa/en) →           │
         │  Normalizer → Intent Gate      │
         └────────────────┬───────────────┘
                          ▼
         ┌────────────────────────────────┐
         │      Intelligence core         │
         │  Task Router                   │
         │   ├── Deterministic (no LLM)   │
         │   ├── Fast Brain  (on-demand)  │
         │   └── Heavy Brain (on-demand)  │
         │  Planner · Orchestrator        │
         └────────────────┬───────────────┘
                          ▼
         ┌────────────────────────────────┐
         │     Safety & action plane      │
         │  Risk Classifier →             │
         │  Permission Engine →           │
         │  Typed Action Executor →       │
         │  Observe & Verify →            │
         │  Audit Log & Undo              │
         └────────────────────────────────┘
```

Hard architectural rules:

- Nothing starts before the wake word — no STT, no router, no gateway.
- The executor runs **typed actions only**, never arbitrary shell commands.
- The model can never override permission policy.
- The phone is only a remote; reasoning and computer control stay on the PC.

Full detail in `docs/qronos_project_context.md`. The phone link has its own document: `docs/QRONOS_DEVICE_LINK.md`.

---

## Requirements

| | |
|---|---|
| Target OS | Windows (`openwakeword` only installs on Windows) |
| Python | 3.14 |
| Models | Local Ollama on `127.0.0.1:11434` |
| Fast Brain | `qwen3:4b-instruct` (~2.5 GB) |
| Heavy Brain | `qwen3:14b` (~9.3 GB) |
| GPU | Optional; status read via `nvidia-smi` |

Note: the model entries on `main` are outdated and have been updated on `feature/mvp-runtime-foundation`.

---

## Setup

```bash
git clone https://github.com/mkhphoto46-rgb/qronos-agent.git
cd qronos-agent

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

### Running the tests

```bash
python -m unittest discover -s tests -v
```

Unit tests require none of the following: a real microphone, an audio device, Ollama, a language model, or internet access. All audio input is simulated. The device link's tests do open sockets, but only on loopback.

### Live tests (manual)

Scripts in `tools/` need real hardware and are not part of CI. They must be started explicitly:

```bash
python tools/test_qronos_wake_word_live.py    # needs a microphone
python tools/wake_word_recorder.py            # record training samples
```

### Diagnostics (manual)

Scripts in `tools/debug/` are diagnostics, not product. They are listed in
`release-exclude.txt` and refuse to run if they detect a release build.

```bash
python tools/debug/link_selfcheck.py      # does the device link work here?
python tools/debug/link_reachability.py   # can a phone on this network reach us?
```

The second one serves a plain HTTP page for a few minutes so a phone's browser
can prove it can open a connection. It stops itself, and it is not the link — a
browser cannot speak that protocol.

---

## Project layout

```
core/
  main.py                 Entry point (currently echo only — does not start the pipeline yet)
  config.py               Paths and security defaults

  Wake word and audio
    voice_trigger.py        Wake-word state machine
    openwakeword_engine.py  openWakeWord / ONNX adapter
    audio_input.py          Microphone access

  Resources and models
    activity_guard.py       Gaming / creator detection + resource pressure
    resource_guard.py       Read CPU / RAM / GPU / VRAM
    resource_policy.py      Thresholds and allow / warn / block decision
    protection_engine.py    Resource protection level
    model_registry.py       Fast and Heavy Brain profiles
    model_manager.py        Resource-aware model selection
    ollama_controller.py    Local Ollama control

  Task execution
    task_router.py          Request routing
    task_plan.py            Multi-step execution plan
    orchestrator.py         Resource-aware plan execution

  Storage manager         storage_*.py, artifact_ownership.py, model_store.py,
                          ollama_models.py — budgets, cleanup, ownership.
                          Only storage_janitor.py ever deletes anything.

  Web research            web_*.py, persian_text.py — search, page fetch,
                          evidence fencing, citation validation. No cloud AI.

  Device link             link_*.py — phone to PC on the local network, TLS 1.3
                          with a pre-shared key. See docs/QRONOS_DEVICE_LINK.md

  Vision                  screen_capture.py — one picture of the screen,
                          through the permission gate. windows_ocr.py reads it
                          for free as a hint the model checks against.
                          vision_worker.py describes; the Heavy Brain reasons.
                          See docs/qronos_vision.md

security/
  permissions.py          Risk levels and permission decisions
  watching.py             A grant that lasts, visibly, and ends by itself

docs/
  QRONOS_DEVICE_LINK.md     Phone link, Layer 1 and Layer 2
  qronos_project_context.md Product direction and constraints
  qronos_vision.md          What Qronos can see, and what it costs
  qronos_wake_word_spec.md  Wake-word specification
  voice_trigger_spec.md     Voice trigger specification

tests/                    Unit tests (no hardware, no network, no model)
tools/                    Live scripts that need real hardware
tools/debug/              Diagnostics — never shipped, see release-exclude.txt
models/wake_word/         Threshold settings (no model files stored)
release-exclude.txt       What a release build must leave out
```

---

## Roadmap

| Stage | Contents | Estimate |
|---|---|---|
| **Local alpha** | UI shell, Fast Brain, basic voice path, resource panel — no broad computer control | 2–4 months |
| **Security-reviewed beta** | Voice session, permission-gated limited tools, Action Broker, logging and undo, installer | 5–9 months |
| **Full scope** | Hardened remote access, broad computer and browser execution, recovery testing, Device Hub | 12–18+ months |

Assumes one primary developer, fixed requirements and no hardware blockers. Part-time work, wake-word retraining or code-signing delays extend these ranges.

### Next gate

1. Verify the real Fast and Heavy model artifacts on the development machine
2. Benchmark cold load, warm response, generation, unload and Fast Brain recovery
3. Process-aware continuous Resource Sentinel monitoring
4. Connect the permission policy to a structured Action Contract
5. First non-elevated desktop UI shell

**The real priority, before any UI work:** prove that whisper.cpp transcribes your Persian accurately and fast enough on the target hardware, and find a local Persian voice of acceptable quality. If those two fail, the rest of the product is pointless.

---

## Security and privacy

Rules that must not be broken:

- Microphone, camera, the device link, remote access and external AI are **off by default**.
- The device link's pairing keys live in `data/`, which is outside version control.
- Raw microphone audio is never sent to an external service.
- No recording file is created without an explicit request and user approval.
- State-changing actions require approval; destructive actions are denied by default.
- Qronos never bypasses UAC.
- Qronos does not generate, analyse or execute code.

---

## Contributing

| Rule | |
|---|---|
| Branches | Off `main`: `feature/*`, `fix/*`, `refactor/*`, `security/*`, `test/*` |
| Tests | Every new module gets unit tests that need no hardware |
| CI | `main` is protected; merges go through a pull request |
| After merge | Delete the feature branch |
| Libraries | Every external library sits behind an interface so it stays replaceable |

---

## Licence

No licence has been chosen yet.
