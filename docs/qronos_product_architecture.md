# Qronos Product Architecture

Last updated: 2026-08-23

## Product Boundary

Qronos is an installable local-first Windows application, not only a Python
script. The first production version must provide:

- A polished desktop UI and system-tray experience.
- A persistent supervisor process and a separate user-session runtime.
- Local wake word, STT, TTS, Fast Brain, and Heavy Brain services.
- Permission-gated tools for applications, files, browsers, and devices.
- Resource-aware model lifecycle management.
- Visible activity, approval, error, and undo history.
- A signed Windows installer and a safe uninstaller.

The Windows service must not directly own the interactive desktop UI,
microphone, or user confirmation dialogs.

## Recommended Windows Process Layout

```text
Qronos Supervisor Service
    |
    | authenticated local IPC
    v
Qronos User Runtime
    |-- Audio / Wake Word / STT / TTS
    |-- Task Router / Planner / Orchestrator
    |-- Fast and Heavy model lifecycle
    |-- Resource and Activity Guards
    |
    +--> Desktop UI + System Tray
    |
    +--> Elevated Action Broker (started only when required)
```

The UI and runtime must use a narrow authenticated IPC contract. The Elevated
Action Broker must receive an exact approved action rather than an open-ended
shell command. Qronos must never bypass UAC.

For the first installable version, PySide6 with QML is the recommended UI path
because the existing backend is Python and QML can provide a modern interface,
animations, system-tray integration, and native Windows packaging without
adding a second application runtime. PyInstaller plus Inno Setup is the initial
packaging path. This choice must be validated with a small UI prototype before
the full interface is built.

## Fast and Heavy Brain Selection

- Fast Brain: `qwen3:4b-instruct`, Q4_K_M, approximately 2.5 GB.
- Heavy Brain: `qwen3:14b`, Q4_K_M, approximately 9.3 GB.
- Vision: `qwen3-vl:4b-instruct`, Q4_K_M, approximately 3.3 GB. Its own model,
  loaded only when there is something to look at. It is not a `TaskClass`: it
  describes, and the Heavy Brain reasons about the description. See
  [Qronos Vision](qronos_vision.md) for what it costs and what it can read.
- Fast Brain is the default conversational and routing model.
- Heavy Brain is only for tasks that require deeper reasoning.
- Deterministic commands should bypass both models when possible.

### Mandatory Heavy-Brain Handoff

```text
Fast Brain classifies the request as Heavy
    -> create a candidate plan
    -> read fresh CPU/RAM/GPU/VRAM/temperature/activity state
    -> evaluate ResourcePolicy and ProtectionEngine
    -> evaluate the action PermissionPolicy
    -> if blocked: keep Fast Brain and explain the reason
    -> if allowed: load Heavy Brain on demand
    -> re-check resources immediately before generation
    -> stream progress and response to the UI
    -> unload Heavy Brain after the configured idle timeout
    -> ensure Fast Brain is available again
```

The resource check must happen for every Heavy Brain request. A previous safe
snapshot must never authorize a later request. Resource pressure must also be
monitored while Heavy Brain is running so a new critical state can stop or
degrade the task safely.

### Always-On Resource Sentinel

Resource protection applies to Fast Brain, Heavy Brain, voice services, vision,
and every future tool—not only Heavy-Brain handoff. The vision model peaks at
about 4.6 GB **during generation**, which is the figure the policy must use;
its load-time figure is much smaller and using that one is how a model gets
onto a card it can only crawl on.

- Read fresh CPU, RAM, GPU utilization, VRAM, GPU temperature, disk pressure,
  battery/power state, and detected game/creator activity before every model
  load. `High` and `Critical` states block both Brains from loading.
- Continue sampling while Qronos is active. Under sustained high pressure,
  stop accepting new work, reduce/cancel the current generation, and unload
  optional models. Under critical thermal or memory pressure, unload models
  immediately and leave only the lightweight UI/supervisor alive.
- Never close, pause, or change priority of a user's application automatically.
  Qronos yields its own resources.
- Attribute usage to Qronos/Ollama separately from other processes. Otherwise
  Qronos could mistake its own reserved VRAM for new user pressure and create an
  unload/reload loop.
- Require consecutive samples and hysteresis before changing states so a single
  short spike does not cause model thrashing.
- Expose conservative, balanced, and custom limits in the UI, while keeping
  non-bypassable thermal and memory safety limits.

Sustained monitoring, hysteresis and self-attribution now exist, in
`core/load_signal.py`. A machine is judged busy when it has been busy for most
of a window rather than at the instant it was asked, which was the whole
problem: measured on the development card, the GPU signal crossed its own
threshold twenty-nine times in forty-five seconds, and ten consecutive
evaluations of an unchanging machine produced three different answers. Twenty
seconds of evidence are required before work is held and thirty before it
resumes — quick to yield, slow to take.

Self-attribution is by subtraction rather than by process, because Windows
reports no per-process VRAM at all: every entry comes back as `[N/A]`. Qronos
subtracts what it knows it loaded, which is enough, and normally nothing, since
both brains are unloaded after every turn.

Work that cannot start is now queued rather than refused — `core/safe_queue.py`
driven by `core/queue_scheduler.py` — with the reason shown to the user and an
override that lifts politeness but never the safety floor in
`core/hard_floor.py`. The floor is deliberately narrow: the card being too hot,
and the model not fitting. A busy processor is a reason to wait, not a reason it
is unsafe to proceed.

Disk and power signals now exist too, in `core/environment_signals.py`. Every
sentinel sample and every foreground pressure check reads them fresh, from the
operating system's own battery status and the free space on the Qronos data
volume. An unreadable signal is recorded as unknown rather than guessed.

- **Sustained monitor.** Running on battery and a low drive count as load,
  with the same dwell and hysteresis as CPU and GPU. Queued work waits for the
  charger, but "run anyway" can still lift that. Battery saver counts too when
  Windows reports it (read-only `GetSystemPowerStatus`, in
  `core/windows_power_status.py`); otherwise it is reported as unknown.
- **Drive contention.** Free space alone misses a creator workload that
  saturates a drive with plenty of room left. The sentinel therefore also
  measures the mean time per completed read or write since the previous
  reading, from the operating system's I/O counters. At 50 ms or more over
  at least ten operations a second, the drive counts as load (politeness
  only), with the same dwell and hysteresis. There is no invented "busy
  percent". The counters are system-wide, so a Qronos model load counts too;
  its short burst stays below the dwell.
- **Foreground model loads.** A battery at or below 20%, or a drive under 5 GiB
  free, is high pressure. A battery at or below 10%, or under 2 GiB free, is
  critical. The existing CPU and GPU thresholds are unchanged.
- **Safety floor.** The floor refuses work on a discharging battery at or below
  10% until it reaches 15%, and on a drive under 2 GiB free until it has 3 GiB.
  Neither can be overridden. Fit is now judged on physically free graphics
  memory, so memory a Qronos model still holds is never counted as room for
  another.

Work already running is protected too. When the floor breaches on two
consecutive readings, the scheduler stops Qronos's own running work through
that work's `pause`, which closes only its model request. Triggers are a hot
card, nearly full RAM, a dying battery, a nearly full drive, or another
application squeezed out of graphics memory. The work goes back in line under
the floor's resume lines, without counting an attempt. Another application is
never paused, closed or reprioritised. Work that cannot be paused, or that has
already committed its answer, finishes, and the reason is shown.

The squeeze rule closes a gap that partial offload opened. A model that cannot
fit the card fills whatever was free, and its declared reservation can exceed
the whole card. Subtracting that reservation therefore hid every other
application's memory. Qronos now measures its own share against the external
usage recorded just before it took the card. It counts growth during loading
as its own and anything after that as somebody else's. A card full of Qronos
alone never triggers a yield, so it cannot unload and reload itself.

Still required: the foreground turn itself is not yet stopped mid-generation
by the sentinel. Queued work is, as described above; a turn the user started
still runs to its end once admitted. The partial-offload load itself still
takes whatever is free, which is a property of the model runtime. Live
behaviour with real batteries, drives and applications is part of the deferred
hardware acceptance. No software can guarantee that a computer will never
crash; the goal is conservative prevention, graceful degradation, and tested
recovery.

## Model-Switch UX Requirements

The UI must never look frozen while a model is loading. It must show explicit
states:

- `Understanding request`
- `Checking system resources`
- `Preparing Heavy Brain`
- `Reasoning`
- `Returning to Fast Brain`
- `Blocked to protect system performance`

Responses must be streamed as soon as tokens are available. The application
must benchmark cold load, warm load, first-token latency, generation speed,
unload time, and Fast-Brain recovery time on the development machine.

The 8 GB development GPU cannot keep both selected models fully resident in
VRAM. Neither brain is kept resident: both are unloaded as soon as they have
answered, so Qronos holds no VRAM between turns. Heavy Brain should use
partial CPU/RAM offload. The UI design must tolerate real measured latency
instead of promising an instant switch.

An earlier revision of this document asked for Fast Brain to remain warm.
Measured on a 16 GB card on 2026-08-28, that costs 3,442 MiB held permanently
and saves about 1.7 seconds on a turn that otherwise takes 4.2 seconds from
cold. The card is the user's, not ours, so the model is loaded on demand and
released immediately. Peak usage, which is reached the moment a model finishes
loading rather than as its context fills, is 3,442 MiB for Fast Brain at 8,192
tokens and 10,220 MiB for Heavy Brain at 16,384.

## Five-Level Permission Model

| Level | Mechanism | Examples |
|---|---|---|
| 1 | Auto Allow | Conversation, resource status, explicitly selected non-sensitive reads |
| 2 | Voice Confirmation | Open a known non-admin application, navigate without submitting data |
| 3 | UI Confirmation | Edit a file, send a message, submit a form, upload data |
| 4 | Typed Qronos Secret | Install/uninstall, whitelisted reversible settings, recoverable deletion, remote/device control |
| 5 | Always Deny | Code operations, Registry/raw disk access, credential access, security bypass, hidden surveillance, irreversible destruction |

Voice confirmation is a convenience confirmation, not biometric identity. A
recorded voice can be replayed. Voice confirmation is therefore limited to
low-risk, visible, reversible operations during an active Qronos session.

The typed secret must be a dedicated Qronos PIN or password. Qronos must never
request or store the user's Windows password. The production implementation
must store only a strong salted password hash and protect local secret material
with Windows security facilities. Windows Hello may be offered as a safer
alternative.

## Action Approval Contract

Every executable action must include:

- A stable action identifier.
- An `ActionCategory`.
- Exact targets such as file paths, application, recipient, device, or URL.
- A human-readable preview.
- The required permission level.
- An expiration time for the approval.
- A unique approval nonce so approval cannot be replayed.
- An audit entry before and after execution.
- An undo or recovery record when the operation is reversible.

The LLM may propose an action but must not execute it directly. Only the Action
Broker may execute an action after validating the structured contract and the
current approval.

## Always-Forbidden Capabilities

Qronos must never:

- Read, reveal, export, or transmit passwords, tokens, private keys, or browser
  credential stores.
- Bypass UAC, antivirus, firewall, access controls, approval dialogs, or the
  Permission Engine.
- Irreversibly wipe disks, partitions, backups, or broad collections of data.
- Record microphone, camera, screen, or activity without visible user consent.
- Hide remote access or create unauthorized persistence.
- Create malware, exploit another system/account, perform credential attacks,
  scan for vulnerable targets, or assist an unauthorized intrusion.
- Change its own security policy to grant itself more authority.
- Execute open-ended elevated shell commands proposed by a model.
- Generate, modify, or execute source code, scripts, macros, exploit logic, or
  executable instructions inside the Qronos runtime. Read-only analysis of code
  the person provides (explaining or reviewing it) is permitted: that is the
  installed runtime policy (docs/qronos_voice_output.md, "Security and ownership
  boundaries"; security/permissions.py CODE_ANALYSIS is AUTO_ALLOW).
- Create, edit, or delete Windows Registry keys or values.
- Change boot configuration, firmware, recovery, antivirus, firewall, UAC, or
  access-control settings.
- Access raw disks, partitions, volume metadata, backups, or recovery data.

These runtime restrictions do not prevent developers from maintaining Qronos
outside the installed application. The signed installer may perform a fixed,
reviewed installation action, but the AI runtime must not receive a general
Registry, shell, script, or installer capability.

### Runtime Code-Prohibition Boundary

A prompt instruction alone cannot enforce the requested code prohibition. The
installed product requires independent layers:

1. Do not ship a shell, compiler, interpreter, macro runner, debugger, source
   repository connector, or general-purpose code tool in the runtime capability
   set.
2. Deny reading, creating, editing, uploading, or executing source-code and
   script targets in the Action Broker, regardless of model output or approval.
3. Reject code-generation, code-modification, exploit, and script-execution
   requests (read-only analysis passes) before
   they reach either Brain, using a deterministic Content Policy Gate.
4. Inspect generated text before display or tool use and replace blocked output
   with a clear refusal event in the audit log.
5. Red-team and fuzz these boundaries, including renamed extensions, code inside
   archives/documents, prompt injection, encoded instructions, macros, and
   model-generated tool arguments.

Free-form language can resemble code, so no prompt-only design can truthfully
promise perfect semantic detection under every phrasing. The enforceable hard
guarantee is that the runtime has no code execution capability and that the
Action Broker always rejects categorized code operations. Textual code refusal
requires the layered gates above and must be measured before production release.

Current implementation of layers 3 and 4 (core/code_policy.py): deterministic
Persian/English patterns, not a semantic classifier. The input check runs at the
start of `Orchestrator.execute_plan` (desktop text and voice turns, the approved
screen question, queued conversations), before any resource admission or model
call. The output check replaces a
generated answer that holds a fenced code block, two consecutive code-like lines
or one machine-changing command before it is recorded, shown or spoken. The
decision and its audit record (category, channel, stage, rule and length, never
the text) come from `security.gate` and the permission engine. Its
red-team measurement (layer 5)
is still owed.

## External Intrusion Response

The Remote Gateway is disabled by default. When enabled, it must use a narrow
allowlist, authenticated encryption, short-lived sessions, signed requests,
nonces, replay protection, rate limits, and an explicit local enable state.

The LLM must never decide whether an intrusion exists. A deterministic Security
Monitor evaluates gateway and policy signals and applies this escalation:

1. `Reject`: deny an invalid or unauthorized request and write an audit event.
2. `Throttle`: rate-limit repeated invalid requests and block the source.
3. `Contain`: stop the Remote Gateway and Action Broker, revoke active remote
   sessions, keep only the local read-only UI, and show a critical alert.
4. `Safe Shutdown`: after preserving a tamper-evident audit record, stop all
   Qronos runtimes when high-confidence critical compromise is detected.

A single malformed packet or a text prompt claiming that an attack exists must
not trigger full shutdown. Otherwise an attacker could use the defense itself
to create a denial-of-service condition. Shutdown signals require deterministic
evidence, correlation, and severity rules that will be reviewed individually.

## UI Screens for the First Product Version

1. Onboarding and privacy defaults.
2. Main conversation and voice-session screen.
3. Resource and Brain status panel.
4. Permission preview and confirmation dialog.
5. Activity, approval, error, and undo history.
6. Model download and storage management.
7. Microphone, STT, TTS, wake-word, and threshold settings.
8. Gaming, Creator, Eco, and manual protection modes.

## Implementation Order

1. Finish and benchmark Fast/Heavy model integration.
2. Complete live wake-word testing and improve model quality.
3. Build the central action schema and connect PermissionPolicy to Orchestrator.
4. Add STT, Voice Session, and TTS.
5. Prototype the PySide6/QML desktop shell and system tray.
6. Add structured application/file/browser tools through the Action Broker.
7. Add Activity/Undo logging and recovery workflows.
8. Split supervisor, user runtime, UI, and elevated broker processes.
9. Package, sign, install, update, and uninstall on a clean Windows machine.
