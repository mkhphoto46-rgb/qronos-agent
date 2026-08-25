# QRONOS AGENT — MASTER PROJECT CONTEXT

> **Single Source of Truth**
> Revision: `2026-08-25-r2`
> Supersedes: `QRONOS_MASTER_CONTEXT_2026-08-25` (r1)

This document carries the architecture, security model, resource management, memory,
storage, vision, voice, filesystem, UI/UX, implementation status and continuation
rules for Qronos in one place.

Written in English with Persian preserved for user-facing strings, so that technical
decisions transfer without ambiguity.

## Status markers used in this document

```text
LOCKED      decided; do not change without explicit user request
REVISED     changed in r2; the r1 rule is superseded
PENDING     decided in principle, blocked on a real measurement
OPEN        not yet decided; do not invent an answer
```

Where r2 supersedes r1, the reason is recorded in the change log kept by the user
outside this repository.

---

# 1. Project Goal — LOCKED

Qronos is a personal, voice-first, local-first, free, API-key-free, secure and
resource-aware agent for Windows.

- Persian and English conversation
- Computer control with user permission
- Analysis, planning and execution of multi-step tasks
- Observing and analysing the desktop and software errors
- Preventing system overload, freezing or crashes
- The operating system and the user's current work always outrank the AI

---

# 2. Reference Hardware and the Binding Constraint — LOCKED

```text
OS:           Windows 11 Pro for Workstations
CPU:          Intel Core i5-12400F
RAM:          32 GB
GPU:          NVIDIA RTX 3070 Ti
VRAM:         8 GB
Project path: E:\Project Qronos Agent
```

**8 GB of VRAM is the binding constraint on the entire design.** Every model
decision, concurrency rule and latency target follows from it. When a design choice
conflicts with the VRAM budget, the VRAM budget wins.

All Resource Profiles must be benchmarked on this machine. Resource figures must
never be guessed by a language model.

---

# 3. Latency Classes — REVISED

r1 split the brains by *capability* (Fast vs Heavy). r2 splits by **latency class**,
because on 8 GB of VRAM latency is what actually breaks.

```text
INTERACTIVE
  The user is waiting to hear an answer.
  Must be fully VRAM-resident. No CPU offload. No cold load on the critical path.

DEFERRED
  The user is told the work will take time and is notified on completion.
  May offload to CPU, may be slow, may be cancelled and requeued.
```

Rules:

- A model that does not fit fully in VRAM alongside its KV cache **may not serve
  interactive requests**.
- Cold-loading weights is never allowed on an interactive path.
- Any capability that cannot meet its latency class is moved to the other class or
  cut, never shipped as a slow interactive path.

---

# 4. Model Roster — REVISED

## 4.1 Interactive model — Fast Brain and Vision Worker are one model

```text
Model:        Qwen/Qwen3-VL-4B-Instruct-GGUF
Quantization: Q4_K_M language model + mmproj Q8_0 vision encoder
Size:         approximately 2.5 GB + 454 MB
Runtime:      llama.cpp with CUDA
Mode:         Instruct, not Thinking
Residency:    permanently resident while Qronos is running
Roles:        conversation, routing fallback, summarisation, translation,
              short writing, structured output, simple tool planning,
              screenshot understanding, OCR, GUI grounding, visual observations
```

r1 specified `qwen3:4b-instruct` and `Qwen3-VL-4B-Instruct` as two separate workers
with separate weights. Qwen3-VL-4B is a 4B instruct model that also accepts images,
so one model serves both roles. This removes one worker process, one set of weights
and one load/unload cycle from the critical path.

Vision still only ever *observes*. It never clicks or modifies anything.

## 4.2 Deferred model — Heavy Brain

```text
Model:        qwen3:14b
Quantization: Q4_K_M
Size:         approximately 9.3 GB
Class:        DEFERRED ONLY
Execution:    on-demand, partial CPU/RAM offload permitted
```

9.3 GB of weights do not fit in 8 GB of VRAM alongside a KV cache. Heavy Brain is
therefore **never** on an interactive path. A Heavy request is acknowledged
immediately by the interactive model, queued, and answered when ready.

`PENDING` — an 8B-class alternative that fits fully in VRAM should be benchmarked
against `qwen3:14b` on the reference PC. If an 8B model is close enough in quality,
it becomes the Heavy Brain and moves to the INTERACTIVE class, which removes the
deferred path entirely. Do not assume the outcome.

## 4.3 Speech to text

```text
Engine:   whisper.cpp
Model:    ggml-large-v3-turbo
Size:     1.51 GB (measured)
Language: forced to `fa`, never `auto`
```

Forcing Persian measurably outperforms auto-detection on Persian speech. Verified on
real Persian voice notes during r2 authoring.

## 4.4 Speech synthesis — OPEN, highest unowned risk

r1 named no TTS model anywhere in 2,075 lines, while specifying image generation and
vision in full. Qronos is a Persian voice-first product that currently cannot speak.

Candidate: Piper, which publishes five Persian voices
(`fa_IR-amir-medium`, `fa_IR-ganji-medium`, `fa_IR-ganji_adabi-medium`,
`fa_IR-gyro-medium`, `fa_IR-reza_ibrahim-medium`), all at medium quality, Apache-style
licensing, CPU-only, no API key.

`OPEN` — a voice must be chosen by listening to all five. Until then the product has
no output path and no section of this document may assume one.

## 4.5 Image generation — DEFERRED to phase 2

r1 approved `SANA 1.5 1.6B` (~9.73 GB). Its components — diffusion transformer, a
Gemma-2-2B text encoder and a VAE — cannot be resident on an 8 GB card alongside the
interactive model. During generation Qronos would be unable to speak, which
contradicts the priority order in section 7, where the voice session outranks image
generation.

Under section 6 rule 8, a game or render starting mid-generation cancels the job. On
a gaming and creator workstation, most image jobs would not complete.

Image generation is removed from v1. Revisit when the desktop product works and only
with a real benchmark.

## 4.6 Mobile app and encrypted gateway — DEFERRED to phase 2

An inbound authenticated listening service, pairing protocol, key management and two
mobile clients constitute the highest-risk component in the design, for the smallest
gain. Section 14.2 forbids unauthorised persistence and backdoors; this feature is a
listening service by construction.

Removed from v1.

---

# 5. Core Architecture — LOCKED

```text
User
→ Wake Word
→ Voice Session / Text Input
→ Supervisor
→ Task Router
→ Resource Governor
→ Safe Queue or Model Runtime Manager
→ Interactive Model / Heavy Brain
→ Risk Classifier
→ Permission Engine
→ Typed Executor
→ Observe and Verify
→ Audit / Undo record
→ Response / UI Task Event
```

Wake Word is always the first Qronos component after the user.

Language models never execute Windows actions directly. They propose structured
intent; the Typed Executor performs approved actions.

## Supervisor children

```text
Supervisor
├─ Resource Governor
├─ Storage Manager
├─ Memory Manager
├─ Filesystem Knowledge
├─ Model Runtime Manager
├─ Safe Queue
├─ Permission Engine
├─ Screenshot Broker
└─ Health / Telemetry Monitor
```

---

# 6. Resource Governor — Non-Negotiable Rules — LOCKED

There is no manual application categorisation and no unsafe override.

Forbidden controls, permanently:

```text
Always Protect · Normal · Allow AI Competition · Ignore
Force Run · Ignore Safety · Use All Resources
```

The user must never be asked whether Premiere, Blender, Photoshop, AutoCAD, a game or
any other program deserves protection. Qronos protects every user workload
automatically, including applications it has never seen.

Qronos never kills, limits or slows the user's applications. Qronos yields its own
resources.

## Mandatory heavy-task lifecycle

```text
Classify → Estimate → Check → Reserve → Prepare → Load → Monitor → Release
```

## Rules

1. Resource demand comes from benchmarked Resource Profiles, never from a model's
   guess.
2. Checks cover CPU, RAM, Windows Commit, pagefile, GPU utilisation, VRAM,
   temperatures, disk I/O, free disk space, current pressure and active models.
3. `Check + Reserve` must be **atomic**. The Safe Queue proposes; only the
   reservation ledger admits. Nothing else may grant admission.
4. Required resources plus adaptive safety headroom must be available.
5. Insufficient resources means notify the user and place the task in the Safe Queue.
6. Unknown resource requirement means loading is **forbidden**.
7. Runtime monitoring continues for the whole task.
8. If the user's workload grows, the AI pauses where a real pause exists, otherwise
   it cancels safely, unloads and requeues.
9. The user may view, cancel and reorder queued tasks, and may never bypass resource
   safety.
10. Only one model may own the GPU at a time, beyond the permanently resident
    interactive model.

## Fail-closed — REVISED

Resource protection is **fail-closed**. If no trustworthy snapshot exists, loading is
refused.

```text
sensor unavailable      → treat as unsafe, refuse the load
reading raises          → treat as unsafe, refuse the load
snapshot older than TTL → treat as unsafe, re-read or refuse
```

r1 stated this intent in the addendum but the shipped implementation inverted it: a
failed GPU read caused every GPU and VRAM check to be skipped, and a raised exception
returned the *least* protective pressure state. Both are defects against this rule
and are fixed in r2. A broken sensor must never read as "all clear".

Every request that can trigger a meaningful load is rechecked immediately before
generation, because an earlier snapshot goes stale.

---

# 7. Fixed Priority Order — LOCKED

```text
1.  Windows and system stability
2.  User foreground and active workloads
3.  Mouse, keyboard, audio input and Wake Word
4.  Safety and permission monitoring
5.  Active voice session
6.  Lightweight deterministic services
7.  Interactive model
8.  Urgent vision tasks supporting the user's current work
9.  Heavy Brain
10. Background and queued AI tasks
```

The user's workload always wins. Qronos waits or unloads rather than competing.

---

# 8. Workload Detection — Behaviour, Not Identity — REVISED

Section 6 requires protecting unknown applications. r1's implementation matched a
hardcoded list of about eleven process names, which is developer-side manual
categorisation and fails for every application not on the list.

r2 detects the *shape of the workload*, never its name:

```text
exclusive or borderless fullscreen window present
sustained high GPU utilisation by a non-Qronos process
foreground process holding recent keyboard/mouse focus
sustained high disk I/O by a non-Qronos process
rising GPU or CPU temperature under non-Qronos load
```

Any of these means a real user workload is running and Qronos yields. No process
name lists are maintained. `DEFAULT_GAME_PROCESSES` and
`DEFAULT_CREATOR_PROCESSES` are removed.

Manual mode overrides may only ever make protection *stronger*. A caller must not be
able to select a less protective state.

---

# 9. Telemetry — REVISED

## Always-running lightweight services

```text
Wake Word · VAD · Supervisor · Deterministic Task Router
Telemetry Monitor · Resource Governor · Safe Queue
Permission Engine · Health Monitor · Screenshot Broker
Model Runtime Manager
```

Target idle behaviour:

```text
GPU usage:  approximately zero
CPU usage:  preferably below 2%
RAM:        approximately 300–800 MB for control services
```

## Split sample rates

r1 mandated a single 250–500 ms cadence for every signal while also requiring under
2% idle CPU. Those two demands conflict: enumerating hundreds of processes four times
a second is not free, and the cost merely moved from per-command to per-tick.

```text
250–500 ms   CPU load, RAM, VRAM, GPU utilisation      (cheap counters)
1–2 s        temperatures, disk I/O
2–5 s        process enumeration, foreground window state
```

## Mechanism

- GPU and VRAM are read through **NVML** with a held handle, never by spawning
  `nvidia-smi`. r1 forbade per-command subprocesses while the implementation spawned
  one on every read.
- CPU load is sampled **non-blocking**. A blocking half-second sample inside the
  decision path makes the latency target in this section unreachable.
- No full WMI scans on the decision path.

## Latency targets

```text
Normal: 20–70 ms
P95:    below 100 ms
P99:    below 200 ms
```

Command handling reads the cached snapshot and performs only a fresh critical
RAM/VRAM revalidation.

## Persistence

Telemetry lives in a RAM ring buffer. It is **not** written to disk on the sample
cadence. Only essential summary state is persisted, to minimise SSD writes.

## Speculative preparation

Partial transcription may predict the required worker before the user stops speaking.

```text
Do not allocate VRAM before final classification and reservation.
Do not pin large RAM allocations.
Cancel immediately when user resource pressure rises.
Cancel if final routing selects another worker.
```

## Persistent runtime, non-persistent weights

Worker processes and IPC channels may stay ready. Large weights, KV caches and
inference buffers unload when no longer justified. The interactive model is the one
exception and stays resident.

---

# 10. Model Admission and Concurrency — LOCKED

```text
One GPU owner at a time, beyond the resident interactive model.
```

The 8 GB card must not host the interactive model and a second heavy model
simultaneously unless a real benchmark proves the combination fits. No exception is
assumed in advance.

Admission is granted only by the atomic reservation ledger described in section 6
rule 3.

---

# 11. Resource Pressure States — LOCKED

### Low

- Interactive model resident.
- Deferred work may run after reservation.

### Medium

- Cancel speculative prefetch.
- Avoid Heavy Brain.
- Permit only short lightweight tasks when safe.

### High

- Pause or cancel the active deferred worker.
- Unload all non-essential models.
- Keep control, safety and voice-core services alive.

### Critical

- Stop all inference workers.
- Release reservations and temporary buffers.
- Reject new model loads.
- Preserve only safety, recovery, Wake Word and Supervisor.

---

# 12. Safe Queue — LOCKED, with one OPEN item

The Safe Queue replaces forced execution. It proposes tasks; it never admits them.

## Ordering

Ordering is by **explicit priority level**, never by arrival time. Arrival-order
scheduling lets two tasks race for admission, and a lost race can end with both
executing — a critical resource failure.

The user may reorder within the queue. User reordering can never lift a task above
the user's own workload and can never skip the resource gate.

`OPEN` — the tie-break between two tasks of equal priority. A stable monotonic
sequence number used **only** as a tie-break, never as the primary key, is the
suggested answer, but it is not yet decided.

## State machine

```text
queued → admitted → running → (paused) → completed
                            → cancelled → requeued
                            → failed
```

`PENDING` — which workers support a genuine pause rather than only cancellation is a
property of each runtime and must be measured on the reference PC. Until measured,
assume cancel-only.

---

# 13. Runtime Processes — REVISED

r1 specified five executables with Windows Job Objects, per-process memory limits,
heartbeats, watchdogs, crash isolation and requeue. That is isolation infrastructure
for a concurrency pattern section 10 forbids.

r2 uses two processes:

```text
qronos-supervisor.exe   control plane, always-on services, IPC, tray
qronos-model-host.exe   hosts whichever model is currently admitted
```

Each still requires:

- Windows Job Object containment
- Memory limit
- Lower CPU and I/O priority than user applications
- Heartbeat and watchdog
- Timeout and cancellation token
- Crash isolation
- Temporary-file cleanup
- VRAM and reservation cleanup
- Automatic safe requeue where appropriate

A model-host crash must never terminate the Supervisor, Wake Word or the application.
Further process separation is added only when a specific observed crash mode
requires it.

---

# 14. Security and Execution Boundary — LOCKED

```text
LLM proposes structured action
→ Risk Classifier
→ Permission Engine
→ Typed Executor
→ Observe
→ Verify
→ Audit / Undo record
```

## 14.1 Five authorisation levels

```text
Level 1 — Automatic
Level 2 — Voice Confirmation
Level 3 — UI Confirmation
Level 4 — Typed Qronos Secret / Windows Hello
Level 5 — Always Denied
```

- Voice confirmation is for low-risk, visible, reversible operations only.
- Voice confirmation is not biometric identity.
- High-risk operations require a typed Qronos secret or Windows Hello.
- Qronos never requests or stores the user's Windows password.
- Real execution passes only through the permission-gated Typed Executor.

`REVISED` — the shipped `security/permissions.py` implements a different five
(`SAFE_READ`, `CREATE_OR_EDIT`, `RUN_APPLICATION`, `CONTROL_SYSTEM`, `SENSITIVE`),
which classify *action kind* rather than *authorisation method*. Action kind is an
input to the Risk Classifier; it is not the authorisation ladder. The ladder above is
the locked model and the code must be brought to it.

## 14.2 Always-denied boundaries

Never permitted by an installed Qronos runtime:

```text
Credential access · Security bypass · Irreversible destructive actions
Hidden surveillance · Self-modification of security policy
Registry modification · Boot/security configuration · Raw disk access
Backup destruction · Privilege escalation · Malware/exploitation workflows
Unauthorised intrusion · Unauthorised persistence or backdoors
Dangerous attacker-oriented scanning
```

## 14.3 Code and script boundary

The installed runtime must not freely generate or run code, scripts or macros, must
not execute arbitrary shell instructions, and must not create self-modifying
automation. Every automation is composed of typed, predefined, auditable actions.

## 14.4 Intrusion response

Deterministic and independent of any language model:

```text
detect → reject/throttle → contain → recover
→ safe shutdown only on high-confidence critical compromise
```

An attacker must not be able to drive Qronos into a shutdown loop by triggering false
positives.

---

# 15. Structured Output and Hallucination Control — REVISED

Deterministic gates sit in front of the models and behind them.

```text
input  → deterministic gate → model
model  → deterministic gate → consumer
```

The inbound gate classifies and routes before a model is involved. The outbound gate
validates the model's answer against a fixed schema. A gate that only filters inputs
still lets an invented field or a fabricated path through on the return trip.

**Grammar-constrained decoding is mandatory for every structured response.**
llama.cpp and Ollama both provide JSON-schema and GBNF grammar enforcement. r1 built
an entire anti-hallucination framework without naming the one cheap mechanism that
does most of the work. Structured output is never parsed out of free text.

Cheapest evidence first, everywhere — not only for file classification:

```text
Level 1  deterministic rules and metadata
Level 2  patterns
Level 3  model inference, on demand only
```

A model is never used for a decision that rules can make.

---

# 16. Storage Manager — REVISED budgets

The Storage Manager is a formal component under the Supervisor, responsible for:

```text
Disk quota · Cache eviction · Temporary file cleanup · Log rotation
Memory compaction support · Model storage awareness
Free-space emergency handling · Orphan cleanup · Vision retention
Artifact ownership registry · Filesystem metadata budget
```

Governing principle:

```text
Disk pressure
→ Qronos cleans Qronos-owned disposable data
→ never user-owned files
```

Critical disk pressure sequence:

```text
Stop model downloads
Clear disposable cache
Clear temp
Pause deferred tasks
Notify user
```

## 16.1 Budgets

r1 set 35 GB of install plus 30 GB of managed data. The managed figures had no model
behind them: 15 GB of Persian text is on the order of fifteen thousand books, and a
million embeddings at 384 dimensions is about 1.5 GB — nothing that respects the
memory rules in section 18 can fill 15 GB. Only unpruned transcripts could, and those
are forbidden.

r2 budgets:

```text
Memory system            2 GB hard cap
Vision temporary data    500 MB hard cap, 7 days maximum age
Filesystem metadata      500 MB hard cap
Logs and temp            500 MB hard cap
------------------------------------------------
Managed data total       approximately 3.5 GB
```

Cleanup triggers on `size OR age`, whichever arrives first.

**A cap that is unreachable is decoration, not a safety limit.** Crossing the memory
cap is treated as evidence of a defect — it raises an alarm and is investigated. It
does not silently evict meaningful memory.

Vision temporary data covers scoped screenshots, OCR crops, transient thumbnails and
short-lived visual analysis artifacts. Ordinary screenshots are deleted at the end of
their task unless the user explicitly saves them. Qronos is not a desktop history
recorder. Given that rule, 500 MB is generous; if it fills, cleanup is broken.

## 16.2 Install footprint

```text
Interactive model (Qwen3-VL-4B, Q4_K_M + mmproj)   approximately 3.0 GB
Heavy Brain (qwen3:14b Q4_K_M)                     approximately 9.3 GB
Whisper large-v3-turbo                             1.51 GB (measured)
Persian TTS voice                                  PENDING, expected under 100 MB
Wake word model                                    small
Application and runtime                            PENDING
------------------------------------------------------------------------
Install total                                      approximately 15 GB
```

Total on-disk requirement is therefore around **18 GB**, against r1's 65 GB. Removing
image generation accounts for most of the difference. Figures marked `PENDING` must be
measured, not estimated.

## 16.3 Storage philosophy

Minimise unnecessary SSD writes. Telemetry stays in RAM. Persistent storage holds
only summary and essential state.

---

# 17. Artifact Ownership — LOCKED

Every artifact Qronos creates carries an ownership state.

```text
owner = qronos                    owner = user
temporary = true                  temporary = false
auto_cleanup = allowed            auto_cleanup = false
counts_toward_quota = true        counts_toward_quota = false
```

Transfer happens when the user explicitly says save, keep, move, export or organise
and the operation verifies:

```text
User request
→ target validation
→ permission check
→ copy/move/save
→ verify destination
→ ownership = user
```

After transfer Qronos may **never** delete that file for cache or storage reasons. It
leaves the quota entirely: it stops counting toward any budget and stops ageing.

A transferred artifact is physically moved out of Qronos's working area, so the
accounting is true on disk rather than a flag in a database.

This applies to generated images, PDFs, reports, renders, converted files, downloaded
assets, temporary archives and every other generated artifact.

Age and size caps apply **only** to Qronos-owned artifacts. Without that
qualification the 30-day rule would eventually delete files the user owns.

---

# 18. Memory Architecture — LOCKED structure, REVISED budget

Qronos grows with the user without accumulating raw history forever.

```text
Memory Manager
├─ Working Memory       current conversation, active files, minutes to session end
├─ Short-Term Memory    recent unfinished tasks, 7–30 days, expires or promotes
├─ Episodic Memory      summaries of significant events, no permanent transcript
├─ Core User Memory     stable preferences and conventions, small and distilled
├─ User-Pinned Memory   explicit, scoped session | project | global
├─ Memory Admission Engine
├─ Memory Forget Engine
├─ Consolidator
└─ Retrieval Engine
```

## 18.1 Admission

Models never write memory directly.

```text
model proposes candidate
→ Memory Admission Engine
→ policy and scoring
→ store or reject
```

Scored on relevance, stability, recurrence, future usefulness, user explicitness,
confidence, sensitivity and novelty.

A single observation never becomes a permanent preference. Permanent memory requires
an explicit user statement or accumulated evidence.

## 18.2 User control

```text
user instruction → Memory Intent Parser → Scope Resolver → Memory Policy
→ store / update / forget
```

Explicit user instruction outranks automatic inference. A memory command can never
override security or resource safety.

Real forgetting deletes the active memory, removes it from the retrieval index,
removes dependent derived summaries where applicable, and retains only minimal
deletion audit metadata. The audit must not re-store the deleted content.

## 18.3 Consolidation and forgetting

Forgetting is a feature. Each memory may carry:

```text
importance · confidence · last_used · access_count · scope · source · pinned
```

Low-value, old, unused memory is compressed, merged or expired. Frequently recalled
memory gains importance. User-pinned core memory is never auto-deleted for disk
pressure.

Before deleting meaningful memory:

```text
consolidate → compress → expire low-value short-term → merge duplicate episodic
→ compact indexes → delete only when appropriate
```

## 18.4 Personality

```text
Base personality + explicit preferences + learned preferences + current context
```

Learnable: response length, technical depth, preferred language, formality, humour
level, frequency of clarifying questions, workflow habits.

Personality comes from curated memory, never from unbounded transcript storage.

The personality profile is the most sensitive data Qronos holds. It must be a single
inspectable artifact the user can read and delete, not state scattered across a
database.

---

# 19. Filesystem Knowledge — REVISED

r1 specified a live index of every file on every volume, USN Journal integration, a
change watcher and background enrichment — a search-engine-scale subsystem serving two
features. It also conflicts with the idle CPU target in section 9.

r2 keeps what those two features actually need:

```text
Location Alias Registry     learned from confirmed destinations, kilobytes
On-demand scoped scanning   runs only for an approved job, discarded after
Artifact Ownership Registry
```

Removed from v1: the persistent whole-drive index, the File Change Watcher, USN
Journal integration and background enrichment.

## 19.1 Location aliases

Several folders legitimately share a name:

```text
C:\Users\...\Pictures   ·   D:\Photos   ·   E:\Photos   ·   F:\Archive\Photos
```

A destination is never chosen by name alone. Evidence: folder name, file-type
distribution, user activity, last confirmed destination, explicit aliases, folder
context, confidence score.

```text
alias: "عکس‌هام"  →  E:\Photos
confidence: high
scope: global
```

Ambiguity means asking the user, then recording the alias. A random destination is
forbidden.

## 19.2 Classification

Cheapest evidence first, per section 15.

```text
Level 1   extension and embedded metadata (EXIF, ID3)
Level 2   filename and folder patterns — Friends.S04E07, The.Matrix.1999
Level 3   model inference, only where levels 1 and 2 are insufficient
```

Low confidence means no action.

## 19.3 Relationships — LOCKED, non-negotiable

Organisation must never break file relationships.

```text
TV series episodes and seasons · subtitles with video
Premiere projects with media · After Effects projects with assets
Blender projects with textures · AutoCAD dependencies
software projects · RAW with XMP sidecars · proxies · render assets
game and mod structures
```

```text
Qronos must not improve category organisation
at the cost of breaking file relationships.
```

Project boundary is tested before independent classification:

```text
File → part of a project or collection?
  ├─ yes → preserve boundary, classify nothing inside it independently
  └─ no  → classify independently
```

A `.wav` inside a Premiere project is project audio, not music.

---

# 20. File Organisation — REVISED to link-first

r1 specified bulk moves, illustrated with 62,180 proposed moves across a user's media
library. Cross-volume moves are copy-then-delete rather than atomic; a mid-batch
crash, a name collision or a partially written file leaves state the undo journal
must reason about ambiguously. This is the single most destructive operation in the
design.

**v1 does not move user files.** It builds an organised *view* using hardlinks,
junctions or symlinks, leaving every original exactly where it is.

```text
Inventory → Classify → Discover relationships → Build view plan
→ Detect conflicts → Preview → User approval → Create links in batches
→ Verify each batch → Undo journal
```

Properties:

- Data-loss risk is zero; originals are never touched.
- Undo is deleting the view.
- The user gets the actual value — browsing series and projects properly organised.

Real moves may be introduced later, per-scope, once the classifier has earned trust.

Preview before any bulk operation stays mandatory:

```text
Found:      41,230 images · 3,281 videos · 18,400 audio files
            214 projects · 37 series · 812 movies
Proposed:   62,180 links
Ambiguous:  1,241 files
Protected:  8,420 relationships
```

The user may narrow scope in natural language:

```text
«فقط عکس‌ها»   ·   «سریال‌ها رو دست نزن»   ·   «فقط D: رو مرتب کن»
```

On error: stop, preserve everything remaining, report. Never continue past a failed
batch.

---

# 21. Voice Pipeline

```text
Microphone → Wake Word → Voice Session → VAD → Whisper (fa)
→ Language and Command Normalizer → Intent and Scope Gate → Task Router
```

Achieved and approved:

- Wake Word starts a voice session.
- Follow-up turns inside an active session need no wake word.
- End-of-turn silence target approximately 2 s.
- Session inactivity timeout approximately 60 s.
- Maximum spoken turn approximately 60 s.
- Conversation history is stored per session and can be reset.
- Interactive-to-deferred handoff preserves shared conversation context.
- Qronos never identifies itself as Qwen, a provider or a model ID.
- UI labels use role names, never model IDs.
- MVP languages: Persian and English, defaulting to Persian.

`OPEN` — the output half of the pipeline. There is no TTS decision. Until section 4.4
is resolved, Qronos is voice-in, text-out.

## 21.1 Persian normalizer

Deterministic, no model:

```text
ی/ي and ک/ك unification · ZWNJ handling
Persian and ASCII digit normalisation · spoken number resolution
punctuation normalisation
```

`OPEN` — not yet implemented. Required by the Intent and Scope Gate.

---

# 22. UI/UX — Canonical Specification

## 22.1 Paths

```text
E:\Project Qronos Agent\desktop\src\App.tsx
E:\Project Qronos Agent\desktop\src\App.css
E:\Project Qronos Agent\desktop\src\components\QronosOrb.tsx
E:\Project Qronos Agent\desktop\src\components\OrbState.ts
E:\Project Qronos Agent\desktop\src\components\OrbTaskRenderer.tsx
```

Root `src/App.jsx` and `src/components/QronosOrb.jsx` are not part of the real UI.

## 22.2 Technology split

```text
React + TypeScript      UI, pages, interactions
Canvas/WebGL + TS       Orb, particles, task visualisation
Rust/Tauri              desktop shell, Windows native access, supervision, installer
Python                  AI, orchestration, voice, memory, resource, security logic
```

Tauri is the confirmed desktop shell. The PySide6 + QML recommendation from the
earlier architecture proposal is superseded and must not be revived.

The UI is never coupled to Python implementation details.

## 22.3 Locked visual direction

- Near-black futuristic background; cyan and blue dominant; violet a limited accent.
- Orb is the visual identity and centre of the network.
- System metrics right; conversation and context left; command input bottom centre;
  navigation bottom; thin connectors tie surrounding UI to the Orb.
- One connected fluid canvas, no detached dashboard cards.
- Persian RTL. Minimal, technological, alive, premium.
- No large purple atmospheric halo around the Orb.

## 22.4 Orb

Design footprint 460 px, locked at normal window size, shrinking and growing
proportionally with the window and returning to the approved footprint. No hard
minimum that prevents shrinking.

```css
.core-zone {
  left: 50%;
  top: 47%;
  width: min(58vw, 58vh, 560px);
  height: min(58vw, 58vh, 560px);
}
```

Geometry must not be a donut, a perfect circle or a uniform sphere. Target: dense
particle shell, irregular asymmetric geometry, cyan ridge, flowing particle waves,
compressed bright folds, a violet twisting ribbon toward lower right, local bright
areas, sparse surrounding particles, breathing, heartbeat, bursts, flare, micro
eruptions, event-driven reaction. An alive digital organism.

Lighting belongs mainly to the Orb. No direct purple halo. No fixed bright
top-centre or bottom-centre lighting that flattens the geometry. Distant subtle
cyan-violet ambient blobs only, tightly controlled. No heavy `shadowBlur`.

Body language:

```text
wake detected → small energy response
user speaks → audio-reactive shell
command understood → processing transition
task starts → energy surge
task progresses → central task visualisation
task completes → success pulse
task fails → failure response
return to rest → smooth decay
```

Internal state names (`idle`, `listening`, `thinking`, `responding`) are debug-only
and must never be shown to the user. Debug buttons are removed before release.

State changes interpolate on a shared animation clock. The user must never perceive a
switch between independent animations.

## 22.5 Audio reactivity

Active only after Wake Word and session start. Idle ambient audio never moves the
Orb. The whole shell reacts; there is no permanent waveform widget in the hollow
centre. The shell may also react during Qronos speech. `audioReactive` is an
independent 0..1 overlay on state. Session end disables it.

## 22.6 Centre Task Renderer

248 px. Particles gather from outside, form a stable semantic task shape, show
progress or result, then disperse outward. Same particle material as the Orb. No
large ring, no diamond or X glyph, no moving-head trail animation. Percentage and
result are themselves built from particles. The centre renderer is an independent
presentation layer and must not alter the Orb's environmental movement.

Task templates:

```text
file_move  ·  print  ·  web_search  ·  vision_check  ·  app_launch
```

## 22.7 Performance decisions — do not regress

- Core Orb canvas stays local inside `.orb-shell`, never fullscreen or fixed.
- Separate front and back effect canvases may portal to body.
- DPR capped around 1.5.
- No per-frame sort/map architecture.
- No 30 Hz physics against 60 Hz display.
- Glow via pre-rendered sprite and `drawImage`.
- Existing A/B ribbon escape and deformation movement is approved; do not replace it
  with orbital motion.

## 22.8 Construction order

After the Orb and Task Renderer checkpoint, build outward without redesigning the Orb:

```text
1. Right system metrics (CPU, GPU, RAM, temperature)
2. Left conversation and context
3. Bottom command input
4. Bottom navigation
5. Thin network connectors to the Orb
```

## 22.9 Note on locking visuals early — REVISED

r1 locked pixel-level visual behaviour before any real task event existed. What a
task visualisation needs cannot be known until tasks emit events.

Sections 22.3 through 22.8 remain the approved *direction*. Pixel-level detail inside
the Task Renderer stays adjustable until the Task Event Model is real and honest
progress is flowing. Adjusting the renderer to fit real event data is not a redesign
and does not require reopening the Orb.

## 22.10 Reference images

"Reference image" refers only to the three UI/UX images the user provided: ready and
standby, listening, and working. No AI-generated image may replace them.

Markdown cannot carry those pixels. This document holds every extracted visual rule,
but pixel-level comparison requires the three images to be present in the
conversation. **If the images are absent, an assistant must not claim to have seen
them.**

---

# 23. Task Event Model — near-term milestone

`OrbState` and `TaskEvent` are different concepts and must never be conflated.

```text
OrbState   interaction and assistant behaviour
TaskEvent  actual task execution
```

`responding` does not mean a task completed.

```text
taskId · kind · phase · progress: number | null · status/result
source semantic icon · target semantic icon · message optional
```

```text
phase: started | progress | completed | failed
```

Task kind maps to a presentation-layer icon and particle shape. The backend never
sends raw SVG coordinates.

```text
Python Core → TaskEvent → Tauri bridge → React state → OrbTaskRenderer
```

The Rust to React event path is verified independently before Python integration.

## Honest progress — LOCKED

```text
backend has real progress      → show real progress
backend has no real progress   → show honest indeterminate animation
```

Fake measurable progress for a real task is forbidden. The demo's simulated 0→100 is
acceptable only in visual testing and must not survive into production. Results come
from the backend, never hardcoded.

---

# 24. Current Implementation Status

Verified against the repository on 2026-08-25, not assumed.

```text
main                              d093e90   131 tests passing
feature/mvp-runtime-foundation    e2ae64e   234 tests passing, 11 commits ahead
```

`e2ae64e` is the known-good desktop checkpoint: Vite production build passed, Task
Renderer committed, branch pushed, working tree clean.

Present on the feature branch:

- openWakeWord/ONNX adapter behind an interface
- whisper.cpp speech-to-text runtime
- Silero VAD runtime
- Voice pipeline and multi-turn conversation session with memory
- Brain runtime abstraction
- Tauri + React desktop shell with the Orb and Task Renderer

Known gaps and defects:

- No text-to-speech of any kind.
- Whisper language is `auto`; it must be `fa`.
- No Persian normalizer.
- `desktop/src-tauri/src/lib.rs` still contains Tauri's stock `greet` command. The UI
  is driven by four debug buttons and is not connected to Python.
- `core/main.py` is an echo loop. Nothing launches the voice pipeline.
- Resource protection is fail-open in two places, against section 6.
- Process-name lists in place of behaviour detection, against section 8.
- `nvidia-smi` spawned per read and a blocking 500 ms CPU sample, against section 9.
- Permission levels do not match section 14.1.
- No disk awareness, no Storage Manager, no logging anywhere.
- Resource Profiles are hardcoded estimates, not benchmarks, against section 6 rule 1.
- The wake-word model is not deployable: integrity problem in training data, CPU
  training terminated. Requires clean artifacts, an integrity gate, staged training,
  validation and ONNX export. No wake-word model file is stored in the repository.

---

# 25. Pending Work

Ordered by dependency, not by appeal.

```text
 1. Fix fail-open resource protection                     no hardware needed
 2. Choose Persian TTS voice and write the adapter        testable off-Windows
 3. Persian normalizer                                    no hardware needed
 4. Telemetry monitor: split rates, NVML, RAM-only        logic testable off-Windows
 5. Behaviour-based workload detection                    logic testable off-Windows
 6. Align permission levels to section 14.1               no hardware needed
 7. Resource Profiles + atomic reservation ledger         profiles need the reference PC
 8. Safe Queue state machine                              logic testable off-Windows
 9. Task Event Model and the Tauri bridge                 needs the desktop stack
10. Storage Manager to section 16 budgets                 logic testable off-Windows
11. Wake-word model regeneration and validation           reference PC only
12. Benchmark qwen3:14b against an 8B alternative         reference PC only
13. Memory Manager and user-directed remember/forget
14. Location alias registry and on-demand scanning
15. Link-first organisation planner
16. Stress tests under real creator and gaming workloads  reference PC only
17. Installer packaging and signed updates
18. Migration to a bundled native runtime
```

Items 1 through 6, 8 and 10 need no Windows hardware and no benchmark. Item 7's
logic is buildable now; its numbers are not.

---

# 26. Continuation Rules for AI Assistants

- Read this entire file before proposing anything.
- Treat `LOCKED` decisions as fixed unless the user explicitly reopens them.
- Do not silently replace selected models.
- **Never invent a benchmark result or a resource requirement.** An unmeasured
  Resource Profile makes loading forbidden, by section 6 rule 6.
- The user's workload always wins.
- Never add Force Run or any resource-safety bypass.
- Language models never execute Windows actions directly.
- Resource protection is fail-closed. A missing reading is unsafe, not safe.
- Bulk file operations require preview and approval. v1 creates links, never moves.
- Qronos-owned temporary data may be cleaned by quota and age. User-owned files are
  never auto-deleted.
- The user may remember, forget, pin, unpin and rescope memories. Pinned memory never
  overrides security or resource policy.
- Filesystem knowledge is metadata-first, on-demand and scoped. No persistent
  whole-drive index in v1.
- Preserve project and file relationships.
- Never show fake progress for a real task.
- Inspect the actual repository state before writing code; do not guess filenames or
  branch state.
- State uncertainty, risk and architectural weakness plainly. Do not agree
  reflexively.
- A capability that cannot meet its latency class is moved or cut, not shipped slow.

---

# 27. Git and Checkpoint Discipline

```text
build/test → git diff/status → stage exact files → commit → push
```

- Never `git add .` casually. Stage only intended files.
- `main` is protected; changes reach it through a pull request.
- Check the actual repository state before claiming it.
- Delete feature branches after merge.

---

# 28. Development Interaction Protocol

Two modes, chosen by where the work runs.

**Repository mode** — for backend work that can be built and tested off-Windows.
Work on a branch, run the suite, open a pull request, leave it for review. Do not
merge without being asked.

**Manual mode** — for anything the user must apply on the Windows machine:

1. Give the exact full path.
2. Give the exact command to open it, usually
   `notepad "FULL\PATH\FILE.tsx"`.
3. Say explicitly: `Ctrl+A`, `Delete`, paste full file, `Ctrl+S`.
4. Provide the entire updated file, first line to last.
5. Never ask the user to find and replace individual snippets.
6. Give run and build commands only when needed, two or three at a time.

In both modes: inspect the real project structure before inventing filenames, and
never claim a measurement that was not taken.

---

# 29. Architecture Snapshot

```text
USER
 │
 ▼
WAKE WORD
 │
 ▼
VOICE SESSION / TEXT
 │
 ▼
SUPERVISOR
 ├─ Task Router
 ├─ Resource Governor          (fail-closed, atomic reserve)
 ├─ Memory Manager
 ├─ Storage Manager
 ├─ Filesystem Knowledge       (aliases + on-demand)
 ├─ Safe Queue                 (priority, never arrival order)
 ├─ Permission Engine          (five authorisation levels)
 ├─ Health / Telemetry Monitor (split rates, NVML, RAM-only)
 ├─ Screenshot Broker          (scoped capture only)
 └─ Model Runtime Manager
      ├─ Interactive model     Qwen3-VL-4B, resident
      └─ Heavy Brain           deferred, on-demand
 │
 ▼
LLM proposes typed intent, inside a grammar
 │
 ▼
RISK / PERMISSION / RESOURCE BOUNDARY
 │
 ▼
TYPED EXECUTOR
 │
 ▼
OBSERVE + VERIFY + AUDIT / UNDO
 │
 ▼
RESPONSE / UI TASK EVENT
```

Core principles:

```text
Local-first, no API key required
User workload always wins
A model that cannot fit VRAM never serves an interactive request
Language models never get unrestricted execution
Resource protection fails closed
Memory is selective and user-controlled
Storage is capped, ownership-aware, and its caps are reachable
Filesystem knowledge is metadata-first and scoped
Vision observes; it never acts
v1 links files; it does not move them
Structured output is grammar-constrained, never parsed from prose
UI is one connected Persian RTL fluid canvas
The Orb is Qronos's body language
```

---

# 30. Document Maintenance

This file is the master context.

When a significant decision is approved:

```text
update this file
→ bump the revision
→ keep prior approved facts unless explicitly superseded
→ mark a changed rule REVISED and say what it replaces
```

Numbers arrived at by reasoning rather than measurement carry `PENDING` until
measured on the reference PC. A `PENDING` figure never authorises a model load.
