# Qronos media index (first wave)

`qronos_media/` is the first slice of the media intelligence module described
in the handoff (*Qronos Media Intelligence — Agent Build Handoff*, SHA-256
`734d719c…356ca5`). It indexes the user's own video files locally, so a
remembered line of dialogue can be found again with the exact file, track and
time range it came from.

This wave covers handoff Stage A (foundation), the subtitle part of Stage B
(dialogue) and the start of Stage D (shot boundaries and keyframes). Speech
recognition, semantic search, visual search and multimodal search are **not**
implemented; the index reports them as pending rather than returning empty
results that look like answers.

## What works

| Handoff level | Capability | State |
|---|---|---|
| 0 | Approved roots, bounded scan, change detection, SHA-256 identity, duplicates as one asset, other cuts as separate assets | done |
| 0 | `ffprobe` metadata: duration, container, streams, languages, dispositions | done |
| 1 | Subtitles first: sidecar SRT/VTT (UTF-8, UTF-16, legacy Windows-1256/1252), sidecar ASS/SSA and embedded text streams via ffmpeg; track and language provenance | done |
| 1 | Image subtitles (PGS, VobSub, DVB) | reported `unsupported_image` (needs OCR) |
| 1 | Speech recognition when no subtitle exists | **pending** |
| 1 | Exact phrase search (FTS5, Persian/Arabic normalisation, split-line pairs) and fuzzy search (rare trigrams + similarity) | done |
| 2 | Semantic / cross-language dialogue search | **pending** |
| 3 | Shot boundaries (`scdet`, chunked and checkpointed) and adaptive keyframes under a cache quota | done |
| 3 | Visual embeddings | **decision pending**: no joint image–text embedding backend is specified |
| 4–5 | Captions, OCR, topics, speaker turns | **pending** |
| 5 | Actions and motion | **not supported**: sparse keyframes cannot establish them |

Reports (JSON, HTML, CSV) are built from the index only and always include the
asset hash, stage versions, pending capabilities and limitations.

Lexical scores are BM25 or a similarity ratio, labelled as such. Results carry
`"confidence": "uncalibrated"`; nothing here has been benchmarked on real films.
Subtitle times are the times the subtitle author wrote, not times verified
against the audio.

## Using it

The command-line tool prints JSON. It uses the real permission gate and the
user's real Files policy, writes gate decisions to the action audit log, and
runs CPU/disk stages only.

```text
set QRONOS_MEDIA_FFMPEG_DIR=<folder containing ffmpeg.exe and ffprobe.exe>
python -m qronos_media add-root "D:\Films"
python -m qronos_media scan
python -m qronos_media index --max-seconds 600
python -m qronos_media search "I never asked for this"
python -m qronos_media report <asset_id> --format html
```

Other commands: `roots`, `remove-root`, `watch`, `pause`, `resume`,
`cancel-job`, `pause-job`, `resume-job`, `retry`, `reindex`, `prioritize`,
`status`, `asset`, `scene`, `export-report`, `open-timestamp` (plan only),
`tools`. The default data folder is `<Qronos data>\Media`; `--data-dir`
overrides it.

## Safety model

**Paths.** Only absolute local drive paths. Refused: UNC shares, device
namespaces, URL-like values, `..`, alternate data streams, reserved device
names (`NUL`, `COM1.mkv`, `con .srt`), segments ending in a dot or space, and
any link or junction anywhere on the path, including above the approved root.
The data folder itself must not be behind a link; every cache write or delete
goes through `paths.owned_child`, and asset ids read back from the database
must match `a_<32 hex>` before they name a folder.

**Decoder input.** Every ffmpeg/ffprobe call is a fixed argument list with
`-protocol_whitelist file`, an explicit `file:` input and a demuxer whitelist
(`matroska, mov, avi, mpegts, mpeg, flv, asf, ogg`; `ass` for ASS sidecars).
Tests confirm that HLS, ffconcat and SDP files disguised as `.mp4`, `.mkv`,
`.ts`, `.avi` or `.ass` are refused before any reference is followed. Filter
graphs contain only range-checked numbers.

**Containment.** Each decoder runs in its own Windows Job Object: created
suspended and assigned before it runs, 2 GiB per-process memory limit, one
active process (it cannot start others), kill-on-close, and whole-job teardown
on timeout, cancel, output overflow and normal exit. If the job cannot be set
up, the child is killed before it runs. It also runs at below-normal priority
with a minimal environment.

**This is not a sandbox.** The whitelists stop protocol and playlist
indirection, and the job bounds memory and processes. A vulnerability in a
native decoder could still read anything the user can read. Stronger isolation
(an AppContainer or restricted token) is a separate deployment gate.

**Evidence identity.** An asset is the SHA-256 of its bytes. A copy is used
only while its size, modification time and file id match the state it was
hashed in; this is checked before and after every decoder stage, and a
mismatch discards the stage result and re-hashes the file. That stat check
cannot see an edit that preserves all three. So playback plans
(`open-timestamp`) and exports re-hash the copy first (`verify_asset`), and
search results label their check `identityCheck: stat`.

## One timeline

All times are on the **asset timeline**: milliseconds from the container's
start time, which is recorded as `start_offset_ms`, alongside stream start
times and time bases.

- **Shot cuts:** ffmpeg measures `-ss` from the container start and reports
  each chunk's times from that point. Chunk start plus reported time is
  therefore asset time.
- **Embedded subtitles:** extracted cue times come out with the container
  start removed.
- **Keyframe seeks:** these use the same `-ss` semantics.
- **Sidecar subtitles:** these are read as relative to playback start. That
  is the convention, not a verified fact, and results label it.
- **Video end:** the end of the last scene is where decoding actually ended
  (`video_end_ms`). The container's duration is only a claim: in a generated
  file that starts at 10 s, Matroska reported the end time (14 s) as its
  duration.

Fixtures cover a container starting at 10 s with a frame-rate change at the
cut, chunked versus unchunked detection, and a sidecar offset one second from
the embedded track. Player seeks (`mpv --start`, `vlc --start-time`) are
*assumed* to use the same zero-based timeline. That has not been tested
against real players.

## Search bounds

Every query has a wall-clock budget (default 1.5 s, at most 10 s). It covers
the whole call, including assembling evidence for each hit, and is enforced:

- inside SQLite by a progress handler;
- by a busy timeout no longer than the budget (restored afterwards);
- between fuzzy candidates, comparison windows and results;
- by a cap on similarity computations.

Fuzzy candidates come from bounded per-trigram lookups, rarest trigrams
first, instead of ranking an OR over every match. Each comparison is cut to
48 words of at most 32 characters and a 160-character query. Each hit lists at
most 8 copies (`pathsTruncated`) and 2,000 characters of dialogue.

Running out returns what was found, with `status: "partial"` and
`stoppedBy` / `notes` (`time_budget`, `work_budget`, `busy`, `common_terms`).
Search opens the index read-only and never decodes, scans or writes.

## Permissions

Adding a root is `READ_NON_SENSITIVE` (explicitly selected local information).
Exporting a report is `CREATE_OR_EDIT_FILE`, and the gate sees the exact
destination and content hash. Both are decided by `security.gate.evaluate`
under the current Files policy:

- **Allowed:** runs.
- **Asks:** the exact prepared action is held under a single-use
  `ApprovalLedger` identity, and `approve_root` / `approve_export` consume it
  once. The approval re-asks the gate, mints an `ActionGrant` for that
  request, and does exactly the prepared thing: same path, kind and depth, or
  the same bytes. Without an in-process ledger (the CLI) the request is
  refused.
- **Typed secret:** if the effective policy is ever this strong, approval
  also needs the user's approval secret, checked by `ApprovalSecretStore`.
  Without a verifier the request fails closed, and a wrong secret spends the
  single-use id. The secret is an in-process argument only: never in a
  request, preview, audit record, report or CLI argument.
- **Denied:** refused.

Background work re-asks the gate for every root at the start of every run and
at least every minute. It continues only while the policy allows reading, or
while the in-process grant that approved the root is still active *and* the
root still has the kind and depth that grant approved. A stored approval row
is a record, never a reason to proceed.

## Resources

The media package owns its job graph, not the machine. Stages reserve through
`MediaResourceAuthority`:

- `GovernorAuthority` wraps the backend's single `ResourceGovernor` when the
  service is hosted in that process, and reserves `Weight.LIGHT`.
- `StandaloneCpuAuthority` (CLI) grants CPU/disk stages only.

Both yield to gaming and creator modes, refuse under critical pressure, keep
a disk reserve, and refuse any GPU stage. Leases are renewed while hashing and
decoding. Pause, cancel, permission loss, an expired run budget and a refused
renewal stop the running child, keep the checkpoint and say why.

No model is loaded. Inference stages need a cross-process model lease with
the backend governor first (see *Integration hooks*).

## ffmpeg provenance

Development tests used a pre-existing user-level install:
`Gyan.FFmpeg` 9.0.1 "full_build" (WinGet). Its configure line has
`--enable-gpl --enable-version3`, which makes it a **GPLv3 binary**, and it
compiles in network protocols (kept unused by the whitelist).

- **Do not redistribute this build** with Qronos.
- **Shipping target:** an LGPL build without network protocols or GPL
  components, with its notices added through the existing notice workflow.

`python -m qronos_media tools` prints the located build's version and licence
flags.

## Integration hooks (Root-owned files, not edited here)

1. `tests/test_offline_network_policy.py`: add `"qronos_media"` to
   `PRODUCT_PACKAGES`. Until then, `tests/test_media_contracts.py` runs the same
   `network_findings` scanner over every media module.
2. Backend service registration (`core/runtime_bridge.py`,
   `build_support/runtime_entry.py`): a `media` service whose methods are
   `MediaService`'s, built with `GovernorAuthority(<the bridge's governor>)` and
   `GatePermissions(audit=<action audit>, ledger=<the bridge's ApprovalLedger>,
   secrets=<ApprovalSecretStore>)`, with `approve_root` / `approve_export` behind a
   bounded private IPC field for the secret.
3. Desktop (Persian UI, later): approval prompts for `approval_required`
   results, result cards, and open-at-timestamp through the approved
   application executor.
4. Before any inference stage: a cross-process model-lease contract with the
   governor that declares the model and its VRAM, so the media process never
   double-loads a model.

## Tests

`tests/test_media_*.py` use synthetic fixtures only: generated subtitles, and
two-colour videos generated with ffmpeg `lavfi` under pytest's temporary folder.
Tests that decode need `QRONOS_MEDIA_FFMPEG_DIR` and are skipped without it.
No real films, audio, screens, GPUs or models are used.
