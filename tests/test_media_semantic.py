"""Semantic dialogue retrieval (qronos_media.semantic, stage "semantic", MediaService.search_semantic). Fixtures only.

No server or model is called: a fake HTTP transport stands in for the runtime's bge-m3 memory sidecar and returns
synthetic, deterministic 1024-dimension vectors (or the malformed answers a test needs). The governor and ledger are
the real classes behind the runtime-issued HostedModelAuthority; the Files permission and activity reading are
fakes. Dialogue rows are synthetic; no decoder runs.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
from pathlib import Path

import pytest

from core.resource_governor import ResourceGovernor
from core.resource_guard import SystemStatus
from core.resource_ownership import ResourceLedger
from qronos_media import semantic, stages as stages_module
from qronos_media.resources import BGE_M3, ResourceRefused, StandaloneCpuAuthority
from qronos_media.service import MediaService
from tests.media.media_support import FakePermissions, quiet_activity

QUIET = SystemStatus(cpu_usage_percent=5.0, ram_usage_percent=20.0, ram_used_gb=8.0, ram_total_gb=64.0)
OWNER = object()  # stands for the owned sidecar process
CUES_EN = ["Where did you leave the key?", "Under the old blue flowerpot.", "We have to leave before midnight.",
           "The train to the coast is late again.", "Nobody saw the letter except you.",
           "Then who opened the door last night?", "I heard music from the empty house.",
           "Keep the lamp burning until morning.", "The river is higher than last year.",
           "Tell your brother the boat is ready."]
CONCEPT = {"Where did you leave the key?": "key", "کلید را کجا گذاشتی؟": "key"}


def vector_for(text: str) -> list[float]:
    """Deterministic synthetic 1024-vector; texts sharing a concept share a direction (cross-language fixture)."""
    seed = CONCEPT.get(text.split(" Under")[0], text)
    values, counter = [], 0
    while len(values) < 1024:
        digest = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        values += [(b - 127.5) / 127.5 for b in digest]
        counter += 1
    return values[:1024]


class FakeSidecar:
    """A fake HTTP transport with the request_json signature; records every call."""

    def __init__(self, *, quantization: str = "F16", answer=None, fail_on: int | None = None, on_embed=None,
                 tags_error: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self.embedded: list[str] = []
        self.tags_error = tags_error  # /api/tags transport failure (before any inference request)
        self.quantization = quantization
        self.answer = answer
        self.fail_on = fail_on
        self.on_embed = on_embed
        self.loaded = True  # what the sidecar's /api/ps lists (bge-m3 resident under keep_alive)

    def __call__(self, method, url, *, timeout, json=None, owner=None, max_bytes=None):
        self.calls.append({"method": method, "url": url, "json": json, "owner": owner, "maxBytes": max_bytes,
                           "timeout": timeout})
        if url.endswith("/api/ps"):
            return {"models": ([{"name": "bge-m3:latest", "model": "bge-m3:latest"}] if self.loaded else [])
                    + [{"name": "someone-elses-model:latest"}]}
        if url.endswith("/api/tags"):
            if self.tags_error is not None:
                raise self.tags_error
            return {"models": [{"name": "bge-m3:latest", "model": "bge-m3:latest",
                                "details": {"quantization_level": self.quantization, "family": "bert"}}]}
        embeds = sum(1 for c in self.calls if c["url"].endswith("/api/embed"))
        if self.fail_on is not None and embeds == self.fail_on:
            raise OSError("simulated sidecar failure")
        if self.on_embed is not None:
            self.on_embed(embeds)
        if self.answer is not None:
            return self.answer(json["input"])
        self.embedded += list(json["input"])
        return {"model": "bge-m3", "embeddings": [vector_for(text) for text in json["input"]]}


class State:
    foreground = False
    paused = False


EMBEDDERS: dict[int, semantic.MediaEmbedder] = {}


LIFECYCLE: dict[int, list] = {}


def authority(tmp_path: Path, state: State | None = None, *, sidecar: FakeSidecar | None = None, owner=OWNER):
    """The runtime-issued authority with the host's ONE embedder as its residency proof (as build_bridge_media_host)."""
    from qronos_media.model_authority import issue_hosted_authority

    state = state or State()
    lifecycle = ["unknown"]  # a test may set "tree_exited" (a trusted contained lifecycle proof)
    embedder = semantic.MediaEmbedder(lambda: owner, transport=sidecar) if sidecar is not None else None
    auth = issue_hosted_authority(governor=ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None),
                                  ledger=ResourceLedger(), disk_root=tmp_path, foreground_busy=lambda: state.foreground,
                                  paused=lambda: state.paused, activity=quiet_activity, reserve_bytes=0,
                                  residency=embedder.residency if embedder is not None else None,
                                  sidecar_lifecycle=lambda: lifecycle[0])
    EMBEDDERS[id(auth)] = embedder
    LIFECYCLE[id(auth)] = lifecycle
    return auth, state


def prepared(tmp_path: Path, auth, sidecar: FakeSidecar | None, cues=CUES_EN, *, permissions=None):
    library = tmp_path / "library"
    library.mkdir()
    (library / "film.mkv").write_bytes(b"synthetic container stand-in")
    embedder = EMBEDDERS.get(id(auth))
    if embedder is None and sidecar is not None:  # e.g. the standalone authority
        embedder = semantic.MediaEmbedder(lambda: OWNER, transport=sidecar)
    service = MediaService(tmp_path / "media-data", tools=None, authority=auth,
                           permissions=permissions or FakePermissions(), embedder=embedder)
    service.add_root(str(library), kind="folder", max_depth=2)
    service.scan()
    indexed = service.index(max_seconds=30, max_stages=0)
    assert indexed["hashed"] == 1, json.dumps(indexed, ensure_ascii=False)
    asset_id = service.store.db.execute("SELECT asset_id FROM assets").fetchone()[0]
    with service.store.transaction() as tx:
        tx.execute("INSERT INTO subtitle_tracks(track_id, asset_id, source, stream_index, language, status, cue_count) "
                   "VALUES (?, ?, 'embedded', 2, 'eng', 'indexed', ?)", (f"{asset_id}:e2", asset_id, len(cues)))
        for seq, text in enumerate(cues):
            tx.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                       "VALUES (?, ?, 'cue', ?, ?, ?, ?, ?)",
                       (asset_id, f"{asset_id}:e2", seq, 10_000 * seq + 500, 10_000 * seq + 4_000, text, text.lower()))
        tx.execute("UPDATE stages SET state = 'COMPLETED' WHERE asset_id = ? AND stage != 'semantic'", (asset_id,))
    return service, asset_id


def windows(service, asset_id) -> list:
    return service.store.db.execute("SELECT * FROM semantic_windows WHERE asset_id = ? ORDER BY start_ms",
                                    (asset_id,)).fetchall()


def stage(service, asset_id):
    return service.store.db.execute("SELECT * FROM stages WHERE asset_id = ? AND stage = 'semantic'",
                                    (asset_id,)).fetchone()


# ------------------------------------------------------------------ the embedder boundary


def test_the_embedder_uses_only_the_fixed_sidecar_profile_through_the_owned_connection():
    # Round 32 (public interface change, documented): the atomic owner + generation seam joins; still no URL, port
    # or model parameter.
    assert list(inspect.signature(semantic.MediaEmbedder).parameters) == ["owner_check", "generation_check",
                                                                          "transport"]
    sidecar = FakeSidecar()
    vectors = semantic.MediaEmbedder(lambda: OWNER, transport=sidecar).embed(["first line", "second line"])
    tags, embed = sidecar.calls
    assert tags["method"] == "GET" and tags["url"] == "http://127.0.0.1:11435/api/tags"
    assert embed["method"] == "POST" and embed["url"] == "http://127.0.0.1:11435/api/embed"
    assert embed["json"] == {"model": "bge-m3", "input": ["first line", "second line"], "truncate": False,
                             "keep_alive": "1h", "options": {"num_gpu": 0}}
    assert all(call["owner"] is OWNER for call in sidecar.calls)  # the accepted connection must be the owned process
    assert embed["maxBytes"] is not None and embed["maxBytes"] <= 40 * 1024 * 2 + 4096
    assert len(vectors) == 2 and all(len(v) == 4096 for v in vectors)
    decoded = semantic.decode_many(vectors)
    assert all(abs(math.sqrt(float((row * row).sum())) - 1.0) < 1e-5 for row in decoded)


def _bad(kind):
    def answer(texts):
        vectors = [vector_for(t) for t in texts]
        if kind == "count":
            vectors = vectors[:-1]
        elif kind == "dimension":
            vectors[0] = vectors[0][:1023]
        elif kind == "nan":
            vectors[0][5] = float("nan")
        elif kind == "inf":
            vectors[0][5] = float("inf")
        elif kind == "bool":
            vectors[0][5] = True
        elif kind == "zero":
            vectors[0] = [0.0] * 1024
        elif kind == "other_model":
            return {"model": "nomic-embed-text", "embeddings": vectors}
        return {"model": "bge-m3", "embeddings": vectors}
    return answer


@pytest.mark.parametrize("kind", ["count", "dimension", "nan", "inf", "bool", "zero", "other_model"])
def test_a_malformed_sidecar_answer_is_rejected(kind):
    with pytest.raises(semantic.EmbeddingInvalid):
        semantic.MediaEmbedder(lambda: OWNER, transport=FakeSidecar(answer=_bad(kind))).embed(["a", "b"])


def test_a_non_f16_model_a_missing_owner_or_a_failed_request_is_refused_typed():
    with pytest.raises(ResourceRefused) as refused:
        semantic.MediaEmbedder(lambda: OWNER, transport=FakeSidecar(quantization="Q4_K_M")).embed(["a"])
    assert refused.value.reason == "model_unavailable"
    sidecar = FakeSidecar()
    with pytest.raises(ResourceRefused) as refused:
        semantic.MediaEmbedder(lambda: None, transport=sidecar).embed(["a"])
    assert refused.value.reason == "model_unavailable" and sidecar.calls == []
    with pytest.raises(ResourceRefused) as refused:
        semantic.MediaEmbedder(lambda: OWNER, transport=FakeSidecar(fail_on=1)).embed(["a"])
    assert refused.value.reason == "embedding_request_failed"
    with pytest.raises(ValueError):
        semantic.MediaEmbedder(lambda: OWNER, transport=FakeSidecar()).embed(["x" * 1201])


# ------------------------------------------------------------------ the stage


def test_the_hosted_stage_indexes_windows_with_lineage_and_frees_the_slot_only_on_unload_proof(tmp_path):
    sidecar = FakeSidecar()
    auth, _ = authority(tmp_path, sidecar=sidecar)
    service, asset_id = prepared(tmp_path, auth, sidecar)
    report = service.index(max_seconds=60, max_stages=5)
    assert f"{asset_id}/semantic" in report["completed"], report
    rows = windows(service, asset_id)
    assert rows and all(row["track_id"] == f"{asset_id}:e2" and row["source"] == "embedded" for row in rows)
    assert rows[0]["text"].startswith("Where did you leave the key?") and rows[0]["start_ms"] == 500
    reserved = [e for e in auth.events if e["event"] == "reserve"]
    assert reserved and reserved[0]["model"] == BGE_M3.name
    # The request returned, but the sidecar keeps bge-m3 loaded (keep_alive): the declared memory stays counted.
    [owned] = auth.ledger.active()
    assert owned.budget.ram_mb == 2048 and len(auth.governor.active()) == 1
    assert auth.status()["modelSlot"]["state"] == "resident"
    assert auth.reconcile() is False and len(auth.ledger.active()) == 1  # still listed by the sidecar's /api/ps
    sidecar.loaded = False  # the sidecar unloaded it itself (media never asks)
    assert auth.reconcile() is True and auth.governor.active() == () and auth.ledger.active() == ()
    assert not any(c["json"] and c["json"].get("keep_alive") in ("0", "0s", 0) for c in sidecar.calls)


def test_the_standalone_authority_never_embeds_and_other_work_carries_on(tmp_path):
    sidecar = FakeSidecar()
    service, asset_id = prepared(tmp_path, StandaloneCpuAuthority(tmp_path, activity=quiet_activity,
                                                                  reserve_bytes=0), sidecar)
    report = service.index(max_seconds=60, max_stages=5)
    assert any(item.endswith("semantic:requires_qronos_governor") for item in report["refused"]), report
    assert sidecar.calls == [] and windows(service, asset_id) == []


def test_foreground_preemption_between_batches_leaves_no_index_and_resume_embeds_only_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "EMBED_BATCH", 2)  # 5 windows -> 3 batches
    auth, state = authority(tmp_path)

    def speak(n):
        if n == 1:
            state.foreground = True  # the person speaks while the first batch is answered

    sidecar = FakeSidecar(on_embed=speak)
    auth, state = authority(tmp_path, state, sidecar=sidecar)
    service, asset_id = prepared(tmp_path, auth, sidecar)
    report = service.index(max_seconds=60, max_stages=5)
    assert report["stopped"] == "deferred" and "foreground" in report["detail"], report
    assert windows(service, asset_id) == []  # nothing searchable from a stopped run
    first = list(sidecar.embedded)
    assert stage(service, asset_id)["state"] == "QUEUED"
    assert len(auth.ledger.active()) == 1  # resident, still counted
    state.foreground = False
    report = service.index(max_seconds=60, max_stages=5)
    assert f"{asset_id}/semantic" in report["completed"], report
    later = sidecar.embedded[len(first):]
    assert len(first) == 2 and later and not set(first) & set(later), "a cached window was embedded twice"
    assert len(windows(service, asset_id)) == len({w.window_key for w in semantic.build_windows(
        service.store.db, asset_id)})
    assert len(auth.ledger.active()) == 1 and any(e["event"] == "reuse_resident" for e in auth.events)


def test_a_failed_or_invalid_request_leaves_no_completed_partial_index(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "EMBED_BATCH", 2)  # 5 windows -> 3 batches; the second one fails
    failing = FakeSidecar(fail_on=2)  # e.g. a client timeout: the server's work is not proven stopped
    auth, _ = authority(tmp_path, sidecar=failing)
    service, asset_id = prepared(tmp_path, auth, failing)
    report = service.index(max_seconds=60, max_stages=5)
    assert report["stopped"] == "deferred" and "embedding_request_failed" in report["detail"], report
    assert auth.status()["modelSlot"]["state"] == "request_unresolved" and len(auth.ledger.active()) == 1
    with pytest.raises(ResourceRefused) as refused:
        auth.reserve("media:semantic:again", stages_module.STAGES["semantic"].needs)
    assert refused.value.reason == "request_unresolved"
    assert auth.reconcile() is False  # the model is still loaded: no drain is proven
    failing.loaded = False  # /api/ps is empty: a queued request may simply not be listed yet
    assert auth.reconcile() is False and len(auth.ledger.active()) == 1
    with pytest.raises(ResourceRefused):
        auth.reserve("media:semantic:again", stages_module.STAGES["semantic"].needs)
    LIFECYCLE[id(auth)][0] = "tree_exited"  # only a contained lifecycle proof of the whole sidecar tree frees it
    assert auth.reconcile() is True and auth.ledger.active() == ()
    assert windows(service, asset_id) == [] and stage(service, asset_id)["state"] == "QUEUED"
    assert service.store.db.execute("SELECT count(*) FROM semantic_cache").fetchone()[0] == 2  # batch 1 cached only
    (tmp_path / "b").mkdir(exist_ok=True)
    bad = FakeSidecar(answer=_bad("nan"))
    auth2, _ = authority(tmp_path / "b", sidecar=bad)
    service2, asset2 = prepared(tmp_path / "b", auth2, bad)
    service2.index(max_seconds=60, max_stages=5)
    assert windows(service2, asset2) == [] and stage(service2, asset2)["state"] == "FAILED"
    assert auth2.status()["modelSlot"]["state"] == "resident"  # the request completed; the model stays loaded


def test_a_revoked_root_stops_the_work_before_anything_is_searchable(tmp_path):
    permissions = FakePermissions()
    library = tmp_path / "library"

    def revoke(n):
        if n == 1:
            permissions.refuse_roots[str(library)] = "revoked"

    sidecar = FakeSidecar(on_embed=revoke)
    auth, _ = authority(tmp_path, sidecar=sidecar)
    service, asset_id = prepared(tmp_path, auth, sidecar, permissions=permissions)
    report = service.index(max_seconds=60, max_stages=5)
    assert report["stopped"] in {"permission", "deferred"} and windows(service, asset_id) == [], report


def test_only_changed_windows_are_re_embedded_and_asr_changes_requeue_the_stage(tmp_path):
    sidecar = FakeSidecar()
    auth, _ = authority(tmp_path, sidecar=sidecar)
    service, asset_id = prepared(tmp_path, auth, sidecar)
    service.index(max_seconds=60, max_stages=5)
    before = len(sidecar.embedded)
    with service.store.transaction() as tx:  # one cue changes (as a rewritten subtitle file would)
        tx.execute("UPDATE dialogue SET text = 'The river is lower than last year.' WHERE asset_id = ? AND seq = 8",
                   (asset_id,))
    service.reindex(asset_id, "semantic")
    service.index(max_seconds=60, max_stages=5)
    changed = sidecar.embedded[before:]
    assert changed and all("lower than last year" in text for text in changed), changed
    assert any("lower than last year" in row["text"] for row in windows(service, asset_id))
    assert not any("higher than last year" in row["text"] for row in windows(service, asset_id))
    with service.store.transaction() as tx:
        stages_module._delete_asr(tx, asset_id)  # an ASR change elsewhere
    assert stage(service, asset_id)["state"] == "QUEUED" and stage(service, asset_id)["note"] == "invalidated:dialogue"


def test_deleting_a_track_or_an_asset_removes_its_vectors_from_the_index(tmp_path):
    sidecar = FakeSidecar()
    auth, _ = authority(tmp_path, sidecar=sidecar)
    service, asset_id = prepared(tmp_path, auth, sidecar)
    service.index(max_seconds=60, max_stages=5)
    assert windows(service, asset_id)
    with service.store.transaction() as tx:
        tx.execute("DELETE FROM subtitle_tracks WHERE asset_id = ?", (asset_id,))
    assert windows(service, asset_id) == []


# ------------------------------------------------------------------ the query


def test_semantic_search_returns_evidence_after_the_lexical_answer_and_crosses_languages(tmp_path):
    sidecar = FakeSidecar()
    auth, _ = authority(tmp_path, sidecar=sidecar)
    service, asset_id = prepared(tmp_path, auth, sidecar)
    service.index(max_seconds=60, max_stages=5)
    answer = service.search_semantic("کلید را کجا گذاشتی؟", limit=3)
    assert list(answer) == ["query", "exact", "semantic"]  # lexical first, semantic as its own block
    block = answer["semantic"]
    assert block["status"] == "complete" and "uncalibrated" in block["scoreNote"]
    top = block["results"][0]
    assert top["assetId"] == asset_id and top["startMs"] == 500
    assert top["dialogue"].startswith("Where did you leave the key?")  # the ORIGINAL transcript text
    assert top["crossScript"] is True and top["track"]["language"] == "eng" and top["confidence"] == "uncalibrated"
    assert top["similarity"] > block["results"][1]["similarity"]
    assert len(auth.ledger.active()) == 1  # indexing and the query share the one resident reservation


def test_the_query_is_bounded_and_refusals_keep_the_lexical_answer(tmp_path):
    sidecar = FakeSidecar()
    auth, _ = authority(tmp_path, sidecar=sidecar)
    service, asset_id = prepared(tmp_path, auth, sidecar)
    service.index(max_seconds=60, max_stages=5)
    from qronos_media.service import MediaRequestError

    with pytest.raises(MediaRequestError):
        service.search_semantic("x" * 1201)
    lease = auth.reserve("media:semantic:other", stages_module.STAGES["semantic"].needs)  # an active request holds it
    try:
        answer = service.search_semantic("key")
        assert answer["semantic"]["status"] == "unavailable" and answer["semantic"]["reason"] == "model_busy"
        assert "results" in answer["exact"]
    finally:
        auth.release(lease)
    with service.store.transaction() as tx:  # an asset whose copies all left scope is not offered
        tx.execute("UPDATE files SET error = 'out_of_scope'")
    assert service.search_semantic("key")["semantic"]["results"] == []


class FakeProcess:
    """Stands for the captured owned sidecar process (a psutil identity)."""

    def __init__(self) -> None:
        self.running = True

    def is_running(self) -> bool:
        return self.running


def test_shared_user_memory_use_never_double_accounts_and_only_exit_proof_frees_it(tmp_path):
    process = FakeProcess()
    sidecar = FakeSidecar()
    auth, _ = authority(tmp_path, sidecar=sidecar, owner=process)
    service, asset_id = prepared(tmp_path, auth, sidecar)
    service.index(max_seconds=60, max_stages=5)
    assert len(auth.ledger.active()) == 1
    # The user's own memory retrieval keeps using the same resident model (the sidecar still lists it, beside
    # an unrelated model): media's one reservation stays, and media unloads nothing.
    service.search_semantic("key")
    assert len(auth.ledger.active()) == 1 and auth.reconcile() is False
    assert all(not (c["json"] or {}).get("keep_alive") in ("0", "0s") for c in sidecar.calls)
    process.running = False  # the server process alone exiting proves nothing: a runner may outlive it
    assert auth.reconcile() is False and len(auth.ledger.active()) == 1
    LIFECYCLE[id(auth)][0] = "tree_exited"
    assert auth.reconcile() is True and auth.ledger.active() == () and auth.governor.active() == ()
    released = [e for e in auth.events if e["event"] == "released"]
    assert released and released[-1]["proof"] == "sidecar_tree_exited"


# ------------------------------------------------------------------ round 29: product composition and ownership
# Main23 F1: build_bridge_media_host always hands the stage an embedder object; with no owned sidecar
# (QRONOS_MEMORY_RUNTIME off, the default) nothing may be reserved for a model that was never loaded.
# Root F2: while any media model request is unresolved, no media model is admitted (another model included).


def product_host(tmp_path: Path, monkeypatch, owner_check, sidecar: FakeSidecar):
    """The product composition: build_bridge_media_host on the bridge's own governor and ledger. Its ONE embedder
    binds core.local_ipc.request_json at construction; that owned transport is the fake sidecar here."""
    from core import local_ipc
    from qronos_media.host import build_bridge_media_host

    monkeypatch.setenv("QRONOS_MEDIA_DATA_DIR", str(tmp_path / "media-data"))
    monkeypatch.setattr(local_ipc, "request_json", sidecar)
    governor = ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None)
    ledger = ResourceLedger()
    host = build_bridge_media_host(governor=governor, ledger=ledger, foreground_busy=lambda: False,
                                   paused=lambda: False, approvals=None, audit=None, activity=quiet_activity,
                                   embedding_owner=owner_check)
    EMBEDDERS[id(host.authority)] = host._embedder()
    return host, governor, ledger


def posts(sidecar: FakeSidecar) -> int:
    return sum(1 for c in sidecar.calls if c["method"] == "POST")


def proofs(auth) -> list:
    return [e.get("proof") for e in auth.events if e["event"] == "released"]


def test_the_embedder_signals_dispatch_only_right_before_the_inference_request():
    seen: list[int] = []
    sidecar = FakeSidecar()
    semantic.MediaEmbedder(lambda: OWNER, transport=sidecar).embed(
        ["a"], on_dispatch=lambda: seen.append(len(sidecar.calls)))
    assert seen == [1] and [c["method"] for c in sidecar.calls] == ["GET", "POST"]  # after /api/tags, before POST
    for embedder in (semantic.MediaEmbedder(lambda: None, transport=FakeSidecar()),
                     semantic.MediaEmbedder(lambda: OWNER, transport=FakeSidecar(tags_error=TimeoutError("t"))),
                     semantic.MediaEmbedder(lambda: OWNER, transport=FakeSidecar(quantization="Q4_K_M"))):
        with pytest.raises(ResourceRefused) as refused:
            embedder.embed(["a"], on_dispatch=lambda: seen.append(-1))
        assert refused.value.reason == "model_unavailable"  # a capability refusal, never an unresolved request
    assert seen == [1]
    assert semantic.MediaEmbedder(lambda: None).unavailable_reason() == "model_unavailable"
    assert semantic.MediaEmbedder(lambda: OWNER, transport=FakeSidecar()).unavailable_reason() is None


def test_product_host_without_a_sidecar_owner_refuses_index_and_query_before_reserving(tmp_path, monkeypatch):
    sidecar = FakeSidecar()
    host, governor, ledger = product_host(tmp_path, monkeypatch, lambda: None, sidecar)
    service, asset_id = prepared(tmp_path, host.authority, sidecar)
    report = service.index(max_seconds=60, max_stages=5)
    assert any(r.endswith("semantic:model_unavailable") for r in report["refused"]), report
    assert not any(c.endswith(":semantic") for c in report["completed"]), report
    row = stage(service, asset_id)
    assert row["state"] == "QUEUED" and row["note"] == "unavailable:model_unavailable"
    assert windows(service, asset_id) == []
    answer = service.search_semantic("key")
    assert answer["semantic"]["reason"] == "model_unavailable" and answer["semantic"]["results"] == []
    assert list(answer) == ["query", "exact", "semantic"]  # the lexical answer is still given
    assert not any(e["event"] == "reserve" for e in host.authority.events)  # nothing was ever reserved
    assert ledger.active() == () and governor.active() == () and sidecar.calls == []
    assert host.authority.reconcile() is True and host.authority.reconcile() is True
    assert host.authority.status()["modelSlot"] is None


def test_product_host_owner_lost_after_the_grant_frees_the_slot_no_request_was_sent(tmp_path, monkeypatch):
    owner, sidecar = [OWNER], FakeSidecar()
    host, governor, ledger = product_host(tmp_path, monkeypatch, lambda: owner[0], sidecar)
    service, asset_id = prepared(tmp_path, host.authority, sidecar)
    real_reserve = host.authority.reserve

    def reserve_then_lose(task, needs):
        lease = real_reserve(task, needs)
        if needs.model is BGE_M3:
            owner[0] = None  # the sidecar goes away right after the grant, before anything is sent
        return lease

    monkeypatch.setattr(host.authority, "reserve", reserve_then_lose)
    report = service.index(max_seconds=60, max_stages=5)
    assert any(r.endswith("semantic:model_unavailable") for r in report["refused"]), report
    assert stage(service, asset_id)["state"] == "QUEUED" and windows(service, asset_id) == []
    assert posts(sidecar) == 0 and proofs(host.authority) == ["no_request_dispatched"]
    assert ledger.active() == () and governor.active() == ()
    owner[0] = OWNER
    answer = service.search_semantic("key")  # the same for the query
    assert answer["semantic"]["reason"] == "model_unavailable" and posts(sidecar) == 0
    assert proofs(host.authority) == ["no_request_dispatched"] * 2 and ledger.active() == ()


def test_product_host_model_check_failure_before_any_request_is_not_unresolved(tmp_path, monkeypatch):
    sidecar = FakeSidecar(tags_error=TimeoutError("tags timed out"))
    host, governor, ledger = product_host(tmp_path, monkeypatch, lambda: OWNER, sidecar)
    service, asset_id = prepared(tmp_path, host.authority, sidecar)
    report = service.index(max_seconds=60, max_stages=5)
    assert any(r.endswith("semantic:model_unavailable") for r in report["refused"]), report
    assert posts(sidecar) == 0 and proofs(host.authority) == ["no_request_dispatched"]
    assert ledger.active() == () and governor.active() == () and host.authority.status()["modelSlot"] is None


def test_product_host_a_possibly_dispatched_request_stays_counted_whatever_else_is_seen(tmp_path, monkeypatch):
    owner, sidecar = [OWNER], FakeSidecar(fail_on=1)  # the first POST fails: the server may still work on it
    host, governor, ledger = product_host(tmp_path, monkeypatch, lambda: owner[0], sidecar)
    service, asset_id = prepared(tmp_path, host.authority, sidecar)
    report = service.index(max_seconds=60, max_stages=5)
    assert report["stopped"] == "deferred" and "embedding_request_failed" in report["detail"], report
    assert host.authority.status()["modelSlot"]["state"] == "request_unresolved"
    assert [o.budget.ram_mb for o in ledger.active()] == [2048] and windows(service, asset_id) == []
    sent = posts(sidecar)
    sidecar.loaded = False  # an empty /api/ps proves no drain
    assert host.authority.reconcile() is False and host.authority.reconcile() is False
    owner[0] = None  # nor does losing the owner (the product supplies no whole-tree proof: kept until restart)
    assert host.authority.reconcile() is False and len(ledger.active()) == 1
    owner[0] = OWNER
    assert service.search_semantic("key")["semantic"]["reason"] == "request_unresolved"
    with pytest.raises(ResourceRefused) as refused:
        host.authority.reserve("media:semantic:again", stages_module.STAGES["semantic"].needs)
    assert refused.value.reason == "request_unresolved" and posts(sidecar) == sent and len(ledger.active()) == 1


def test_product_host_resident_reuse_stays_counted_and_a_fully_cached_run_holds_nothing(tmp_path, monkeypatch):
    owner, sidecar = [OWNER], FakeSidecar()
    host, governor, ledger = product_host(tmp_path, monkeypatch, lambda: owner[0], sidecar)
    service, asset_id = prepared(tmp_path, host.authority, sidecar)
    service.index(max_seconds=60, max_stages=5)
    assert stage(service, asset_id)["state"] == "COMPLETED" and windows(service, asset_id)
    assert host.authority.status()["modelSlot"]["state"] == "resident" and len(ledger.active()) == 1
    service.search_semantic(CUES_EN[0])
    assert any(e["event"] == "reuse_resident" for e in host.authority.events) and len(ledger.active()) == 1
    assert host.authority.status()["modelSlot"]["state"] == "resident"
    owner[0] = None  # no owner now: new work is refused before reserving, the resident model stays counted
    assert service.search_semantic("key")["semantic"]["reason"] == "model_unavailable"
    assert host.authority.reconcile() is False and len(ledger.active()) == 1
    owner[0] = OWNER
    sidecar.loaded = False  # the owned sidecar's own /api/ps no longer lists bge-m3: freed (media never asked)
    assert host.authority.reconcile() is True and ledger.active() == ()
    sent = posts(sidecar)
    with service.store.transaction() as tx:  # re-run with every window cached: no request, nothing held after
        tx.execute("UPDATE stages SET state = 'QUEUED' WHERE stage = 'semantic' AND asset_id = ?", (asset_id,))
    service.index(max_seconds=60, max_stages=5)
    assert stage(service, asset_id)["state"] == "COMPLETED" and posts(sidecar) == sent
    assert proofs(host.authority)[-1] == "no_request_dispatched"
    assert ledger.active() == () and governor.active() == ()


def test_an_unresolved_request_refuses_every_media_model_until_the_whole_tree_proof(tmp_path):
    from qronos_media.resources import WHISPER_LARGE_V3_TURBO_Q5_0, StageNeeds

    failing = FakeSidecar(fail_on=1)
    auth, _ = authority(tmp_path, sidecar=failing)
    service, asset_id = prepared(tmp_path, auth, failing)
    service.index(max_seconds=60, max_stages=5)
    assert auth.status()["modelSlot"]["state"] == "request_unresolved"
    failing.loaded = False
    with pytest.raises(ResourceRefused) as refused:
        auth.reserve("media:asr:other", StageNeeds("asr", model=WHISPER_LARGE_V3_TURBO_Q5_0))
    assert refused.value.reason == "request_unresolved"
    assert [o.workload for o in auth.ledger.active()] == ["media-model:" + BGE_M3.name]  # nothing else admitted
    LIFECYCLE[id(auth)][0] = "tree_exited"  # the trusted whole-tree proof resolves it; then Whisper is admitted
    whisper = auth.reserve("media:asr:other", StageNeeds("asr", model=WHISPER_LARGE_V3_TURBO_Q5_0))
    assert [o.workload for o in auth.ledger.active()] == ["media-model:" + WHISPER_LARGE_V3_TURBO_Q5_0.name]
    auth.release(whisper)
    assert auth.ledger.active() == ()


def test_a_proved_resident_model_coexists_with_another_declared_model_both_accounted(tmp_path):
    from qronos_media.resources import WHISPER_LARGE_V3_TURBO_Q5_0, StageNeeds

    sidecar = FakeSidecar()
    auth, _ = authority(tmp_path, sidecar=sidecar)
    service, asset_id = prepared(tmp_path, auth, sidecar)
    service.index(max_seconds=60, max_stages=5)
    assert auth.status()["modelSlot"]["state"] == "resident"
    whisper = auth.reserve("media:asr:other", StageNeeds("asr", model=WHISPER_LARGE_V3_TURBO_Q5_0))
    assert sorted(o.workload for o in auth.ledger.active()) == sorted(
        ["media-model:" + BGE_M3.name, "media-model:" + WHISPER_LARGE_V3_TURBO_Q5_0.name])
    auth.release(whisper)
    assert [o.workload for o in auth.ledger.active()] == ["media-model:" + BGE_M3.name]  # bge-m3 still resident
