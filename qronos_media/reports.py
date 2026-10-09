"""Reports derived from the index only (handoff s37, s38).

Nothing here opens a media file or calls a model: changing the export format
never re-analyses anything. Every report carries the asset hash, stage
versions and states, and the capabilities that are still pending, so a short
report cannot be read as "nothing else is in this film".

HTML is fully escaped (subtitle text is untrusted) and Persian, right to left.
CSV cells that a spreadsheet would treat as a formula are prefixed with ``'``.
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
import sqlite3
from typing import Any

from qronos_media.jobs import file_matches_hash
from qronos_media.stages import PENDING_CAPABILITIES, STAGES

REPORT_SCHEMA = "qronos.media.report/1"
KINDS = ("full", "transcript", "evidence")
FORMATS = ("json", "html", "csv")
MAX_DIALOGUE_ROWS = 50_000


def clock_text(ms: int | None) -> str:
    if ms is None:
        return ""
    seconds, millis = divmod(int(ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def levels(db: sqlite3.Connection, asset_id: str) -> dict[str, Any]:
    stages = {
        row["stage"]: {"state": row["state"], "version": row["version"], "attempts": row["attempts"],
                       "error": row["error"], "note": row["note"]}
        for row in db.execute("SELECT * FROM stages WHERE asset_id = ?", (asset_id,))
    }
    for name, spec in STAGES.items():
        stages.setdefault(name, {"state": "NOT_QUEUED", "version": spec.version})
        stages[name]["level"] = spec.level
    return {"stages": stages, "pending": list(PENDING_CAPABILITIES)}


def gather(db: sqlite3.Connection, asset_id: str, kind: str = "full") -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    asset = db.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    if asset is None:
        raise KeyError(asset_id)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "kind": kind,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asset": {
            "assetId": asset["asset_id"], "sha256": asset["sha256"], "size": asset["size"],
            "durationMs": asset["duration_ms"], "container": asset["container"], "title": asset["title"],
        },
        "files": [{"path": row["path"], "status": row["status"],
                   "matchesHash": row["status"] == "present" and file_matches_hash(row)}
                  for row in db.execute("SELECT * FROM files WHERE asset_id = ? ORDER BY path", (asset_id,))],
        "levels": levels(db, asset_id),
        "limitations": [
            "Dialogue comes from subtitles only; speech recognition is pending.",
            "Subtitle timing is as authored and may not match this exact cut.",
            "Shot boundaries are pixel-change detection, not semantic scenes.",
            "Lexical scores are not calibrated confidence.",
        ],
    }
    tracks = [dict(row) for row in db.execute(
        "SELECT track_id, source, stream_index, sidecar_path, sidecar_sha256, language, title, codec, forced, "
        "encoding, status, detail, cue_count FROM subtitle_tracks WHERE asset_id = ? ORDER BY track_id", (asset_id,))]
    report["subtitleTracks"] = tracks
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
            {"trackId": row["track_id"], "language": row["language"], "startMs": row["start_ms"],
             "endMs": row["end_ms"], "text": row["text"]}
            for row in db.execute(
                "SELECT d.track_id, t.language, d.start_ms, d.end_ms, d.text FROM dialogue d "
                "JOIN subtitle_tracks t ON t.track_id = d.track_id WHERE d.asset_id = ? AND d.kind = 'cue' "
                f"ORDER BY d.track_id, d.start_ms LIMIT {MAX_DIALOGUE_ROWS}", (asset_id,))
        ]
    return report


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@", "\t", "\r"} else text


def to_csv(report: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    sha = report["asset"]["sha256"]
    if "dialogue" in report:
        writer.writerow(["asset_sha256", "track_id", "language", "start", "end", "start_ms", "end_ms", "text"])
        for row in report["dialogue"]:
            writer.writerow([_csv_cell(v) for v in (sha, row["trackId"], row["language"], clock_text(row["startMs"]),
                                                    clock_text(row["endMs"]), row["startMs"], row["endMs"], row["text"])])
    else:
        writer.writerow(["asset_sha256", "scene_index", "start", "end", "start_ms", "end_ms", "keyframes"])
        for scene in report.get("scenes", []):
            writer.writerow([_csv_cell(v) for v in (sha, scene["sceneIndex"], clock_text(scene["startMs"]),
                                                    clock_text(scene["endMs"]), scene["startMs"], scene["endMs"],
                                                    len(scene["keyframes"]))])
    return "﻿" + buffer.getvalue()  # BOM so spreadsheet apps read Persian as UTF-8


_STATE_FA = {"COMPLETED": "انجام شد", "QUEUED": "در صف", "RUNNING": "در حال اجرا", "PAUSED": "متوقف",
             "FAILED": "ناموفق", "CANCELLED": "لغو شد", "NOT_QUEUED": "در صف نیست"}
_STAGE_FA = {"metadata": "مشخصات فایل", "subtitles": "زیرنویس", "scenes": "تشخیص برش‌ها", "keyframes": "فریم‌های کلیدی"}


def to_html(report: dict[str, Any]) -> str:
    e = lambda value: html.escape("" if value is None else str(value), quote=True)  # noqa: E731
    asset = report["asset"]
    parts = [
        "<!doctype html>",
        '<html lang="fa" dir="rtl"><head><meta charset="utf-8">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
        f"<title>گزارش رسانه {e(asset['title'] or asset['assetId'])}</title>",
        "<style>body{font-family:Tahoma,sans-serif}td,th{border:1px solid #ccc;padding:4px}"
        "table{border-collapse:collapse}.ltr{direction:ltr;unicode-bidi:embed}</style></head><body>",
        f"<h1>گزارش رسانه</h1><p>شناسه: <span class=\"ltr\">{e(asset['assetId'])}</span></p>",
        f"<p>SHA-256: <span class=\"ltr\">{e(asset['sha256'])}</span></p>",
        f"<p>مدت: <span class=\"ltr\">{e(clock_text(asset['durationMs']))}</span> — قالب: {e(asset['container'])}</p>",
        "<h2>مراحل پردازش</h2><table><tr><th>مرحله</th><th>وضعیت</th><th>نسخه</th></tr>",
    ]
    for name, stage in report["levels"]["stages"].items():
        parts.append(f"<tr><td>{e(_STAGE_FA.get(name, name))}</td><td>{e(_STATE_FA.get(stage['state'], stage['state']))}"
                     f"</td><td>{e(stage['version'])}</td></tr>")
    parts.append("</table><h2>هنوز در دسترس نیست</h2><ul>")
    for item in report["levels"]["pending"]:
        parts.append(f"<li dir=\"ltr\">{e(item['capability'])}: {e(item['status'])}</li>")
    parts.append("</ul><h2>زیرنویس‌ها</h2><table><tr><th>منبع</th><th>زبان</th><th>وضعیت</th><th>تعداد</th></tr>")
    for track in report["subtitleTracks"]:
        parts.append(f"<tr><td>{e(track['source'])}</td><td>{e(track['language'] or 'نامشخص')}</td>"
                     f"<td>{e(track['status'])}</td><td>{e(track['cue_count'])}</td></tr>")
    parts.append("</table>")
    if "scenes" in report:
        parts.append(f"<h2>برش‌ها ({len(report['scenes'])})</h2><table><tr><th>#</th><th>شروع</th><th>پایان</th></tr>")
        for scene in report["scenes"]:
            parts.append(f"<tr><td>{e(scene['sceneIndex'])}</td><td class=\"ltr\">{e(clock_text(scene['startMs']))}</td>"
                         f"<td class=\"ltr\">{e(clock_text(scene['endMs']))}</td></tr>")
        parts.append("</table>")
    if "dialogue" in report:
        parts.append("<h2>دیالوگ</h2><table><tr><th>زمان</th><th>متن</th></tr>")
        for row in report["dialogue"]:
            parts.append(f"<tr><td class=\"ltr\">{e(clock_text(row['startMs']))}</td>"
                         f"<td dir=\"auto\">{e(row['text'])}</td></tr>")
        parts.append("</table>")
    parts.append("<h2>محدودیت‌ها</h2><ul>")
    parts += [f"<li dir=\"ltr\">{e(item)}</li>" for item in report["limitations"]]
    parts.append("</ul></body></html>")
    return "\n".join(parts)


def render(report: dict[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report, ensure_ascii=False, indent=2)
    if fmt == "csv":
        return to_csv(report)
    if fmt == "html":
        return to_html(report)
    raise ValueError(f"format must be one of {FORMATS}")


def write_report(db: sqlite3.Connection, asset_id: str, *, kind: str, fmt: str, exports_dir: Path) -> dict[str, Any]:
    content = render(gather(db, asset_id, kind), fmt).encode("utf-8")
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
