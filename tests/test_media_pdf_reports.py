"""M7 reports: one schema from the index, rendered as JSON, HTML, CSV and a shaped Persian PDF.

Synthetic indexed stores only: no media file is read, no decoder or model runs (``run_bounded`` is replaced by a
tripwire), and the PDF is parsed back with pypdf. Untrusted subtitle strings (markup, formulas, bidi overrides,
newlines) and long transcripts are part of the fixture.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import pypdf
import pytest

from qronos_media import reports
from qronos_media import stages as stages_module
from qronos_media import tools as tools_module
from qronos_media.service import MediaRequestError, MediaService
from qronos_media.stages import STAGES
from qronos_media.store import MediaStore
from tests.media.media_support import FakePermissions, make_service

ASSET = "a_" + "3f" * 16
SHA = "ab" * 32
UNTRUSTED = ["<script>alert(1)</script>", '=HYPERLINK("http://x")', "‮evil‬ reversed", "line\nbreak"]
PERSIAN = "سلام، این یک جملهٔ آزمایشی فارسی است با کلمهٔ English و عدد 42."


def make_store(root: Path, *, asr: bool = True, sidecar: bool = True, lines: int = 30) -> tuple[MediaStore, Path]:
    """One asset with a sidecar subtitle track and an ASR track (with its recorded provenance)."""
    media = root / "library" / "فیلم آزمایشی.mkv"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"not a real video")
    store = MediaStore(root / "media-index.sqlite3")
    state = os.stat(media)
    with store.transaction() as db:
        db.execute("INSERT INTO roots(path, kind, approval_action_id, approval_decision, approved_at, max_depth) "
                   "VALUES (?, 'folder', 'x', 'allow', 1.0, 4)", (str(media.parent),))
        db.execute("INSERT INTO assets(asset_id, sha256, size, duration_ms, start_offset_ms, container, title, "
                   "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (ASSET, SHA, 16, 7_200_000, 0, "matroska,webm", "فیلم آزمایشی <b>Test</b>", 1_760_000_000.0,
                    1_760_000_100.0))
        db.execute("INSERT INTO files(path, root_id, size, mtime_ns, file_id, asset_id, hashed_size, hashed_mtime_ns, "
                   "hashed_file_id, status, seen_at) VALUES (?,1,?,?,?,?,?,?,?,'present',1.0)",
                   (str(media), state.st_size, state.st_mtime_ns, state.st_ino, ASSET, state.st_size,
                    state.st_mtime_ns, state.st_ino))
        db.execute("INSERT INTO streams(asset_id, idx, kind, codec, language, is_default, forced, width, height, "
                   "start_ms) VALUES (?, 0, 'video', 'h264', NULL, 1, 0, 1920, 1080, 0)", (ASSET,))
        db.execute("INSERT INTO streams(asset_id, idx, kind, codec, language, is_default, forced, channels, start_ms) "
                   "VALUES (?, 1, 'audio', 'aac', 'fas', 1, 0, 2, 0)", (ASSET,))
        for name, spec in STAGES.items():
            done = name != "semantic"
            db.execute("INSERT INTO stages(asset_id, stage, version, state, attempts, queued_at, started_at, "
                       "finished_at) VALUES (?,?,?,?,1,?,?,?)",
                       (ASSET, name, spec.version, "COMPLETED" if done else "QUEUED", 1_760_000_000.0,
                        1_760_000_010.0 if done else None, 1_760_000_020.0 if done else None))
        seq = 0
        if sidecar:
            db.execute("INSERT INTO subtitle_tracks(track_id, asset_id, source, sidecar_path, sidecar_sha256, "
                       "language, title, codec, encoding, status, detail, cue_count) VALUES "
                       "(?,?,'sidecar',?,?,'eng','English','srt','utf-8','indexed',NULL,?)",
                       (f"{ASSET}:s1", ASSET, str(media.with_suffix(".en.srt")), "cd" * 32, len(UNTRUSTED) + 2))
            for index, text in enumerate(UNTRUSTED + ["Hello there, General Kenobi.", PERSIAN]):
                db.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                           "VALUES (?,?,'cue',?,?,?,?,?)",
                           (ASSET, f"{ASSET}:s1", seq, 1000 + index * 2000, 2500 + index * 2000, text, text.lower()))
                seq += 1
        if asr:
            provenance = {"source": "asr", "schema": 2, "asrVersion": 2, "model": "ggml-large-v3-turbo-q5_0",
                          "runtime": "whisper.cpp v1.9.5 (b5454, d1be6fde)", "verified": False, "calibrated": False,
                          "label": "unverified machine transcription (whisper.cpp); no accuracy claim",
                          "generatedAt": 1_760_000_015.0, "assetSha256": SHA, "chunks": 3}
            db.execute("INSERT INTO subtitle_tracks(track_id, asset_id, source, stream_index, language, title, codec, "
                       "encoding, status, detail, cue_count, provenance_json) VALUES "
                       "(?,?,'asr',1,'fas','ASR (unverified)','whisper.cpp','utf-8','indexed',?,?,?)",
                       (f"{ASSET}:asr", ASSET, provenance["label"], lines, json.dumps(provenance)))
            for index in range(lines):
                text = f"{PERSIAN} سطر {index}" if index % 2 == 0 else f"Machine line {index}: mixed فارسی text"
                db.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                           "VALUES (?,?,'cue',?,?,?,?,?)",
                           (ASSET, f"{ASSET}:asr", seq, 60_000 + index * 3000, 62_000 + index * 3000, text, text))
                seq += 1
        for scene in range(3):
            db.execute("INSERT INTO scenes(asset_id, scene_index, start_ms, end_ms, cut_score) VALUES (?,?,?,?,?)",
                       (ASSET, scene, scene * 10_000, scene * 10_000 + 9_999, 0.5))
            db.execute("INSERT INTO keyframes(asset_id, scene_index, ordinal, time_ms, path, bytes) VALUES "
                       "(?,?,0,?,?,10)", (ASSET, scene, scene * 10_000 + 500, f"keyframes/{scene}.jpg"))
    return store, media


@pytest.fixture()
def no_decoding(monkeypatch):
    def tripwire(*args, **kwargs):
        raise AssertionError("a report must never start a child")
    monkeypatch.setattr(tools_module, "run_bounded", tripwire)
    monkeypatch.setattr(stages_module, "run_bounded", tripwire)


def pdf_text(data: bytes) -> tuple[pypdf.PdfReader, str]:
    reader = pypdf.PdfReader(__import__("io").BytesIO(data))
    return reader, "\n".join(page.extract_text() or "" for page in reader.pages)


# ---------------------------------------------------------------- the schema


def test_the_report_tells_the_actual_sources_and_invents_nothing(tmp_path, no_decoding):
    store, _ = make_store(tmp_path)
    report = reports.gather(store.db, ASSET, "full")
    assert report["schema"] == "qronos.media.report/2"
    assert report["summary"]["dialogueLinesBySource"] == {"sidecar": 6, "asr": 30}
    codes = {item["code"] for item in report["limitations"]}
    assert {"asr_unverified", "subtitle_timing", "shots_not_scenes", "no_confidence"} <= codes
    not_available = {item["capability"] for item in report["notAvailable"]}
    assert "asr" not in not_available  # completed for THIS asset
    assert {"semantic_dialogue", "topics", "vision_captions", "ocr", "speaker_turns"} <= not_available
    asr = next(track for track in report["subtitleTracks"] if track["source"] == "asr")
    assert asr["provenance"]["verified"] is False and asr["provenance"]["generatedAt"].startswith("2025-")
    assert {line["source"] for line in report["dialogue"]} == {"sidecar", "asr"}
    text = json.dumps(report, ensure_ascii=False)
    assert "speech recognition is pending" not in text and "confidence\":" not in text and "topics\": [" not in text
    short = reports.gather(store.db, ASSET, "short")
    assert all(not item["available"] for item in short["generated"].values())
    assert "dialogue" not in short and len(short["keyframes"]) == 3


def test_an_asset_without_dialogue_says_so(tmp_path, no_decoding):
    store, _ = make_store(tmp_path, asr=False, sidecar=False)
    report = reports.gather(store.db, ASSET, "transcript")
    assert report["dialogue"] == [] and report["summary"]["dialogueLines"] == 0
    assert "no_dialogue" in {item["code"] for item in report["limitations"]}
    _, text = pdf_text(reports.to_pdf(report))
    assert "%PDF" not in text and text.strip()


# ---------------------------------------------------------------- the PDF


@pytest.mark.parametrize("kind", reports.KINDS)
def test_every_kind_is_a_shaped_persian_pdf_with_its_evidence(tmp_path, no_decoding, kind):
    store, _ = make_store(tmp_path)
    data = reports.render_bytes(reports.gather(store.db, ASSET, kind), "pdf")
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    reader, text = pdf_text(data)
    for needle in (ASSET, SHA[:32], "Qronos"):
        assert needle in text.replace("\n", ""), needle
    fonts = {str(f.get_object()["/BaseFont"]) for page in reader.pages
             for f in page["/Resources"]["/Font"].values()}
    assert fonts and all("Vazirmatn" in name for name in fonts), fonts  # the pinned font, embedded (subset)
    raw = data.decode("latin-1")
    assert all(marker not in raw for marker in ("/JavaScript", "/JS ", "/Launch", "/URI", "/SubmitForm", "/EmbeddedFile"))
    assert re.search(r"/OpenAction\s*<<", raw) is None  # only fpdf's page view [n 0 R /FitH], never an action
    assert reader.trailer["/Root"]["/Lang"] == "fa"
    if kind in {"full", "transcript"}:
        # drawn as plain text, never markup (pypdf may order a leading neutral like < into its own run)
        assert "00:01:00.000" in text and "script>alert(1)</script>" in text and 'HYPERLINK("http://x")' in text
        assert "‮" not in text  # a bidi override in a subtitle is dropped
    (tmp_path / f"{kind}.pdf").write_bytes(data)


def test_a_long_transcript_is_paginated_and_bounded_with_a_count(tmp_path, no_decoding, monkeypatch):
    monkeypatch.setattr(reports, "MAX_PDF_DIALOGUE_ROWS", 150)
    store, _ = make_store(tmp_path, lines=400)
    reader, text = pdf_text(reports.render_bytes(reports.gather(store.db, ASSET, "transcript"), "pdf"))
    assert len(reader.pages) >= 5
    assert "256" in text  # 406 lines, 150 drawn: the other 256 are counted, never silently lost
    pages = [page.extract_text() for page in reader.pages]
    assert all("Qronos" in page for page in pages)  # the header on every page


def test_the_pdf_refuses_a_font_that_is_not_the_pinned_one(tmp_path, monkeypatch):
    fonts = tmp_path / "fonts"
    shutil.copytree(reports.FONT_DIR, fonts)
    data = bytearray((fonts / "Vazirmatn-Regular.ttf").read_bytes())
    data[100] ^= 0xFF
    (fonts / "Vazirmatn-Regular.ttf").write_bytes(bytes(data))
    monkeypatch.setattr(reports, "FONT_DIR", fonts)
    monkeypatch.setattr(reports, "FONT_REGISTRY", fonts / "fonts.json")
    with pytest.raises(reports.ReportUnavailable, match="not the pinned font"):
        reports.pinned_fonts()


def test_the_font_registry_pins_the_shipped_bytes_and_licence():
    registry = json.loads(reports.FONT_REGISTRY.read_text(encoding="utf-8"))
    assert registry["licence"] == "OFL-1.1" and registry["source"]["archiveSha256"]
    for entry in registry["files"]:
        data = (reports.FONT_DIR / entry["file"]).read_bytes()
        assert len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert "SIL Open Font License" in (reports.FONT_DIR / "OFL.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------- the other formats keep their protections


def test_html_and_csv_keep_escaping_and_formula_protection(tmp_path, no_decoding):
    store, _ = make_store(tmp_path)
    report = reports.gather(store.db, ASSET, "full")
    page = reports.to_html(report)
    assert "<script>alert" not in page and "&lt;script&gt;" in page and 'dir="rtl"' in page
    assert "<b>Test</b>" not in page
    sheet = reports.to_csv(report)
    assert "'=HYPERLINK" in sheet and "\n=HYPERLINK" not in sheet and sheet.startswith("﻿")
    short = reports.to_csv(reports.gather(store.db, ASSET, "short"))
    assert short.splitlines()[0].endswith("finished_at")


# ---------------------------------------------------------------- the service: the same gates for PDF bytes


def _service(tmp_path: Path) -> tuple[MediaService, FakePermissions]:
    store, _ = make_store(tmp_path / "data")
    store.db.close()
    permissions = FakePermissions()
    return make_service(tmp_path / "data", tools=None, permissions=permissions), permissions


def test_generate_and_preview_pdf_come_from_the_index(tmp_path, no_decoding):
    service, _ = _service(tmp_path)
    written = service.generate_report(ASSET, fmt="pdf", kind="evidence")
    data = Path(written["path"]).read_bytes()
    assert data.startswith(b"%PDF-") and written["sha256"] == hashlib.sha256(data).hexdigest()
    view = service.preview_report(ASSET, kind="transcript")
    assert view["kind"] == "transcript" and len(view["dialogue"]) == reports.PREVIEW_DIALOGUE_ROWS
    assert len(json.dumps(view, ensure_ascii=False)) < 64_000
    with pytest.raises(MediaRequestError):
        service.preview_report(ASSET, kind="summary")
    service.close()


def test_a_pdf_export_waits_for_its_single_use_approval_and_writes_exactly_those_bytes(tmp_path, monkeypatch):
    service, permissions = _service(tmp_path)
    permissions.ask = True
    monkeypatch.setattr(service, "verify_asset", lambda asset_id, **k: {"status": "verified", "verified": ["copy"]})
    destination = tmp_path / "out" / "گزارش.pdf"
    destination.parent.mkdir()
    pending = service.export_report(ASSET, str(destination), fmt="pdf", kind="full")
    assert pending["status"] == "approval_required" and not destination.exists()
    prepared = next(iter(permissions.pending.values()))
    assert prepared["content"].startswith(b"%PDF-")
    done = service.approve_export(pending["approvalId"], "require_ui_confirmation")
    assert done["status"] == "written" and destination.read_bytes() == prepared["content"]
    assert done["sha256"] == hashlib.sha256(prepared["content"]).hexdigest()
    with pytest.raises(Exception, match="stale"):
        service.approve_export(pending["approvalId"], "require_ui_confirmation")  # single use
    with pytest.raises(MediaRequestError):
        service.export_report(ASSET, str(tmp_path / "out" / "report.html"), fmt="pdf")  # extension must match
    service.close()
