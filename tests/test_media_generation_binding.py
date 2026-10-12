"""Round 32: runtime -> media composition of the memory sidecar's per-generation lifecycle capability.

The product host is built with ``embedding_generation`` (the runtime's atomic ``owned_generation`` pair). Fakes only:
a capability object with ``state()`` stands for one ``SidecarLifecycle`` generation; the owned transport is the fake
sidecar from test_media_semantic. No server, model or process.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.resource_governor import ResourceGovernor
from core.resource_ownership import ResourceLedger
from qronos_media import semantic, stages as stages_module
from qronos_media.resources import BGE_M3, ResourceRefused
from tests.media.media_support import quiet_activity
from tests.test_media_semantic import CUES_EN, EMBEDDERS, QUIET, FakeSidecar, prepared, stage, windows


class Generation:
    """Stands for one SidecarLifecycle: immutable identity, a whole-tree state the test sets."""

    def __init__(self, number: int) -> None:
        self.generation = number
        self.current = "running"

    def state(self) -> str:
        return self.current


class Owner:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def host_with(tmp_path: Path, monkeypatch, pair: list, sidecar: FakeSidecar):
    from core import local_ipc
    from qronos_media.host import build_bridge_media_host

    monkeypatch.setenv("QRONOS_MEDIA_DATA_DIR", str(tmp_path / "media-data"))
    monkeypatch.setattr(local_ipc, "request_json", sidecar)
    governor, ledger = ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: None), ResourceLedger()
    host = build_bridge_media_host(governor=governor, ledger=ledger, foreground_busy=lambda: False,
                                   paused=lambda: False, approvals=None, audit=None, activity=quiet_activity,
                                   embedding_generation=lambda: pair[0])
    EMBEDDERS[id(host.authority)] = host._embedder()
    return host, governor, ledger


def posts(sidecar) -> int:
    return sum(1 for c in sidecar.calls if c["method"] == "POST")


def tags(sidecar) -> int:
    return sum(1 for c in sidecar.calls if c["url"].endswith("/api/tags"))


def test_no_generation_means_unavailable_before_any_reservation(tmp_path, monkeypatch):
    sidecar = FakeSidecar()
    host, governor, ledger = host_with(tmp_path, monkeypatch, [None], sidecar)
    service, asset_id = prepared(tmp_path, host.authority, sidecar)
    report = service.index(max_seconds=60, max_stages=5)
    assert any(r.endswith("semantic:model_unavailable") for r in report["refused"]), report
    assert service.search_semantic("key")["semantic"]["reason"] == "model_unavailable"
    assert not any(e["event"] == "reserve" for e in host.authority.events) and sidecar.calls == []
    assert ledger.active() == () and governor.active() == ()
    for broken in ((Owner(1),), (None, Generation(1)), (Owner(1), object()), "pair"):  # malformed pairs
        assert semantic.MediaEmbedder(generation_check=lambda b=broken: b).unavailable_reason() == "model_unavailable"


def test_the_slot_is_bound_to_the_admitted_generation_and_only_its_proof_frees_it(tmp_path, monkeypatch):
    gen1, gen2 = Generation(1), Generation(2)
    pair = [(Owner(101), gen1)]
    sidecar = FakeSidecar()
    host, governor, ledger = host_with(tmp_path, monkeypatch, pair, sidecar)
    service, asset_id = prepared(tmp_path, host.authority, sidecar)
    service.index(max_seconds=60, max_stages=5)
    assert stage(service, asset_id)["state"] == "COMPLETED" and windows(service, asset_id)
    slot = host.authority._slot
    assert slot.state == "resident" and slot.capability is gen1
    # A later generation is current and its own model list is empty: that says nothing about generation 1.
    pair[0] = (Owner(202), gen2)
    sidecar.loaded = False
    gen2.current = "tree_exited"  # even a LATER generation's whole-tree proof never frees the earlier slot
    assert host.authority.reconcile() is False and len(ledger.active()) == 1
    gen1.current = "tree_exited"  # THAT generation's proof frees it
    assert host.authority.reconcile() is True and ledger.active() == () and governor.active() == ()
    assert [e.get("proof") for e in host.authority.events if e["event"] == "released"][-1] == "sidecar_tree_exited"


def test_an_unresolved_request_keeps_its_captured_generation_across_a_swap(tmp_path, monkeypatch):
    gen1, gen2 = Generation(1), Generation(2)
    pair = [(Owner(101), gen1)]
    sidecar = FakeSidecar(fail_on=1)
    host, governor, ledger = host_with(tmp_path, monkeypatch, pair, sidecar)
    service, asset_id = prepared(tmp_path, host.authority, sidecar)
    service.index(max_seconds=60, max_stages=5)
    slot = host.authority._slot
    assert slot.state == "request_unresolved" and slot.capability is gen1
    pair[0] = (Owner(202), gen2)
    gen2.current = "tree_exited"
    sidecar.loaded = False
    assert host.authority.reconcile() is False and [o.budget.ram_mb for o in ledger.active()] == [2048]
    with pytest.raises(ResourceRefused) as refused:  # Root F2: no media model while it is unresolved
        host.authority.reserve("media:asr:x", stages_module.STAGES["asr"].needs)
    assert refused.value.reason == "request_unresolved"
    gen1.current = "tree_exited"
    assert host.authority.reconcile() is True and ledger.active() == ()


def test_a_generation_swap_between_admission_and_request_sends_nothing_and_holds_nothing(tmp_path, monkeypatch):
    gen1, gen2 = Generation(1), Generation(2)
    pair = [(Owner(101), gen1)]
    sidecar = FakeSidecar()
    host, governor, ledger = host_with(tmp_path, monkeypatch, pair, sidecar)
    service, asset_id = prepared(tmp_path, host.authority, sidecar)
    real_reserve = host.authority.reserve

    def reserve_then_swap(task, needs):
        lease = real_reserve(task, needs)
        if needs.model is BGE_M3:
            pair[0] = (Owner(202), gen2)  # the sidecar restarted right after admission
        return lease

    monkeypatch.setattr(host.authority, "reserve", reserve_then_swap)
    report = service.index(max_seconds=60, max_stages=5)
    assert report["stopped"] == "deferred" and "generation_changed" in report["detail"], report
    assert posts(sidecar) == 0 and sidecar.calls == []  # not even the model check went to the new server
    assert ledger.active() == () and governor.active() == () and windows(service, asset_id) == []
    answer = service.search_semantic("key")  # the query binds the swapped generation consistently: it may run
    assert answer["semantic"]["status"] in ("complete", "partial", "unavailable")


def test_a_resident_slot_of_another_generation_is_never_reused_by_a_new_one(tmp_path, monkeypatch):
    gen1, gen2 = Generation(1), Generation(2)
    pair = [(Owner(101), gen1)]
    sidecar = FakeSidecar()
    host, governor, ledger = host_with(tmp_path, monkeypatch, pair, sidecar)
    service, asset_id = prepared(tmp_path, host.authority, sidecar)
    service.index(max_seconds=60, max_stages=5)
    assert host.authority._slot.capability is gen1
    sent = posts(sidecar)
    pair[0] = (Owner(202), gen2)
    answer = service.search_semantic("key")
    assert answer["semantic"]["reason"] == "generation_changed" and posts(sidecar) == sent
    slot = host.authority._slot
    assert slot.state == "resident" and slot.capability is gen1 and len(ledger.active()) == 1


def test_the_f16_check_is_repeated_for_each_bound_generation(tmp_path, monkeypatch):
    gen1, gen2 = Generation(1), Generation(2)
    pair = [(Owner(101), gen1)]
    sidecar = FakeSidecar()
    embedder = semantic.MediaEmbedder(generation_check=lambda: pair[0], transport=sidecar)
    embedder.embed(["a"])
    embedder.embed(["b"])
    assert tags(sidecar) == 1  # once for generation 1
    pair[0] = (Owner(202), gen2)
    sidecar.quantization = "Q4_K_M"  # the new generation's server does not serve F16
    with pytest.raises(ResourceRefused) as refused:
        embedder.embed(["c"])
    assert refused.value.reason == "model_unavailable" and tags(sidecar) == 2
    bound = semantic.SidecarBinding(Owner(101), gen1)
    with pytest.raises(ResourceRefused) as refused:  # a binding of generation 1 cannot talk to generation 2
        embedder.embed(["d"], binding=bound)
    assert refused.value.reason == "generation_changed"
    assert embedder.residency(gen1) == "unknown"  # another generation's model list is not this one's


def test_the_bridge_reads_the_atomic_pair_or_nothing():
    from core import runtime_bridge

    pair = (Owner(5), Generation(3))

    class Sidecar:
        def __init__(self, answer):
            self.answer = answer

        def owned_generation(self):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    def bridge(sidecar):
        memory = type("Memory", (), {"sidecar": sidecar})()
        return type("Bridge", (), {"orchestrator": type("Orchestrator", (), {"memory_runtime": memory})()})()

    read = runtime_bridge.QronosRuntime._memory_sidecar_generation
    assert read(bridge(Sidecar(pair))) is pair
    assert read(bridge(Sidecar(None))) is None
    assert read(bridge(Sidecar(RuntimeError("unreadable")))) is None
    assert read(bridge(None)) is None
    assert read(type("Bridge", (), {"orchestrator": None})()) is None
