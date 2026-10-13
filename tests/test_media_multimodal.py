"""Multimodal search: a dialogue constraint AND a visual constraint fused by time in the same current asset.

Synthetic index of two assets (real store, scope filter, search, bge-m3 stages, product route). A FAKE memory sidecar
answers with concept vectors (texts sharing a concept share a direction) and a FAKE orchestrator stands for the
governed Fast brain, so these tests prove the fusion contract (both constraints, same asset, bounded window, scope,
states, provenance), never retrieval or split quality.

    asset A: "Put the kettle on, please."  2-5 s     frame 62 s  "A small kitchen with a kettle on the stove."
             "My father died last winter." 60-64 s   frame 150 s "An empty street at night."
             "The train is late again."    200-204 s frame 301 s "A kitchen table with two cups."
             "Nobody came to the funeral." 300-303 s
    asset B: "My father died in that house." 10-13 s frame 40 s  "A garden with roses."
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from qronos_media import multimodal, visual_evidence
from qronos_media.brain_gateway import FastBrainGateway
from qronos_media.text import normalise_for_search
from tests.test_media_semantic import FakeSidecar, authority

CONCEPTS = {"father died": "death", "funeral": "death", "مرگ": "death", "kitchen": "kitchen", "آشپزخانه": "kitchen",
            "street": "street", "garden": "garden"}
DIALOGUE = {"A": [(2_000, 5_000, "Put the kettle on, please."), (60_000, 64_000, "My father died last winter."),
                  (200_000, 204_000, "The train is late again."), (300_000, 303_000, "Nobody came to the funeral.")],
            "B": [(10_000, 13_000, "My father died in that house.")]}
FRAMES = {"A": [(62_000, "A small kitchen with a kettle on the stove."), (150_000, "An empty street at night."),
                (301_000, "A kitchen table with two cups.")],
          "B": [(40_000, "A garden with roses.")]}


def concept_vector(text: str) -> list[float]:
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

    def embeds(self) -> int:
        return len([c for c in self.calls if c["url"].endswith("/api/embed")])


class Orchestrator:
    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls: list = []

    def answer_chat_only(self, messages) -> str:
        self.calls.append(list(messages))
        return self.answer


def build(tmp_path: Path, *, orchestrator: Orchestrator | None = None, busy=lambda: False):
    from qronos_media.service import MediaService
    from tests.media.media_support import FakePermissions
    from tests.test_media_semantic import EMBEDDERS

    tmp_path.mkdir(parents=True, exist_ok=True)
    sidecar = Sidecar()
    auth, _ = authority(tmp_path, sidecar=sidecar)
    library = tmp_path / "library"
    library.mkdir()
    (library / "film_a.mkv").write_bytes(b"synthetic container stand-in A")
    (library / "film_b.mkv").write_bytes(b"synthetic container stand-in B")
    brain = None if orchestrator is None else FastBrainGateway(lambda: (orchestrator, busy),
                                                               identity=lambda: ("fake-fast:Q5", "fake"))
    service = MediaService(tmp_path / "media-data", tools=None, authority=auth, permissions=FakePermissions(),
                           embedder=EMBEDDERS[id(auth)], brain=brain)
    service.add_root(str(library), kind="folder", max_depth=2)
    service.scan()
    service.index(max_seconds=30, max_stages=0)
    ids = {Path(row[0]).stem[-1].upper(): row[1] for row in service.store.db.execute(
        "SELECT path, asset_id FROM files ORDER BY path")}
    with service.store.transaction() as tx:
        for name, asset_id in ids.items():
            tx.execute("INSERT INTO subtitle_tracks(track_id, asset_id, source, stream_index, language, status, "
                       "cue_count) VALUES (?, ?, 'embedded', 2, 'eng', 'indexed', ?)",
                       (f"{asset_id}:e2", asset_id, len(DIALOGUE[name])))
            for seq, (start, end, text) in enumerate(DIALOGUE[name]):
                tx.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                           "VALUES (?, ?, 'cue', ?, ?, ?, ?, ?)",
                           (asset_id, f"{asset_id}:e2", seq, start, end, text, normalise_for_search(text)))
            for scene, (at, caption) in enumerate(FRAMES[name]):
                frame(tx, asset_id, scene, at, caption)
            tx.execute("UPDATE stages SET state = 'COMPLETED' WHERE asset_id = ? AND stage NOT IN "
                       "('semantic', 'visual_semantic')", (asset_id,))
    service.index(max_seconds=60, max_stages=20)  # the real bge-m3 stages over the stored dialogue and frame text
    states = {row[0] for row in service.store.db.execute(
        "SELECT state FROM stages WHERE stage IN ('semantic', 'visual_semantic')")}
    assert states == {"COMPLETED"}
    return service, ids, sidecar


def frame(tx, asset_id, scene, at, caption):
    tx.execute("INSERT INTO scenes(asset_id, scene_index, start_ms, end_ms) VALUES (?, ?, ?, ?)",
               (asset_id, scene, at - 4_000, at + 4_000))
    tx.execute("INSERT INTO keyframes(asset_id, scene_index, ordinal, time_ms, path, bytes) VALUES (?,?,?,?,?,?)",
               (asset_id, scene, 0, at, f"C:/nowhere/{scene:06d}_0.jpg", 1000))
    tx.execute(
        "INSERT INTO visual_evidence(asset_id, scene_index, ordinal, time_ms, frame_sha256, status, caption, "
        "visible_text_json, observations_json, reason, provenance_json, norm_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (asset_id, scene, 0, at, hashlib.sha256(f"{asset_id}:{caption}".encode()).hexdigest(), "described", caption,
         "[]", "[]", None, json.dumps({"kind": "visual_evidence", "generated": True, "verified": False,
                                       "visualVersion": 1}),
         visual_evidence.search_text(caption, [], [])))


def search(service, dialogue, visual, **options):
    return service.search_multimodal(dialogue, visual, **options)["multimodal"]


# ---------------------------------------------------------------- the fusion contract


def test_a_result_needs_both_constraints_in_the_same_asset_within_the_window(tmp_path):
    service, ids, sidecar = build(tmp_path)
    embeds = sidecar.embeds()
    block = search(service, "father died", "kitchen")
    # B also has "father died" but no kitchen frame; A's other kitchen frame (301 s) is far from the line (60-64 s)
    (hit,) = block["results"]
    assert block["status"] == "complete" and block["outcome"] == "matched" and hit["assetId"] == ids["A"]
    assert hit["dialogue"]["text"] == "My father died last winter." and hit["dialogue"]["channels"] == ["exact"]
    assert hit["dialogue"]["track"] == {"trackId": f"{ids['A']}:e2", "source": "embedded", "language": "eng"}
    (shown,) = hit["frames"]
    assert shown["timeMs"] == 62_000 and shown["caption"].startswith("A small kitchen") and shown["channels"] == ["lexical"]
    assert shown["generated"] is True and shown["verified"] is False and len(shown["frameSha256"]) == 64
    assert hit["alignment"]["relation"] == "during" and hit["alignment"]["gapMs"] == 0
    assert hit["alignment"]["windowMs"] == 15_000 and (hit["startMs"], hit["endMs"]) == (60_000, 64_000)
    assert hit["matched"] == {"dialogue": ["exact"], "visual": ["lexical"]}
    assert block["counts"]["dialogueAssets"] == 2 and block["counts"]["visualAssets"] == 1
    assert "not a probability" in block["rankNote"] and block["absent"]["ocr"] == "not built"
    text = json.dumps(block)
    assert str(tmp_path) not in text and str(tmp_path).replace("\\", "/") not in text and '"paths"' not in text
    assert sidecar.embeds() == embeds  # lexical constraints only: no model call


def test_the_same_asset_at_another_time_is_not_a_result_until_the_window_reaches_it(tmp_path):
    service, ids, _ = build(tmp_path)
    # "The train is late again." 200-204 s; the street frame is at 150 s: 50,000 ms before the line starts
    far = search(service, "train is late", "street")
    assert far["results"] == [] and far["outcome"] == "same_asset_outside_window"
    assert far["counts"]["sameAssetOutsideWindow"] == 1
    assert search(service, "train is late", "street", window_ms=49_999)["results"] == []
    (hit,) = search(service, "train is late", "street", window_ms=50_000)["results"]
    assert hit["assetId"] == ids["A"] and hit["alignment"] == {
        "relation": "before", "gapMs": 50_000, "windowMs": 50_000,
        "rule": "frame time within [dialogue start - window, dialogue end + window], same asset"}
    assert (hit["startMs"], hit["endMs"]) == (150_000, 204_000)


def test_one_missing_modality_or_different_assets_never_yield_a_result(tmp_path):
    service, _, _ = build(tmp_path)
    cases = {("father died", "bicycle"): "only_dialogue",        # no frame says bicycle
             ("never said anywhere", "kitchen"): "only_visual",   # no line says it
             ("kettle on", "roses"): "different_assets",          # the line is in A, the roses frame in B
             ("never said anywhere", "bicycle"): "neither"}
    for (dialogue, visual), expected in cases.items():
        block = search(service, dialogue, visual, window_ms=multimodal.MAX_WINDOW_MS)
        assert block["results"] == [] and block["outcome"] == expected, (dialogue, visual)
        assert block["status"] == "complete"


def test_semantic_channels_add_cross_language_matches_above_the_floor_and_say_so(tmp_path):
    service, ids, sidecar = build(tmp_path)
    embeds, texts = sidecar.embeds(), len(sidecar.embedded)
    block = search(service, "مرگ پدر", "آشپزخانه", dialogue_match="semantic", visual_match="semantic")
    assert sidecar.embeds() == embeds + 1 and sidecar.embedded[texts:] == ["مرگ پدر", "آشپزخانه"]  # ONE governed batch
    # death windows: A 60-64 s, A 300-303 s, B 10-13 s; kitchen frames: A 62 s, A 301 s. The street and garden frames
    # and the other lines are below the cosine floor: no "everything matches" in a small library.
    pairs = {(hit["assetId"], hit["dialogue"]["startMs"], hit["frames"][0]["timeMs"]) for hit in block["results"]}
    assert pairs == {(ids["A"], 60_000, 62_000), (ids["A"], 300_000, 301_000)}
    for hit in block["results"]:
        assert hit["matched"] == {"dialogue": ["semantic"], "visual": ["semantic"]}
        assert hit["dialogue"]["scoreKind"] == "cosine_uncalibrated" and hit["dialogue"]["similarity"] >= 0.5
        assert "uncalibrated" in hit["frames"][0]["scoreKind"]
    for channel in ("dialogueChannel", "visualChannel"):
        semantic_block = block[channel]["semantic"]
        assert semantic_block["status"] == "complete" and semantic_block["minCosine"] == 0.5
        assert "never the image" in semantic_block["note"]
    assert block["visualChannel"]["semantic"]["matched"] == 2 and block["dialogueChannel"]["semantic"]["matched"] == 3
    # an exact line and the semantic window around it are ONE result with both channels; the funeral window (semantic
    # only) pairs with the other kitchen frame
    mixed = search(service, "father died", "kitchen", dialogue_match="semantic")["results"]
    assert [(hit["dialogue"]["startMs"], hit["dialogue"]["channels"], hit["frames"][0]["timeMs"]) for hit in mixed] == [
        (60_000, ["exact", "semantic"], 62_000), (300_000, ["semantic"], 301_000)]


def test_a_stale_frame_vector_is_not_a_semantic_visual_match(tmp_path):
    service, ids, _ = build(tmp_path)
    with service.store.transaction() as tx:  # the vector row still names the frame's OLD SHA-256
        tx.execute("UPDATE visual_semantic SET frame_sha256 = ? WHERE asset_id = ? AND time_ms = 62000",
                   ("e" * 64, ids["A"]))
    block = search(service, "مرگ پدر", "آشپزخانه", dialogue_match="semantic", visual_match="semantic")
    assert [(hit["dialogue"]["startMs"], hit["frames"][0]["timeMs"]) for hit in block["results"]] == [(300_000, 301_000)]


def test_assets_out_of_scope_are_never_counted_queried_or_embedded(tmp_path):
    service, ids, sidecar = build(tmp_path)
    with service.store.transaction() as tx:
        tx.execute("UPDATE files SET error = 'out_of_scope' WHERE asset_id = ?", (ids["A"],))
    block = search(service, "father died", "kitchen")
    assert block["results"] == [] and block["outcome"] == "only_dialogue"  # B's line remains, A's kitchen is gone
    assert block["scope"] == {"dialogueLines": 1, "describedFrames": 1} and block["counts"]["dialogueAssets"] == 1
    assert ids["A"] not in json.dumps(block)
    with service.store.transaction() as tx:
        tx.execute("UPDATE files SET error = 'out_of_scope'")
    embeds = sidecar.embeds()
    gone = search(service, "مرگ", "آشپزخانه", dialogue_match="semantic", visual_match="semantic")
    assert gone["status"] == "unavailable" and gone["reason"] == "no_visual_evidence" and gone["results"] == []
    assert sidecar.embeds() == embeds  # nothing in scope: no model admission at all


def test_without_the_model_the_lexical_fusion_still_answers_and_says_what_is_missing(tmp_path):
    service, ids, sidecar = build(tmp_path)
    service.embedder = None  # e.g. the memory sidecar is off now
    block = search(service, "father died", "kitchen", dialogue_match="semantic", visual_match="semantic")
    (hit,) = block["results"]
    assert hit["assetId"] == ids["A"] and hit["matched"] == {"dialogue": ["exact"], "visual": ["lexical"]}
    assert block["status"] == "partial"
    assert set(block["notes"]) >= {"dialogue_semantic_unavailable", "visual_semantic_unavailable"}
    for channel in ("dialogueChannel", "visualChannel"):
        assert block[channel]["semantic"]["status"] == "unavailable" and block[channel]["semantic"]["reason"]


def test_a_stopped_budget_is_partial_and_never_invents_a_pair(tmp_path, monkeypatch):
    from qronos_media.search import BudgetExhausted

    service, _, _ = build(tmp_path)

    def out_of_time(*args, **kwargs):
        raise BudgetExhausted("time_budget")

    monkeypatch.setattr(visual_evidence, "visual_candidates", out_of_time)
    block = search(service, "father died", "kitchen", visual_match="semantic")
    assert block["status"] == "partial" and block["stoppedBy"] == "time_budget"
    assert block["results"] == []  # the dialogue was never gathered: no pair can be claimed


def common_phrase_library(tmp_path, b_lines):
    """B: many lines sharing every trigram of the phrase (inserted first); then A: the phrase at 500 s next to a
    kitchen frame at 501 s."""
    service, ids, _ = build(tmp_path)
    with service.store.transaction() as tx:
        for i in range(b_lines):
            text = "grandmother sang softly today"
            tx.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                       "VALUES (?, ?, 'cue', ?, ?, ?, ?, ?)", (ids["B"], f"{ids['B']}:e2", 100 + i, 100_000 + i * 2_000,
                                                              101_000 + i * 2_000, text, normalise_for_search(text)))
        tx.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                   "VALUES (?, ?, 'cue', 10, 500000, 503000, 'grandmother sang softly', 'grandmother sang softly')",
                   (ids["A"], f"{ids['A']}:e2"))
        frame(tx, ids["A"], 10, 501_000, "A kitchen with a radio.")
    return service, ids


@pytest.mark.parametrize("b_lines", [500, 2_100])  # past the 400-candidate cut / past the 2,000-row trigram cut
def test_fuzzy_dialogue_finds_the_paired_moment_behind_common_term_lines_of_another_asset(tmp_path, b_lines):
    service, ids = common_phrase_library(tmp_path, b_lines)
    for match in ("exact", "fuzzy"):
        block = search(service, "grandmother sang softly", "kitchen", dialogue_match=match)
        assert [(hit["assetId"], hit["dialogue"]["startMs"], hit["frames"][0]["timeMs"]) for hit in block["results"]] \
            == [(ids["A"], 500_000, 501_000)], match
        assert block["status"] == "complete" and block["outcome"] == "matched" and block["notes"] == [], match
    assert block["results"][0]["dialogue"]["channels"] == ["fuzzy"]
    # the per-asset look really examines A: its candidates are A's rows, not the first rows of the whole index
    alone = service.search_dialogue("grandmother sang softly", mode="fuzzy", limit=100, asset_id=ids["A"])
    assert [r["startMs"] for r in alone["results"]] == [500_000] and alone["status"] == "complete"
    overall = service.search_dialogue("grandmother sang softly", mode="fuzzy", limit=100)
    # the scope-wide look is cut and says which limit cut it (the 400-candidate cut was silent before)
    assert overall["status"] == "partial" and ("candidates_cut" if b_lines == 500 else "common_terms") in overall["notes"]


def test_out_of_scope_common_term_lines_never_use_up_the_fuzzy_candidates_of_the_current_scope(tmp_path):
    service, ids = common_phrase_library(tmp_path, 2_100)
    with service.store.transaction() as tx:
        tx.execute("UPDATE files SET error = 'out_of_scope' WHERE asset_id = ?", (ids["B"],))
    overall = service.search_dialogue("grandmother sang softly", mode="fuzzy", limit=100)
    assert [r["assetId"] for r in overall["results"]] == [ids["A"]] and overall["status"] == "complete"
    assert overall["notes"] == []  # B's 2,100 rows were filtered before any trigram or candidate limit
    block = search(service, "grandmother sang softly", "kitchen", dialogue_match="fuzzy")
    assert block["status"] == "complete" and block["outcome"] == "matched" and ids["B"] not in json.dumps(block)


def test_a_fuzzy_look_still_cut_inside_the_asset_is_partial_never_a_complete_answer(tmp_path, monkeypatch):
    from qronos_media import search as search_module

    service, ids = common_phrase_library(tmp_path, 2_100)
    with service.store.transaction() as tx:  # A itself now has more matching rows than the (lowered) trigram limit
        for i in range(8):
            tx.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                       "VALUES (?, ?, 'cue', ?, ?, ?, 'grandmother sang softly today', 'grandmother sang softly today')",
                       (ids["A"], f"{ids['A']}:e2", 20 + i, 700_000 + i * 2_000, 701_000 + i * 2_000))
    monkeypatch.setattr(search_module, "ROWS_PER_TRIGRAM", 4)
    block = search(service, "grandmother sang softly", "kitchen", dialogue_match="fuzzy")
    assert block["status"] == "partial"
    assert "dialogue_candidates_cut" in block["notes"] and "dialogue:common_terms" in block["notes"]
    exact = search(service, "grandmother sang softly", "kitchen")  # the exact route is not affected by fuzzy limits
    assert exact["status"] == "complete" and [h["dialogue"]["startMs"] for h in exact["results"]] == [500_000]


@pytest.mark.parametrize("fields, needle", [
    ({"dialogue": "x" * 301, "visual": "kitchen"}, "dialogue must be"),
    ({"dialogue": "father", "visual": " ".join(["w"] * 17)}, "more than 16 words"),
    ({"dialogue": "!!!", "visual": "kitchen"}, "no searchable words"),
    ({"dialogue": "father", "visual": "kitchen", "window_ms": 120_001}, "windowMs"),
    ({"dialogue": "father", "visual": "kitchen", "dialogue_match": "image"}, "dialogueMatch"),
    ({"dialogue": "father", "visual": "kitchen", "visual_match": "pixels"}, "visualMatch"),
])
def test_malformed_or_overbounded_constraints_are_refused_before_anything_is_read(tmp_path, fields, needle):
    from qronos_media.service import MediaRequestError

    service, _, sidecar = build(tmp_path)
    embeds = sidecar.embeds()
    args = dict(fields)
    with pytest.raises(MediaRequestError, match=needle):
        service.search_multimodal(args.pop("dialogue"), args.pop("visual"), **args)
    assert sidecar.embeds() == embeds


# ---------------------------------------------------------------- a natural combined request


def test_labelled_parts_split_without_any_model_and_nothing_is_searched(tmp_path):
    service, _, _ = build(tmp_path)  # no Fast brain in this process
    answer = service.interpret_multimodal("dialogue: father died; visual: kitchen")
    assert answer["interpretation"] == {"status": "proposed", "dialogue": "father died", "visual": "kitchen",
                                        "method": "labels", "generated": False, "verified": False,
                                        "note": "taken from your own labels; confirm or edit the two parts, then search"}
    persian = service.interpret_multimodal("گفتگو: مرگ پدر / تصویر: آشپزخانه")["interpretation"]
    assert (persian["dialogue"], persian["visual"]) == ("مرگ پدر", "آشپزخانه")
    assert "multimodal" not in answer and "results" not in json.dumps(answer)
    plain = service.interpret_multimodal("a kitchen scene where they talk about the father's death")["interpretation"]
    assert plain["status"] == "unavailable" and plain["reason"] == "model_unavailable"


def test_a_fast_brain_split_is_a_labelled_unverified_proposal_made_of_the_request_words(tmp_path):
    request = "a kitchen scene where they talk about the father's death"
    good = Orchestrator('Here: {"dialogue": "the father\'s death", "visual": "kitchen"}')
    service, _, _ = build(tmp_path / "good", orchestrator=good)
    proposal = service.interpret_multimodal(request)["interpretation"]
    assert proposal["status"] == "proposed" and proposal["method"] == "fast_brain"
    assert (proposal["dialogue"], proposal["visual"]) == ("the father's death", "kitchen")
    assert proposal["generated"] is True and proposal["verified"] is False and "unverified" in proposal["note"]
    assert proposal["provenance"]["promptVersion"] == multimodal.SPLIT_PROMPT_VERSION
    assert proposal["provenance"]["model"] == "fake-fast:Q5" and len(good.calls) == 1
    assert "<<<REQUEST" in good.calls[0][-1].content and good.calls[0][0].content == multimodal.SPLIT_SYSTEM
    invented = Orchestrator('{"dialogue": "funeral", "visual": "kitchen"}')  # "funeral" is not in the request
    service, _, _ = build(tmp_path / "invented", orchestrator=invented)
    assert service.interpret_multimodal(request)["interpretation"] == {
        "status": "ambiguous", "reason": "not_from_request", "note": "the request could not be split safely; type both parts"}
    one_part = Orchestrator('{"dialogue": "the father\'s death", "visual": ""}')
    service, _, _ = build(tmp_path / "one", orchestrator=one_part)
    assert service.interpret_multimodal(request)["interpretation"]["reason"] == "ambiguous"
    waiting = Orchestrator('{"dialogue": "death", "visual": "kitchen"}')
    service, _, _ = build(tmp_path / "busy", orchestrator=waiting, busy=lambda: True)  # the person's own turn first
    assert service.interpret_multimodal(request)["interpretation"]["reason"] == "model_busy" and waiting.calls == []


# ---------------------------------------------------------------- the product route


class SyncHost:
    """Runs a product request at once; records which host lane it used (read-only query thread or writer)."""

    on_index_report = None

    def __init__(self, service) -> None:
        self.service = service
        self.lanes: list[tuple[str, str]] = []

    def query(self, op, fn, reply):
        self.lanes.append(("query", op))
        reply(fn(self.service))
        return {"accepted": True}

    def call(self, op, fn, reply):
        self.lanes.append(("call", op))
        reply(fn(self.service))
        return {"accepted": True}

    def status(self):
        return {}


def ask(product, **data):
    answers: list = []
    product.handle({"requestId": "r1", "op": "search", **data}, reply=answers.append)
    return answers[0]


def test_the_product_route_types_multimodal_and_keeps_the_other_modes_unchanged(tmp_path):
    from qronos_media.product import SEARCH_MODES, MediaProduct, _search, capabilities

    service, ids, _ = build(tmp_path)
    host = SyncHost(service)
    product = MediaProduct(host, emit=lambda *args: None)
    assert SEARCH_MODES[-1] == "multimodal" and SEARCH_MODES[:5] == ("exact", "fuzzy", "semantic", "topic", "visual")
    found = ask(product, mode="multimodal", dialogue="father died", visual="kitchen", windowMs=15_000)
    assert found["ok"] and found["multimodal"]["results"][0]["assetId"] == ids["A"] and host.lanes[-1] == ("query", "search")
    split = ask(product, mode="multimodal", query="dialogue: father died; visual: kitchen")
    assert split["ok"] and split["interpretation"]["status"] == "proposed" and "multimodal" not in split
    for data, code in (({"dialogue": "father died"}, "bad_request"),                       # one constraint only
                       ({"dialogue": "a", "visual": "b", "windowMs": 10**9}, "bad_request"),
                       ({"dialogue": "a", "visual": "b", "visualMatch": "image"}, "bad_request"),
                       ({"dialogue": "a", "visual": "b", "query": "c"}, "bad_request"),
                       ({"dialogue": "!!!", "visual": "kitchen"}, "refused"),
                       ({}, "bad_request")):
        refused = ask(product, mode="multimodal", **data)
        assert refused["ok"] is False and refused["code"] == code, data
    # the existing modes answer exactly as before (dialogue first, unchanged shapes)
    assert _search(service, "father died", "exact", 5, None) == {
        "ok": True, "results": service.search_dialogue("father died", mode="exact", limit=5, language=None)}
    visual = _search(service, "kitchen", "visual", 5, None)
    assert visual["exact"] == service.search_dialogue("kitchen", mode="exact", limit=5) and "multimodal" not in visual
    statuses = {item["capability"]: item["status"] for item in capabilities()["capabilities"]}
    assert statuses["multimodal_search"] == "implemented_unverified" and statuses["ocr"] == "pending"
    assert statuses["speaker_turns"] == "pending" and statuses["visual_embeddings"] == "decision_pending"
