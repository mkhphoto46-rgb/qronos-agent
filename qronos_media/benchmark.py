"""Offline evaluation of SAVED media benchmark files (handoff s44, s45, s68): retrieval, speech and system metrics.

This tool only reads files the person names on the command line and prints one JSON result to stdout. It never opens
the media index, starts a model, the service or a profile, observes the machine, scans folders or writes files.
Every number it prints is computed from the saved inputs; a supplied system measurement is validated and reported
with its stated provenance, never measured here. Scores on synthetic data say nothing about real quality and must not
drive model selection (the configured models are fixed).

Usage::

    python -m qronos_media.benchmark retrieval --dataset items.json --predictions ranked.json
    python -m qronos_media.benchmark speech --dataset references.json --hypotheses hypotheses.json
    python -m qronos_media.benchmark system --measurements measurements.json

Exit codes: 0 result printed; 2 usage error; 3 an input was refused (malformed, unsafe, ambiguous or out of range);
4 the work budget was exceeded. The contract is documented in docs/media-benchmarks.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable

TOOL_VERSION = 1
RESULT_SCHEMA = "qronos.media.benchmark.result/1"
SCHEMAS = {
    "retrieval": "qronos.media.benchmark.retrieval/1",
    "predictions": "qronos.media.benchmark.predictions/1",
    "speech": "qronos.media.benchmark.speech/1",
    "hypotheses": "qronos.media.benchmark.speech-hypotheses/1",
    "system": "qronos.media.benchmark.system/1",
}
QUERY_TYPES = ("exact", "fuzzy", "semantic", "topic", "visual", "multimodal", "cross-language")
DIFFICULTIES = ("easy", "medium", "hard")
RECALL_AT = (5, 10, 50)
# Bounds on what a saved file may hold (a hostile or broken file is refused, never partly scored).
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 10_000
MAX_RELEVANT = 50
MAX_RANKED = 100
MAX_TEXT_CHARS = 2_000
MAX_ID_CHARS = 128
MAX_MS = 10**9                      # about 277 hours
MAX_TOLERANCE_MS = 60_000
MAX_EDIT_CELLS = 50_000_000         # total edit-distance work (cells) per run, words and characters together
NOTICE = ("Computed only from the saved inputs named here; nothing was observed, run or measured by this tool. "
          "Synthetic data proves the contract, not quality; no score here is a calibrated confidence or a model "
          "selection.")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,%d}$" % (MAX_ID_CHARS - 1))
_LANG = re.compile(r"^[a-z]{2,3}$")
# System measurements this tool understands: name -> (unit, lowest, highest).
SYSTEM_METRICS = {
    "searchLatencyMs": ("ms", 0.0, 3_600_000.0),
    "indexThroughputMediaHoursPerHour": ("media hours per wall-clock hour", 0.0, 100_000.0),
    "peakVramMb": ("MiB", 0.0, 1_048_576.0),
    "peakRamMb": ("MiB", 0.0, 16_777_216.0),
    "cpuLoadPercent": ("percent of all logical processors", 0.0, 100.0),
    "storageMbPerIndexedHour": ("MiB per indexed media hour", 0.0, 1_048_576.0),
    "reindexCostSeconds": ("s", 0.0, 31_536_000.0),
    "resumeSuccessRate": ("fraction of interrupted runs that resumed correctly", 0.0, 1.0),
}


class Refused(ValueError):
    """An input this tool will not score (exit 3)."""


class BudgetExceeded(RuntimeError):
    """The bounded amount of work was exceeded (exit 4)."""


# ------------------------------------------------------------------ safe reading


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise Refused(f"duplicate JSON key {key[:40]!r}")
        seen[key] = value
    return seen


def _no_constants(name: str) -> Any:
    raise Refused(f"non-finite number {name} is not allowed")


def read_json(path_text: str) -> tuple[Any, str]:
    """(parsed JSON, SHA-256) of ONE explicitly named local regular file; links, URLs, devices and oversize refused."""
    if not isinstance(path_text, str) or not path_text or "\0" in path_text or "://" in path_text \
            or path_text.startswith(("\\\\", "//")):
        raise Refused("inputs must be local file paths (no URL or network path)")
    path = Path(path_text)
    try:
        info = os.lstat(path)
    except OSError:
        raise Refused(f"cannot read {path.name!r}") from None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or not stat.S_ISREG(info.st_mode):
        raise Refused(f"{path.name!r} is not a regular file (links and reparse points are refused)")
    if info.st_size > MAX_FILE_BYTES:
        raise Refused(f"{path.name!r} is larger than {MAX_FILE_BYTES} bytes")
    with open(path, "rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise Refused(f"{path.name!r} grew beyond {MAX_FILE_BYTES} bytes")
    try:
        text = data.decode("utf-8-sig")
        value = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants)
    except Refused:
        raise
    except (UnicodeDecodeError, ValueError) as error:
        raise Refused(f"{path.name!r} is not UTF-8 JSON ({type(error).__name__})") from None
    except RecursionError:  # raised by the json decoder itself on hostile nesting; a small file must not crash the CLI
        raise Refused(f"{path.name!r} is nested too deeply to read") from None
    return value, hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ validation helpers


def _obj(value: Any, where: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    if not isinstance(value, dict):
        raise Refused(f"{where} must be an object")
    missing = [key for key in required if key not in value]
    extra = [key for key in value if key not in required and key not in optional]
    if missing or extra:
        raise Refused(f"{where}: missing {missing} / unknown {extra}")
    return value


def _text(value: Any, where: str, *, limit: int = MAX_TEXT_CHARS, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise Refused(f"{where} must be a {'' if empty else 'non-empty '}string of at most {limit} characters")
    return value


def _ident(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise Refused(f"{where} must be an identifier ([A-Za-z0-9._:-], at most {MAX_ID_CHARS} characters)")
    return value


def _int(value: Any, where: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise Refused(f"{where} must be an integer in {low}..{high}")
    return value


def _number(value: Any, where: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise Refused(f"{where} must be a finite number in {low}..{high}")
    return float(value)


def _lang(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _LANG.match(value):
        raise Refused(f"{where} must be a lower-case ISO 639 code such as 'fa' or 'en'")
    return value


def _window(value: dict, where: str) -> tuple[int, int]:
    start = _int(value.get("startMs"), f"{where}.startMs", 0, MAX_MS)
    end = _int(value.get("endMs"), f"{where}.endMs", 0, MAX_MS)
    if end <= start:
        raise Refused(f"{where}: endMs must be greater than startMs")
    return start, end


def _provenance(value: Any, where: str) -> dict:
    record = _obj(value, where, ("id", "version", "source", "synthetic"), ("description", "labelledBy", "created"))
    if not isinstance(record["synthetic"], bool):
        raise Refused(f"{where}.synthetic must be true or false (say whether the labels are synthetic)")
    return {"id": _ident(record["id"], f"{where}.id"), "version": _ident(str(record["version"]), f"{where}.version"),
            "source": _text(record["source"], f"{where}.source", limit=500), "synthetic": record["synthetic"],
            **{key: _text(record[key], f"{where}.{key}", limit=500) for key in ("description", "labelledBy", "created")
               if key in record}}


def _system(value: Any, where: str) -> dict:
    record = _obj(value, where, ("name",), ("model", "profile", "sourceHead", "settings", "note"))
    out = {"name": _text(record["name"], f"{where}.name", limit=200)}
    for key in ("model", "profile", "sourceHead", "note"):
        if key in record:
            out[key] = _text(record[key], f"{where}.{key}", limit=500)
    if "settings" in record:
        settings = record["settings"]
        if not isinstance(settings, dict) or len(settings) > 50 or any(
                not isinstance(k, str) or len(k) > 64 or not isinstance(v, (str, int, bool)) or
                (isinstance(v, str) and len(v) > 200) for k, v in settings.items()):
            raise Refused(f"{where}.settings must be at most 50 short string/int/bool values")
        out["settings"] = dict(settings)
    return out


def _same_dataset(reference: dict, value: Any, where: str) -> None:
    record = _obj(value, where, ("id", "version"))
    if record["id"] != reference["id"] or str(record["version"]) != reference["version"]:
        raise Refused(f"{where} names dataset {record['id']!r} v{record['version']!r}, not "
                      f"{reference['id']!r} v{reference['version']!r}")


# ------------------------------------------------------------------ retrieval


def load_retrieval(dataset: Any) -> dict:
    doc = _obj(dataset, "dataset", ("schema", "dataset", "items"), ("matching",))
    if doc["schema"] != SCHEMAS["retrieval"]:
        raise Refused(f"dataset.schema must be {SCHEMAS['retrieval']!r}")
    provenance = _provenance(doc["dataset"], "dataset.dataset")
    matching = _obj(doc.get("matching", {}), "dataset.matching", (), ("toleranceMs",))
    tolerance = _int(matching.get("toleranceMs", 0), "dataset.matching.toleranceMs", 0, MAX_TOLERANCE_MS)
    items = doc["items"]
    if not isinstance(items, list) or not 0 < len(items) <= MAX_ITEMS:
        raise Refused(f"dataset.items must hold 1..{MAX_ITEMS} items")
    out: dict[str, dict] = {}
    for index, raw in enumerate(items):
        where = f"dataset.items[{index}]"
        item = _obj(raw, where, ("id", "query", "type", "queryLanguage", "contentLanguage", "difficulty", "relevant"))
        item_id = _ident(item["id"], f"{where}.id")
        if item_id in out:
            raise Refused(f"{where}: duplicate item id {item_id!r}")
        if item["type"] not in QUERY_TYPES:
            raise Refused(f"{where}.type must be one of {QUERY_TYPES}")
        if item["difficulty"] not in DIFFICULTIES:
            raise Refused(f"{where}.difficulty must be one of {DIFFICULTIES}")
        relevant = item["relevant"]
        if not isinstance(relevant, list) or not 0 < len(relevant) <= MAX_RELEVANT:
            raise Refused(f"{where}.relevant must hold 1..{MAX_RELEVANT} targets")
        targets = []
        for t_index, target in enumerate(relevant):
            t_where = f"{where}.relevant[{t_index}]"
            record = _obj(target, t_where, ("assetId", "startMs", "endMs"), ("scene",))
            start, end = _window(record, t_where)
            scene = _int(record["scene"], f"{t_where}.scene", 0, 1_000_000) if "scene" in record else None
            key = (_ident(record["assetId"], f"{t_where}.assetId"), scene, start, end)
            if key in targets:
                raise Refused(f"{t_where}: duplicate relevant target")
            targets.append(key)
        out[item_id] = {"query": _text(item["query"], f"{where}.query"), "type": item["type"],
                        "queryLanguage": _lang(item["queryLanguage"], f"{where}.queryLanguage"),
                        "contentLanguage": _lang(item["contentLanguage"], f"{where}.contentLanguage"),
                        "difficulty": item["difficulty"], "targets": targets}
    return {"provenance": provenance, "toleranceMs": tolerance, "items": out}


def load_predictions(predictions: Any, dataset: dict) -> dict:
    doc = _obj(predictions, "predictions", ("schema", "dataset", "system", "results"))
    if doc["schema"] != SCHEMAS["predictions"]:
        raise Refused(f"predictions.schema must be {SCHEMAS['predictions']!r}")
    _same_dataset(dataset["provenance"], doc["dataset"], "predictions.dataset")
    system = _system(doc["system"], "predictions.system")
    results = doc["results"]
    if not isinstance(results, list) or len(results) > MAX_ITEMS:
        raise Refused(f"predictions.results must hold at most {MAX_ITEMS} entries")
    ranked: dict[str, list] = {}
    for index, raw in enumerate(results):
        where = f"predictions.results[{index}]"
        record = _obj(raw, where, ("itemId", "ranked"))
        item_id = _ident(record["itemId"], f"{where}.itemId")
        if item_id not in dataset["items"]:
            raise Refused(f"{where}: unknown item {item_id!r}")
        if item_id in ranked:
            raise Refused(f"{where}: a second result list for item {item_id!r}")
        entries = record["ranked"]
        if not isinstance(entries, list) or len(entries) > MAX_RANKED:
            raise Refused(f"{where}.ranked must hold at most {MAX_RANKED} predictions")
        rows = []
        for r_index, entry in enumerate(entries):
            r_where = f"{where}.ranked[{r_index}]"
            pred = _obj(entry, r_where, ("assetId", "startMs", "endMs"), ("scene", "score"))
            start, end = _window(pred, r_where)
            if "score" in pred:
                _number(pred["score"], f"{r_where}.score", -1e12, 1e12)  # kept out of every metric (not calibrated)
            rows.append((_ident(pred["assetId"], f"{r_where}.assetId"),
                         _int(pred["scene"], f"{r_where}.scene", 0, 1_000_000) if "scene" in pred else None,
                         start, end))
        ranked[item_id] = rows
    return {"system": system, "ranked": ranked}


def matches(prediction: tuple, target: tuple, tolerance_ms: int) -> bool:
    """Same asset; the scenes agree when BOTH name one; and the predicted window, widened by the tolerance, overlaps
    the relevant window (a shared instant is not enough: the overlap must have positive length)."""
    p_asset, p_scene, p_start, p_end = prediction
    t_asset, t_scene, t_start, t_end = target
    if p_asset != t_asset or (p_scene is not None and t_scene is not None and p_scene != t_scene):
        return False
    return max(p_start - tolerance_ms, t_start) < min(p_end + tolerance_ms, t_end)


def score_item(ranked: list[tuple], targets: list[tuple], tolerance_ms: int) -> dict:
    """Per-item ranks: each relevant target is credited once, at the first prediction that matches it; a later
    prediction matching only already-credited targets is a duplicate (it still takes its rank)."""
    credited: dict[int, int] = {}
    duplicates = 0
    first_hit = None
    for rank, prediction in enumerate(ranked, start=1):
        hit = [index for index, target in enumerate(targets) if matches(prediction, target, tolerance_ms)]
        new = [index for index in hit if index not in credited]
        if hit and not new:
            duplicates += 1
        for index in new[:1]:  # one prediction credits at most ONE target (the earliest listed)
            credited[index] = rank
        if new and first_hit is None:
            first_hit = rank
    recall = {k: sum(1 for rank in credited.values() if rank <= k) / len(targets) for k in RECALL_AT}
    return {"recall": recall, "found": {k: sum(1 for rank in credited.values() if rank <= k) for k in RECALL_AT},
            "targets": len(targets), "reciprocalRank": 1.0 / first_hit if first_hit else 0.0, "firstHitRank": first_hit,
            "duplicates": duplicates, "ranked": len(ranked)}


def _aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"items": 0}
    targets = sum(row["targets"] for row in rows)
    return {"items": len(rows), "relevantTargets": targets,
            "recallAt": {str(k): round(sum(row["recall"][k] for row in rows) / len(rows), 6) for k in RECALL_AT},
            "microRecallAt": {str(k): round(sum(row["found"][k] for row in rows) / targets, 6) for k in RECALL_AT},
            "mrr": round(sum(row["reciprocalRank"] for row in rows) / len(rows), 6),
            "itemsWithAHit": sum(1 for row in rows if row["firstHitRank"]),
            "duplicatePredictions": sum(row["duplicates"] for row in rows)}


def evaluate_retrieval(dataset_doc: Any, predictions_doc: Any) -> dict:
    dataset = load_retrieval(dataset_doc)
    predictions = load_predictions(predictions_doc, dataset)
    rows: dict[str, dict] = {}
    for item_id, item in dataset["items"].items():
        rows[item_id] = {**score_item(predictions["ranked"].get(item_id, []), item["targets"], dataset["toleranceMs"]),
                         "type": item["type"], "pair": f"{item['queryLanguage']}->{item['contentLanguage']}",
                         "difficulty": item["difficulty"], "predicted": item_id in predictions["ranked"]}
    values = list(rows.values())
    by = lambda key: {name: _aggregate([r for r in values if r[key] == name])  # noqa: E731
                      for name in sorted({r[key] for r in values})}
    return {
        "metric": "retrieval", "dataset": dataset["provenance"], "system": predictions["system"],
        "matching": {"toleranceMs": dataset["toleranceMs"],
                     "rule": "same asset; scenes equal when both given; positive-length overlap of the predicted "
                             "window widened by toleranceMs with the relevant window; each relevant target credited "
                             "once at its first matching rank; one prediction credits at most one target"},
        "definitions": {"recallAt": "mean over items of credited targets within the top k / that item's targets",
                        "microRecallAt": "credited targets within the top k over all items / all targets",
                        "mrr": "mean over items of 1/rank of the first prediction that credits a target (0 if none)",
                        "missingPredictions": "items without a result list score 0 and are counted"},
        "overall": _aggregate(values),
        "missingPredictions": sum(1 for r in values if not r["predicted"]),
        "byType": by("type"), "byLanguagePair": by("pair"), "byDifficulty": by("difficulty"),
        "crossLanguage": {"fa->en": _aggregate([r for r in values if r["pair"] == "fa->en"]),
                          "en->fa": _aggregate([r for r in values if r["pair"] == "en->fa"])},
        "perItem": {item_id: {"recallAt": {str(k): round(r["recall"][k], 6) for k in RECALL_AT},
                              "reciprocalRank": round(r["reciprocalRank"], 6), "firstHitRank": r["firstHitRank"],
                              "targets": r["targets"], "ranked": r["ranked"], "duplicates": r["duplicates"],
                              "predicted": r["predicted"]} for item_id, r in rows.items()},
    }


# ------------------------------------------------------------------ speech


def raw_form(text: str) -> str:
    """'raw': only Unicode NFC and whitespace collapsing; case, punctuation, letter variants and digits all count."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def normalized_form(text: str) -> str:
    """'normalized': qronos_media.text.normalise_for_search (Persian/Arabic letter variants, Persian/Arabic-Indic digits,
    diacritics, tatweel, bidi marks and ZWNJ folded; case folded; punctuation removed; subtitle markup removed)."""
    from qronos_media.text import normalise_for_search

    return normalise_for_search(text)


FORMS: dict[str, Callable[[str], str]] = {"raw": raw_form, "normalized": normalized_form}


class _Budget:
    def __init__(self, cells: int) -> None:
        self.left = cells

    def spend(self, cells: int) -> None:
        self.left -= cells
        if self.left < 0:
            raise BudgetExceeded(f"more than {MAX_EDIT_CELLS} edit-distance cells would be needed")


def edit_distance(reference: list, hypothesis: list, budget: _Budget) -> tuple[int, int, int, int]:
    """(substitutions, deletions, insertions, distance) of the minimal Levenshtein alignment (unit costs)."""
    budget.spend((len(reference) + 1) * (len(hypothesis) + 1))
    # each cell holds (distance, substitutions, deletions, insertions); ties prefer substitution, then deletion
    previous = [(j, 0, 0, j) for j in range(len(hypothesis) + 1)]
    for i in range(1, len(reference) + 1):
        current = [(i, 0, i, 0)]
        for j in range(1, len(hypothesis) + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                best = previous[j - 1]
            else:
                d, s, de, ins = previous[j - 1]
                best = (d + 1, s + 1, de, ins)
                d, s, de, ins = previous[j]
                if d + 1 < best[0]:
                    best = (d + 1, s, de + 1, ins)
                d, s, de, ins = current[j - 1]
                if d + 1 < best[0]:
                    best = (d + 1, s, de, ins + 1)
            current.append(best)
        previous = current
    distance, subs, dels, ins = previous[-1]
    return subs, dels, ins, distance


def load_speech(dataset_doc: Any, hypotheses_doc: Any) -> tuple[dict, dict, list[tuple]]:
    doc = _obj(dataset_doc, "dataset", ("schema", "dataset", "segments"))
    if doc["schema"] != SCHEMAS["speech"]:
        raise Refused(f"dataset.schema must be {SCHEMAS['speech']!r}")
    provenance = _provenance(doc["dataset"], "dataset.dataset")
    hyp = _obj(hypotheses_doc, "hypotheses", ("schema", "dataset", "system", "segments"))
    if hyp["schema"] != SCHEMAS["hypotheses"]:
        raise Refused(f"hypotheses.schema must be {SCHEMAS['hypotheses']!r}")
    _same_dataset(provenance, hyp["dataset"], "hypotheses.dataset")
    system = _system(hyp["system"], "hypotheses.system")

    def segments(value: Any, where: str, text_key: str) -> dict[str, dict]:
        if not isinstance(value, list) or len(value) > MAX_ITEMS:
            raise Refused(f"{where} must hold at most {MAX_ITEMS} segments")
        out: dict[str, dict] = {}
        for index, raw in enumerate(value):
            s_where = f"{where}[{index}]"
            record = _obj(raw, s_where, ("id", text_key), ("language", "startMs", "endMs"))
            segment_id = _ident(record["id"], f"{s_where}.id")
            if segment_id in out:
                raise Refused(f"{s_where}: duplicate segment id {segment_id!r}")
            timed = ("startMs" in record, "endMs" in record)
            if timed[0] != timed[1]:
                raise Refused(f"{s_where}: startMs and endMs come together or not at all")
            out[segment_id] = {"text": _text(record[text_key], f"{s_where}.{text_key}", empty=True),
                               "language": _lang(record["language"], f"{s_where}.language")
                               if "language" in record else None,
                               "window": _window(record, s_where) if timed[0] else None}
        return out

    references = segments(doc["segments"], "dataset.segments", "reference")
    if not references:
        raise Refused("dataset.segments must hold at least one segment")
    hypotheses = segments(hyp["segments"], "hypotheses.segments", "hypothesis")
    unknown = sorted(set(hypotheses) - set(references))
    if unknown:
        raise Refused(f"hypotheses for unknown segments: {unknown[:5]}")
    pairs = [(segment_id, reference, hypotheses.get(segment_id)) for segment_id, reference in references.items()]
    return {"provenance": provenance}, {"system": system}, pairs


def evaluate_speech(dataset_doc: Any, hypotheses_doc: Any) -> dict:
    dataset, system, pairs = load_speech(dataset_doc, hypotheses_doc)
    budget = _Budget(MAX_EDIT_CELLS)
    forms: dict[str, dict] = {}
    for form_name, form in FORMS.items():
        totals = {"wer": [0, 0, 0, 0, 0], "cer": [0, 0, 0, 0, 0]}  # subs, dels, ins, distance, reference length
        per_segment: dict[str, dict] = {}
        macro: dict[str, list[float]] = {"wer": [], "cer": []}
        empty_refs = {"count": 0, "withInsertions": 0}
        for segment_id, reference, hypothesis in pairs:
            ref_text = form(reference["text"])
            hyp_text = form(hypothesis["text"]) if hypothesis else ""
            units = {"wer": (ref_text.split(), hyp_text.split()),
                     "cer": (list(ref_text.replace(" ", "")), list(hyp_text.replace(" ", "")))}
            row = {}
            for metric, (ref_units, hyp_units) in units.items():
                subs, dels, ins, distance = edit_distance(ref_units, hyp_units, budget)
                for slot, value in enumerate((subs, dels, ins, distance, len(ref_units))):
                    totals[metric][slot] += value
                rate = distance / len(ref_units) if ref_units else None
                if rate is not None:
                    macro[metric].append(rate)
                row[metric] = {"substitutions": subs, "deletions": dels, "insertions": ins,
                               "referenceUnits": len(ref_units), "rate": None if rate is None else round(rate, 6)}
            if not ref_text.split():
                empty_refs["count"] += 1
                empty_refs["withInsertions"] += 1 if hyp_text.split() else 0
            row["hypothesisMissing"] = hypothesis is None
            per_segment[segment_id] = row
        forms[form_name] = {
            metric: {"micro": round(totals[metric][3] / totals[metric][4], 6) if totals[metric][4] else None,
                     "macro": round(sum(macro[metric]) / len(macro[metric]), 6) if macro[metric] else None,
                     "macroSegments": len(macro[metric]),
                     "substitutions": totals[metric][0], "deletions": totals[metric][1],
                     "insertions": totals[metric][2], "referenceUnits": totals[metric][4]}
            for metric in ("wer", "cer")}
        forms[form_name]["emptyReferences"] = empty_refs
        forms[form_name]["perSegment"] = per_segment
    paired = [(r["window"], h["window"]) for _, r, h in pairs if h and r["window"] and h["window"]]
    timing = ({"pairedSegments": len(paired),
               "meanAbsStartErrorMs": round(sum(abs(r[0] - h[0]) for r, h in paired) / len(paired), 3),
               "meanAbsEndErrorMs": round(sum(abs(r[1] - h[1]) for r, h in paired) / len(paired), 3),
               "maxAbsErrorMs": max(max(abs(r[0] - h[0]), abs(r[1] - h[1])) for r, h in paired)}
              if paired else {"pairedSegments": 0, "status": "unavailable",
                              "reason": "no segment carries an annotated window in BOTH the reference and the hypothesis"})
    timing["unpairedSegments"] = len(pairs) - len(paired)
    return {"metric": "speech", "dataset": dataset["provenance"], "system": system["system"],
            "segments": len(pairs), "missingHypotheses": sum(1 for _, _, h in pairs if h is None),
            "definitions": {
                "forms": {"raw": raw_form.__doc__.strip(), "normalized": normalized_form.__doc__.strip()},
                "wer": "word edit distance / reference words; words are the form's whitespace-separated tokens",
                "cer": "character edit distance / reference characters, spaces removed first",
                "micro": "summed edits over summed reference units; insertions against an empty reference count",
                "macro": "mean of per-segment rates over segments whose reference is not empty (empty: undefined)",
                "missingHypothesis": "scored as an empty hypothesis (every reference unit deleted)",
                "timestamp": "only segments with an annotated window in BOTH files; no alignment is inferred"},
            "byForm": forms, "timestamps": timing}


# ------------------------------------------------------------------ system


def evaluate_system(measurements_doc: Any) -> dict:
    doc = _obj(measurements_doc, "measurements", ("schema", "dataset", "system", "measurements"))
    if doc["schema"] != SCHEMAS["system"]:
        raise Refused(f"measurements.schema must be {SCHEMAS['system']!r}")
    provenance = _provenance(doc["dataset"], "measurements.dataset")
    system = _system(doc["system"], "measurements.system")
    supplied = doc["measurements"]
    if not isinstance(supplied, dict):
        raise Refused("measurements.measurements must be an object")
    unknown = sorted(set(supplied) - set(SYSTEM_METRICS))
    if unknown:
        raise Refused(f"unknown measurements {unknown[:5]} (known: {sorted(SYSTEM_METRICS)})")
    out = {}
    for name, (unit, low, high) in SYSTEM_METRICS.items():
        if name not in supplied:
            out[name] = {"status": "unavailable", "unit": unit}
            continue
        record = _obj(supplied[name], f"measurements.{name}", ("value", "unit", "method", "measuredBy"),
                      ("samples", "note"))
        if record["unit"] != unit:
            raise Refused(f"measurements.{name}.unit must be {unit!r}")
        out[name] = {"status": "supplied", "value": _number(record["value"], f"measurements.{name}.value", low, high),
                     "unit": unit, "method": _text(record["method"], f"measurements.{name}.method", limit=500),
                     "measuredBy": _text(record["measuredBy"], f"measurements.{name}.measuredBy", limit=200)}
        if "samples" in record:
            out[name]["samples"] = _int(record["samples"], f"measurements.{name}.samples", 1, 10_000_000)
        if "note" in record:
            out[name]["note"] = _text(record["note"], f"measurements.{name}.note", limit=500)
    return {"metric": "system", "dataset": provenance, "system": system, "observedByThisTool": False,
            "definitions": {"supplied": "validated value as stated by its measurer; not reproduced here",
                            "unavailable": "not supplied; never zero and never a performance claim"},
            "measurements": out}


# ------------------------------------------------------------------ CLI


def _result(kind: str, body: dict, inputs: dict[str, str], synthetic: bool) -> dict:
    return {"schema": RESULT_SCHEMA, "tool": {"name": "qronos_media.benchmark", "version": TOOL_VERSION},
            "kind": kind, "synthetic": synthetic, "notice": NOTICE, "inputs": inputs, **body}


def run(argv: list[str]) -> tuple[int, dict]:
    parser = argparse.ArgumentParser(prog="python -m qronos_media.benchmark",
                                     description="Offline metrics over SAVED, explicitly named benchmark files.")
    commands = parser.add_subparsers(dest="command", required=True)
    retrieval = commands.add_parser("retrieval", help="Recall@5/10/50 and MRR of saved ranked predictions")
    retrieval.add_argument("--dataset", required=True)
    retrieval.add_argument("--predictions", required=True)
    speech = commands.add_parser("speech", help="WER/CER (raw and normalized) and paired timestamp error")
    speech.add_argument("--dataset", required=True)
    speech.add_argument("--hypotheses", required=True)
    system = commands.add_parser("system", help="validate supplied system measurements (never measured here)")
    system.add_argument("--measurements", required=True)
    args = parser.parse_args(argv)  # usage errors exit 2 (argparse)
    try:
        if args.command == "retrieval":
            (dataset, d_sha), (predictions, p_sha) = read_json(args.dataset), read_json(args.predictions)
            body = evaluate_retrieval(dataset, predictions)
            inputs = {"datasetSha256": d_sha, "predictionsSha256": p_sha}
        elif args.command == "speech":
            (dataset, d_sha), (hypotheses, h_sha) = read_json(args.dataset), read_json(args.hypotheses)
            body = evaluate_speech(dataset, hypotheses)
            inputs = {"datasetSha256": d_sha, "hypothesesSha256": h_sha}
        else:
            measurements, m_sha = read_json(args.measurements)
            body = evaluate_system(measurements)
            inputs = {"measurementsSha256": m_sha}
    except Refused as error:
        return 3, {"schema": RESULT_SCHEMA, "error": "input_refused", "detail": str(error)[:500]}
    except BudgetExceeded as error:
        return 4, {"schema": RESULT_SCHEMA, "error": "work_budget_exceeded", "detail": str(error)[:500]}
    return 0, _result(args.command, body, inputs, bool(body["dataset"]["synthetic"]))


def main(argv: list[str] | None = None) -> int:
    code, payload = run(sys.argv[1:] if argv is None else argv)
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
