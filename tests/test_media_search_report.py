"""Reports of one multimodal search result (qronos_media.search_report): a server-held snapshot of the exact answer,
rechecked against the current scope, Files gate and stored rows, rendered with the asset reports' own primitives.

Synthetic index of tests/test_media_multimodal (fake bge-m3 sidecar, synthetic rows), FakePermissions for the gate,
the REAL product route over SyncHost. JSON/HTML/CSV are rendered here; PDF needs the pinned fpdf2/HarfBuzz stack and
is rendered separately with the packaging interpreter. Nothing searches again, no model runs, no media file is read.
"""
from __future__ import annotations

import json

import pytest

from qronos_media import search_report
from qronos_media.product import MediaProduct
from qronos_media.service import MediaRequestError
from tests.test_media_multimodal import SyncHost, build


def held(service, dialogue="father died", visual="kitchen", **options):
    block = service.search_multimodal(dialogue, visual, **options)["multimodal"]
    assert block["status"] != "unavailable"
    return block, block["resultId"]


def ask(product, op, **data):
    answers = []
    product.handle({"requestId": "q1", "op": op, **data}, reply=answers.append)
    return answers[0]


def test_the_report_is_the_held_answer_with_both_channels_alignment_and_honest_labels(tmp_path):
    service, ids, sidecar = build(tmp_path)
    block, result_id = held(service)
    embeds = sidecar.embeds()
    report = service.search_report(result_id)
    assert sidecar.embeds() == embeds  # nothing searched again, no model call
    assert report["schema"] == "qronos.media.search-report/1" and report["kind"] == "search"
    assert report["query"] == {"dialogue": "father died", "visual": "kitchen", "windowMs": 15_000,
                               "dialogueMatch": "exact", "visualMatch": "lexical"}
    assert report["recheck"] == {"kept": 1, "outOfScope": 0, "permission": 0, "changed": 0}
    (hit,) = report["results"]
    sha = service.store.db.execute("SELECT sha256 FROM assets WHERE asset_id = ?", (ids["A"],)).fetchone()[0]
    assert hit["assetSha256"] == sha and hit["dialogue"]["text"] == "My father died last winter."
    assert hit["dialogue"]["channels"] == ["exact"] and hit["alignment"]["relation"] == "during"
    (frame,) = hit["frames"]
    assert frame["generated"] is True and frame["verified"] is False and frame["channels"] == ["lexical"]
    assert set(frame["provenance"]) <= {"kind", "generated", "verified", "visualVersion", "promptVersion",
                                        "promptSha256", "frameSha256", "model", "profile"}
    text = " ".join(report["limitations"])
    assert "جست‌وجو دوباره اجرا نشد" in text and "تولیدشده و تأییدنشده" in text and "OCR" in text
    assert "احتمال یا اطمینان کالیبره‌شده نیست" in text
    dumped = json.dumps(report, ensure_ascii=False)
    assert str(tmp_path) not in dumped and str(tmp_path).replace("\\", "/") not in dumped and "nowhere" not in dumped


def test_the_recheck_removes_out_of_scope_refused_and_changed_hits_and_says_so(tmp_path):
    service, ids, _ = build(tmp_path)
    _, scoped = held(service)
    _, refused = held(service)
    _, changed = held(service)
    with service.store.transaction() as tx:  # one frame was described again: its SHA-256 is no longer the held one
        tx.execute("UPDATE visual_evidence SET frame_sha256 = ? WHERE asset_id = ? AND time_ms = 62000", ("e" * 64, ids["A"]))
    assert service.search_report(changed)["recheck"] == {"kept": 0, "outOfScope": 0, "permission": 0, "changed": 1}
    root = service.store.db.execute("SELECT path FROM roots").fetchone()[0]
    service.permissions.refuse_roots[root] = "revoked"
    gated = service.search_report(refused)
    assert gated["recheck"]["permission"] == 1 and gated["results"] == []
    del service.permissions.refuse_roots[root]
    with service.store.transaction() as tx:
        tx.execute("UPDATE files SET error = 'out_of_scope' WHERE asset_id = ?", (ids["A"],))
    report = service.search_report(scoped)
    assert report["recheck"]["outOfScope"] == 1 and report["results"] == []
    assert any("دوباره بررسی شد" in item for item in report["limitations"]) and ids["A"] not in json.dumps(report)


def test_partial_and_semantic_labels_travel_into_the_report(tmp_path):
    service, _, _ = build(tmp_path)
    service.embedder = None  # the semantic channels could not run: the search was partial
    _, result_id = held(service, dialogue_match="semantic", visual_match="semantic")
    report = service.search_report(result_id)
    assert report["status"] == "partial" and "dialogue_semantic_unavailable" in report["notes"]
    text = " ".join(report["limitations"])
    assert "این جست‌وجو ناقص بود" in text and "کالیبره‌نشدهٔ متن" in text


def test_an_unknown_expired_or_evicted_result_is_refused_with_no_side_effect(tmp_path):
    service, _, _ = build(tmp_path)
    exports = service.layout.exports
    for bad in ("x" * 20, "short", "../../etc/passwd-xxxxxxxx", 7):
        with pytest.raises(MediaRequestError, match="result_unavailable"):
            service.search_report(bad)
    _, first = held(service)
    for _ in range(8):  # eight newer answers evict the oldest of the bounded snapshots
        held(service)
    with pytest.raises(MediaRequestError, match="result_unavailable"):
        service.search_report(first)
    _, latest = held(service)
    clock = service._monotonic
    service._monotonic = lambda: clock() + 1801.0  # past the snapshot lifetime
    with pytest.raises(MediaRequestError, match="result_unavailable"):
        service.generate_search_report(latest, fmt="json")
    assert not exports.exists() or not any(exports.iterdir())
    assert not [c for c in service.permissions.calls if c[0] == "request_export"]


def test_json_html_and_csv_are_escaped_bounded_and_written_only_to_qronos_exports(tmp_path):
    service, ids, _ = build(tmp_path)
    with service.store.transaction() as tx:
        tx.execute("UPDATE assets SET title = ? WHERE asset_id = ?", ('<script>alert(1)</script>', ids["A"]))
        tx.execute("UPDATE dialogue SET text = '=HYPERLINK(\"x\") father died' WHERE asset_id = ? AND start_ms = 60000",
                   (ids["A"],))
        tx.execute("UPDATE dialogue SET norm_text = 'hyperlink x father died' WHERE asset_id = ? AND start_ms = 60000",
                   (ids["A"],))
    _, result_id = held(service)
    page = search_report.to_html(service.search_report(result_id))
    assert "<script>" not in page and "&lt;script&gt;" in page and "default-src 'none'" in page
    assert "توصیف تولیدشده، تأییدنشده" in page
    sheet = search_report.to_csv(service.search_report(result_id))
    assert sheet.startswith("\ufeff") and "frame_text_generated_unverified" in sheet and "'=HYPERLINK" in sheet
    written = service.generate_search_report(result_id, fmt="json")
    from pathlib import Path

    path = Path(written["path"])
    assert path.parent == service.layout.exports and path.name.startswith("search-") and path.suffix == ".json"
    assert json.loads(path.read_bytes())["results"][0]["dialogue"]["startMs"] == 60_000


def test_the_product_route_names_only_a_held_result_and_export_needs_the_same_approval(tmp_path):
    service, ids, _ = build(tmp_path)
    product = MediaProduct(SyncHost(service), emit=lambda *a: None)
    found = ask(product, "search", mode="multimodal", dialogue="father died", visual="kitchen")
    result_id = found["multimodal"]["resultId"]
    forged = ask(product, "search.report.preview", resultId=result_id,
                 results=[{"assetId": "a_" + "9" * 32, "title": "FORGED"}], query={"dialogue": "forged"})
    assert forged["ok"] and "FORGED" not in json.dumps(forged) and forged["preview"]["query"]["dialogue"] == "father died"
    assert product.host.lanes[-1] == ("call", "search.report.preview")
    assert ask(product, "search.report.preview")["code"] == "bad_request"
    assert ask(product, "search.report.preview", resultId="y" * 24)["code"] == "result_unavailable"
    assert ask(product, "search.report.generate", resultId=result_id, format="docx")["code"] == "bad_request"
    destination = tmp_path / "out" / "hits.csv"
    destination.parent.mkdir()
    service.permissions.ask = True
    pending = ask(product, "search.report.export", resultId=result_id, destination=str(destination), format="csv")
    assert pending["status"] == "approval_required" and not destination.exists()
    done = ask(product, "decide", approvalId=pending["approvalId"], approved=True)
    assert done["ok"] and done["status"] == "written" and done["kind"] == "search"
    assert destination.read_bytes().startswith("\ufeff".encode("utf-8")) and done["sha256"]
    assert ask(product, "decide", approvalId=pending["approvalId"], approved=True)["code"] == "stale_approval"
    service.permissions.ask = False
    again = ask(product, "search.report.export", resultId=result_id, destination=str(destination), format="csv")
    assert again["ok"] is False and again["code"] == "refused"  # never overwrites
    service.permissions.refuse_exports = True
    other = tmp_path / "out" / "denied.json"
    denied = ask(product, "search.report.export", resultId=result_id, destination=str(other), format="json")
    assert denied["code"] == "denied" and not other.exists()
