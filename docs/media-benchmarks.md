# Media benchmarks (offline evaluator)

`qronos_media/benchmark.py` scores **saved** benchmark files for the media index (handoff sections 44, 45 and 68).
It reads only files named on the command line and prints one JSON result to stdout. It never opens the media index,
starts a model, the service or a profile, observes the machine, scans folders, downloads anything or writes files.

```text
python -m qronos_media.benchmark retrieval --dataset items.json --predictions ranked.json
python -m qronos_media.benchmark speech    --dataset references.json --hypotheses hypotheses.json
python -m qronos_media.benchmark system    --measurements measurements.json
```

Exit codes: `0` result printed; `2` usage error; `3` an input was refused (`{"error": "input_refused", "detail"}`);
`4` the work budget was exceeded (`work_budget_exceeded`). Output is UTF-8 JSON (Persian text kept as is).

What it is not: the configured models are fixed, so these scores never select or change a model, and nothing here is
a calibrated confidence. Scores on the bundled synthetic sample (`tests/media/benchmark_sample/`) prove the contract,
not quality. Real archive samples, real model runs and human labels are not part of this tool and remain deferred.

## Inputs (all versioned JSON, UTF-8, at most 16 MiB each)

Every file names its dataset with **provenance**: `id`, `version`, `source`, and `synthetic` (true/false, required);
optional `description`, `labelledBy`, `created`. Prediction, hypothesis and measurement files also name the
**system** that produced them: `name`, optional `model`, `profile`, `sourceHead`, `settings` (≤ 50 short values),
`note`. The result repeats both, plus the SHA-256 of every input file and `synthetic`.

### Retrieval (`qronos.media.benchmark.retrieval/1` + `qronos.media.benchmark.predictions/1`)

Dataset item (≤ 10,000 items): `id`, `query`, `type` (`exact`, `fuzzy`, `semantic`, `topic`, `visual`, `multimodal`,
`cross-language`), `queryLanguage`, `contentLanguage` (ISO 639 codes such as `fa`, `en`), `difficulty` (`easy`,
`medium`, `hard`), `relevant`: 1–50 targets `{assetId, startMs, endMs, scene?}`. Optional
`matching.toleranceMs` (0–60,000, default 0).

Predictions: `dataset {id, version}` (must equal the dataset), `system`, `results`: one entry per item at most
`{itemId, ranked: [{assetId, startMs, endMs, scene?, score?}]}` (≤ 100 ranked). `score` is validated and ignored.

**Matching.** A prediction matches a relevant target when it is the same asset, the scenes agree when BOTH name one,
and the predicted window widened by `toleranceMs` overlaps the relevant window with positive length (touching is not
an overlap). Each target is credited once, at its first matching rank; one prediction credits at most one target
(the first listed it matches); a prediction that matches only already-credited targets is a **duplicate** (it keeps
its rank and is counted).

**Metrics.** `recallAt` k ∈ {5, 10, 50}: mean over items of (targets credited within the top k / that item's
targets). `microRecallAt`: all credited targets within the top k / all targets. `mrr`: mean over items of 1/rank of
the first crediting prediction, 0 if none. An item without a result list scores 0 and is counted in
`missingPredictions`. Results are also grouped `byType`, `byDifficulty`, `byLanguagePair`, and `crossLanguage`
reports `fa->en` (Persian query → English content) and `en->fa` with their item counts; `perItem` gives each item's
ranks.

### Speech (`qronos.media.benchmark.speech/1` + `qronos.media.benchmark.speech-hypotheses/1`)

Reference segment: `id`, `reference` (may be empty), optional `language`, optional `startMs`+`endMs` (both or neither).
Hypothesis segment: `id`, `hypothesis` (may be empty), optional `startMs`+`endMs`. Unknown or duplicate ids are
refused; a reference without a hypothesis is scored as an empty hypothesis and counted (`missingHypotheses`).

Both forms are always reported, so normalization never hides an error:

- `raw`: Unicode NFC and whitespace collapsing only; case, punctuation, Arabic/Persian letter variants and digits count.
- `normalized`: `qronos_media.text.normalise_for_search` (the index's own form): Persian/Arabic letter variants,
  Persian/Arabic-Indic digits, diacritics, tatweel, bidi marks and ZWNJ folded; case folded; punctuation and subtitle
  markup removed.

WER = word edit distance / reference words (words = the form's whitespace tokens). CER = character edit distance /
reference characters, spaces removed. `micro`: summed edits / summed reference units (insertions against an empty
reference count in the numerator). `macro`: mean of per-segment rates over segments with a non-empty reference (an
empty reference has no rate: `null`, reported under `emptyReferences`). Substitutions, deletions and insertions are
reported. **Timestamp error** only over segments with an annotated window in BOTH files (mean absolute start/end
error, maximum); nothing is aligned or inferred; with no pair it is `unavailable`.

### System (`qronos.media.benchmark.system/1`)

Known measurements (each `{value, unit, method, measuredBy, samples?, note?}` with the exact unit):
`searchLatencyMs`, `indexThroughputMediaHoursPerHour`, `peakVramMb`, `peakRamMb`, `cpuLoadPercent`,
`storageMbPerIndexedHour`, `reindexCostSeconds`, `resumeSuccessRate`. A supplied value is validated (finite, in range)
and reported as `supplied` with its method and measurer; a missing one is `unavailable` - never zero and never a
performance claim. `observedByThisTool` is always false.

## Refused inputs and bounds

Refused (exit 3): a URL, network/UNC path, symbolic link, reparse point, folder or device; a file over 16 MiB; not
UTF-8 JSON; NaN/Infinity; duplicate JSON keys; unknown or missing fields; duplicate item/segment ids; duplicate
relevant targets; a second result list for an item; predictions for an unknown item or another dataset version;
`endMs <= startMs`, negative or out-of-range numbers; unknown query types, difficulties, language codes or system
measurements; units that do not match. Texts are at most 2,000 characters, identifiers 128. Edit-distance work is
capped at 50 million cells per run (exit 4).

## Minimal realistic dataset (not provided here)

A dataset worth scoring needs real material from the person's own archive, labelled by a person: for each item the
exact asset, scene and time range of every correct answer, covering every query type, both `fa->en` and `en->fa`, and
the handoff categories (exact/approximate dialogue, topic, cross-language, visual, visual + dialogue, noisy audio,
Persian dub, English original, subtitle present/absent). Predictions come from a saved run of the configured system
with its exact source head and settings in `system`. Speech references are human transcripts with annotated windows.
Collecting them, running the real models and the human review are deferred; nothing in this tool substitutes for them.
