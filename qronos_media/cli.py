"""Command-line front end: ``python -m qronos_media <command>``; JSON on stdout.

This is the same contract the backend IPC service will expose. It uses the
standalone authority, which runs CPU/disk stages only and yields to gaming or
creator activity, and the real permission gate: a folder is accepted only
when the user's Files policy allows reading it outright. Where the policy asks
for approval, this tool refuses, because only Qronos can show and record that
approval. Exit codes: 0 ok, 2 refused request, 3 index busy.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from core.action_audit import ActionAuditLog
from qronos_media.paths import UnsafeMediaPath
from qronos_media.permissions import GatePermissions, MediaPermissionRefused
from qronos_media.scope import OutOfScope
from qronos_media.resources import StandaloneCpuAuthority
from qronos_media.search import SearchError
from qronos_media.service import (
    DEFAULT_CACHE_QUOTA_BYTES, MediaRequestError, MediaService, default_data_dir, tools_from_environment,
)
from qronos_media.stages import STAGES

MAX_WATCH_ITERATIONS = 10_000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qronos_media", description="Local media index")
    parser.add_argument("--data-dir", help="media index directory (default: Qronos data/Media)")
    parser.add_argument("--cache-quota-mb", type=int, default=DEFAULT_CACHE_QUOTA_BYTES // 1024**2)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-root", help="select a folder or file; the Qronos Files permission decides")
    add.add_argument("path")
    add.add_argument("--kind", choices=("folder", "file"), default="folder")
    add.add_argument("--max-depth", type=int, default=12)
    remove = sub.add_parser("remove-root")
    remove.add_argument("root_id", type=int)
    sub.add_parser("roots")
    scan = sub.add_parser("scan")
    scan.add_argument("--root-id", type=int)
    index = sub.add_parser("index")
    index.add_argument("--max-seconds", type=float, default=60.0)
    index.add_argument("--max-stages", type=int, default=100)
    watch = sub.add_parser("watch", help="poll: scan then index, a bounded number of times")
    watch.add_argument("--interval", type=float, default=60.0)
    watch.add_argument("--iterations", type=int, default=1)
    watch.add_argument("--max-seconds", type=float, default=60.0)
    sub.add_parser("pause")
    sub.add_parser("resume")
    for name in ("cancel-job", "pause-job", "resume-job", "retry", "reindex"):
        job = sub.add_parser(name)
        job.add_argument("asset_id")
        job.add_argument("stage", choices=tuple(STAGES))
    prio = sub.add_parser("prioritize")
    prio.add_argument("asset_id")
    prio.add_argument("--priority", type=int, default=10)
    status = sub.add_parser("status")
    status.add_argument("--asset-id")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--mode", choices=("auto", "exact", "fuzzy"), default="auto")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--language")
    search.add_argument("--asset-id")
    search.add_argument("--min-similarity", type=float, default=0.6)
    asset = sub.add_parser("asset")
    asset.add_argument("asset_id")
    scene = sub.add_parser("scene")
    scene.add_argument("asset_id")
    scene.add_argument("scene_index", type=int)
    report = sub.add_parser("report")
    report.add_argument("asset_id")
    report.add_argument("--format", choices=("json", "html", "csv"), default="json")
    report.add_argument("--kind", choices=("full", "transcript", "evidence"), default="full")
    export = sub.add_parser("export-report", help="copy a report to a destination the Files permission allows")
    export.add_argument("asset_id")
    export.add_argument("destination")
    export.add_argument("--format", choices=("json", "html", "csv"), default="json")
    export.add_argument("--kind", choices=("full", "transcript", "evidence"), default="full")
    opener = sub.add_parser("open-timestamp", help="plan only; launching needs Qronos approval")
    opener.add_argument("asset_id")
    opener.add_argument("start_ms", type=int)
    opener.add_argument("--player")
    sub.add_parser("tools", help="report the located ffmpeg build and its licence flags")
    return parser


def dispatch(service: MediaService, args: argparse.Namespace) -> Any:
    command = args.command
    if command == "add-root":
        return service.add_root(args.path, kind=args.kind, max_depth=args.max_depth)
    if command == "remove-root":
        return service.remove_root(args.root_id)
    if command == "roots":
        return service.list_roots()
    if command == "scan":
        return service.scan(args.root_id)
    if command == "index":
        return service.index(max_seconds=args.max_seconds, max_stages=args.max_stages)
    if command == "watch":
        iterations = max(1, min(MAX_WATCH_ITERATIONS, args.iterations))
        rounds = []
        for number in range(iterations):
            rounds.append({"scan": service.scan(), "index": service.index(max_seconds=args.max_seconds)})
            if number + 1 < iterations:
                time.sleep(max(1.0, args.interval))
        return rounds
    if command in {"pause", "resume"}:
        return getattr(service, command)()
    if command in {"cancel-job", "pause-job", "resume-job", "retry", "reindex"}:
        return getattr(service, command.replace("-", "_"))(args.asset_id, args.stage)
    if command == "prioritize":
        return service.prioritize(args.asset_id, args.priority)
    if command == "status":
        return service.status(args.asset_id)
    if command == "search":
        return service.search_dialogue(args.query, mode=args.mode, limit=args.limit, language=args.language,
                                       asset_id=args.asset_id, min_similarity=args.min_similarity)
    if command == "asset":
        return service.get_asset(args.asset_id)
    if command == "scene":
        return service.get_scene(args.asset_id, args.scene_index)
    if command == "report":
        return service.generate_report(args.asset_id, fmt=args.format, kind=args.kind)
    if command == "export-report":
        return service.export_report(args.asset_id, args.destination, fmt=args.format, kind=args.kind)
    if command == "open-timestamp":
        return service.open_timestamp(args.asset_id, args.start_ms, player=args.player)
    if command == "tools":
        return service.tools.describe() if service.tools else {"available": False}
    raise MediaRequestError(f"unknown command {command}")


def _emit(value: Any) -> None:
    """UTF-8 JSON regardless of the console code page (Persian text otherwise fails on cp1252)."""
    data = (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(data.decode("utf-8"))
    else:
        sys.stdout.flush()
        stream.write(data)
        stream.flush()


def main(argv: Sequence[str] | None = None, *, service: MediaService | None = None) -> int:
    args = build_parser().parse_args(argv)
    owned = service is None
    try:
        if service is None:
            data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
            service = MediaService(
                data_dir, tools=tools_from_environment(),
                authority=StandaloneCpuAuthority(data_dir),
                # The real gate and the user's real Files policy; decisions
                # are written to the same action audit log the backend uses.
                permissions=GatePermissions(audit=ActionAuditLog().record_verdict),
                cache_quota_bytes=max(1, args.cache_quota_mb) * 1024**2,
            )
        result = dispatch(service, args)
    except (MediaRequestError, UnsafeMediaPath, SearchError, MediaPermissionRefused, OutOfScope) as exc:
        _emit({"error": str(exc)})
        return 2
    finally:
        if owned and service is not None:
            service.close()
    _emit(result)
    return 3 if isinstance(result, dict) and result.get("stopped") == "busy" else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
