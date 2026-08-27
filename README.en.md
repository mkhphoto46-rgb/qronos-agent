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
| Tests on `main` | 131, all passing |

These are engineering estimates, not a release date. A tested module is not a finished feature — a feature is finished when it is connected end to end, packaged, secured, and validated on a clean Windows machine.

**Important:** `main` lags behind `feature/mvp-runtime-foundation`. The newer work (speech-to-text, VAD, voice pipeline, desktop shell) lives on that branch and has not been reviewed or merged yet.

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
| Mobile app (push-to-talk) | Secure remote, not a second cloud | ❌ Does not exist |
| Signed installer and updates | Installable product | ❌ Does not exist |
| Audit log and undo | Failure is never silent | ❌ Designed, not built |

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
         │   ├── Fast Brain  (warm)       │
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

Full detail in `docs/qronos_product_architecture.md`.

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

Unit tests require none of the following: a real microphone, an audio device, Ollama, or internet access. All audio input is simulated.

### Live tests (manual)

Scripts in `tools/` need real hardware and are not part of CI. They must be started explicitly:

```bash
python tools/test_qronos_wake_word_live.py    # needs a microphone
python tools/wake_word_recorder.py            # record training samples
```

---

## Project layout

```
core/
  main.py                 Entry point (currently echo only — does not start the pipeline yet)
  config.py               Paths and security defaults
  voice_trigger.py        Wake-word state machine
  openwakeword_engine.py  openWakeWord / ONNX adapter
  audio_input.py          Microphone access
  activity_guard.py       Gaming / creator detection + resource pressure
  resource_guard.py       Read CPU / RAM / GPU / VRAM
  resource_policy.py      Thresholds and allow / warn / block decision
  protection_engine.py    Resource protection level
  model_registry.py       Fast and Heavy Brain profiles
  model_manager.py        Resource-aware model selection
  ollama_controller.py    Local Ollama control
  task_router.py          Request routing
  task_plan.py            Multi-step execution plan
  orchestrator.py         Resource-aware plan execution

security/
  permissions.py          Risk levels and permission decisions

docs/                     Specifications and architecture
tests/                    Unit tests (no hardware required)
tools/                    Live scripts and audio recording
models/wake_word/         Threshold settings (no model files stored)
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

- Microphone, camera, remote access and external AI are **off by default**.
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
