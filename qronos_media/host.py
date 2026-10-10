"""The in-process media host: one bounded background worker inside the Qronos bridge process.

There is no media server, no second governor and no cross-process model
service. The bridge builds one :class:`MediaHost` lazily, the first time media
work is asked for (:func:`build_bridge_media_host`), from its own objects:

* the queue scheduler's ``ResourceGovernor`` and the shared
  ``GLOBAL_RESOURCE_LEDGER`` become a runtime-issued
  :class:`qronos_media.model_authority.HostedModelAuthority`;
* the bridge's ``ApprovalLedger`` and action audit back the media Files
  permission (``GatePermissions``), so media approvals are the same single-use
  prompts every other Qronos approval is;
* the foreground signal (a voice/text/screen turn in progress) and the global
  Pause are read live from the runtime.

Importing this module starts nothing: no thread, no activity reading, no
device, no model, no store. The worker thread starts with the first request.
Before the media store opens, the store's ACTUAL resolved folder goes through
the existing start-up admission (``core.startup_admission.enter_for_store``);
a refusal leaves the store unopened and is reported.

Requests are bounded (a short queue, a time and stage budget per run). Stop,
shutdown and revocation preempt the running stage at its next check; the
stage is requeued with its checkpoint, and any model child is torn down and
its exit proven before its reservation is freed.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import threading
import time
from typing import Any, Callable

from qronos_media.model_authority import HostedModelAuthority, is_issued, issue_hosted_authority

MAX_QUEUED_REQUESTS = 4
MAX_RUN_SECONDS = 600.0
MAX_RUN_STAGES = 200
JOIN_SECONDS = 10.0


class MediaHostClosed(RuntimeError):
    pass


#: The index database inside the media folder and the side files SQLite opens next to it.
STORE_FILE = "media-index.sqlite3"
STORE_SIDE_FILES = ("-wal", "-shm", "-journal")


def media_store_admission(root: Path) -> Any:
    """The media store's own start-up admission on the ACTUAL database file, before anything opens.

    The database path itself goes through ``enter_for_store`` (its folder's targeting is the same: the
    file lies in it), so a database that is a file with more than one name (a hard link into installed
    data) is refused even when its folder is an ordinary outside folder (H20-1). SQLite's side files that
    already exist beside it are checked the same way, since SQLite opens them by name too. A store that
    does not exist yet is a missing leaf and is admitted like any fresh store.
    """
    from core import startup_admission

    database = Path(root) / STORE_FILE
    admission = startup_admission.enter_for_store("media", "QRONOS_MEDIA_DATA_DIR", database)
    for suffix in STORE_SIDE_FILES:
        side = Path(str(database) + suffix)
        if side.exists():
            startup_admission.enter_for_store("media", "QRONOS_MEDIA_DATA_DIR", side)
    return admission


class MediaHost:
    """One worker thread running bounded media index passes under the runtime-issued authority."""

    def __init__(
        self,
        authority: HostedModelAuthority,
        *,
        data_dir: Callable[[], Path],
        tools: Callable[[], Any],
        permissions: Callable[[], Any],
        admit_store: Callable[[Path], Any] = media_store_admission,
        service_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not is_issued(authority):
            # A DTO, a Boolean, a flag or a home-made object is never a model authority.
            raise TypeError("the media host needs the runtime-issued HostedModelAuthority")
        self.authority = authority
        self._data_dir = data_dir
        self._tools = tools
        self._permissions = permissions
        self._admit_store = admit_store
        self._service_factory = service_factory
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._requests: deque[dict[str, Any]] = deque()
        self._thread: threading.Thread | None = None
        self._closing = False
        self._service: Any = None
        self._running: dict[str, Any] | None = None
        self.last_report: dict[str, Any] | None = None
        self.runs = 0

    # ---- requests (any thread)

    def submit_index(self, *, max_seconds: float = 120.0, max_stages: int = 50) -> dict[str, Any]:
        request = {"maxSeconds": max(1.0, min(MAX_RUN_SECONDS, float(max_seconds))),
                   "maxStages": max(1, min(MAX_RUN_STAGES, int(max_stages))), "at": time.time()}
        with self._lock:
            if self._closing:
                return {"accepted": False, "reason": "closed"}
            if len(self._requests) >= MAX_QUEUED_REQUESTS:
                return {"accepted": False, "reason": "queue_full"}
            # A new explicit request is the person asking again after a Stop.
            self.authority.clear_preemption()
            self._requests.append(request)
            self._ensure_thread()
            self._wake.notify()
            return {"accepted": True, "queued": len(self._requests)}

    def preempt(self, reason: str) -> None:
        """Stop/shutdown/revocation: the running stage stops at its next check; nothing new starts."""
        self.authority.preempt(reason)
        with self._lock:
            self._requests.clear()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"workerStarted": self._thread is not None, "queued": len(self._requests),
                    "running": dict(self._running) if self._running else None, "runs": self.runs,
                    "storeOpen": self._service is not None, "lastReport": self.last_report,
                    "authority": self.authority.status(), "closing": self._closing}

    def close(self, timeout: float = JOIN_SECONDS) -> None:
        with self._lock:
            self._closing = True
            self._requests.clear()
            self._wake.notify_all()
            thread = self._thread
        self.authority.close()  # preempts: a running stage stops and its model child is torn down
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            service, self._service = self._service, None
        if service is not None:
            try:
                service.close()
            except Exception:  # noqa: BLE001 - shutting down
                pass
        self.authority.reconcile()

    # ---- worker

    def _ensure_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="qronos-media-host", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            with self._lock:
                while not self._requests and not self._closing:
                    self._wake.wait(1.0)
                if self._closing:
                    return
                request = self._requests.popleft()
                self._running = request
            try:
                report = self._run(request)
            except Exception as exc:  # noqa: BLE001 - one failed pass never kills the host
                report = {"stopped": "error", "detail": f"{type(exc).__name__}: {exc}"[:500]}
            finally:
                self.authority.reconcile()
            with self._lock:
                self._running = None
                self.last_report = report
                self.runs += 1

    def _open(self) -> Any:
        if self._service is not None:
            return self._service
        from qronos_media.paths import local_path
        from qronos_media.service import MediaService

        root = local_path(self._data_dir())
        self._admit_store(root)  # StartupRefused: nothing below runs, nothing opens
        factory = self._service_factory or MediaService
        self._service = factory(root, tools=self._tools(), authority=self.authority,
                                permissions=self._permissions())
        return self._service

    def _run(self, request: dict[str, Any]) -> dict[str, Any]:
        from core.startup_admission import StartupRefused

        reason = self.authority.yield_reason()
        if reason is not None:
            return {"stopped": "deferred", "detail": f"deferred:{reason}"}
        try:
            service = self._open()
        except StartupRefused as refused:
            return {"stopped": "store_admission", "code": refused.code, "detail": str(refused)}
        return service.index(max_seconds=request["maxSeconds"], max_stages=request["maxStages"])


def build_bridge_media_host(
    *,
    governor: Any,
    ledger: Any,
    foreground_busy: Callable[[], bool],
    paused: Callable[[], bool],
    approvals: Any,
    audit: Any,
    activity: Any = None,
) -> MediaHost:
    """The bridge's one media host, bound to the bridge's own governor, ledger, approvals and audit."""
    from qronos_media.service import default_data_dir, tools_from_environment

    data_dir = default_data_dir()
    authority = issue_hosted_authority(governor=governor, ledger=ledger, disk_root=data_dir,
                                       foreground_busy=foreground_busy, paused=paused, activity=activity)

    def permissions() -> Any:
        from qronos_media.permissions import GatePermissions

        return GatePermissions(audit=audit, ledger=approvals)

    return MediaHost(authority, data_dir=default_data_dir, tools=tools_from_environment, permissions=permissions)
