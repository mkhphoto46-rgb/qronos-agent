"""M4 topic retrieval (qronos_media.topics): segments from stored bge-m3 vectors, evidence-backed topic search,
selective pseudo-relevance expansion and the bounded rerank, through the real stage machinery and service.

Synthetic only: a fake sidecar answers deterministic concept vectors (two topics, plus a vague word), no model or
server runs, nothing is decoded. The vectors encode the fixture's topics, so the tests prove plumbing, evidence and
invalidation, never retrieval quality.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from qronos_media import reports, stages as stages_module, topics
from qronos_media.intents import MediaSearchIntent, parse_media_intent, spoken_answer
from qronos_media.service import MediaRequestError
from tests.test_media_semantic import FakeSidecar, authority

BETRAYAL = ["You lied to me for three years.", "I trusted you more than my own brother.",
            "She sold our plans to the other company.", "How can I ever believe a word you say?",
            "He signed the papers behind my back.", "You were supposed to be on my side."]
TRAVEL = ["The train leaves at six from platform two.", "Pack the maps and the warm coats.",
          "We cross the border before sunrise.", "The road through the mountains is closed.",
          "Buy two tickets for the night bus.", "The ship docks in Lisbon on Friday."]
CUES = BETRAYAL + TRAVEL
CONCEPT_OF = {**{text: "betrayal" for text in BETRAYAL}, **{text: "travel" for text in TRAVEL}}
QUERIES = {"دعوا درباره خیانت و پنهان‌کاری": {"betrayal": 1.0},
           "a long trip by train and bus": {"travel": 1.0},
           "secrets": {"betrayal": 0.35, "noise": 1.0}}  # vague: weak first pass, expansion expected


def _direction(name: str) -> np.ndarray:
    values, counter = [], 0
    while len(values) < 1024:
        values += list(hashlib.sha256(f"{name}:{counter}".encode()).digest())
        counter += 1
    vector = np.array(values[:1024], dtype="float64") - 127.5
    return vector / np.linalg.norm(vector)


def concept_vector(text: str) -> list[float]:
    """Queries by table; a window is the mean of its cues' concepts plus a little text-specific noise."""
    if text in QUERIES:
        mix = sum(weight * _direction(name) for name, weight in QUERIES[text].items())
    else:
        found = [CONCEPT_OF[cue] for cue in CUES if cue in text]
        mix = sum(_direction(name) for name in found) if found else _direction("other")
        mix = mix / np.linalg.norm(mix) + 0.25 * _direction("noise:" + text)
    return list(mix / np.linalg.norm(mix))


class TopicSidecar(FakeSidecar):
    def __init__(self) -> None:
        super().__init__(answer=lambda inputs: {"model": "bge-m3", "embeddings": [concept_vector(t) for t in inputs]})


def build(tmp_path: Path, cues=CUES):
    """A service whose one asset has an indexed track; every stage done except semantic and topics."""
    from qronos_media.service import MediaService
    from tests.media.media_support import FakePermissions
    from tests.test_media_semantic import EMBEDDERS

    sidecar = TopicSidecar()
    auth, _ = authority(tmp_path, sidecar=sidecar)
    library = tmp_path / "library"
    library.mkdir()
    (library / "film.mkv").write_bytes(b"synthetic container stand-in")
    service = MediaService(tmp_path / "media-data", tools=None, authority=auth, permissions=FakePermissions(),
                           embedder=EMBEDDERS[id(auth)])
    service.add_root(str(library), kind="folder", max_depth=2)
    service.scan()
    service.index(max_seconds=30, max_stages=0)
    asset_id = service.store.db.execute("SELECT asset_id FROM assets").fetchone()[0]
    with service.store.transaction() as tx:
        tx.execute("INSERT INTO subtitle_tracks(track_id, asset_id, source, stream_index, language, status, cue_count) "
                   "VALUES (?, ?, 'embedded', 2, 'eng', 'indexed', ?)", (f"{asset_id}:e2", asset_id, len(cues)))
        for seq, text in enumerate(cues):
            tx.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                       "VALUES (?, ?, 'cue', ?, ?, ?, ?, ?)",
                       (asset_id, f"{asset_id}:e2", seq, 10_000 * seq + 500, 10_000 * seq + 4_000, text, text.lower()))
        tx.execute("UPDATE stages SET state = 'COMPLETED' WHERE asset_id = ? "
                   "AND stage NOT IN ('semantic', 'topics', 'topic_labels')", (asset_id,))
    return service, asset_id, sidecar


def stage_row(service, asset_id, name="topics"):
    return service.store.db.execute("SELECT * FROM stages WHERE asset_id = ? AND stage = ?",
                                    (asset_id, name)).fetchone()


def segments(service, asset_id):
    return service.store.db.execute("SELECT * FROM topic_segments WHERE asset_id = ? ORDER BY start_ms",
                                    (asset_id,)).fetchall()


def embeds(sidecar) -> int:
    return sum(1 for call in sidecar.calls if call["url"].endswith("/api/embed"))


# ---------------------------------------------------------------- the stage


def test_the_topics_stage_groups_windows_by_meaning_from_stored_vectors_with_lineage(tmp_path):
    service, asset_id, sidecar = build(tmp_path)
    report = service.index(max_seconds=60, max_stages=1)  # semantic only
    assert f"{asset_id}/semantic" in report["completed"]
    before = embeds(sidecar)
    report = service.index(max_seconds=60, max_stages=5)
    assert f"{asset_id}/topics" in report["completed"] and embeds(sidecar) == before  # no model call at all
    rows = segments(service, asset_id)
    assert len(rows) >= 2
    known = {row["window_key"] for row in service.store.db.execute(
        "SELECT window_key FROM semantic_windows WHERE asset_id = ?", (asset_id,))}
    for row in rows:
        keys = json.loads(row["window_keys"])
        assert keys and set(keys) <= known and len(np.frombuffer(row["vector"], dtype="<f4")) == 1024
        record = json.loads(row["provenance_json"])
        assert record["method"] == topics.METHOD and record["labels"] is None and record["topicsVersion"] == 1
    first_texts = " ".join(r["text"] for r in service.store.db.execute(
        "SELECT text FROM semantic_windows WHERE window_key IN (%s)" % ",".join("?" * len(json.loads(rows[0]["window_keys"]))),
        json.loads(rows[0]["window_keys"])))
    assert BETRAYAL[0] in first_texts and TRAVEL[-1] not in first_texts  # the betrayal run is not the travel run


def test_dialogue_changes_requeue_topics_and_no_dialogue_skips_and_clears_them(tmp_path):
    service, asset_id, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=5)
    assert segments(service, asset_id)
    with service.store.transaction() as tx:
        stages_module.requeue_semantic(tx, asset_id)  # what an ASR change does
    assert stage_row(service, asset_id)["state"] == "QUEUED"
    assert stage_row(service, asset_id)["note"] == "invalidated:dialogue"
    with service.store.transaction() as tx:  # the dialogue goes away entirely
        tx.execute("DELETE FROM dialogue WHERE asset_id = ?", (asset_id,))
    service.index(max_seconds=60, max_stages=5)
    assert stage_row(service, asset_id, "semantic")["note"] == "skipped:no_dialogue"
    assert stage_row(service, asset_id)["note"] == "skipped:no_semantic_windows"
    assert segments(service, asset_id) == []  # nothing searchable outlives its dialogue


def test_deleting_the_track_removes_its_segments(tmp_path):
    service, asset_id, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=5)
    with service.store.transaction() as tx:
        tx.execute("DELETE FROM subtitle_tracks WHERE asset_id = ?", (asset_id,))
    assert segments(service, asset_id) == []


# ---------------------------------------------------------------- search


def _indexed_windows(service, asset_id) -> set[str]:
    return {row["text"] for row in service.store.db.execute(
        "SELECT text FROM semantic_windows WHERE asset_id = ?", (asset_id,))}


def test_topic_search_is_lexical_first_then_evidence_backed_and_crosses_languages(tmp_path):
    service, asset_id, sidecar = build(tmp_path)
    service.index(max_seconds=60, max_stages=5)
    calls = embeds(sidecar)
    answer = service.search_topics("دعوا درباره خیانت و پنهان‌کاری", limit=3)
    assert list(answer) == ["query", "exact", "topic"] and embeds(sidecar) == calls + 1  # ONE query vector
    assert answer["exact"] == service.search_dialogue("دعوا درباره خیانت و پنهان‌کاری", mode="exact", limit=3)
    block = answer["topic"]
    assert block["status"] == "complete" and "uncalibrated" in block["scoreNote"] and block["queryScript"] == "arabic"
    top = block["results"][0]
    assert top["assetId"] == asset_id and top["scoreKind"] == "rank_uncalibrated"
    assert set(top["components"]) == {"segment", "evidence", "terms", "expansion"}
    assert top["components"]["terms"] == 0  # Persian words never overlap English lines: no lexical credit
    texts = [line["text"] for line in top["evidence"]]
    assert texts and all(any(cue in text for cue in BETRAYAL) for text in texts)
    assert all(line["script"] == "latin_or_other" for line in top["evidence"])  # English lines for a Persian query
    indexed = _indexed_windows(service, asset_id)
    for hit in block["results"]:
        assert hit["evidence"] and all(line["text"] in indexed for line in hit["evidence"])  # never fabricated
        assert hit["startMs"] <= hit["evidence"][0]["startMs"] <= hit["endMs"]
    travel = service.search_topics("a long trip by train and bus", limit=1)["topic"]["results"][0]
    assert any(cue in travel["evidence"][0]["text"] for cue in TRAVEL)


def test_expansion_is_selective_evidence_backed_and_only_a_hint(tmp_path):
    service, asset_id, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=5)
    strong = service.search_topics("دعوا درباره خیانت و پنهان‌کاری")["topic"]
    assert strong["expansion"] == {"applied": False}  # a clear, long query is not expanded
    vague = service.search_topics("secrets")["topic"]
    expansion = vague["expansion"]
    assert expansion["applied"] is True and expansion["method"] == "pseudo_relevance_feedback"
    assert expansion["trigger"] in {"weak_first_pass", "short_query"} and expansion["note"].endswith("not facts")
    source_text = " ".join(_indexed_windows(service, asset_id)).casefold()
    assert expansion["hints"] and all(hint in source_text for hint in expansion["hints"])  # words from real lines
    assert all(hint not in {h["segmentKey"] for h in vague["results"]} for hint in expansion["hints"])
    assert vague["results"] and all(hit["evidence"] for hit in vague["results"])
    assert any(hit["components"]["expansion"] > 0 for hit in vague["results"])
    assert len(vague["results"]) <= topics.MAX_RERANK


def test_refusals_and_bounds_keep_the_lexical_answer(tmp_path):
    service, asset_id, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=5)
    with pytest.raises(MediaRequestError):
        service.search_topics("x" * 1201)
    lease = service.authority.reserve("media:semantic:other", stages_module.STAGES["semantic"].needs)
    try:
        busy = service.search_topics("secrets")
        assert busy["topic"]["status"] == "unavailable" and busy["topic"]["reason"] == "model_busy"
        assert "results" in busy["exact"]
    finally:
        service.authority.release(lease)
    service.embedder = None
    off = service.search_topics("secrets")["topic"]
    assert off["status"] == "unavailable" and off["results"] == []


def test_an_out_of_scope_asset_is_never_offered(tmp_path):
    service, asset_id, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=5)
    with service.store.transaction() as tx:
        tx.execute("UPDATE files SET error = 'out_of_scope'")
    assert service.search_topics("secrets")["topic"]["results"] == []


# ---------------------------------------------------------------- reports, product, voice


def test_reports_carry_topic_segments_from_actual_indexed_lines(tmp_path):
    service, asset_id, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=5)
    full = reports.gather(service.store.db, asset_id, "full")
    assert full["summary"]["topicSegments"] == len(segments(service, asset_id)) >= 2
    indexed = _indexed_windows(service, asset_id)
    for segment in full["topicSegments"]:
        assert segment["label"] is None and segment["lines"]
        assert all(line["text"] in indexed for line in segment["lines"])
    assert "topic_segments_unlabelled" in {item["code"] for item in full["limitations"]}
    assert "topic_retrieval" not in {item["capability"] for item in full["notAvailable"]}
    assert "topics" in {item["capability"] for item in full["notAvailable"]}  # labels stay deferred
    assert len(reports.gather(service.store.db, asset_id, "short")["topicSegments"]) <= 10
    assert "topicSegments" not in reports.gather(service.store.db, asset_id, "transcript")
    page = reports.to_html(full)
    assert "بخش‌های موضوعی" in page and "<script" not in page
    assert reports.preview(full)["topicSegments"]
    pdf = reports.to_pdf(full)  # the segments are drawn too (pinned font, shaped), from the same report
    assert pdf.startswith(b"%PDF-")
    out = Path(__import__("os").environ.get("QRONOS_R46_RENDER", ""))
    if out.name:
        out.write_bytes(pdf)  # evidence only: the reviewer can look at the rendered page


def test_topic_caps_are_stated_truthfully_in_json_html_and_pdf(tmp_path, monkeypatch):
    """Main42 R1: a report capped below the asset's segment count says so, and the PDF tells "not drawn here" (the
    report's JSON has it) apart from "not in this report" (no export of it has it)."""
    import pypdf

    service, asset_id, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=5)
    total = len(segments(service, asset_id))
    assert total >= 2
    monkeypatch.setitem(reports.REPORT_TOPIC_SEGMENTS, "full", 1)
    full = reports.gather(service.store.db, asset_id, "full")
    summary = full["summary"]
    assert (summary["topicSegments"], summary["topicSegmentsListed"], summary["topicSegmentsOmitted"],
            summary["topicSegmentsCap"]) == (total, 1, total - 1, 1)
    assert len(full["topicSegments"]) == 1 and json.loads(reports.render(full, "json"))["summary"] == summary
    omitted = f"{total - 1} بخش دیگر از {total} بخش این فایل در این گزارش نیست (سقف 1 بخش برای گزارش کامل)"
    assert omitted in reports.to_html(full)

    drawn: list[str] = []
    original = reports._Pdf.paragraph

    def recording(self, text, size=10):
        # What the PDF draws as paragraphs. pypdf cannot extract these mixed Persian/number lines reliably (neither
        # plain nor layout mode); the rendered page shows them whole (evidence: r1-rendered-omission-page.pdf).
        drawn.append(text)
        return original(self, text, size)

    monkeypatch.setattr(reports._Pdf, "paragraph", recording)

    def pdf_text(report):
        drawn.clear()
        assert pypdf.PdfReader(__import__("io").BytesIO(reports.to_pdf(report))).pages  # a real PDF was produced
        return "\n".join(drawn)

    text = pdf_text(full)
    assert omitted in text and "هیچ خروجی این گزارش آن‌ها را ندارد" in text and "JSON همه را دارد" not in text
    monkeypatch.setitem(reports.REPORT_TOPIC_SEGMENTS, "full", 200)
    monkeypatch.setattr(reports, "MAX_PDF_TOPIC_CHARS", 10)  # every listed segment is over the PDF text budget
    listed = reports.gather(service.store.db, asset_id, "full")
    assert listed["summary"]["topicSegmentsOmitted"] == 0
    text = pdf_text(listed)
    assert (f"{total} بخش فهرست‌شدهٔ این گزارش به‌خاطر سقف 10 نویسهٔ متن در این PDF کشیده نشده است؛ "
            "خروجی JSON همین گزارش آن‌ها را دارد.") in text  # not drawn here, but in this report's JSON
    assert "در این گزارش نیست" not in text and "در این گزارش نیست" not in reports.to_html(listed)
    transcript = reports.gather(service.store.db, asset_id, "transcript")["summary"]
    assert (transcript["topicSegmentsListed"], transcript["topicSegmentsOmitted"]) == (0, total)


def test_the_product_route_and_the_voice_intent_reach_topic_search(tmp_path):
    from qronos_media.product import SEARCH_MODES, _search

    service, asset_id, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=5)
    assert "topic" in SEARCH_MODES
    answer = _search(service, "دعوا درباره خیانت و پنهان‌کاری", "topic", 5, None)
    assert answer["ok"] and answer["topic"]["results"]
    intent = parse_media_intent("در فیلم‌ها صحنه‌ای پیدا کن که موضوعش «دعوا درباره خیانت و پنهان‌کاری» است")
    assert intent == MediaSearchIntent("دعوا درباره خیانت و پنهان‌کاری", "topic")
    assert parse_media_intent('search my videos for "what about the key"').mode == "exact"  # cue inside the quote
    spoken = spoken_answer(intent, answer)
    assert "بخش هم‌موضوع" in spoken and "کالیبره نیست" in spoken
