# Qronos media index (first wave)

`qronos_media/` is the first slice of the media intelligence module described
in the handoff (*Qronos Media Intelligence — Agent Build Handoff*, SHA-256
`734d719c…356ca5`). It indexes the user's own video files locally, so a
remembered line of dialogue can be found again with the exact file, track and
time range it came from.

This wave covers handoff Stage A (foundation), Stage B dialogue (subtitles
first, with whisper.cpp speech recognition as the fallback for files without
subtitles, inside the Qronos runtime only) and the start of Stage D (shot
boundaries and keyframes). Semantic search, visual search and multimodal
search are **not** implemented; the index reports them as pending rather than
returning empty results that look like answers.

## What works

| Handoff level | Capability | State |
|---|---|---|
| 0 | Approved roots, bounded scan, change detection, SHA-256 identity, duplicates as one asset, other cuts as separate assets | done |
| 0 | `ffprobe` metadata: duration, container, streams, languages, dispositions | done |
| 1 | Subtitles first: sidecar SRT/VTT (UTF-8, UTF-16, legacy Windows-1256/1252), sidecar ASS/SSA and embedded text streams via ffmpeg; track and language provenance | done |
| 1 | Image subtitles (PGS, VobSub, DVB) | reported `unsupported_image` (needs OCR) |
| 1 | Speech recognition when no subtitle exists (whisper.cpp large-v3-turbo q5_0, chunked, timestamped) | **implemented, unverified**: runs only in the runtime's media host with the pinned assets provisioned; never in the standalone tool; no real-model run accepted, no accuracy claim |
| 1 | Subtitle/ASR agreement labels (handoff s9) | **pending** |
| 1 | Exact phrase search (FTS5, Persian/Arabic normalisation, split-line pairs) and fuzzy search (rare trigrams + similarity) | done |
| 2 | Semantic / cross-language dialogue search (bge-m3 F16, 1024 dims, via the runtime's CPU memory sidecar) | **implemented, unverified**: runs only in the runtime's media host with the memory sidecar running (`QRONOS_MEMORY_RUNTIME=1`) and bge-m3 installed; never in the standalone tool; scores are uncalibrated similarities; no real-model run accepted |
| 3 | Shot boundaries (`scdet`, chunked and checkpointed) and adaptive keyframes under a cache quota | done |
| 3 | Visual embeddings | **decision pending**: no joint image–text embedding backend is specified |
| 4–5 | Captions, OCR, speaker turns | **pending** |
| 4 | Topics | **deferred**: labels would come from a brain model and no specified brain authority is wired to media; none are generated |
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

**Per-file admission (rounds 3-4).** A `files` row is a record of what a scan
saw, never permission to read. Every read goes through
`qronos_media.scope.admit` against the root as approved *now*, immediately
before the file is opened. "Every read" means:

- the stat and full hash for identity;
- the copies a stage may use, re-admitted again right before the decoder
  opens one;
- sidecar discovery, fingerprinting and reading;
- strict verification, exports and playback plans;
- the paths search offers.

A path is admitted only if all of these hold:

- it equals an approved file root, or lies inside an approved folder root at
  a folder depth no greater than the root's *current* `max_depth` (files in the
  root folder are depth 0);
- it has a video extension;
- it is a safe local path: no UNC share, device namespace, URL-like value,
  `..`, alternate data stream or reserved device name;
- it has no link or junction anywhere on its path;
- it is not inside the media index's own data folder (database, keyframe
  cache, reports), so the index never feeds on its own state. A root inside
  that folder is refused when it is added.

That is the whole read boundary. It is not a new permission system, and
nothing else is excluded: an explicitly selected folder on an external drive,
under the user profile or under AppData is an ordinary root.
`core.library_executor.default_protected_roots` describes where *Library
actions* may not write or act; it is not a read policy and media does not
apply it to reads.

Approved companion-subtitle scope: only `.srt/.vtt/.ass/.ssa` files in the
same folder as an admitted video, named after it (`<stem>[.<tags>].<ext>`),
link-free and not inside the media data folder. Nothing else beside a film is
read.

A row that fails admission is marked `missing` with `error = out_of_scope`;
its asset's other copies are unaffected. Scans sweep every row of the root
against its scope, even a capped scan, outside any write transaction.
Re-approving a root with a narrower depth or a different kind sweeps at once.
Search does not offer an asset whose every copy is out of scope. Widening the
scope again re-admits files through a fresh scan and full hash.

The Files policy is category-scoped today (`security.gate.evaluate` decides
per category), so the gate is asked per root (`recheck_root`). `admit` is the
single place a future path-scoped rule would apply.

**Fresh permission at every read (round 4, M-P4).**

- **At every read handoff:** before a file is stat'ed for its identity hash,
  when a stage starts, before every decoder spawn that opens a copy (each
  embedded subtitle extraction, scene chunk and keyframe), before each
  sidecar's hash and again before its read or decode, and before strict
  verification reads a copy. The gate is asked fresh, with no cache, for the
  root of *that* copy. Since round 6 this holds per open, not only per stage
  start.
- **While a hash or decoder runs:** the gate is asked again for the roots
  actually in use at least every `REVOCATION_CHECK_SECONDS` (production default
  2 s), so a Files DENY or an expired approval stops the work within about that
  long, with its checkpoint kept and the reason reported.
- **Binding to the copy actually opened:** the watch is bound to the roots of
  the copies actually opened, and every one of them must still be allowed. A
  duplicate copy under another root with a live approval never carries a copy
  whose own root was revoked or expired.
- **Grant lifetime:** an ALWAYS_ASK approval carries background work only for
  the gate's own grant lifetime, measured from approval time.

**Every subtitle and decoder read is its own handoff (round 6, M-P5).**

- **Sidecars:** each one is admitted separately. Its copy is admitted again
  with a fresh gate answer, the sidecar itself is admitted, and pause, Stop,
  the deadline and permission are checked uncached first. This happens
  before the hash, again before the read, and for ASS/SSA once more right
  before ffmpeg is started, because each is a separate open or handoff.
- **Size limit:** a sidecar larger than 16 MiB (SRT, VTT, ASS or SSA) is
  refused from its size alone, before any hash, read or ffmpeg spawn.
- **Hashing:** the sidecar's identity hash streams through `full_sha256` with
  the stage's cancellation. Pause, Stop, the deadline, revocation and lease
  loss stop it between chunks and requeue the stage.
- **Hash byte bound (round 9; raw reads round 13):** the hash read itself is
  bounded by `full_sha256(..., limit=16 MiB)`. No hash read request asks for a
  byte past 16 MiB + 1, and the bytes consumed are at most 16 MiB + 1. Since
  round 13 the bounded hash and the bounded read use the unbuffered file
  beneath `open()`, so for a real file the operating system is not asked past
  16 MiB + 1 either (before, one 8 KiB read-ahead buffer could go beyond it;
  those bytes were never hashed). This is measured at the raw file-object
  level; nothing is claimed about caching below it. A sidecar that grew after
  its size check, at the hash or during
  it, is refused there as a failed track ("too large: grew past ... while
  hashed"), with no parse read and no ffmpeg run. The stage still completes.
  The size check after the hash is only a second check. Video asset identity
  passes no limit and is still a full hash.
- **Reading:** every sidecar (SRT, VTT, ASS and SSA) is then read in 256 KiB
  chunks, at most 16 MiB, with the same cancellation. The bytes read must hash
  to the same digest, so the stored sidecar hash is of exactly the bytes used;
  a substitution is refused by content even when size, mtime and file id are
  unchanged. A file that changes between the hash and the read, or while it is
  read, or whose size, mtime or file id differ at the end, is recorded as a
  failed track.
- **ASS/SSA (round 13):** ffmpeg never opens the user's sidecar. It decodes a
  private copy of exactly the verified bytes, created new under
  `<cache>/subtitle-input/sub-<random>.ass|ssa`; the name never contains the
  user's file name. On Windows that copy is held open with read-only sharing
  until ffmpeg has finished, so no other process can write, truncate, rename
  or delete it meanwhile. While it is held, Windows also refuses to rename or
  delete the folders above it (NTFS; tested for `subtitle-input`, the cache,
  the media data root and an outer folder in the tested tree). That is the
  whole namespace claim: same-user changes to name resolution outside that
  folder tree (for example per-logon DOS device mappings) were not tested and
  are not covered. The handle's final path
  must be the intended one, and the path is checked for links again before
  use. The copy is re-read and re-hashed through that handle before ffmpeg
  starts and after it ends. Only once the copy is ready is the original
  admitted again with a fresh gate answer, immediately before ffmpeg starts,
  so a refusal that lands while the copy is prepared stops it. Afterwards the
  copy is deleted through a handle whose file identity must match, so a
  different file that took its name is never deleted (that is reported as a
  failure, not hidden).
  - **Cleanup never hides a stop (round 19).** If the copy cannot be removed
    (for example another same-user program reads it without delete sharing)
    while a permission, cancellation or resource stop is already on its way,
    that stop is kept as it is: the stage is still requeued with its typed
    reason, and the cleanup failure is added to it as a note. Without such a
    stop a failed cleanup fails the track. If the new copy cannot even be
    identified right after it is created, its handle is closed and the empty
    file is left for the sweep, never deleted by name.
  - **Crash sweep (round 19).** Copies left behind are removed at the start
    of the next subtitles stage: only exact `sub-<32 hex>.ass|ssa` plain files,
    at most 256 entries looked at per stage, each deleted only if it is still
    the very file (volume and file index) its check saw, so a file that takes
    the name in between is left alone. A copy held open by a live owner, or one
    that changed, is skipped and counted as busy, never forced.
  Same-user limit: another program of the same user
  can read the copy, but cannot change, rename or delete it, or rename the
  folders above it, while ffmpeg may read it. The decoder keeps
  its usual limits (file-only protocol, the `ass` demuxer, the Job object,
  bounded output). Until round 13 ffmpeg reopened the sidecar by name after the
  hash, so a file that grew in between could reach it; that is repaired.
- **Decoder spawns:** every embedded subtitle extraction, every scene chunk
  and every keyframe opens its copy again through a fresh admission.
- **Refusals:** a refusal at any of these handoffs requeues the stage with
  `note = refused:<reason>` and lists it under `refused` in the run report.
  `stopped` is `permission` when the root was revoked or its approval expired,
  and `out_of_scope` when the copy left scope. It is never a crash, a stuck
  RUNNING row or a partial success.
- **Before commit:** the stage's watch is checked uncached, and every root it
  opened is asked again with a fresh gate answer, immediately before results
  are written. Work that finished after a pause, Stop, deadline, lease loss or
  revocation is requeued, not committed, so the stage runs again on resume.

**Strict verification is a bounded, owned operation.** `verify_asset(asset,
max_seconds=30, cancelled=None)` does these steps, in this order:

1. Asks the gate for every root holding a copy; nothing is read for a refused
   or revoked root.
2. Admits each copy.
3. Reserves a resource lease, so it defers to gaming and creator work.
4. Hashes the copy in full, never a sample. While hashing, the same `Watch`
   as indexing observes pause, `cancelled` (Stop), the time budget,
   permission revocation and lease renewal.

It returns a typed `status`:

| Status | Meaning |
|---|---|
| `verified` | One copy's bytes hash to the asset; it is listed in `verified`. |
| `mismatch` | Every admitted copy hashed differently; those copies go back for re-identity. |
| `no_copy` | No present, in-scope copy whose stat still matches. |
| `refused` | The gate refused every root holding a copy; see `refusedRoots`. |
| `deferred` | The resource authority said not now; see `reason`. |
| `incomplete` | It stopped mid-hash; see `reason`. Nothing is claimed. |

**API change from round 1 (service and CLI).**

- **`open_timestamp`:** returns `{"status": "planned", ...}`, or
  `{"status": "not_verified", "verification": {...}}` with no path, instead of
  raising. The CLI now exits 0 with that JSON where it used to exit 2.
- **`export_report`:** validates the destination, renders from the index,
  then asks the write gate on those exact bytes *before* any media read.
  Only then does it verify and write. A copy that cannot be verified now
  returns `{"status": "not_verified", ...}` and writes nothing.
- **`verify_asset`:** stops at the first verified copy (one proof is
  enough). Its `verified` list therefore has at most one entry.

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

Background work asks the gate fresh at every read handoff and, while a read is
in progress, at least every 2 seconds (see "Fresh permission at every read").
It continues only while the policy allows reading, or
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

A stage that runs a model is *model-bearing* whatever device it uses (CPU
whisper is still gigabytes). Neither authority above grants one; only the
runtime-issued `HostedModelAuthority` does (next section).

## In-process host and model ownership

`core/runtime_bridge.py` builds one `MediaHost` (`qronos_media/host.py`) the
first time media work is asked for (`media_index` / `media_status` commands).
Nothing about media runs, probes or opens at startup or on import.

- **One authority.** The host's `HostedModelAuthority` is issued by the bridge
  from the queue scheduler's own `ResourceGovernor` and the shared
  `GLOBAL_RESOURCE_LEDGER`. It is an object reference; no request field, CLI
  flag, Boolean, database row or requested model/size creates or widens it. A
  model stage names one of `DECLARED_MODELS` (today only whisper, declared at a
  conservative 2 GiB) and gets that model's declared budget.
- **Counted twice, freed once, on proof.** A model slot is a ledger reservation
  (QRONOS, BACKGROUND, 2048 MiB) plus a *managed* HEAVY governor reservation
  that the lease clock never expires. Both stay counted until every child the
  lease started has provably exited: a spawn is counted before the process
  exists, the owned process and Job handles are attached right after creation,
  and the lease asks those handles (process exited AND the Job holds no
  process). A release during a spawn handoff or with an unproven child frees
  nothing; the slot stays `termination_unproven`, further model loads are
  refused, the unproven Job is kept open, and `reconcile()` retries teardown and
  proof. No TTL, acknowledgement or caller "exited" flag is proof.
- **Yielding.** Every hosted stage yields (checked about twice a second while a
  child runs) to the person's own voice/text/screen turn, Pause, Stop,
  shutdown, gaming/creator modes and critical pressure. The running child is
  torn down, its exit proven, the stage requeued with its checkpoint.
- **Permissions.** The host's `GatePermissions` uses the bridge's
  `ApprovalLedger` and action audit. Typed-secret approvals are not wired into
  the host yet (no UI); such roots are refused.
- **Store admission.** Before the store opens, the ACTUAL database file
  (`media-index.sqlite3`, plus SQLite's `-wal`/`-shm`/`-journal` files when
  they exist) goes through `core.startup_admission.enter_for_store("media",
  "QRONOS_MEDIA_DATA_DIR", path)`: the existing order (update barrier,
  installed-data binding, one-name rule) on that exact path, so a database that
  is a hard link into installed data is refused even inside an ordinary folder;
  for an installed binding the path must lie inside the admitted data root. The standalone CLI
  does the same and exits 78 on refusal. `QRONOS_MEDIA_DATA_DIR` is a store
  path override like the others; the default stays `<data>/Media`.

## Speech recognition (fallback)

`qronos_media/asr.py`, stage `asr` (depends on `subtitles`).

- Runs only when the asset has no indexed subtitle cue; subtitle evidence is
  never deleted or overwritten. No audio stream means nothing to do.
- One audio stream by an explicit, recorded rule (the default-flagged stream,
  else the lowest index), addressed by absolute index.
- Timeline: asset time is presentation time minus the container start
  (`assets.start_offset_ms`). Chunks (5 minutes) are planned on the chosen
  stream's own timeline: chunk *k* starts at `(audioStart - assetOrigin) + k*L`,
  so a segment at `s` ms is at `k*L + s + (audioStart - assetOrigin)`.
- ffmpeg writes 16 kHz mono PCM WAV into the owned cache; `whisper-cli` runs with
  the fixed argv `-m <model> -f <wav> -l <lang|auto> -t 4 -fa -oj -of <base>`
  inside the existing suspended-before-Job containment (2 GiB, one process).
- Each finished chunk's segments are stored with the checkpoint in one
  transaction (`asr_chunks`); a resumed run never transcribes a chunk twice.
  The final commit (with COMPLETED) replaces only the asset's `source='asr'`
  track and its dialogue rows, so both FTS indexes update atomically.
- Provenance (`subtitle_tracks.provenance_json`): model and SHA-256s, asset
  SHA-256, stream, language, selection rule, offsets, `verified: false`,
  `calibrated: false`, "unverified machine transcription; no accuracy claim".
- Assets are the voice assets under `CONFIG.paths.runtime_assets/whisper`,
  checked read-only by size and SHA-256 before any child starts. Since round 37
  the runtime is the official portable whisper.cpp v1.9.5 prebuilt (release
  b5454, commit d1be6fde; archive `whisper-bin-x64.zip` `6ba69e34…`):
  `whisper-cli.exe` 479,744 B `331dba46…` AND its 12 libraries (whisper.dll,
  ggml.dll, ggml-base.dll and the ggml-cpu-* variants), each pinned; a missing
  or planted library refuses. The model is unchanged:
  `ggml-large-v3-turbo-q5_0.bin` 574,041,195 B `39422170…`. Missing or
  different means `model_unavailable` and nothing runs. The runtime and the
  library digest are part of the ASR provenance and of the chunk plan key, so
  a different runtime never resumes another's chunks. The CLI argv and JSON
  output (`transcription[].offsets.from/to`, `text`) were checked against the
  v1.9.5 source. The build needs the Visual C++ 2015-2022 x64 runtime (an
  installer prerequisite supplied separately). Provisioning: the verified stage
  (`whisper/`, `ffmpeg/`) goes to `<RuntimeAssets>/runtime/`; nothing is bundled
  in the installer.

Limits, stated plainly: no real whisper, model, GPU or audio run has been done
or accepted; the 2 GiB profile is declared, not measured; no lease period ever
frees a model slot, and if the bridge process itself dies its Jobs close and
their children end with it (kill-on-close); the CLI never transcribes.

## ffmpeg provenance

Development tests used a pre-existing user-level install:
`Gyan.FFmpeg` 9.0.1 "full_build" (WinGet). Its configure line has
`--enable-gpl --enable-version3`, which makes it a **GPLv3 binary**, and it
compiles in network protocols (kept unused by the whitelist).

- **Do not redistribute this build** with Qronos. It remains a development/test
  fixture only (QRONOS_MEDIA_FFMPEG_DIR), reported as an unpinned override; some
  fixtures encode test clips with its GPL libx264, which the shipping build lacks.
- **Shipping build (round 37):** FFmpeg 9.0.2 built by Qronos from the signed
  official source (`ffmpeg-9.0.2.tar.xz` `8c385028…`, GOODSIG by
  FCF986EA15E6E293A5644F10B4322F04D67658D8), LGPL v2.1 or later, no GPL,
  version3, nonfree or network code; protocols file and pipe only; reproduced
  byte-for-byte by a second build. `ffmpeg.exe` 21,947,392 B `6c3fc768…`,
  `ffprobe.exe` 21,726,208 B `ef43e6e3…`. Only these exact files are admitted from
  `<runtime>/ffmpeg/bin`; anything else there means no tools. Licence texts, the
  IJG credit, the source and the build recipe ship beside them
  (`runtime/ffmpeg/LICENSE`, `runtime/ffmpeg/SOURCE`). This is NOT a publisher
  binary; the registry marks it `reproducible_build_of_signed_source`.
- **Known limit:** AV1 video does not decode (no libdav1d, no hardware
  accelerator); probing still works. Media status reports it.

`python -m qronos_media tools` prints the located build's version, licence
flags, identity (pinned or unpinned) and limits.

## Semantic dialogue retrieval (Level 2)

`qronos_media/semantic.py`, stage `semantic` (after `subtitles`; an ASR
change requeues it), `MediaService.search_semantic`.

- **Model and server are not chosen here.** Qronos already owns one CPU-only
  Ollama sidecar for bge-m3 (`core/memory_runtime.py`: 127.0.0.1:11435,
  `num_gpu 0`, `keep_alive 1h`, one loaded model, an exact process identity).
  `MediaEmbedder` sends exactly that provider's request
  (`{"model": "bge-m3", "truncate": false, "keep_alive": "1h", "options":
  {"num_gpu": 0}}`) to that fixed endpoint, through `core.local_ipc` with the
  owned process as `owner`. No URL, port or model comes from a caller; media
  never starts a server and never unloads a model. Without an owned sidecar
  (the memory runtime is off by default) semantic is `unavailable`: the stage
  and the query check the embedder's current owner BEFORE reserving, so nothing
  is reserved (round 29, Main23 F1).
- **Checks before storage:** the sidecar must list bge-m3 with quantization
  F16; every answer must name bge-m3, hold one vector per text, exactly 1024
  finite numeric values, non-zero length; the response body is byte-bounded
  (new optional `max_bytes` in `core.local_ipc`, default unchanged).
- **Ownership:** a declared model (`BGE_M3`: CPU, 2048 MiB declared RAM, LIGHT
  managed governor reservation) through the same `HostedModelAuthority` as ASR.
  A returned request does not free that memory: the sidecar keeps bge-m3
  loaded (keep_alive), so the slot stays `resident` with its ledger and
  governor reservations, and later media requests reuse that one reservation
  (shared memory-runtime use is never double-counted; media never unloads the
  model). It is freed when the owned sidecar's `/api/ps` no longer lists
  bge-m3, or on a trusted contained lifecycle proof of the whole sidecar tree.
  A lease under which no inference request was dispatched (the owner was lost
  after the grant, `/api/tags` failed or refused, or every window was cached)
  and which took over no resident slot is freed at release (proof
  `no_request_dispatched`); an `/api/tags` failure is `model_unavailable`,
  never unresolved. A failed or timed-out POST leaves `request_unresolved`:
  while ANY media model request is unresolved, no media model is admitted
  (Whisper included; Root round-29 decision), and an empty `/api/ps` does NOT
  free it (a queued
  request may not be listed); only the contained lifecycle proof does. Since
  round 32 the bridge hands media the runtime's atomic
  `MemorySidecarManager.owned_generation()` pair (owner + that generation's
  immutable `SidecarLifecycle`, accepted 767b87e). A request binds it once at
  admission; every call refuses `generation_changed` before sending when the
  sidecar is no longer that generation; the slot keeps the captured
  capability and is freed only when THAT capability's `state()` is
  `tree_exited` (a later generation, its empty model list, a parent PID exit,
  a timeout, an HTTP close or an issued terminate never free it). The F16
  check is cached per bound generation. The server process merely exiting is
  not accepted (a runner may outlive it). A proved `resident`
  bge-m3 is not uncertain work: it coexists with another declared model
  through the governor and ledger, both accounted. Yielding is checked
  between batches (8 texts); one in-flight batch is bounded by its timeout
  (30 s).
- **Index:** windows of consecutive cues of one track (at most 600 characters
  and 30 s, one cue of overlap) keep the original text, track, seq range and
  times. Vectors go to `semantic_cache` (keyed by model, version, dimension and
  text) with the stage checkpoint; the searchable `semantic_windows` rows are
  written only in the result+COMPLETED transaction. A stopped, preempted,
  revoked or failed run leaves nothing searchable. Unchanged text is never
  embedded again; no ASR, scene or keyframe work reruns. Deleting a track or
  an asset deletes its windows.
- **Query:** the exact lexical answer comes first; semantic matches follow as a
  separate block with asset, times, the original window text, track
  provenance, `similarity` and the note "uncalibrated, not a probability,
  percentage or accuracy". The query vector needs the same model slot (busy or
  unavailable is reported; the lexical answer is still returned). Vectors are
  scanned in bounded chunks under the search budget and a 200,000-window cap.
  Assets whose copies all left scope are not offered.
- **Cross-language:** bge-m3 is multilingual, so a Persian query may match
  English dialogue; results report the track language and whether query and
  window scripts differ. Nothing is translated and no accuracy is claimed.
- **Topics:** deferred (above).

## Product route (round 32)

The desktop and the brain/voice turns reach the media index only through typed operations on the runtime's
ONE in-process media host (`qronos_media/product.py`):

- Transport: the registered `send_runtime_action("qronos.media", {op, requestId, ...})` (bridge stdin), answered by
  `media_result` runtime events with the same `requestId`; `media_progress` events publish index passes. No Rust
  change. The bridge routes the action before its generic pause refusal; the product refuses every non-read
  operation while Qronos is paused (status, capabilities, roots.list, search, asset.detail and decline still answer).
- Operations (allow-listed, every field bounded): roots.add / roots.remove / roots.list, scan (then a continuous
  background index), index.start / stop / pause / resume, job.retry / reindex / cancel / pause / resume,
  asset.priority, asset.detail, search (exact, fuzzy, semantic), open.prepare, report.generate (json, html, csv),
  report.export, decide / decline.
- Threads: product calls run on the host's one writer worker, between continuous index passes of at most 20 s each;
  searches run on a separate bounded query thread with read-only SQLite connections (WAL), so search answers while
  indexing. Stop drops queued calls (each is answered `stopped`); close answers everything still queued.
- Approvals: roots, exports and opens answer `approval_required` with a single-use id and `needsSecret`. The gate
  decision stays in the product (the request says yes/no only); unknown, answered or revoked ids are stale. The
  typed secret is checked by the bridge's borrowed `WorkflowSecretCheck`, passed in-process only, never echoed.
- Open at a time: the copy is re-verified now (Files read gate plus the full hash); the player comes only from the
  trusted application catalog (mpv or VLC, `core.computer_executor.trusted_media_players`); the OPEN_APPLICATION gate
  decides; `core.computer_executor.open_media_at_time` builds the exact argv (player, start option, the verified
  file) with no shell and no caller arguments. Decision time re-verifies again: a replaced copy launches nothing.
- Brain/voice: `qronos_media/intents.py` recognises quoted Persian/English library searches and index status
  deterministically (never a guess) and runs the SAME operations; the answer is a short Persian sentence.
- Desktop: `desktop/src/mediaStore.ts` (validated events, requestId matching, stale/late answers dropped, the secret
  sent once and never stored) and `components/MediaPanel.tsx` / `MediaDrawer.tsx` (Persian), mounted in App.tsx
  beside the queue and history drawers (509204f), with the round-30 repair (own MEDIA_ACTION approval kind
  revoked at Pause/Stop/close; only an mpv/VLC .exe is a player).
- Honest capabilities: implemented-unverified (ASR, semantic dialogue) are shown as such; topics, vision captions,
  OCR, subtitle/ASR fusion, speaker turns and visual embeddings are shown as not built; motion is not supported.

## Integration state

Done in this chunk (leased): `qronos_media` is in `PRODUCT_PACKAGES` (offline
policy and release hygiene), collected by `build_runtime`, a project root for
notices; the bridge hosts the media worker (above); `core/update_stores.py`
registers `media_index` (schema 3 since the semantic tables, reads schema 1, never snapshotted, so an
update that changes its format is refused: a release compatibility gate, not
rollback support).

Done since: the Persian desktop media panel (commands, approval prompts with
the typed secret, evidence/result cards, open-at-timestamp through the approved
application executor), mounted in the app; semantic (bge-m3) dialogue search
(round 26, owner/generation-bound in round 32).

Still open:

1. Human/live acceptance of the desktop workflow (no real archive, player or
   model run has been accepted).
2. Done as pins (round 37): whisper.cpp v1.9.5 and the LGPL protocol-free
   FFmpeg 9.0.2 are pinned and admitted only when exact; provisioning them into
   `<RuntimeAssets>/runtime/` is a separate step (nothing installed by this code).
3. Vision/OCR, topics, subtitle/ASR fusion and the other pending capabilities
   listed by `PENDING_CAPABILITIES`: later chunks, under the same authority.

## Tests

`tests/test_media_*.py` use synthetic fixtures only: generated subtitles, and
two-colour videos generated with ffmpeg `lavfi` under pytest's temporary folder.
Tests that decode need `QRONOS_MEDIA_FFMPEG_DIR` and are skipped without it.
No real films, audio, screens, GPUs or models are used.
