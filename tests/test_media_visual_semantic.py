"""Semantic (cross-language) retrieval over the stored GENERATED visual text with the fixed bge-m3 (stage
"visual_semantic", schema 7). A FAKE memory sidecar answers with synthetic vectors in which texts sharing a concept share a
direction; everything else is real: the runtime-issued authority and its BGE_M3 lease, the embedder, the job runner, the
store, search, reports. Fake vectors prove contracts (what is embedded, when, provenance, merge order, scope, states),
never cross-language accuracy.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from qronos_media import reports, visual_evidence
from tests.test_media_semantic import FakeSidecar, authority

CAPTIONS = {(0, 0, 5_000): "A red bicycle leans against a brick wall.",
            (1, 0, 25_000): "An empty street at night.",
            (2, 0, 45_000): "A kitchen table with two cups."}
CONCEPTS = {"bicycle": "bicycle", "دوچرخه": "bicycle", "street": "street", "خیابان": "street", "kitchen": "kitchen"}


def concept_vector(text: str) -> list[float]:
    """Deterministic 1024-vector per concept found in the text (unrelated texts get their own direction)."""
    seed = next((concept for word, concept in CONCEPTS.items() if word in text.casefold()), text)
    values, counter = [], 0
    while len(values) < 1024:
        values += [(b - 127.5) / 127.5 for b in hashlib.sha256(f"{seed}:{counter}".encode()).digest()]
        counter += 1
    return values[:1024]


class Sidecar(FakeSidecar):
    def __init__(self) -> None:
        super().__init__(answer=self._answer)

    def _answer(self, texts):
        self.embedded += list(texts)
        return {"model": "bge-m3", "embeddings": [concept_vector(text) for text in texts]}


def build(tmp_path: Path, *, sidecar: Sidecar | None = None, owner="default"):
    from qronos_media.service import MediaService
    from tests.media.media_support import FakePermissions
    from tests.test_media_semantic import EMBEDDERS, OWNER

    tmp_path.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar if sidecar is not None else Sidecar()
    auth, state = authority(tmp_path, sidecar=sidecar, owner=OWNER if owner == "default" else owner)
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
                   "VALUES (?, ?, 'embedded', 2, 'eng', 'indexed', 1)", (f"{asset_id}:e2", asset_id))
        tx.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                   "VALUES (?, ?, 'cue', 0, 4000, 7000, 'Look at that.', 'look at that.')", (asset_id, f"{asset_id}:e2"))
        for (scene, ordinal, at), caption in CAPTIONS.items():
            write_frame(tx, asset_id, scene, ordinal, at, caption)
        tx.execute("UPDATE stages SET state = 'COMPLETED' WHERE asset_id = ? AND stage != 'visual_semantic'", (asset_id,))
    return service, asset_id, auth, state, sidecar


def write_frame(tx, asset_id, scene, ordinal, at, caption, status="described", sha=None):
    tx.execute("INSERT OR IGNORE INTO scenes(asset_id, scene_index, start_ms, end_ms) VALUES (?, ?, ?, ?)",
               (asset_id, scene, at - 4_000, at + 4_000))
    tx.execute("INSERT OR IGNORE INTO keyframes(asset_id, scene_index, ordinal, time_ms, path, bytes) VALUES (?,?,?,?,?,?)",
               (asset_id, scene, ordinal, at, f"C:/nowhere/{scene:06d}_{ordinal}.jpg", 1000))
    lines, seen = (["EXIT"], ["bicycle"]) if "bicycle" in caption else ([], [])
    tx.execute("DELETE FROM visual_evidence WHERE asset_id = ? AND scene_index = ? AND ordinal = ?", (asset_id, scene, ordinal))
    tx.execute(
        "INSERT INTO visual_evidence(asset_id, scene_index, ordinal, time_ms, frame_sha256, status, caption, "
        "visible_text_json, observations_json, reason, provenance_json, norm_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (asset_id, scene, ordinal, at, sha or hashlib.sha256(f"{scene}:{caption}".encode()).hexdigest(), status,
         caption if status == "described" else None, json.dumps(lines) if status == "described" else None,
         json.dumps(seen) if status == "described" else None, None if status == "described" else "wrong_fields",
         json.dumps({"kind": "visual_evidence", "generated": True, "verified": False, "visualVersion": 1}),
         visual_evidence.search_text(caption, lines, seen) if status == "described" else ""))


def stage_row(service, asset_id):
    return service.store.db.execute("SELECT * FROM stages WHERE asset_id = ? AND stage = 'visual_semantic'",
                                    (asset_id,)).fetchone()


def vector_rows(service, asset_id):
    return service.store.db.execute("SELECT * FROM visual_semantic WHERE asset_id = ? ORDER BY time_ms",
                                    (asset_id,)).fetchall()


def requeue(service, asset_id):
    with service.store.transaction() as tx:
        tx.execute("UPDATE stages SET state = 'QUEUED', checkpoint = NULL WHERE asset_id = ? AND stage = 'visual_semantic'",
                   (asset_id,))


# ---------------------------------------------------------------- the stage


def test_the_stored_generated_text_of_each_described_frame_is_embedded_with_the_fixed_model(tmp_path):
    service, asset_id, _, _, sidecar = build(tmp_path)
    with service.store.transaction() as tx:
        write_frame(tx, asset_id, 3, 0, 60_000, "x", status="rejected")  # a rejected frame has no text to embed
    service.index(max_seconds=60, max_stages=10)
    assert stage_row(service, asset_id)["state"] == "COMPLETED"
    stored = vector_rows(service, asset_id)
    assert [r["time_ms"] for r in stored] == [5_000, 25_000, 45_000]
    expected = [visual_evidence.embedding_text("A red bicycle leans against a brick wall.", ["EXIT"], ["bicycle"]),
                visual_evidence.embedding_text("An empty street at night.", [], []),
                visual_evidence.embedding_text("A kitchen table with two cups.", [], [])]
    assert [r["text"] for r in stored] == expected and sorted(sidecar.embedded) == sorted(expected)
    record = json.loads(stored[0]["provenance_json"])
    frame_sha = service.store.db.execute("SELECT frame_sha256 FROM visual_evidence WHERE asset_id = ? AND time_ms = 5000",
                                         (asset_id,)).fetchone()[0]
    assert record["frameSha256"] == frame_sha and record["textSha256"] == hashlib.sha256(expected[0].encode()).hexdigest()
    from qronos_media import semantic

    assert record["model"] == semantic._fixed_profile()["model"] and record["model"] in semantic.ACCEPTED_MODEL_NAMES
    assert record["dimension"] == 1024 and record["quantization"] == "F16"
    assert "never the image" in record["embeds"] and record["visualSemanticVersion"] == 1
    embeds = [c for c in sidecar.calls if c["url"].endswith("/api/embed")]
    assert embeds and all(c["url"].startswith("http://127.0.0.1:11435") for c in embeds)  # the existing CPU sidecar


def test_unchanged_texts_are_never_embedded_again_and_only_a_changed_frame_is(tmp_path):
    service, asset_id, _, _, sidecar = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    first = len(sidecar.embedded)
    requeue(service, asset_id)
    service.index(max_seconds=60, max_stages=10)
    assert stage_row(service, asset_id)["state"] == "COMPLETED" and len(sidecar.embedded) == first  # cache hits only
    with service.store.transaction() as tx:  # visual_evidence re-ran and one frame now says something else
        write_frame(tx, asset_id, 1, 0, 25_000, "A wet street after rain.", sha="d" * 64)
    assert len(vector_rows(service, asset_id)) == 2  # the replaced visual row took its vector row with it (FK)
    requeue(service, asset_id)
    service.index(max_seconds=60, max_stages=10)
    assert sidecar.embedded[first:] == [visual_evidence.embedding_text("A wet street after rain.", [], [])]
    assert {r["frame_sha256"] for r in vector_rows(service, asset_id)} >= {"d" * 64}


def test_without_the_sidecar_the_stage_is_unavailable_and_the_lexical_visual_channel_still_answers(tmp_path):
    service, asset_id, auth, _, sidecar = build(tmp_path, owner=None)  # no owned sidecar process behind the embedder
    service.index(max_seconds=60, max_stages=10)
    row = stage_row(service, asset_id)
    assert row["state"] == "QUEUED" and str(row["note"]).startswith("unavailable:")
    assert not any(e["event"] == "reserve" for e in auth.events)
    found = service.search_visual("bicycle")["visual"]
    assert found["results"] and found["results"][0]["channels"] == ["lexical"]
    assert found["semantic"]["status"] == "unavailable" and found["semantic"]["reason"] == "no_visual_vectors"
    assert not [c for c in sidecar.calls if c["url"].endswith("/api/embed")]  # no query vector without stored vectors


# ---------------------------------------------------------------- search


def test_a_query_in_another_language_reaches_the_frame_by_its_text_vector_after_the_lexical_hits(tmp_path):
    service, asset_id, _, _, sidecar = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    persian = service.search_visual("دوچرخهٔ قرمز")
    assert persian["exact"] == service.search_dialogue("دوچرخهٔ قرمز", mode="exact", limit=10)  # unchanged, first
    visual = persian["visual"]
    (hit, *rest) = visual["results"]
    assert hit["timeMs"] == 5_000 and hit["channels"] == ["semantic"] and hit["generated"] is True
    assert hit["verified"] is False and "uncalibrated" in hit["scoreKind"] and isinstance(hit["similarity"], float)
    assert "not image similarity" in visual["semantic"]["scoreNote"] and visual["semantic"]["status"] == "complete"
    assert visual["semantic"]["model"]["embeds"] == "generated visual text, never the image"
    mixed = service.search_visual("street")["visual"]["results"]
    assert mixed[0]["timeMs"] == 25_000 and mixed[0]["channels"] == ["lexical", "semantic"]  # lexical first, then merged
    assert all(r["channels"] == ["semantic"] for r in mixed[1:])


def test_an_asset_out_of_scope_is_never_offered_counted_or_queried_for(tmp_path):
    service, asset_id, _, _, sidecar = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    with service.store.transaction() as tx:
        tx.execute("UPDATE files SET error = 'out_of_scope' WHERE asset_id = ?", (asset_id,))
    calls = len([c for c in sidecar.calls if c["url"].endswith("/api/embed")])
    visual = service.search_visual("دوچرخه")["visual"]
    assert visual["results"] == [] and visual["status"] == "unavailable" and visual["reason"] == "no_visual_evidence"
    assert len([c for c in sidecar.calls if c["url"].endswith("/api/embed")]) == calls  # no query vector asked for


def test_reports_and_capabilities_say_text_vectors_not_image_similarity(tmp_path):
    service, asset_id, _, _, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    full = reports.gather(service.store.db, asset_id, "full")
    assert full["summary"]["visualFramesSemanticIndexed"] == 3
    assert "visual_semantic_text_only" in {item["code"] for item in full["limitations"]}
    not_available = {item["capability"] for item in full["notAvailable"]}
    assert "visual_semantic_text" not in not_available and "visual_embeddings" in not_available
