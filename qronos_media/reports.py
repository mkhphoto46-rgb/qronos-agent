"""Reports derived from the index only (handoff s37, s38).

Nothing here opens a media file or calls a model: changing the export format
never re-analyses anything. Every report is built from one schema (``gather``)
read from the structured database, and JSON, HTML, CSV and PDF are renderings
of that same dictionary. Every report carries the asset hash, stage versions,
states and times, where each line of dialogue came from (authored subtitles or
the unverified machine transcription and its recorded provenance), and the
capabilities this asset does not have, so a short report cannot be read as
"nothing else is in this film". No summary, topic, important-scene ranking,
visual description or confidence is produced: the index has none.

HTML is fully escaped (subtitle text is untrusted) and Persian, right to left.
CSV cells that a spreadsheet would treat as a formula are prefixed with ``'``.
PDF is laid out right to left with the pinned Vazirmatn font (SIL OFL 1.1,
``qronos_media/fonts``), shaped by HarfBuzz through fpdf2; untrusted text is
drawn as plain text (no markup) with bidi controls removed, and long
transcripts are bounded with the count of what was left out.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from qronos_media.jobs import file_matches_hash
from qronos_media.stages import PENDING_CAPABILITIES, STAGES

REPORT_SCHEMA = "qronos.media.report/2"
KINDS = ("short", "full", "transcript", "evidence")
FORMATS = ("json", "html", "csv", "pdf")
MAX_DIALOGUE_ROWS = 50_000
#: A PDF lists at most this many dialogue lines and scenes; the rest is counted (JSON/CSV carry them all).
MAX_PDF_DIALOGUE_ROWS = 4_000
MAX_PDF_SCENE_ROWS = 2_000
MAX_PDF_TEXT_CHARS = 600
SHORT_KEYFRAMES = 12
FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_REGISTRY = FONT_DIR / "fonts.json"

KIND_FA = {"short": "گزارش کوتاه", "full": "گزارش کامل", "transcript": "گزارش متن گفتار", "evidence": "گزارش شواهد"}
SOURCE_FA = {"embedded": "زیرنویس درون فایل", "sidecar": "زیرنویس کنار فایل", "asr": "رونویسی ماشینی (تأییدنشده)"}
_STATE_FA = {"COMPLETED": "انجام شد", "QUEUED": "در صف", "RUNNING": "در حال اجرا", "PAUSED": "متوقف",
             "FAILED": "ناموفق", "CANCELLED": "لغو شد", "NOT_QUEUED": "در صف نیست"}
_STAGE_FA = {"metadata": "مشخصات فایل", "subtitles": "زیرنویس", "asr": "رونویسی گفتار", "semantic": "جستجوی معنایی",
             "scenes": "تشخیص برش‌ها", "keyframes": "فریم‌های کلیدی"}
_CAPABILITY_FA = {"asr": "رونویسی گفتار", "subtitle_asr_fusion": "تطبیق زیرنویس و رونویسی",
                  "semantic_dialogue": "جستجوی معنایی دیالوگ", "visual_embeddings": "جستجوی تصویری",
                  "vision_captions": "توصیف تصویر", "ocr": "خواندن متن تصویر (OCR)", "topics": "موضوع‌ها",
                  "speaker_turns": "تشخیص گوینده", "actions_motion": "کنش و حرکت"}
_CAPABILITY_STATUS_FA = {"pending": "پیاده‌سازی نشده", "deferred": "به تعویق افتاده",
                         "decision_pending": "در انتظار تصمیم", "not_supported": "پشتیبانی نمی‌شود",
                         "implemented_unverified": "پیاده‌سازی‌شده، تأییدنشده"}
# Bidi embedding/override/isolate controls in untrusted text could reorder what is drawn around them.
_BIDI_CONTROLS = re.compile("[‎‏‪-‮⁦-⁩؜]")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ReportUnavailable(RuntimeError):
    """A format cannot be produced in this installation (for example, the PDF font is not the pinned one)."""


def clock_text(ms: int | None) -> str:
    if ms is None:
        return ""
    seconds, millis = divmod(int(ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec="seconds")


def levels(db: sqlite3.Connection, asset_id: str) -> dict[str, Any]:
    stages = {
        row["stage"]: {"state": row["state"], "version": row["version"], "attempts": row["attempts"],
                       "error": row["error"], "note": row["note"], "queuedAt": _iso(row["queued_at"]),
                       "startedAt": _iso(row["started_at"]), "finishedAt": _iso(row["finished_at"])}
        for row in db.execute("SELECT * FROM stages WHERE asset_id = ?", (asset_id,))
    }
    for name, spec in STAGES.items():
        stages.setdefault(name, {"state": "NOT_QUEUED", "version": spec.version})
        stages[name]["level"] = spec.level
    return {"stages": stages, "pending": list(PENDING_CAPABILITIES)}


def _sources(db: sqlite3.Connection, asset_id: str) -> list[dict[str, Any]]:
    sources = []
    for row in db.execute(
            "SELECT track_id, source, stream_index, sidecar_path, sidecar_sha256, language, title, codec, forced, "
            "encoding, status, detail, cue_count, provenance_json FROM subtitle_tracks WHERE asset_id = ? "
            "ORDER BY track_id", (asset_id,)):
        item = {key: row[key] for key in row.keys() if key != "provenance_json"}
        provenance = None
        if row["provenance_json"]:
            try:
                provenance = json.loads(row["provenance_json"])
            except ValueError:
                provenance = {"unreadable": True}
            if isinstance(provenance, dict) and isinstance(provenance.get("generatedAt"), (int, float)):
                provenance["generatedAt"] = _iso(provenance["generatedAt"])
        item["provenance"] = provenance
        sources.append(item)
    return sources


def _limitations(stages: dict[str, Any], sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What the reader must know about THIS asset's report, from what was actually indexed."""
    indexed = [s for s in sources if s["status"] == "indexed" and s["cue_count"]]
    by_source = {s["source"] for s in indexed}
    items: list[dict[str, str]] = []
    if not indexed:
        items.append({"code": "no_dialogue",
                      "text": "برای این فایل هیچ دیالوگی نمایه نشده است (نه زیرنویس، نه رونویسی ماشینی)."})
    if by_source & {"embedded", "sidecar"}:
        items.append({"code": "subtitle_timing",
                      "text": "زمان‌بندی زیرنویس همان است که نوشته شده و ممکن است با این نسخهٔ فیلم دقیقاً یکی نباشد."})
    if "asr" in by_source:
        items.append({"code": "asr_unverified",
                      "text": "بخشی از دیالوگ رونویسی ماشینی whisper.cpp است: تأییدنشده و بدون ادعای دقت."})
    elif stages.get("asr", {}).get("state") == "COMPLETED" and not by_source & {"embedded", "sidecar"}:
        items.append({"code": "asr_empty", "text": "رونویسی ماشینی انجام شد ولی متنی تولید نکرد."})
    if stages.get("scenes", {}).get("state") == "COMPLETED":
        items.append({"code": "shots_not_scenes",
                      "text": "برش‌ها با تغییر تصویر تشخیص داده شده‌اند، نه صحنه‌های معنایی."})
    items.append({"code": "no_confidence",
                  "text": "هیچ اطمینان کالیبره‌شده‌ای محاسبه نمی‌شود و در این گزارش هم عددی برای اطمینان نیست."})
    items.append({"code": "no_generated_content",
                  "text": "خلاصه، موضوع، رتبه‌بندی صحنه‌های مهم، توصیف تصویر و OCR تولید نمی‌شوند (فهرست «در این فایل در دسترس نیست»)."})
    return items


def _not_available(stages: dict[str, Any]) -> list[dict[str, Any]]:
    """Capabilities this asset does not have: never implemented, or implemented but not completed here."""
    out = []
    stage_of = {"asr": "asr", "semantic_dialogue": "semantic"}
    for item in PENDING_CAPABILITIES:
        stage = stage_of.get(str(item["capability"]))
        if item["status"] == "implemented_unverified" and stage \
                and stages.get(stage, {}).get("state") == "COMPLETED":
            continue
        out.append({"capability": item["capability"], "status": item["status"],
                    "stageState": stages.get(stage, {}).get("state") if stage else None, "reason": item["reason"]})
    return out


def gather(db: sqlite3.Connection, asset_id: str, kind: str = "full") -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    asset = db.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    if asset is None:
        raise KeyError(asset_id)
    level = levels(db, asset_id)
    sources = _sources(db, asset_id)
    cue_counts = {source: count for source, count in db.execute(
        "SELECT t.source, COUNT(*) FROM dialogue d JOIN subtitle_tracks t ON t.track_id = d.track_id "
        "WHERE d.asset_id = ? AND d.kind = 'cue' GROUP BY t.source", (asset_id,))}
    scene_count = db.execute("SELECT COUNT(*) FROM scenes WHERE asset_id = ?", (asset_id,)).fetchone()[0]
    keyframe_count = db.execute("SELECT COUNT(*) FROM keyframes WHERE asset_id = ?", (asset_id,)).fetchone()[0]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "kind": kind,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "derivedFrom": "media index database (no media read, no model call)",
        "asset": {
            "assetId": asset["asset_id"], "sha256": asset["sha256"], "size": asset["size"],
            "durationMs": asset["duration_ms"], "startOffsetMs": asset["start_offset_ms"],
            "videoEndMs": asset["video_end_ms"], "container": asset["container"], "title": asset["title"],
            "indexedAt": _iso(asset["created_at"]), "updatedAt": _iso(asset["updated_at"]),
        },
        "files": [{"path": row["path"], "status": row["status"],
                   "matchesHash": row["status"] == "present" and file_matches_hash(row)}
                  for row in db.execute("SELECT * FROM files WHERE asset_id = ? ORDER BY path", (asset_id,))],
        "levels": level,
        "summary": {"dialogueLines": sum(cue_counts.values()), "dialogueLinesBySource": cue_counts,
                    "subtitleTracks": len(sources), "scenes": scene_count, "keyframes": keyframe_count},
        "notAvailable": _not_available(level["stages"]),
        "limitations": _limitations(level["stages"], sources),
    }
    report["subtitleTracks"] = sources
    if kind == "short":
        report["keyframes"] = [{"sceneIndex": f["scene_index"], "timeMs": f["time_ms"]} for f in db.execute(
            "SELECT scene_index, time_ms FROM keyframes WHERE asset_id = ? ORDER BY time_ms LIMIT ?",
            (asset_id, SHORT_KEYFRAMES))]
        report["generated"] = {name: {"available": False, "reason": reason} for name, reason in (
            ("summary", "no summary is generated from the index"),
            ("topics", "topics are deferred: no brain authority is wired to media"),
            ("importantScenes", "no importance ranking exists; scene boundaries only"),
            ("keyDialogue", "no key-dialogue selection exists; see the transcript report"))}
    if kind in {"full", "evidence"}:
        report["streams"] = [dict(row) for row in db.execute(
            "SELECT * FROM streams WHERE asset_id = ? ORDER BY idx", (asset_id,))]
        scenes = []
        for row in db.execute("SELECT * FROM scenes WHERE asset_id = ? ORDER BY scene_index", (asset_id,)):
            frames = [{"timeMs": f["time_ms"], "path": f["path"]} for f in db.execute(
                "SELECT time_ms, path FROM keyframes WHERE asset_id = ? AND scene_index = ? ORDER BY ordinal",
                (asset_id, row["scene_index"]))]
            scenes.append({"sceneIndex": row["scene_index"], "startMs": row["start_ms"], "endMs": row["end_ms"],
                           "cutScore": row["cut_score"], "keyframes": frames})
        report["scenes"] = scenes
    if kind in {"full", "transcript"}:
        report["dialogue"] = [
            {"trackId": row["track_id"], "source": row["source"], "language": row["language"],
             "startMs": row["start_ms"], "endMs": row["end_ms"], "text": row["text"]}
            for row in db.execute(
                "SELECT d.track_id, t.source, t.language, d.start_ms, d.end_ms, d.text FROM dialogue d "
                "JOIN subtitle_tracks t ON t.track_id = d.track_id WHERE d.asset_id = ? AND d.kind = 'cue' "
                f"ORDER BY d.track_id, d.start_ms LIMIT {MAX_DIALOGUE_ROWS}", (asset_id,))
        ]
        report["dialogueTruncated"] = report["summary"]["dialogueLines"] > len(report["dialogue"])
    return report


# ------------------------------------------------------------------ CSV


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@", "\t", "\r"} else text


def to_csv(report: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    sha = report["asset"]["sha256"]
    if "dialogue" in report:
        writer.writerow(["asset_sha256", "track_id", "language", "start", "end", "start_ms", "end_ms", "text",
                         "source"])
        for row in report["dialogue"]:
            writer.writerow([_csv_cell(v) for v in (sha, row["trackId"], row["language"], clock_text(row["startMs"]),
                                                    clock_text(row["endMs"]), row["startMs"], row["endMs"], row["text"],
                                                    row["source"])])
    elif "scenes" in report:
        writer.writerow(["asset_sha256", "scene_index", "start", "end", "start_ms", "end_ms", "keyframes"])
        for scene in report.get("scenes", []):
            writer.writerow([_csv_cell(v) for v in (sha, scene["sceneIndex"], clock_text(scene["startMs"]),
                                                    clock_text(scene["endMs"]), scene["startMs"], scene["endMs"],
                                                    len(scene["keyframes"]))])
    else:  # short: the stage table
        writer.writerow(["asset_sha256", "stage", "state", "version", "finished_at"])
        for name, stage in report["levels"]["stages"].items():
            writer.writerow([_csv_cell(v) for v in (sha, name, stage["state"], stage["version"],
                                                    stage.get("finishedAt"))])
    return "﻿" + buffer.getvalue()  # BOM so spreadsheet apps read Persian as UTF-8


# ------------------------------------------------------------------ HTML


def to_html(report: dict[str, Any]) -> str:
    e = lambda value: html.escape("" if value is None else str(value), quote=True)  # noqa: E731
    asset = report["asset"]
    parts = [
        "<!doctype html>",
        '<html lang="fa" dir="rtl"><head><meta charset="utf-8">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
        f"<title>{e(KIND_FA[report['kind']])} {e(asset['title'] or asset['assetId'])}</title>",
        "<style>body{font-family:Vazirmatn,Tahoma,sans-serif}td,th{border:1px solid #ccc;padding:4px}"
        "table{border-collapse:collapse}.ltr{direction:ltr;unicode-bidi:embed}</style></head><body>",
        f"<h1>{e(KIND_FA[report['kind']])}</h1><p>شناسه: <span class=\"ltr\">{e(asset['assetId'])}</span></p>",
        f"<p>SHA-256: <span class=\"ltr\">{e(asset['sha256'])}</span></p>",
        f"<p>مدت: <span class=\"ltr\">{e(clock_text(asset['durationMs']))}</span> — قالب: {e(asset['container'])}</p>",
        f"<p>زمان ساخت گزارش: <span class=\"ltr\">{e(report['generatedAt'])}</span> — "
        f"طرح: <span class=\"ltr\">{e(report['schema'])}</span></p>",
        "<h2>مراحل پردازش</h2><table><tr><th>مرحله</th><th>وضعیت</th><th>نسخه</th><th>پایان</th></tr>",
    ]
    for name, stage in report["levels"]["stages"].items():
        parts.append(f"<tr><td>{e(_STAGE_FA.get(name, name))}</td><td>{e(_STATE_FA.get(stage['state'], stage['state']))}"
                     f"</td><td>{e(stage['version'])}</td><td class=\"ltr\">{e(stage.get('finishedAt'))}</td></tr>")
    # Implemented-but-unverified capabilities (ASR, semantic dialogue) are not "unavailable": whether THIS asset ran
    # them is in the stage table above. Only the rest is listed as not yet available.
    implemented = [item for item in report["levels"]["pending"] if item["status"] == "implemented_unverified"]
    missing = [item for item in report["levels"]["pending"] if item["status"] != "implemented_unverified"]
    parts.append("</table><h2>پیاده‌سازی‌شده، تأییدنشده (وضعیت این فایل در جدول بالا)</h2><ul>")
    for item in implemented:
        parts.append(f"<li dir=\"ltr\">{e(item['capability'])}: {e(item['status'])}</li>")
    parts.append("</ul><h2>هنوز در دسترس نیست</h2><ul>")
    for item in missing:
        parts.append(f"<li dir=\"ltr\">{e(item['capability'])}: {e(item['status'])}</li>")
    parts.append("</ul><h2>منابع دیالوگ</h2><table><tr><th>منبع</th><th>زبان</th><th>وضعیت</th><th>تعداد</th>"
                 "<th>شرح</th></tr>")
    for track in report["subtitleTracks"]:
        parts.append(f"<tr><td>{e(SOURCE_FA.get(track['source'], track['source']))}</td>"
                     f"<td>{e(track['language'] or 'نامشخص')}</td><td>{e(track['status'])}</td>"
                     f"<td>{e(track['cue_count'])}</td><td dir=\"auto\">{e(track['detail'])}</td></tr>")
    parts.append("</table>")
    if "keyframes" in report:
        parts.append("<h2>فریم‌های کلیدی (زمان)</h2><ul>")
        parts += [f"<li class=\"ltr\">{e(clock_text(f['timeMs']))}</li>" for f in report["keyframes"]]
        parts.append("</ul>")
    if "scenes" in report:
        parts.append(f"<h2>برش‌ها ({len(report['scenes'])})</h2><table><tr><th>#</th><th>شروع</th><th>پایان</th></tr>")
        for scene in report["scenes"]:
            parts.append(f"<tr><td>{e(scene['sceneIndex'])}</td><td class=\"ltr\">{e(clock_text(scene['startMs']))}</td>"
                         f"<td class=\"ltr\">{e(clock_text(scene['endMs']))}</td></tr>")
        parts.append("</table>")
    if "dialogue" in report:
        parts.append("<h2>دیالوگ</h2><table><tr><th>زمان</th><th>منبع</th><th>متن</th></tr>")
        for row in report["dialogue"]:
            parts.append(f"<tr><td class=\"ltr\">{e(clock_text(row['startMs']))}</td>"
                         f"<td>{e(SOURCE_FA.get(row['source'], row['source']))}</td>"
                         f"<td dir=\"auto\">{e(row['text'])}</td></tr>")
        parts.append("</table>")
    parts.append("<h2>محدودیت‌ها</h2><ul>")
    parts += [f"<li>{e(item['text'])}</li>" for item in report["limitations"]]
    parts.append("</ul></body></html>")
    return "\n".join(parts)


# ------------------------------------------------------------------ PDF


def _plain(value: Any, limit: int = MAX_PDF_TEXT_CHARS) -> str:
    """Untrusted text as one plain line: no control or bidi-override characters, bounded."""
    text = "" if value is None else str(value)
    text = _BIDI_CONTROLS.sub("", _CONTROL.sub(" ", text.replace("\r", " ").replace("\n", " ").replace("\t", " ")))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pinned_fonts() -> dict[str, Path]:
    """The bundled Vazirmatn files, each checked against the font registry's SHA-256 (else ReportUnavailable)."""
    try:
        registry = json.loads(FONT_REGISTRY.read_text(encoding="utf-8"))
        fonts = {}
        for entry in registry["files"]:
            path = FONT_DIR / entry["file"]
            data = path.read_bytes()
            if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ReportUnavailable(f"{entry['file']} is not the pinned font")
            if entry.get("style") is not None:
                fonts[entry["style"]] = path
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReportUnavailable(f"the PDF font registry is unusable: {type(exc).__name__}") from None
    if set(fonts) != {"", "B"}:
        raise ReportUnavailable("the PDF font registry needs the regular and bold faces")
    return fonts


class _Pdf:
    """A right-to-left A4 document: Persian labels on the right, timecodes and hashes in left-to-right cells."""

    FAMILY = "Vazirmatn"

    def __init__(self, report: dict[str, Any]) -> None:
        try:
            from fpdf import FPDF
        except ImportError:
            raise ReportUnavailable("the PDF library is not installed") from None
        fonts = pinned_fonts()
        title = KIND_FA[report["kind"]]
        outer = self

        class Document(FPDF):
            def header(self) -> None:  # noqa: D401 - fpdf hook
                self.set_font(outer.FAMILY, "", 8)
                outer.rtl()
                self.cell(0, 6, f"Qronos — {title}", align="R", new_x="LMARGIN", new_y="NEXT")
                self.set_draw_color(180, 180, 180)
                self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
                self.ln(2)

            def footer(self) -> None:  # noqa: D401 - fpdf hook
                self.set_y(-12)
                self.set_font(outer.FAMILY, "", 8)
                outer.ltr()
                self.cell(0, 6, f"{self.page_no()} / {{nb}}", align="C")

        self.pdf = Document(format="A4", unit="mm")
        self.pdf.set_margins(15, 15, 15)
        self.pdf.set_auto_page_break(True, margin=16)
        self.pdf.alias_nb_pages()
        for style, path in fonts.items():
            self.pdf.add_font(self.FAMILY, style, str(path))
        try:
            self.pdf.set_text_shaping(use_shaping_engine=True, direction="rtl", script="arab", language="fas")
        except Exception:  # noqa: BLE001 - fpdf raises its own error when uharfbuzz is missing
            raise ReportUnavailable("the text shaping engine is not installed") from None
        generated = datetime.fromisoformat(report["generatedAt"])
        self.pdf.set_creation_date(generated)
        self.pdf.set_lang("fa")
        self.pdf.set_title(f"{title} — {report['asset']['assetId']}")
        self.pdf.set_creator("Qronos media index")
        self.pdf.set_producer("Qronos (fpdf2, HarfBuzz)")
        self.pdf.add_page()

    # direction switches: Persian labels, auto (untrusted text decides by its first strong letter), LTR values
    def rtl(self) -> None:
        self.pdf.set_text_shaping(use_shaping_engine=True, direction="rtl", script="arab", language="fas")

    def auto(self) -> None:
        self.pdf.set_text_shaping(use_shaping_engine=True)

    def ltr(self) -> None:
        self.pdf.set_text_shaping(use_shaping_engine=True, direction="ltr")

    @property
    def width(self) -> float:
        return self.pdf.w - self.pdf.l_margin - self.pdf.r_margin

    def heading(self, text: str, size: int = 13) -> None:
        if self.pdf.get_y() > self.pdf.page_break_trigger - 20:
            self.pdf.add_page()
        self.pdf.ln(2)
        self.pdf.set_font(self.FAMILY, "B", size)
        self.rtl()
        self.pdf.multi_cell(0, size * 0.55, text, align="R", new_x="LMARGIN", new_y="NEXT")
        self.pdf.ln(1)

    def paragraph(self, text: str, size: int = 10) -> None:
        self.pdf.set_font(self.FAMILY, "", size)
        self.rtl()
        self.pdf.multi_cell(0, 5.5, _plain(text, 2000), align="R", new_x="LMARGIN", new_y="NEXT")

    def row(self, cells: list[tuple[str, float, str]], *, bold: bool = False, size: int = 9) -> None:
        """One table row, columns from right to left: (text, width, direction 'rtl'|'ltr'|'auto')."""
        pdf = self.pdf
        pdf.set_font(self.FAMILY, "B" if bold else "", size)
        line = size * 0.5
        heights = []
        for text, width, direction in cells:
            getattr(self, direction)()
            lines = pdf.multi_cell(width, line, text, dry_run=True, output="LINES")
            heights.append(max(1, len(lines)) * line)
        height = max(heights) + 1.5
        if pdf.get_y() + height > pdf.page_break_trigger:
            pdf.add_page()
        top, right = pdf.get_y(), pdf.w - pdf.r_margin
        for text, width, direction in cells:
            right -= width
            pdf.set_xy(right, top)
            getattr(self, direction)()
            pdf.multi_cell(width, line, text, border=0, align="R", new_x="RIGHT", new_y="TOP")
        pdf.set_draw_color(220, 220, 220)
        pdf.line(pdf.l_margin, top + height, pdf.w - pdf.r_margin, top + height)
        pdf.set_xy(pdf.l_margin, top + height + 0.5)

    def pairs(self, items: list[tuple[str, Any, str]]) -> None:
        for label, value, direction in items:
            self.row([(label, 45, "rtl"), (_plain(value), self.width - 45, direction)])

    def output(self) -> bytes:
        return bytes(self.pdf.output())


def to_pdf(report: dict[str, Any]) -> bytes:
    doc = _Pdf(report)
    asset = report["asset"]
    width = doc.width
    doc.heading(f"{KIND_FA[report['kind']]} رسانه", 16)
    doc.paragraph("این گزارش فقط از پایگاه دادهٔ نمایه ساخته شده است: هیچ فایل رسانه‌ای دوباره خوانده نشد و هیچ "
                  "مدلی اجرا نشد.")
    doc.pairs([
        ("عنوان", asset["title"] or "—", "auto"),
        ("شناسه", asset["assetId"], "ltr"),
        ("SHA-256", asset["sha256"], "ltr"),
        ("حجم (بایت)", asset["size"], "ltr"),
        ("مدت", clock_text(asset["durationMs"]) or "—", "ltr"),
        ("قالب", asset["container"] or "—", "ltr"),
        ("نخستین نمایه", asset["indexedAt"], "ltr"),
        ("زمان ساخت گزارش", report["generatedAt"], "ltr"),
        ("طرح گزارش", report["schema"], "ltr"),
    ])
    doc.heading("فایل‌ها")
    doc.row([("مسیر", width - 50, "rtl"), ("وضعیت", 25, "rtl"), ("هش هنوز معتبر", 25, "rtl")], bold=True)
    for item in report["files"]:
        doc.row([(_plain(item["path"]), width - 50, "auto"), (item["status"], 25, "ltr"),
                 ("بله" if item["matchesHash"] else "خیر", 25, "rtl")])

    doc.heading("مراحل پردازش و نسخه‌ها")
    doc.row([("مرحله", 40, "rtl"), ("وضعیت", 30, "rtl"), ("نسخه", 15, "rtl"), ("پایان", width - 85, "rtl")],
            bold=True)
    for name, stage in report["levels"]["stages"].items():
        doc.row([(_STAGE_FA.get(name, name), 40, "rtl"), (_STATE_FA.get(stage["state"], stage["state"]), 30, "rtl"),
                 (str(stage["version"]), 15, "ltr"), (stage.get("finishedAt") or "—", width - 85, "ltr")])

    summary = report["summary"]
    doc.heading("آنچه نمایه شده است")
    doc.pairs([("سطرهای دیالوگ", summary["dialogueLines"], "ltr")]
              + [(f"— {SOURCE_FA.get(source, source)}", count, "ltr")
                 for source, count in sorted(summary["dialogueLinesBySource"].items())]
              + [("ترک‌های دیالوگ", summary["subtitleTracks"], "ltr"), ("برش‌ها", summary["scenes"], "ltr"),
                 ("فریم‌های کلیدی", summary["keyframes"], "ltr")])

    doc.heading("منابع دیالوگ")
    for track in report["subtitleTracks"]:
        provenance = track.get("provenance") or {}
        items = [("منبع", SOURCE_FA.get(track["source"], track["source"]), "rtl"),
                 ("ترک", track["track_id"], "ltr"), ("زبان", track["language"] or "نامشخص", "auto"),
                 ("وضعیت", track["status"], "ltr"), ("تعداد سطر", track["cue_count"], "ltr")]
        if track["detail"]:
            items.append(("شرح", track["detail"], "auto"))
        if track["source"] == "asr" and provenance:
            items += [("مدل", provenance.get("model"), "ltr"), ("اجراکننده", provenance.get("runtime"), "ltr"),
                      ("نسخهٔ رونویسی", provenance.get("asrVersion"), "ltr"),
                      ("تأییدشده", "خیر" if not provenance.get("verified") else "بله", "rtl"),
                      ("زمان رونویسی", provenance.get("generatedAt"), "ltr")]
        doc.pairs(items)
        doc.pdf.ln(2)

    if report["kind"] == "short":
        doc.heading("خلاصه، موضوع‌ها، صحنه‌های مهم و دیالوگ کلیدی")
        doc.paragraph("هیچ‌کدام تولید نمی‌شوند: نمایه خلاصه، موضوع یا رتبه‌بندی ندارد و چیزی ساخته نمی‌شود. "
                      "برای متن کامل گفتار، گزارش متن گفتار را بگیرید.")
        doc.heading("فریم‌های کلیدی (زمان)")
        if report["keyframes"]:
            doc.row([("#", 20, "rtl"), ("زمان", width - 20, "rtl")], bold=True)
            for frame in report["keyframes"]:
                doc.row([(str(frame["sceneIndex"]), 20, "ltr"), (clock_text(frame["timeMs"]), width - 20, "ltr")])
        else:
            doc.paragraph("فریم کلیدی‌ای نمایه نشده است.")

    if "streams" in report:
        doc.heading("جریان‌های فایل")
        doc.row([("#", 10, "rtl"), ("نوع", 25, "rtl"), ("کدک", 30, "rtl"), ("زبان", 25, "rtl"),
                 ("جزئیات", width - 90, "rtl")], bold=True)
        for stream in report["streams"]:
            detail = " ".join(str(part) for part in (
                f"{stream.get('width')}x{stream.get('height')}" if stream.get("width") else "",
                f"{stream.get('channels')}ch" if stream.get("channels") else "",
                f"start {clock_text(stream.get('start_ms'))}" if stream.get("start_ms") is not None else "") if part)
            doc.row([(str(stream["idx"]), 10, "ltr"), (_plain(stream["kind"]), 25, "ltr"),
                     (_plain(stream.get("codec")), 30, "ltr"), (_plain(stream.get("language")), 25, "ltr"),
                     (detail or "—", width - 90, "ltr")])

    if "scenes" in report:
        scenes = report["scenes"]
        doc.heading(f"برش‌ها ({len(scenes)})")
        doc.row([("#", 15, "rtl"), ("شروع", 35, "rtl"), ("پایان", 35, "rtl"), ("فریم‌های کلیدی", width - 85, "rtl")],
                bold=True)
        for scene in scenes[:MAX_PDF_SCENE_ROWS]:
            frames = ", ".join(clock_text(f["timeMs"]) for f in scene["keyframes"]) or "—"
            doc.row([(str(scene["sceneIndex"]), 15, "ltr"), (clock_text(scene["startMs"]), 35, "ltr"),
                     (clock_text(scene["endMs"]), 35, "ltr"), (frames, width - 85, "ltr")])
        if len(scenes) > MAX_PDF_SCENE_ROWS:
            doc.paragraph(f"{len(scenes) - MAX_PDF_SCENE_ROWS} برش دیگر در این PDF نیامده است؛ "
                          "خروجی JSON یا CSV همه را دارد.")

    if "dialogue" in report:
        dialogue = report["dialogue"]
        doc.heading(f"دیالوگ ({len(dialogue)})")
        if not dialogue:
            doc.paragraph("دیالوگی نمایه نشده است.")
        else:
            doc.row([("زمان", 42, "rtl"), ("منبع", 24, "rtl"), ("متن", width - 66, "rtl")], bold=True)
            short = {"embedded": "زیرنویس", "sidecar": "زیرنویس", "asr": "ماشینی"}
            for line in dialogue[:MAX_PDF_DIALOGUE_ROWS]:
                doc.row([(f"{clock_text(line['startMs'])}\n{clock_text(line['endMs'])}", 42, "ltr"),
                         (short.get(line["source"], line["source"]), 24, "rtl"),
                         (_plain(line["text"]) or "—", width - 66, "auto")])
            left_out = report["summary"]["dialogueLines"] - min(len(dialogue), MAX_PDF_DIALOGUE_ROWS)
            if left_out > 0:
                doc.paragraph(f"{left_out} سطر دیگر در این PDF نیامده است؛ خروجی JSON یا CSV همه را دارد.")

    doc.heading("در این فایل در دسترس نیست")
    doc.row([("توانایی", 45, "rtl"), ("وضعیت", 45, "rtl"), ("شرح (متن پروژه)", width - 90, "rtl")], bold=True, size=8)
    for item in report["notAvailable"]:
        status = _CAPABILITY_STATUS_FA.get(str(item["status"]), str(item["status"]))
        if item["stageState"]:
            status += f" (این فایل: {_STATE_FA.get(item['stageState'], item['stageState'])})"
        doc.row([(_CAPABILITY_FA.get(str(item["capability"]), str(item["capability"])), 45, "rtl"),
                 (status, 45, "rtl"), (_plain(item["reason"]), width - 90, "auto")], size=8)
    doc.heading("محدودیت‌ها")
    for item in report["limitations"]:
        doc.paragraph("• " + item["text"])
    return doc.output()


# ------------------------------------------------------------------ output

PREVIEW_DIALOGUE_ROWS = 20
PREVIEW_TRACKS = 20
PREVIEW_TEXT_CHARS = 300


def preview(report: dict[str, Any]) -> dict[str, Any]:
    """A bounded slice of one report for the desktop panel: the same schema, never more than a small wire message."""
    tracks = [{"source": t["source"], "language": t["language"], "status": t["status"], "cueCount": t["cue_count"],
               "detail": _plain(t["detail"], PREVIEW_TEXT_CHARS) if t["detail"] else None}
              for t in report["subtitleTracks"][:PREVIEW_TRACKS]]
    out: dict[str, Any] = {
        "schema": report["schema"], "kind": report["kind"], "generatedAt": report["generatedAt"],
        "asset": {key: report["asset"][key] for key in ("assetId", "sha256", "title", "durationMs", "container")},
        "stages": {name: {"state": s["state"], "version": s["version"], "finishedAt": s.get("finishedAt")}
                   for name, s in report["levels"]["stages"].items()},
        "summary": report["summary"], "subtitleTracks": tracks,
        "tracksOmitted": max(0, len(report["subtitleTracks"]) - PREVIEW_TRACKS),
        "notAvailable": [{"capability": i["capability"], "status": i["status"], "stageState": i["stageState"]}
                         for i in report["notAvailable"]],
        "limitations": report["limitations"],
    }
    if "dialogue" in report:
        out["dialogue"] = [{"startMs": d["startMs"], "endMs": d["endMs"], "source": d["source"],
                            "text": _plain(d["text"], PREVIEW_TEXT_CHARS)}
                           for d in report["dialogue"][:PREVIEW_DIALOGUE_ROWS]]
    if "keyframes" in report:
        out["keyframes"] = report["keyframes"]
    if "scenes" in report:
        out["scenes"] = len(report["scenes"])
    return out


def render_bytes(report: dict[str, Any], fmt: str) -> bytes:
    if fmt == "pdf":
        return to_pdf(report)
    return render(report, fmt).encode("utf-8")


def render(report: dict[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report, ensure_ascii=False, indent=2)
    if fmt == "csv":
        return to_csv(report)
    if fmt == "html":
        return to_html(report)
    if fmt == "pdf":
        raise ValueError("pdf is binary: use render_bytes")
    raise ValueError(f"format must be one of {FORMATS}")


def write_report(db: sqlite3.Connection, asset_id: str, *, kind: str, fmt: str, exports_dir: Path) -> dict[str, Any]:
    content = render_bytes(gather(db, asset_id, kind), fmt)
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = exports_dir / f"{asset_id}-{kind}-{stamp}.{fmt}"
    temporary = target.with_suffix(target.suffix + ".tmp")
    with open(temporary, "xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return {"path": str(target), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
            "format": fmt, "kind": kind}
