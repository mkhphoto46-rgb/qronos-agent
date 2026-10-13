"""The offline media benchmark evaluator (qronos_media.benchmark) over saved, synthetic, hand-labelled files.

Every expected number below is computed by hand from tests/media/benchmark_sample (documented next to each assertion).
Nothing here touches a model, the media index, a device or the network; the CLI is run as a real subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from qronos_media import benchmark as bm

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "media" / "benchmark_sample"


def cli(*args: str) -> tuple[int, dict]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run([sys.executable, "-B", "-m", "qronos_media.benchmark", *args], cwd=ROOT, env=env,
                          capture_output=True, timeout=120)
    text = done.stdout.decode("utf-8")
    return done.returncode, (json.loads(text) if text.strip() else {"stderr": done.stderr.decode("utf-8", "replace")})


def write(path: Path, value) -> str:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return str(path)


def sample(name: str):
    return json.loads((SAMPLE / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- retrieval


def test_retrieval_cli_reports_hand_computed_recall_mrr_groups_and_provenance():
    code, out = cli("retrieval", "--dataset", str(SAMPLE / "retrieval_dataset.json"),
                    "--predictions", str(SAMPLE / "retrieval_predictions.json"))
    assert code == 0 and out["schema"] == bm.RESULT_SCHEMA and out["synthetic"] is True and "not quality" in out["notice"]
    assert out["dataset"]["id"] == "synthetic-sample" and out["system"]["model"] == "none (hand-written)"
    assert len(out["inputs"]["datasetSha256"]) == 64
    overall = out["overall"]
    # per item recall@5: q1 1, q2 1 (rank 3, tolerance hit), q3 1/2, q4 0 (scene mismatch), q5 0 (no list) -> 2.5/5
    # recall@50: q3 reaches 2/2 at rank 13 -> 3/5; MRR = (1 + 1/3 + 1 + 0 + 0) / 5
    assert overall["recallAt"] == {"5": 0.5, "10": 0.5, "50": 0.6}
    assert overall["microRecallAt"] == {"5": 0.5, "10": 0.5, "50": round(4 / 6, 6)}  # 6 relevant targets in total
    assert overall["mrr"] == round((1 + 1 / 3 + 1) / 5, 6) and overall["itemsWithAHit"] == 3
    assert overall["duplicatePredictions"] == 1 and out["missingPredictions"] == 1
    assert out["crossLanguage"]["fa->en"]["items"] == 1 and out["crossLanguage"]["fa->en"]["mrr"] == round(1 / 3, 6)
    assert out["crossLanguage"]["en->fa"]["recallAt"] == {"5": 0.5, "10": 0.5, "50": 1.0}
    assert set(out["byType"]) == {"exact", "cross-language", "semantic", "visual", "topic"}
    assert out["byDifficulty"]["easy"]["items"] == 2 and out["perItem"]["q5"]["predicted"] is False
    assert out["perItem"]["q2"]["firstHitRank"] == 3 and out["perItem"]["q3"]["duplicates"] == 1


def test_the_overlap_rule_scene_rule_and_one_credit_per_prediction():
    target = ("a1", 3, 10_000, 14_000)
    assert bm.matches(("a1", None, 13_999, 15_000), target, 0)              # positive overlap, no scene given
    assert not bm.matches(("a1", None, 14_000, 15_000), target, 0)          # touching only: no overlap
    assert bm.matches(("a1", None, 14_500, 15_000), target, 1_000)          # within the tolerance
    assert not bm.matches(("a1", 4, 10_000, 14_000), target, 0)             # both name a scene: must agree
    assert not bm.matches(("a2", 3, 10_000, 14_000), target, 0)             # another asset
    two = [("a1", None, 0, 100), ("a1", None, 50, 150)]
    first = bm.score_item([("a1", None, 0, 200), ("a1", None, 0, 200)], two, 0)
    # the first prediction credits ONE target (the first listed), the second credits the other: no duplicate
    assert first["found"][5] == 2 and first["duplicates"] == 0 and first["reciprocalRank"] == 1.0
    again = bm.score_item([("a1", None, 0, 40), ("a1", None, 10, 20)], two, 0)
    assert again["found"][5] == 1 and again["duplicates"] == 1  # the second only hits an already-credited target


# ---------------------------------------------------------------- speech


def test_speech_cli_reports_raw_and_normalized_wer_honest_denominators_and_paired_timestamps():
    code, out = cli("speech", "--dataset", str(SAMPLE / "speech_dataset.json"),
                    "--hypotheses", str(SAMPLE / "speech_hypotheses.json"))
    assert code == 0 and out["synthetic"] is True and out["segments"] == 4 and out["missingHypotheses"] == 1
    raw, norm = out["byForm"]["raw"]["wer"], out["byForm"]["normalized"]["wer"]
    # raw: s1 3 substitutions / 6 words (case, "the"/"a", "key?"/"key"); s2 3/4 (Arabic kaf/yeh variants);
    # s3 one insertion against an EMPTY reference; s4 missing hypothesis = 4 deletions -> micro 11/14
    assert raw["micro"] == round(11 / 14, 6) and raw["macro"] == round((3 / 6 + 3 / 4 + 1) / 3, 6)
    assert raw["macroSegments"] == 3 and raw["insertions"] == 1 and raw["deletions"] == 4
    # normalized (normalise_for_search): s1 1/6, s2 0/4 (letter variants folded), s3 +1, s4 4 -> micro 6/14
    assert norm["micro"] == round(6 / 14, 6) and norm["macro"] == round((1 / 6 + 0 + 1) / 3, 6)
    assert out["byForm"]["raw"]["emptyReferences"] == {"count": 1, "withInsertions": 1}
    assert out["byForm"]["raw"]["perSegment"]["s3"]["wer"]["rate"] is None  # undefined, not 0 and not infinite
    timing = out["timestamps"]
    # paired only s1 (1000-3000 vs 1200-2900) and s3 (7000-8000 vs 7100-7900); s2 and s4 have no paired window
    assert timing == {"pairedSegments": 2, "meanAbsStartErrorMs": 150.0, "meanAbsEndErrorMs": 100.0,
                      "maxAbsErrorMs": 200, "unpairedSegments": 2}


def test_edit_distance_and_cer_by_hand():
    budget = bm._Budget(10_000)
    assert bm.edit_distance(list("kitten"), list("sitting"), budget) == (2, 0, 1, 3)
    assert bm.edit_distance([], list("ab"), budget) == (0, 0, 2, 2)
    assert bm.edit_distance(list("ab"), [], budget) == (0, 2, 0, 2)
    assert bm.raw_form("  Key?  \n ") == "Key?" and bm.normalized_form("Key?") == "key"


def test_no_timestamp_is_paired_without_both_windows(tmp_path):
    hyp = sample("speech_hypotheses.json")
    for segment in hyp["segments"]:
        segment.pop("startMs", None), segment.pop("endMs", None)
    code, out = cli("speech", "--dataset", str(SAMPLE / "speech_dataset.json"), "--hypotheses",
                    write(tmp_path / "h.json", hyp))
    assert code == 0 and out["timestamps"]["status"] == "unavailable" and out["timestamps"]["pairedSegments"] == 0


# ---------------------------------------------------------------- system


def test_system_cli_validates_supplied_measurements_and_never_invents_the_rest():
    code, out = cli("system", "--measurements", str(SAMPLE / "system_measurements.json"))
    assert code == 0 and out["observedByThisTool"] is False and out["synthetic"] is True
    measured = out["measurements"]
    assert measured["searchLatencyMs"] == {"status": "supplied", "value": 120.5, "unit": "ms",
                                           "method": "invented for the fixture", "measuredBy": "nobody", "samples": 3}
    assert measured["peakVramMb"] == {"status": "unavailable", "unit": "MiB"}  # not supplied: never zero
    assert set(measured) == set(bm.SYSTEM_METRICS)


# ---------------------------------------------------------------- refusals and bounds


def _dataset_with(change):
    value = sample("retrieval_dataset.json")
    change(value)
    return value


@pytest.mark.parametrize("change, needle", [
    (lambda d: d["items"].append(dict(d["items"][0])), "duplicate item id"),
    (lambda d: d["items"][0]["relevant"].append(dict(d["items"][0]["relevant"][0])), "duplicate relevant target"),
    (lambda d: d["items"][0]["relevant"][0].update(startMs=5000, endMs=5000), "endMs must be greater"),
    (lambda d: d["items"][0].update(type="guess"), "type must be one of"),
    (lambda d: d["items"][0].update(queryLanguage="Farsi"), "ISO 639"),
    (lambda d: d["dataset"].update(synthetic="yes"), "synthetic must be true or false"),
    (lambda d: d["items"][0].update(extra=1), "unknown ['extra']"),
    (lambda d: d["items"][0]["relevant"][0].update(startMs=-1), "startMs must be an integer"),
])
def test_malformed_or_ambiguous_datasets_are_refused_with_exit_3(tmp_path, change, needle):
    code, out = cli("retrieval", "--dataset", write(tmp_path / "d.json", _dataset_with(change)),
                    "--predictions", str(SAMPLE / "retrieval_predictions.json"))
    assert code == 3 and out["error"] == "input_refused" and needle in out["detail"]


def test_predictions_must_belong_to_the_dataset_and_name_each_item_once(tmp_path):
    preds = sample("retrieval_predictions.json")
    preds["results"].append({"itemId": "q99", "ranked": []})
    code, out = cli("retrieval", "--dataset", str(SAMPLE / "retrieval_dataset.json"),
                    "--predictions", write(tmp_path / "p1.json", preds))
    assert code == 3 and "unknown item" in out["detail"]
    preds = sample("retrieval_predictions.json")
    preds["results"].append(dict(preds["results"][0]))
    code, out = cli("retrieval", "--dataset", str(SAMPLE / "retrieval_dataset.json"),
                    "--predictions", write(tmp_path / "p2.json", preds))
    assert code == 3 and "second result list" in out["detail"]
    preds = sample("retrieval_predictions.json")
    preds["dataset"]["version"] = "2"
    code, out = cli("retrieval", "--dataset", str(SAMPLE / "retrieval_dataset.json"),
                    "--predictions", write(tmp_path / "p3.json", preds))
    assert code == 3 and "names dataset" in out["detail"]


def test_hostile_json_and_unsafe_paths_are_refused(tmp_path):
    nan = tmp_path / "nan.json"
    nan.write_text('{"schema": NaN}', encoding="utf-8")
    dup = tmp_path / "dup.json"
    dup.write_text('{"schema": 1, "schema": 2}', encoding="utf-8")
    for bad in (nan, dup):
        code, out = cli("system", "--measurements", str(bad))
        assert code == 3 and out["error"] == "input_refused"
    code, out = cli("system", "--measurements", "https://example.invalid/m.json")
    assert code == 3 and "URL" in out["detail"]
    code, out = cli("system", "--measurements", str(tmp_path))  # a folder, not a file
    assert code == 3 and "regular file" in out["detail"]
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (bm.MAX_FILE_BYTES + 1))
    code, out = cli("system", "--measurements", str(big))
    assert code == 3 and "larger than" in out["detail"]
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this account cannot create a symbolic link")
    code, out = cli("system", "--measurements", str(link))
    assert code == 3 and "links" in out["detail"]


def test_deeply_nested_json_is_an_input_refusal_not_a_traceback(tmp_path):
    # a few hundred KB of nesting makes the json decoder raise RecursionError; the documented contract is exit 3 input_refused
    for name, content in (("arrays.json", b"[" * 100_000 + b"]" * 100_000),
                          ("objects.json", b'{"a":' * 100_000 + b"1" + b"}" * 100_000)):
        (tmp_path / name).write_bytes(content)
        code, out = cli("system", "--measurements", str(tmp_path / name))
        assert code == 3 and out["error"] == "input_refused" and "nested too deeply" in out["detail"], name


def test_a_reparse_point_or_link_is_refused_even_where_links_cannot_be_created(monkeypatch, tmp_path):
    import stat as stat_module

    real = tmp_path / "plain.json"
    real.write_text("{}", encoding="utf-8")
    info = os.lstat(real)

    class Reparse:  # what os.lstat reports for a junction/symlink-like reparse point on Windows
        st_mode, st_size, st_file_attributes = info.st_mode, info.st_size, 0x400

    class Link:
        st_mode, st_size = stat_module.S_IFLNK | 0o644, info.st_size

    for fake in (Reparse, Link):
        monkeypatch.setattr(bm.os, "lstat", lambda path, fake=fake: fake)
        with pytest.raises(bm.Refused, match="not a regular file"):
            bm.read_json(str(real))


def test_unknown_or_out_of_range_system_measurements_are_refused(tmp_path):
    for change, needle in ((lambda m: m["measurements"].update(gpuMagic={"value": 1}), "unknown measurements"),
                           (lambda m: m["measurements"]["resumeSuccessRate"].update(value=1.5), "finite number"),
                           (lambda m: m["measurements"]["searchLatencyMs"].update(unit="s"), "unit must be")):
        value = sample("system_measurements.json")
        change(value)
        code, out = cli("system", "--measurements", write(tmp_path / "m.json", value))
        assert code == 3 and needle in out["detail"]


def test_the_work_budget_stops_hostile_edit_distance_work(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "MAX_EDIT_CELLS", 50)
    code, out = bm.run(["speech", "--dataset", str(SAMPLE / "speech_dataset.json"),
                        "--hypotheses", str(SAMPLE / "speech_hypotheses.json")])
    assert code == 4 and out["error"] == "work_budget_exceeded"


def test_usage_errors_exit_2_and_the_module_starts_nothing():
    code, _ = cli("retrieval", "--dataset", "x.json")  # --predictions missing
    assert code == 2
    probe = ("import sys, qronos_media.benchmark; print(sorted(m for m in sys.modules if m.startswith(('qronos_media',"
             " 'core'))))")
    done = subprocess.run([sys.executable, "-B", "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    loaded = done.stdout.strip()
    assert done.returncode == 0 and "qronos_media.service" not in loaded and "qronos_media.store" not in loaded
    assert "core.memory_runtime" not in loaded and "core.model_registry" not in loaded
