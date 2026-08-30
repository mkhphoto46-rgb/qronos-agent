# Qronos Voice Output

Production status and measured design notes for the local Persian text-to-speech path.

## Current implementation

Qronos uses a local Chatterbox Persian pipeline through the CrispASR runtime. The production implementation is `core/chatterbox_runtime.py`, with the shared voice interface in `core/voice_runtime.py`.

The production baseline was measured on the development machine with an NVIDIA RTX 3070 Ti 8 GB using:

- T3 Persian `q4_k`
- S3Gen `q8_0`
- 10 diffusion steps
- full-GPU Vulkan placement

Current measured production baseline recorded by the runtime implementation:

- warm ready-to-speak: about 1.95 s
- resident VRAM: about 1.21 GiB
- generation peak delta: about 1.43 GiB
- normal warm unload: about 0.08 s

These values are measurements from the development machine, not universal guarantees.

## Production design rules

The production runtime is deliberately resource-aware:

- constructing the runtime starts nothing;
- TTS is lazy-loaded only when needed or explicitly prewarmed;
- the TTS process may stay resident temporarily during a voice conversation;
- idle resident resources are disposable and released automatically;
- Qronos manages only the CrispASR process that it started;
- admitted TTS work is registered as `QRONOS_OWNED` in the shared resource ledger;
- Qronos-owned VRAM must not be mistaken for user-owned pressure;
- Chatterbox cache and temporary data stay under `runtime/chatterbox/`.

The runtime uses a single-flight startup path so concurrent requests do not launch duplicate CrispASR processes or duplicate VRAM reservations.

## GPU admission and user-workload priority

The production runtime reserves 1536 MiB for TTS generation and requires an additional 1024 MiB admission margin before creating new TTS load. The resulting minimum free-memory admission target is 2560 MiB.

This is intentionally conservative. Earlier measurements on the predecessor implementation showed why simple "does the model fit?" logic is insufficient: the voice could remain technically functional while becoming several times slower when the GPU was already under pressure.

The production rule is therefore not merely to fit the model, but to avoid adding Qronos work when doing so would interfere with the user's active workload.

Critical GPU temperature is treated as 85 C for this path.

## Lifecycle

The production voice runtime exposes explicit lifecycle states:

- `UNLOADED`
- `STARTING`
- `READY`
- `SPEAKING`
- `HOT_IDLE`
- `RELEASING`
- `FAILED`

A successful spoken turn returns to `HOT_IDLE`; idle resources are later released. Emergency release is separate from normal release so a hard resource-safety condition can evict Qronos-owned voice resources immediately.

## Chunked playback and interactive voice sessions

The production voice work is no longer only an isolated "generate one WAV file" experiment. The production TTS branch added chunked playback and interactive voice-session integration so long replies can begin playback without requiring the entire response to finish first, and voice can participate in multi-turn interaction.

The desktop side contains the Qronos voice player, and the runtime bridge integrates the production voice runtime with the rest of the application path.

## Historical benchmark context retained from PR #21

PR #21 introduced the first measured local Persian Chatterbox/CrispASR implementation. Its implementation has been superseded by the production runtime, but several measurements remain useful as historical evidence for design choices:

- `q4_k` was selected because measured quality differences between tested quantisations were dominated by seed-to-seed variation, while cost differences were measurable;
- full-GPU placement outperformed the runtime's mixed CPU/GPU default in the tested environment;
- 10 diffusion steps were retained because higher step counts cost more without a measured quality gain in that sweep;
- a resident local server removed repeated model-load cost from each short utterance;
- measurements under GPU pressure demonstrated that "fits in VRAM" is not equivalent to "runs acceptably without competing with the user's workload".

Those historical measurements were produced against an earlier runtime and must not be treated as current production timing guarantees. The production runtime's own measured baseline above is the current reference.

## Security and ownership boundaries

Voice output does not weaken Qronos permission boundaries. TTS produces local audio; it does not grant an action executor additional authority.

The installed Qronos runtime policy remains:

- read-only code analysis is permitted;
- code generation is forbidden;
- code modification is forbidden;
- arbitrary script execution is forbidden.

Voice resource ownership is explicitly tracked so Qronos can release its own GPU load without terminating unrelated user processes.

## Files and model storage

Model/runtime artifacts are not committed to the repository. Chatterbox runtime assets live below `runtime/chatterbox/` and are ignored by Git.

The production implementation keeps its temporary and cache data under that same runtime-owned area rather than scattering generated files through the project or user directories.

## Licensing note

The predecessor PR recorded an unresolved distribution concern: the GGUF conversion was described as MIT while deriving from weights released under CC BY-NC 4.0. This repository does not distribute those model weights. Before a commercial distribution of Qronos includes or automatically redistributes those weights, the applicable model licenses must be verified against the exact artifacts being shipped.

This is a release/compliance constraint, not a runtime failure.

## Validation status

The production Voice + Vision integration was validated locally before merge with:

- the full Python suite passing (2074 tests in the validated integration state);
- Desktop TypeScript/Vite production build passing;
- Rust `cargo check`, `cargo test`, and `cargo fmt --check` passing;
- Git diff/hygiene checks passing;
- GitHub Actions passing on both Ubuntu and Windows for integration commit `9c1d928e4440c47a3bbfbd57e4b7ceac81a8a831`.

PR #25 then merged that validated integration into `feature/tts-production-runtime`.

## Superseded predecessor

PR #21 (`feature/voice-output`) is the predecessor implementation. Its runtime and runtime-specific tests/harness must not be merged into the production branch because they target the older implementation and would reintroduce a parallel voice path.

The useful architectural rationale and benchmark history from PR #21 are retained in this document instead.
