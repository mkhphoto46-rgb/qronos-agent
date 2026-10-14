"""The media product route: typed, bounded operations on the ONE in-process media host.

The desktop sends ``qronos.media`` actions ``{op, requestId, ...}`` (send_runtime_action -> bridge stdin) and gets
``media_result`` runtime events ``{requestId, op, ok, ...}``. Brain/voice requests call the same operations. Every
field is validated against an allow-list and bounds here; nothing in a request names a model, a URL, a shell
command, a SQL statement, an approval decision or a resource budget.

Approvals: a gated operation answers ``approval_required`` with a single-use ``approvalId`` and ``needsSecret``. The
gate decision behind that id stays here (the request only says approved yes/no); a decision for an id this route
did not issue, or one already answered, is refused before anything runs. A typed secret is passed in-process only
and never echoed, stored or put in a result. Approvals are MEDIA_ACTION tickets of the bridge's one ledger (never
file-action tickets, so no file consumer reads their payload); its Pause/Stop/shutdown points revoke them, and a
late answer is stale.
"""
from __future__ import annotations

from collections import OrderedDict
import re
import threading
from typing import Any, Callable

from qronos_media.reports import FORMATS, KINDS
from qronos_media.stages import PENDING_CAPABILITIES, STAGES

ACTION_ID = "qronos.media"
RESULT_EVENT = "media_result"
PROGRESS_EVENT = "media_progress"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_APPROVAL_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_ASSET_ID = re.compile(r"^a_[0-9a-f]{32}$")
_RESULT_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_PATH_CHARS = 4096
MAX_QUERY_CHARS = 1200
MAX_SECRET_CHARS = 256
MAX_START_MS = 7 * 24 * 3600 * 1000
MAX_PENDING_APPROVALS = 32
SEARCH_MODES = ("exact", "fuzzy", "semantic", "topic", "visual", "multimodal")
#: The multimodal capability (qronos_media.multimodal), listed with the stage capabilities; never claimed verified.
MULTIMODAL_CAPABILITY = {"capability": "multimodal_search", "level": 3, "status": "implemented_unverified",
                         "reason": "Dialogue AND generated-visual-text constraints fused by time in one asset; "
                                   "no speaker, OCR or image-encoder evidence."}

#: Operations that only read (allowed while Qronos is paused). Everything else is refused while paused.
READ_OPS = frozenset({"status", "roots.list", "search", "asset.detail", "capabilities", "decline", "report.preview",
                      "search.report.preview"})
OPS = frozenset({
    "status", "capabilities", "roots.list", "roots.add", "roots.remove", "scan", "index.start", "index.stop",
    "index.pause", "index.resume", "job.retry", "job.reindex", "job.cancel", "job.pause", "job.resume",
    "asset.priority", "asset.detail", "search", "open.prepare", "report.preview", "report.generate", "report.export",
    "decide", "decline", "search.report.preview", "search.report.generate", "search.report.export",
})


class MediaOpError(ValueError):
    """A malformed or refused request; ``code`` is a stable word for the desktop."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def _text(data: dict, name: str, limit: int, *, required: bool = True) -> str | None:
    value = data.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise MediaOpError("bad_request", f"{name} must be 1..{limit} characters")
    return value


def _int(data: dict, name: str, low: int, high: int, *, default: int | None = None) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not (low <= value <= high):
        raise MediaOpError("bad_request", f"{name} must be an integer in {low}..{high}")
    return value


def _asset(data: dict) -> str:
    value = data.get("assetId")
    if not isinstance(value, str) or not _ASSET_ID.match(value):
        raise MediaOpError("bad_request", "assetId is malformed")
    return value


def _stage(data: dict) -> str:
    value = data.get("stage")
    if value not in STAGES:
        raise MediaOpError("bad_request", "unknown stage")
    return value


def capabilities() -> dict[str, Any]:
    """Honest capability levels: implemented-unverified stages, and what is not built (never claimed)."""
    return {"capabilities": [dict(item) for item in PENDING_CAPABILITIES] + [dict(MULTIMODAL_CAPABILITY)],
            "stages": {name: {"level": spec.level, "version": spec.version} for name, spec in STAGES.items()},
            "reports": {"formats": list(FORMATS), "kinds": list(KINDS)}}


class MediaProduct:
    """Validates one request, runs it on the host and answers through ``emit(event, status, payload)``."""

    def __init__(self, host: Any, emit: Callable[[str, str, dict[str, Any]], None],
                 paused: Callable[[], bool] = lambda: False) -> None:
        self.host = host
        self.emit = emit
        self.paused = paused
        self._lock = threading.Lock()
        # approvalId -> (operation family, gate decision) for ids THIS route issued; single-use.
        self._approvals: OrderedDict[str, tuple[str, str]] = OrderedDict()
        host.on_index_report = self._progress

    # ---- entry

    def handle(self, data: object, *, reply: Callable[[dict[str, Any]], None] | None = None) -> None:
        """One desktop/brain request. Every outcome (also a refusal) is answered once."""
        if not isinstance(data, dict):
            self._answer({"requestId": None, "op": None, "ok": False, "code": "bad_request"}, reply)
            return
        request_id = data.get("requestId")
        op = data.get("op")
        base = {"requestId": request_id if isinstance(request_id, str) and _REQUEST_ID.match(request_id) else None,
                "op": op if op in OPS else None}
        try:
            if base["requestId"] is None:
                raise MediaOpError("bad_request", "requestId is malformed")
            if base["op"] is None:
                raise MediaOpError("unknown_op")
            if op not in READ_OPS and self.paused():
                raise MediaOpError("paused", "Qronos is paused")
            self._dispatch(op, data, lambda result: self._answer({**base, **result}, reply))
        except MediaOpError as error:
            self._answer({**base, "ok": False, "code": error.code, "detail": str(error)[:200]}, reply)

    def _answer(self, payload: dict[str, Any], reply: Callable[[dict[str, Any]], None] | None) -> None:
        payload.pop("secret", None)
        if reply is not None:
            try:
                reply(payload)
            except Exception:  # noqa: BLE001
                pass
        self.emit(RESULT_EVENT, "ready" if payload.get("ok") else "warning", payload)

    def _progress(self, report: dict[str, Any]) -> None:
        self.emit(PROGRESS_EVENT, "ready", {"report": _bounded_report(report), "status": self.host.status()})

    # ---- dispatch

    def _dispatch(self, op: str, data: dict, done: Callable[[dict[str, Any]], None]) -> None:
        host = self.host
        if op == "status":
            done({"ok": True, "status": _status(host.status())})
            return
        if op == "capabilities":
            done({"ok": True, **capabilities()})
            return
        if op == "index.start":
            done({"ok": True, **host.submit_index(max_seconds=20.0, max_stages=50, continuous=True)})
            return
        if op == "index.stop":
            host.preempt("user_stop")
            done({"ok": True, "stopped": True})
            return
        if op == "search":
            mode = data.get("mode", "exact")
            if mode not in SEARCH_MODES:
                raise MediaOpError("bad_request", f"mode must be one of {', '.join(SEARCH_MODES)}")
            if mode == "multimodal":
                self._submit(host.query, op, _multimodal(data), done)
                return
            query = _text(data, "query", MAX_QUERY_CHARS)
            limit = _int(data, "limit", 1, 50, default=20)
            language = _text(data, "language", 16, required=False)
            hints = data.get("brainHints") is True and mode == "topic"  # generated hints only when asked
            self._submit(host.query, op, lambda s: _search(s, query, mode, limit, language, hints), done)
            return
        if op == "report.preview":
            asset, kind = _asset(data), _kind(data)
            self._submit(host.query, op, lambda s: {"ok": True, "preview": s.preview_report(asset, kind=kind)}, done)
            return
        if op == "decline":
            approval_id = self._approval_id(data)
            family, _ = self._take_approval(approval_id)
            self._submit(host.call, op, lambda s: (s.permissions.decline(approval_id), {"ok": True, "declined": True,
                                                                                         "family": family})[1], done)
            return
        if op == "decide":
            approval_id = self._approval_id(data)
            if data.get("approved") is not True:
                raise MediaOpError("bad_request", "use decline to refuse")
            secret = data.get("secret")
            if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET_CHARS):
                raise MediaOpError("bad_request", "secret must be a short string")  # never echoed
            family, decision = self._take_approval(approval_id)
            self._submit(host.call, op, lambda s: self._decide(s, family, approval_id, decision, secret), done)
            return
        handler = self._writer_ops().get(op)
        if handler is None:  # pragma: no cover - OPS and the table agree
            raise MediaOpError("unknown_op")
        call = handler(data)
        self._submit(host.call, op, call, done)

    def _writer_ops(self) -> dict[str, Callable[[dict], Callable[[Any], dict[str, Any]]]]:
        return {
            "roots.list": lambda d: lambda s: {"ok": True, "roots": s.list_roots()},
            "roots.add": self._roots_add,
            "roots.remove": lambda d: (lambda root_id: lambda s: {"ok": True, **s.remove_root(root_id)})(
                _int(d, "rootId", 1, 2**31)),
            "scan": self._scan,
            "index.pause": lambda d: lambda s: {"ok": True, **s.pause()},
            "index.resume": self._resume,
            "job.retry": lambda d: self._job(d, "retry"),
            "job.reindex": lambda d: self._job(d, "reindex"),
            "job.cancel": lambda d: self._job(d, "cancel_job"),
            "job.pause": lambda d: self._job(d, "pause_job"),
            "job.resume": lambda d: self._job(d, "resume_job"),
            "asset.priority": lambda d: (lambda a, p: lambda s: {"ok": True, **s.prioritize(a, p)})(
                _asset(d), _int(d, "priority", -100, 100)),
            "asset.detail": lambda d: (lambda a: lambda s: {"ok": True, "asset": s.get_asset(a)})(_asset(d)),
            "open.prepare": self._open_prepare,
            "report.generate": self._report_generate,
            "report.export": self._report_export,
            # The report of one HELD multimodal answer: only its server-issued resultId is accepted.
            "search.report.preview": lambda d: (lambda r: lambda s: {"ok": True, "preview": s.preview_search_report(r)})(
                _result_id(d)),
            "search.report.generate": lambda d: (lambda r, f: lambda s: {"ok": True, "report": s.generate_search_report(
                r, fmt=f)})(_result_id(d), _format(d)),
            "search.report.export": lambda d: (lambda r, f, dest: lambda s: _ok(s.export_search_report(
                r, dest, fmt=f), "export"))(_result_id(d), _format(d), _text(d, "destination", MAX_PATH_CHARS)),
        }

    def _submit(self, method: Callable, op: str, fn: Callable[[Any], dict[str, Any]],
                done: Callable[[dict[str, Any]], None]) -> None:
        def guarded(service: Any) -> dict[str, Any]:
            try:
                return fn(service)
            except MediaOpError as error:
                return {"ok": False, "code": error.code, "detail": str(error)[:200]}
            except Exception as error:  # noqa: BLE001 - typed below
                return _typed_failure(error)

        def finish(result: dict[str, Any]) -> None:
            done(self._track(op, result))

        accepted = method(op, guarded, finish)
        if not accepted.get("accepted"):
            done({"ok": False, "code": accepted.get("reason", "refused")})

    # ---- operations

    def _roots_add(self, data: dict) -> Callable[[Any], dict[str, Any]]:
        path = _text(data, "path", MAX_PATH_CHARS)
        kind = data.get("kind", "folder")
        if kind not in ("folder", "file"):
            raise MediaOpError("bad_request", "kind must be folder or file")
        depth = _int(data, "maxDepth", 0, 64, default=8)
        return lambda s: _ok(s.add_root(path, kind=kind, max_depth=depth), "roots")

    def _scan(self, data: dict) -> Callable[[Any], dict[str, Any]]:
        root_id = None if data.get("rootId") is None else _int(data, "rootId", 1, 2**31)

        def run(service: Any) -> dict[str, Any]:
            summaries = service.scan(root_id)
            queued = self.host.submit_index(max_seconds=20.0, max_stages=50, continuous=True)
            return {"ok": True, "scan": summaries, "index": queued}

        return run

    def _resume(self, data: dict) -> Callable[[Any], dict[str, Any]]:
        def run(service: Any) -> dict[str, Any]:
            resumed = service.resume()
            return {"ok": True, **resumed, "index": self.host.submit_index(max_seconds=20.0, max_stages=50,
                                                                            continuous=True)}
        return run

    def _job(self, data: dict, method: str) -> Callable[[Any], dict[str, Any]]:
        asset, stage = _asset(data), _stage(data)
        return lambda s: {"ok": True, **getattr(s, method)(asset, stage)}

    def _open_prepare(self, data: dict) -> Callable[[Any], dict[str, Any]]:
        asset, start = _asset(data), _int(data, "startMs", 0, MAX_START_MS)
        return lambda s: _ok(s.prepare_open(asset, start), "open")

    def _report_generate(self, data: dict) -> Callable[[Any], dict[str, Any]]:
        asset, fmt, kind = _asset(data), _format(data), _kind(data)
        return lambda s: {"ok": True, "report": s.generate_report(asset, fmt=fmt, kind=kind)}

    def _report_export(self, data: dict) -> Callable[[Any], dict[str, Any]]:
        asset, fmt, kind = _asset(data), _format(data), _kind(data)
        destination = _text(data, "destination", MAX_PATH_CHARS)
        return lambda s: _ok(s.export_report(asset, destination, fmt=fmt, kind=kind), "export")

    @staticmethod
    def _decide(service: Any, family: str, approval_id: str, decision: str, secret: str | None) -> dict[str, Any]:
        if family == "roots":
            return _ok(service.approve_root(approval_id, decision, secret=secret), "roots")
        if family == "export":
            return _ok(service.approve_export(approval_id, decision, secret=secret), "export")
        return _ok(service.approve_open(approval_id, decision, secret=secret), "open")

    # ---- approvals issued by this route

    def _approval_id(self, data: dict) -> str:
        value = data.get("approvalId")
        if not isinstance(value, str) or not _APPROVAL_ID.match(value):
            raise MediaOpError("bad_request", "approvalId is malformed")
        return value

    def _take_approval(self, approval_id: str) -> tuple[str, str]:
        with self._lock:
            held = self._approvals.pop(approval_id, None)
        if held is None:
            raise MediaOpError("stale_approval", "unknown or already answered")
        return held

    def _track(self, op: str, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("status") == "approval_required" and isinstance(result.get("approvalId"), str):
            family = result.pop("_family", None) or {"roots.add": "roots", "report.export": "export",
                                                      "search.report.export": "export",
                                                      "open.prepare": "open"}.get(op)
            decision = str(result.get("decision", ""))
            if family is not None:
                with self._lock:
                    self._approvals[result["approvalId"]] = (family, decision)
                    while len(self._approvals) > MAX_PENDING_APPROVALS:
                        self._approvals.popitem(last=False)
            result["needsSecret"] = decision == "require_typed_secret"
        result.pop("_family", None)
        return result


def _result_id(data: dict) -> str:
    value = data.get("resultId")
    if not isinstance(value, str) or not _RESULT_ID.match(value):
        raise MediaOpError("bad_request", "resultId is malformed")
    return value


def _format(data: dict) -> str:
    fmt = data.get("format", "json")
    if fmt not in FORMATS:
        raise MediaOpError("bad_request", f"format must be one of {', '.join(FORMATS)}")
    return fmt


def _kind(data: dict) -> str:
    kind = data.get("kind", "full")
    if kind not in KINDS:
        raise MediaOpError("bad_request", f"kind must be one of {', '.join(KINDS)}")
    return kind


def _ok(result: dict[str, Any], family: str) -> dict[str, Any]:
    out = {"ok": True, **result}
    if result.get("status") == "approval_required":
        out["_family"] = family
    return out


def _search(service: Any, query: str, mode: str, limit: int, language: str | None,
            brain_hints: bool = False) -> dict[str, Any]:
    if mode == "semantic":
        return {"ok": True, **service.search_semantic(query, limit=limit, language=language)}
    if mode == "topic":
        return {"ok": True, **service.search_topics(query, limit=limit, language=language, brain_hints=brain_hints)}
    if mode == "visual":  # the separate visual channel; the exact dialogue answer comes first, unchanged
        return {"ok": True, **service.search_visual(query, limit=limit)}
    return {"ok": True, "results": service.search_dialogue(query, mode=mode, limit=limit, language=language)}


def _multimodal(data: dict) -> Callable[[Any], dict[str, Any]]:
    """Typed multimodal arguments: BOTH explicit constraints (searched, no model inference needed), or one natural
    ``query`` only (a proposed split is returned for the person to confirm; nothing is searched)."""
    from qronos_media import multimodal

    if "dialogue" in data or "visual" in data:
        dialogue = _text(data, "dialogue", multimodal.MAX_CONSTRAINT_CHARS)
        visual = _text(data, "visual", multimodal.MAX_CONSTRAINT_CHARS)
        window = _int(data, "windowMs", 0, multimodal.MAX_WINDOW_MS, default=multimodal.DEFAULT_WINDOW_MS)
        dialogue_match = data.get("dialogueMatch", "exact")
        visual_match = data.get("visualMatch", "lexical")
        if dialogue_match not in multimodal.DIALOGUE_MATCHES or visual_match not in multimodal.VISUAL_MATCHES:
            raise MediaOpError("bad_request", "dialogueMatch is exact, fuzzy or semantic; visualMatch lexical, semantic or image")
        if "query" in data:
            raise MediaOpError("bad_request", "send either the two constraints or one query to split, not both")
        limit = _int(data, "limit", 1, 50, default=20)
        return lambda s: {"ok": True, **s.search_multimodal(dialogue, visual, window_ms=window,
                                                             dialogue_match=dialogue_match, visual_match=visual_match,
                                                             limit=limit)}
    query = _text(data, "query", MAX_QUERY_CHARS)
    return lambda s: {"ok": True, **s.interpret_multimodal(query)}


def _typed_failure(error: Exception) -> dict[str, Any]:
    from qronos_media.permissions import MediaPermissionRefused
    from qronos_media.resources import ResourceRefused

    if isinstance(error, MediaPermissionRefused):
        return {"ok": False, "code": error.reason}
    if isinstance(error, ResourceRefused):
        return {"ok": False, "code": error.reason}
    name = type(error).__name__
    if name in ("MediaRequestError", "SearchError"):
        code = next((word for word in ("report_unavailable", "result_unavailable") if str(error).startswith(word)),
                    "refused")
        return {"ok": False, "code": code, "detail": str(error)[:200]}
    return {"ok": False, "code": "internal", "detail": name}


def _status(raw: dict[str, Any]) -> dict[str, Any]:
    keep = ("workerStarted", "queued", "running", "runs", "storeOpen", "closing", "queries", "store", "authority")
    out = {key: raw.get(key) for key in keep}
    out["lastReport"] = _bounded_report(raw.get("lastReport") or {})
    return out


def _bounded_report(report: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key in ("stopped", "detail", "hashed", "continuing", "code"):
        if key in report:
            out[key] = report[key] if not isinstance(report[key], str) else report[key][:300]
    for key in ("completed", "failed", "refused", "identityChanged", "outOfScope"):
        if isinstance(report.get(key), list):
            out[key] = report[key][:50]
            out[key + "Count"] = len(report[key])
    return out
