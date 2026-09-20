from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable, Protocol

from core.windows_app_activation import activate_application

from core.action_audit import ActionAuditLog, AuditEvent
from core.actions import (
    ActionOutcome,
    ActionRequest,
    ActionResult,
    InvalidAction,
)
from core.undo_journal import Reversal, UndoEntry, UndoJournal
from core.window_binding import (
    WindowBinder,
    WindowBinding,
    WindowBindingAmbiguous,
    WindowBindingStale,
    WindowBindingUnavailable,
    WindowSnapshot,
    WindowTarget,
)
from security.gate import Verdict, evaluate
from security.permissions import ActionCategory, PermissionDecision


class ComputerOperation(Enum):
    """Typed Windows-control vocabulary."""

    OPEN_APP = "open_app"
    CLOSE_APP = "close_app"
    FOCUS_WINDOW = "focus_window"
    MINIMIZE_WINDOW = "minimize_window"
    MAXIMIZE_WINDOW = "maximize_window"
    OPEN_LOCATION = "open_location"
    DRAW_BASIC_HOUSE = "draw_basic_house"


@dataclass(frozen=True)
class AppSpec:
    app_id: str
    executable: str
    display_name: str
    shortcut: str = ""
    aumid: str = ""
    aliases: tuple[str, ...] = ()
    process_names: tuple[str, ...] = ()


KNOWN_APPS: dict[str, AppSpec] = {
    "notepad": AppSpec(
        app_id="notepad",
        executable="notepad.exe",
        display_name="Notepad",
        aliases=("notepad", "note pad", "نوت پد", "نوت‌پد", "دفترچه یادداشت"),
        process_names=("notepad.exe",),
    ),
    "calculator": AppSpec(
        app_id="calculator",
        executable="calc.exe",
        display_name="Calculator",
        aliases=("calculator", "calc", "ماشین حساب", "ماشین‌حساب"),
        process_names=("calculatorapp.exe", "calculator.exe", "calc.exe"),
    ),
    "file_explorer": AppSpec(
        app_id="file_explorer",
        executable="explorer.exe",
        display_name="File Explorer",
        aliases=("file explorer", "windows explorer", "explorer", "فایل اکسپلورر", "فایل‌اکسپلورر", "اکسپلورر"),
        process_names=("explorer.exe",),
    ),
    "paint": AppSpec(
        app_id="paint",
        executable="mspaint.exe",
        display_name="Paint",
        aumid="Microsoft.Paint_8wekyb3d8bbwe!App",
        aliases=("microsoft paint", "mspaint", "paint", "پینت", "پاینت", "نقاشی"),
        process_names=("mspaint.exe",),
    ),
}

_BLOCKED_EXECUTABLES = {
    "cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe", "wscript.exe",
    "cscript.exe", "mshta.exe", "regedit.exe", "rundll32.exe",
}

_COMMON_APP_ALIASES: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    (("telegram",), ("تلگرام",), ("telegram.exe",)),
    (("discord",), ("دیسکورد",), ("discord.exe",)),
    (("premiere",), ("پریمیر", "پریمیر پرو", "premiere pro"), ("adobe premiere pro.exe",)),
    (("photoshop",), ("فتوشاپ", "فوتوشاپ", "ادوبی فتوشاپ", "ادوبی فوتوشاپ", "ادوب فتوشاپ"), ("photoshop.exe",)),
    (("chrome",), ("کروم", "گوگل کروم"), ("chrome.exe",)),
    (("firefox",), ("فایرفاکس",), ("firefox.exe",)),
    (("steam",), ("استیم",), ("steam.exe",)),
    (("spotify",), ("اسپاتیفای",), ("spotify.exe",)),
    (("vlc",), ("وی ال سی", "وی‌ال‌سی"), ("vlc.exe",)),
    (("visual", "studio", "code"), ("وی اس کد", "vscode", "vs code"), ("code.exe",)),
    (("word",), ("ورد", "مایکروسافت ورد"), ("winword.exe",)),
    (("excel",), ("اکسل",), ("excel.exe",)),
    (("powerpoint",), ("پاورپوینت",), ("powerpnt.exe",)),
)

_CATALOG_CACHE: tuple[float, dict[str, AppSpec]] | None = None
_CATALOG_TTL_SECONDS = 20.0


def _norm(value: str) -> str:
    text = str(value or "").replace("\u200c", " ")
    text = text.replace("ي", "ی").replace("ك", "ک")
    text = re.sub(r"[^\w\u0600-\u06ff]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _trusted_start_menu_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA", "").strip()
    programdata = os.environ.get("ProgramData", "").strip()
    if appdata:
        roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if programdata:
        roots.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.path.normcase(os.path.abspath(str(root)))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return tuple(unique)


def _aliases_for_display(display: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    n = _norm(display)
    aliases: list[str] = [display]
    processes: list[str] = []
    for required, extra_aliases, process_names in _COMMON_APP_ALIASES:
        if all(token in n for token in required):
            aliases.extend(extra_aliases)
            processes.extend(process_names)
    for suffix in (" desktop", " app", " application"):
        if n.endswith(suffix):
            aliases.append(n[: -len(suffix)])
    out_aliases: list[str] = []
    seen_alias: set[str] = set()
    for alias in aliases:
        key = _norm(alias)
        if key and key not in seen_alias:
            seen_alias.add(key)
            out_aliases.append(alias)
    out_processes: list[str] = []
    seen_proc: set[str] = set()
    for process in processes:
        key = process.lower()
        if key and key not in seen_proc:
            seen_proc.add(key)
            out_processes.append(key)
    return tuple(out_aliases), tuple(out_processes)


def _dynamic_id(kind: str, target: str) -> str:
    digest = hashlib.sha256(
        (kind + "\0" + os.path.normcase(os.path.abspath(target))).encode("utf-8", "surrogatepass")
    ).hexdigest()[:20]
    return f"discovered:{kind}:{digest}"


def _discover_start_menu_apps() -> dict[str, AppSpec]:
    found: dict[str, AppSpec] = {}
    if os.name != "nt":
        return found
    count = 0
    for root in _trusted_start_menu_roots():
        if not root.is_dir():
            continue
        try:
            iterator = root.rglob("*.lnk")
            for shortcut in iterator:
                count += 1
                if count > 6000:
                    return found
                try:
                    if not shortcut.is_file():
                        continue
                except OSError:
                    continue
                display = shortcut.stem.strip()
                if not display:
                    continue
                aliases, processes = _aliases_for_display(display)
                app_id = _dynamic_id("shortcut", str(shortcut))
                found[app_id] = AppSpec(
                    app_id=app_id,
                    executable="",
                    display_name=display,
                    shortcut=str(shortcut.resolve(strict=False)),
                    aliases=aliases,
                    process_names=processes,
                )
        except OSError:
            continue
    return found


def _discover_app_paths() -> dict[str, AppSpec]:
    found: dict[str, AppSpec] = {}
    if os.name != "nt":
        return found
    try:
        import winreg
    except Exception:
        return found

    locations = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
    )
    for hive, base in locations:
        try:
            parent = winreg.OpenKey(hive, base)
        except OSError:
            continue
        with parent:
            index = 0
            while index < 5000:
                try:
                    subname = winreg.EnumKey(parent, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(parent, subname) as sub:
                        target, _ = winreg.QueryValueEx(sub, "")
                except OSError:
                    continue
                target = os.path.expandvars(str(target or "").strip().strip('"'))
                if not target:
                    continue
                path = Path(target)
                try:
                    if not path.is_file():
                        continue
                except OSError:
                    continue
                exe_name = path.name.lower()
                if exe_name in _BLOCKED_EXECUTABLES:
                    continue
                display = path.stem
                aliases, extra_processes = _aliases_for_display(display)
                processes = tuple(dict.fromkeys((exe_name, *extra_processes)))
                app_id = _dynamic_id("exe", str(path))
                found[app_id] = AppSpec(
                    app_id=app_id,
                    executable=str(path.resolve(strict=False)),
                    display_name=display,
                    aliases=aliases,
                    process_names=processes,
                )
    return found


def application_catalog(*, refresh: bool = False) -> dict[str, AppSpec]:
    global _CATALOG_CACHE
    now = time.monotonic()
    if (
        not refresh
        and _CATALOG_CACHE is not None
        and now - _CATALOG_CACHE[0] <= _CATALOG_TTL_SECONDS
    ):
        return dict(_CATALOG_CACHE[1])

    catalog = dict(KNOWN_APPS)
    if os.name == "nt":
        catalog.update(_discover_app_paths())
        catalog.update(_discover_start_menu_apps())
    _CATALOG_CACHE = (now, dict(catalog))
    return catalog


def get_app_spec(app_id: str) -> AppSpec:
    if app_id in KNOWN_APPS:
        return KNOWN_APPS[app_id]
    spec = application_catalog().get(app_id)
    if spec is None:
        spec = application_catalog(refresh=True).get(app_id)
    if spec is None:
        raise InvalidAction(f"Unknown or unavailable application: {app_id!r}.")
    return spec


def _app_score(query: str, spec: AppSpec) -> int:
    q = _norm(query)
    if not q:
        return 0
    choices = (spec.display_name, *spec.aliases)
    best = 0
    for value in choices:
        c = _norm(value)
        if not c:
            continue
        if c == q:
            best = max(best, 1000)
        elif c.startswith(q) or q.startswith(c):
            best = max(best, 950)
        elif q in c or c in q:
            best = max(best, 900)
        else:
            q_tokens = q.split()
            c_tokens = c.split()
            if q_tokens and all(token in c_tokens for token in q_tokens):
                best = max(best, 860)
    return best


def resolve_application_id(query: str, *, dynamic: bool = True) -> str:
    catalog = application_catalog() if dynamic else dict(KNOWN_APPS)
    scored = [(_app_score(query, spec), app_id, spec) for app_id, spec in catalog.items()]
    scored = [item for item in scored if item[0] > 0]
    if not scored:
        raise LookupError(f"No installed application matched {query!r}.")
    scored.sort(
        key=lambda item: (
            -item[0],
            0 if item[2].executable else 1,
            _norm(item[2].display_name),
            item[1],
        )
    )
    best_score, best_id, best_spec = scored[0]
    for second in scored[1:]:
        if second[0] != best_score:
            break
        # A shortcut and an executable may advertise one installation under
        # different labels. Require the shortcut label to match the executable
        # or its installation directory, not merely a shared process basename:
        # two installed Photoshop versions legitimately share Photoshop.exe.
        same_target = False
        if best_spec.executable and second[2].shortcut:
            labels = {
                _norm(Path(best_spec.executable).stem),
                _norm(Path(best_spec.executable).parent.name),
            }
            same_target = _norm(second[2].display_name) in labels
        elif best_spec.executable and second[2].executable:
            same_target = os.path.normcase(best_spec.executable) == os.path.normcase(second[2].executable)
        elif best_spec.shortcut and second[2].shortcut:
            same_target = os.path.normcase(best_spec.shortcut) == os.path.normcase(second[2].shortcut)
        if second[1] != best_id and not same_target:
            raise LookupError(
                f"More than one installed application matched {query!r}: "
                f"{best_spec.display_name}, {second[2].display_name}."
            )
    return best_id


@dataclass(frozen=True)
class ComputerAction:
    operation: ComputerOperation
    app_id: str = ""
    location: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.operation, ComputerOperation):
            raise InvalidAction("ComputerAction requires a ComputerOperation.")
        if self.operation is ComputerOperation.OPEN_LOCATION:
            raw = self.location.strip()
            if not raw:
                raise InvalidAction("OPEN_LOCATION requires a directory path.")
            if not Path(raw).is_absolute():
                raise InvalidAction("OPEN_LOCATION requires an absolute directory path.")
            if self.app_id.strip():
                raise InvalidAction("OPEN_LOCATION does not accept an app_id.")
            return
        if self.operation in {
            ComputerOperation.OPEN_APP,
            ComputerOperation.CLOSE_APP,
            ComputerOperation.FOCUS_WINDOW,
            ComputerOperation.MINIMIZE_WINDOW,
            ComputerOperation.MAXIMIZE_WINDOW,
            ComputerOperation.DRAW_BASIC_HOUSE,
        }:
            app_id = self.app_id.strip()
            if not app_id:
                raise InvalidAction(f"{self.operation.value} requires an app_id.")
            if app_id not in KNOWN_APPS and not app_id.startswith("discovered:"):
                raise InvalidAction(f"Unknown or unapproved application: {self.app_id!r}.")
            if (
                self.operation is ComputerOperation.DRAW_BASIC_HOUSE
                and app_id != "paint"
            ):
                raise InvalidAction("DRAW_BASIC_HOUSE is restricted to Paint.")
            if self.location.strip():
                raise InvalidAction(f"{self.operation.value} does not accept a location argument.")
            return
        raise InvalidAction(f"Unsupported computer operation: {self.operation!r}.")

    @property
    def category(self) -> ActionCategory:
        if self.operation is ComputerOperation.DRAW_BASIC_HOUSE:
            return ActionCategory.GUI_AUTOMATION
        return ActionCategory.OPEN_APPLICATION

    def to_request(self) -> ActionRequest:
        if self.operation is ComputerOperation.OPEN_LOCATION:
            path = Path(self.location).resolve(strict=False)
            return ActionRequest(
                category=self.category,
                target=str(path),
                summary="Open one exact local directory in Windows Explorer.",
                parameters={"operation": self.operation.value, "location": str(path)},
            )
        spec = get_app_spec(self.app_id)
        summaries = {
            ComputerOperation.OPEN_APP: f"Open application {spec.display_name}.",
            ComputerOperation.CLOSE_APP: f"Close application window {spec.display_name}.",
            ComputerOperation.FOCUS_WINDOW: f"Focus application window {spec.display_name}.",
            ComputerOperation.MINIMIZE_WINDOW: f"Minimize application window {spec.display_name}.",
            ComputerOperation.MAXIMIZE_WINDOW: f"Maximize application window {spec.display_name}.",
            ComputerOperation.DRAW_BASIC_HOUSE: (
                f"Draw one bounded basic house template in {spec.display_name}."
            ),
        }
        return ActionRequest(
            category=self.category,
            target=f"app:{spec.app_id}",
            summary=summaries[self.operation],
            parameters={"operation": self.operation.value, "appId": spec.app_id},
        )


@dataclass(frozen=True)
class PreparedComputerAction:
    action: ComputerAction
    request: ActionRequest
    verdict: Verdict


@dataclass(frozen=True)
class ComputerExecution:
    request: ActionRequest
    verdict: Verdict
    result: ActionResult
    undo_entry: UndoEntry | None = None
    window_binding: WindowBinding | None = None


class ProcessLike(Protocol):
    pid: int
    def poll(self) -> int | None: ...


EvaluateFn = Callable[..., Verdict]
LaunchFn = Callable[[tuple[str, ...]], ProcessLike]
PackagedLaunchFn = Callable[[str], int]
LocationOpenFn = Callable[[str], None]
WindowActionFn = Callable[[AppSpec, ComputerOperation], str]
GuiActionFn = Callable[[AppSpec, ComputerOperation], str]
SleepFn = Callable[[float], None]


class ComputerExecutor:
    """Execute typed, permission-gated Windows actions."""

    def __init__(
        self,
        *,
        audit: ActionAuditLog | None = None,
        journal: UndoJournal | None = None,
        evaluate_fn: EvaluateFn = evaluate,
        launcher: LaunchFn | None = None,
        packaged_launcher: PackagedLaunchFn | None = None,
        location_opener: LocationOpenFn | None = None,
        window_action: WindowActionFn | None = None,
        gui_action: GuiActionFn | None = None,
        window_binder: WindowBinder | None = None,
        sleep_fn: SleepFn = time.sleep,
        verification_delay_seconds: float = 0.20,
        launch_binding_attempts: int = 25,
        launch_binding_poll_seconds: float = 0.20,
        dynamic_apps: bool = False,
    ) -> None:
        self.audit = audit
        self.journal = journal
        self._evaluate = evaluate_fn
        self._launcher = launcher if launcher is not None else _default_launcher
        self._packaged_launcher = (
            packaged_launcher if packaged_launcher is not None else activate_application
        )
        self._location_opener = location_opener if location_opener is not None else _default_open_location
        self._window_action = window_action if window_action is not None else _default_window_action
        self._gui_action = gui_action if gui_action is not None else _default_gui_action
        self._window_binder = window_binder if window_binder is not None else WindowBinder()
        self._sleep = sleep_fn
        self._verification_delay_seconds = max(0.0, float(verification_delay_seconds))
        self._launch_binding_attempts = max(1, int(launch_binding_attempts))
        self._launch_binding_poll_seconds = max(0.0, float(launch_binding_poll_seconds))
        self.dynamic_apps = bool(dynamic_apps)

    def resolve_app_id(self, query: str) -> str:
        return resolve_application_id(query, dynamic=self.dynamic_apps)

    def display_name_for_app_id(self, app_id: str) -> str:
        """Return a human-readable name without exposing a discovered app id."""
        return get_app_spec(app_id).display_name

    @property
    def window_binder(self) -> WindowBinder:
        """The process-local binder used by later Vision/input stages."""
        return self._window_binder

    def bind_app_window(
        self,
        app_id: str,
        *,
        require_foreground: bool = False,
        expected_pid: int | None = None,
    ) -> WindowBinding:
        """Bind one unambiguous, strongly identified visible app window."""
        spec = get_app_spec(app_id)
        target = WindowTarget(
            app_id=spec.app_id,
            display_name=spec.display_name,
            aliases=spec.aliases,
            executable=spec.executable,
            process_names=spec.process_names,
        )
        if expected_pid is None:
            return self._window_binder.bind(
                target,
                require_foreground=require_foreground,
            )
        return self._window_binder.bind(
            target,
            require_foreground=require_foreground,
            expected_pid=expected_pid,
        )

    def revalidate_window_binding(
        self,
        binding: WindowBinding,
        *,
        require_foreground: bool = True,
        require_same_layout: bool = True,
    ) -> WindowSnapshot:
        """Fail closed if a bound HWND/process/focus/layout changed."""
        return self._window_binder.revalidate(
            binding,
            require_foreground=require_foreground,
            require_same_layout=require_same_layout,
        )

    def acquire_foreground(
        self,
        binding: WindowBinding,
        *,
        timeout_seconds: float = 3.0,
        poll_seconds: float = 0.10,
    ) -> WindowSnapshot:
        """
        Bring the exact already-bound HWND to the foreground without rediscovery.

        Windows may refuse a plain SetForegroundWindow call when the current
        foreground thread owns the activation queue. Temporarily attaching the
        current thread to the target/foreground input queues is the bounded
        Win32 path validated by the Stage 4B.3 live tests. Identity is checked
        again afterwards, but layout equality is intentionally not required:
        restoring/focusing Paint may legitimately settle its chrome before the
        caller creates a fresh layout-bound capability.
        """
        if os.name != "nt":
            raise RuntimeError("Foreground acquisition supports Windows only.")
        timeout = float(timeout_seconds)
        poll = float(poll_seconds)
        if timeout <= 0.0 or poll <= 0.0:
            raise ValueError("Foreground acquisition timing must be positive.")

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetActiveWindow.argtypes = [wintypes.HWND]
        user32.SetActiveWindow.restype = wintypes.HWND
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        target_hwnd = wintypes.HWND(binding.hwnd)
        user32.ShowWindow(target_hwnd, 9)  # SW_RESTORE

        foreground_before = int(user32.GetForegroundWindow() or 0)
        target_pid = wintypes.DWORD()
        target_thread = int(
            user32.GetWindowThreadProcessId(
                target_hwnd,
                ctypes.byref(target_pid),
            )
            or 0
        )
        if target_pid.value != binding.snapshot.pid:
            raise WindowBindingStale(
                "The bound Paint HWND no longer belongs to the launched PID."
            )

        foreground_pid = wintypes.DWORD()
        foreground_thread = 0
        if foreground_before:
            foreground_thread = int(
                user32.GetWindowThreadProcessId(
                    wintypes.HWND(foreground_before),
                    ctypes.byref(foreground_pid),
                )
                or 0
            )

        current_thread = int(kernel32.GetCurrentThreadId() or 0)
        attached_target = False
        attached_foreground = False

        try:
            if target_thread and target_thread != current_thread:
                attached_target = bool(
                    user32.AttachThreadInput(
                        current_thread,
                        target_thread,
                        True,
                    )
                )
            if (
                foreground_thread
                and foreground_thread != current_thread
                and foreground_thread != target_thread
            ):
                attached_foreground = bool(
                    user32.AttachThreadInput(
                        current_thread,
                        foreground_thread,
                        True,
                    )
                )

            deadline = time.monotonic() + timeout
            while True:
                user32.BringWindowToTop(target_hwnd)
                user32.SetActiveWindow(target_hwnd)
                user32.SetForegroundWindow(target_hwnd)
                foreground = int(user32.GetForegroundWindow() or 0)
                if foreground == binding.hwnd:
                    break
                if time.monotonic() >= deadline:
                    raise WindowBindingUnavailable(
                        "Windows did not grant foreground to the exact bound window."
                    )
                self._sleep(poll)
        finally:
            if attached_foreground:
                user32.AttachThreadInput(
                    current_thread,
                    foreground_thread,
                    False,
                )
            if attached_target:
                user32.AttachThreadInput(
                    current_thread,
                    target_thread,
                    False,
                )

        return self._window_binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=False,
        )

    def _wait_for_launched_window(
        self,
        spec: AppSpec,
        pid: int,
    ) -> WindowBinding:
        """Wait briefly for the exact process Qronos launched to expose a window."""
        last_error: Exception | None = None
        for attempt in range(self._launch_binding_attempts):
            try:
                return self.bind_app_window(
                    spec.app_id,
                    require_foreground=False,
                    expected_pid=pid,
                )
            except (
                WindowBindingUnavailable,
                WindowBindingAmbiguous,
                WindowBindingStale,
            ) as exc:
                last_error = exc
                if attempt + 1 < self._launch_binding_attempts:
                    self._sleep(self._launch_binding_poll_seconds)

        detail = str(last_error) if last_error is not None else "no window was observed"
        raise RuntimeError(
            f"{spec.display_name} activated as pid={pid}, but Qronos could not bind "
            f"a trustworthy window for that exact process: {detail}"
        )

    def prepare(self, action: ComputerAction) -> PreparedComputerAction:
        request = action.to_request()
        verdict = self._evaluate_request(request)
        return PreparedComputerAction(action=action, request=request, verdict=verdict)

    def execute(self, action: ComputerAction, *, approval: PermissionDecision | None = None) -> ComputerExecution:
        return self.execute_prepared(self.prepare(action), approval=approval, reevaluate=False)

    def execute_prepared(
        self,
        prepared: PreparedComputerAction,
        *,
        approval: PermissionDecision | None = None,
        reevaluate: bool = True,
    ) -> ComputerExecution:
        if not isinstance(prepared, PreparedComputerAction):
            raise InvalidAction("execute_prepared requires a PreparedComputerAction.")
        verdict = self._evaluate_request(prepared.request) if reevaluate else prepared.verdict
        if verdict.refused:
            return ComputerExecution(prepared.request, verdict, verdict.as_result())
        if verdict.needs_approval:
            if approval is not verdict.decision:
                return ComputerExecution(prepared.request, verdict, verdict.as_result())
            if self.audit is not None:
                self.audit.record(AuditEvent.APPROVED, prepared.request, decision=verdict.decision)
        return self._run(prepared.action, prepared.request, verdict)

    def decline_prepared(self, prepared: PreparedComputerAction) -> ComputerExecution:
        if not isinstance(prepared, PreparedComputerAction):
            raise InvalidAction("decline_prepared requires a PreparedComputerAction.")
        if not prepared.verdict.needs_approval:
            raise InvalidAction("Only an action awaiting approval can be declined.")
        result = ActionResult(
            action_id=prepared.request.action_id,
            outcome=ActionOutcome.REFUSED,
            detail="Denied by user.",
        )
        if self.audit is not None:
            self.audit.record(
                AuditEvent.DENIED_BY_USER,
                prepared.request,
                decision=prepared.verdict.decision,
                outcome=ActionOutcome.REFUSED,
            )
        return ComputerExecution(prepared.request, prepared.verdict, result)

    def _evaluate_request(self, request: ActionRequest) -> Verdict:
        return self._evaluate(
            request,
            audit=self.audit.record_verdict if self.audit is not None else None,
        )

    def _run(self, action: ComputerAction, request: ActionRequest, verdict: Verdict) -> ComputerExecution:
        window_binding: WindowBinding | None = None
        try:
            detail, window_binding = self._perform_with_context(action)
            result = ActionResult(
                action_id=request.action_id,
                outcome=ActionOutcome.SUCCEEDED,
                detail=detail,
            )
        except Exception as exc:
            result = ActionResult(
                action_id=request.action_id,
                outcome=ActionOutcome.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )
        if self.audit is not None:
            self.audit.record_result(request, result)
        undo_entry: UndoEntry | None = None
        if result.outcome is ActionOutcome.SUCCEEDED and self.journal is not None:
            undo_entry = self.journal.record(request, Reversal.NOTHING_TO_UNDO, {})
        return ComputerExecution(
            request, verdict, result, undo_entry, window_binding
        )

    def _perform(self, action: ComputerAction) -> str:
        """Compatibility wrapper for tests and legacy callers that only need text."""
        detail, _binding = self._perform_with_context(action)
        return detail

    def _perform_with_context(
        self,
        action: ComputerAction,
    ) -> tuple[str, WindowBinding | None]:
        if action.operation is ComputerOperation.OPEN_LOCATION:
            path = Path(action.location).resolve(strict=False)
            if not path.is_dir():
                raise FileNotFoundError(f"Directory does not exist: {path}")
            self._location_opener(str(path))
            return f"Opened directory {path} in Windows Explorer.", None

        spec = get_app_spec(action.app_id)
        if action.operation is ComputerOperation.OPEN_APP:
            if spec.aumid:
                pid = int(self._packaged_launcher(spec.aumid) or 0)
                if pid <= 0:
                    raise RuntimeError(
                        "Windows packaged-app activation did not return a valid process identifier."
                    )
                binding = self._wait_for_launched_window(spec, pid)
                return (
                    f"Opened {spec.display_name}; pid={pid}; hwnd={binding.hwnd}; "
                    "verification=packaged-app activation and exact-pid window binding succeeded.",
                    binding,
                )

            if spec.shortcut:
                _launch_trusted_shortcut(spec)
                if self._verification_delay_seconds:
                    self._sleep(max(0.45, self._verification_delay_seconds))
                return f"Opened {spec.display_name} from installed-app catalog.", None

            command = (spec.executable,)
            if spec.app_id in KNOWN_APPS:
                process = self._launcher(command)
            else:
                process = _launch_discovered_executable(spec)
            pid = int(getattr(process, "pid", 0) or 0)
            if pid <= 0:
                raise RuntimeError("Windows did not return a valid process identifier.")
            if self._verification_delay_seconds:
                self._sleep(self._verification_delay_seconds)
            return_code = process.poll()
            if return_code is not None and int(return_code) != 0:
                raise RuntimeError(f"{spec.display_name} launch exited with code {return_code}.")

            if return_code is None:
                verification = "process is running"
            else:
                # Some Windows launchers delegate to an existing application
                # instance and then exit cleanly. Preserve the Execution Spine
                # V1 compatibility contract: exit code 0 is accepted as a
                # successful Windows delegation.
                verification = "Windows accepted/delegated the launch"

            return (
                f"Opened {spec.display_name}; "
                f"pid={pid}; verification={verification}.",
                None,
            )

        if action.operation in {
            ComputerOperation.CLOSE_APP,
            ComputerOperation.FOCUS_WINDOW,
            ComputerOperation.MINIMIZE_WINDOW,
            ComputerOperation.MAXIMIZE_WINDOW,
        }:
            return self._window_action(spec, action.operation), None

        if action.operation is ComputerOperation.DRAW_BASIC_HOUSE:
            if spec.app_id != "paint":
                raise InvalidAction("DRAW_BASIC_HOUSE is restricted to Microsoft Paint.")
            return self._gui_action(spec, action.operation), None

        raise InvalidAction(f"Unsupported computer operation: {action.operation!r}.")


def _explorer_location_command(path: str) -> tuple[str, str]:
    target = Path(path).resolve(strict=False)
    if not target.is_dir():
        raise FileNotFoundError(str(target))
    return ("explorer.exe", str(target))


def _default_open_location(path: str) -> None:
    if os.name != "nt":
        raise RuntimeError("OPEN_LOCATION supports Windows only.")
    command = _explorer_location_command(path)
    process = subprocess.Popen(
        list(command), shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if int(getattr(process, "pid", 0) or 0) <= 0:
        raise RuntimeError("Windows Explorer did not return a valid process identifier.")


def _default_launcher(command: tuple[str, ...]) -> ProcessLike:
    if os.name != "nt":
        raise RuntimeError("ComputerExecutor supports Windows only.")
    if len(command) != 1:
        raise InvalidAction("Static application launch accepts no arguments.")
    executable = command[0]
    approved = {spec.executable for spec in KNOWN_APPS.values()}
    if executable not in approved:
        raise InvalidAction("Application executable is not in the static reviewed allowlist.")
    return subprocess.Popen(
        [executable], shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _launch_discovered_executable(spec: AppSpec) -> ProcessLike:
    if os.name != "nt":
        raise RuntimeError("Dynamic application launch supports Windows only.")
    current = application_catalog().get(spec.app_id)
    if current != spec:
        current = application_catalog(refresh=True).get(spec.app_id)
    if current != spec or not spec.executable:
        raise InvalidAction("Discovered executable is no longer present in the trusted catalog.")
    target = Path(spec.executable).resolve(strict=False)
    if not target.is_file() or target.name.lower() in _BLOCKED_EXECUTABLES:
        raise InvalidAction("Discovered executable failed validation.")
    return subprocess.Popen(
        [str(target)], shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _launch_trusted_shortcut(spec: AppSpec) -> None:
    if os.name != "nt":
        raise RuntimeError("Shortcut launch supports Windows only.")
    current = application_catalog().get(spec.app_id)
    if current != spec:
        current = application_catalog(refresh=True).get(spec.app_id)
    if current != spec or not spec.shortcut:
        raise InvalidAction("Discovered shortcut is no longer present in the trusted catalog.")
    shortcut = Path(spec.shortcut).resolve(strict=False)
    if shortcut.suffix.lower() != ".lnk" or not shortcut.is_file():
        raise InvalidAction("Discovered shortcut failed validation.")
    trusted = False
    shortcut_key = os.path.normcase(os.path.abspath(str(shortcut)))
    for root in _trusted_start_menu_roots():
        try:
            root_key = os.path.normcase(os.path.abspath(str(root.resolve(strict=False))))
            if os.path.commonpath([shortcut_key, root_key]) == root_key:
                trusted = True
                break
        except (OSError, ValueError):
            continue
    if not trusted:
        raise InvalidAction("Shortcut is outside trusted Start Menu roots.")
    os.startfile(str(shortcut))


@dataclass(frozen=True)
class _WindowInfo:
    hwnd: int
    pid: int
    title: str
    process_name: str
    score: int


def _query_process_name(pid: int) -> str:
    if os.name != "nt":
        return ""
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def _window_match_score(spec: AppSpec, title: str, process_name: str) -> int:
    process_name = process_name.lower()
    if process_name and process_name in {p.lower() for p in spec.process_names}:
        return 1200
    title_n = _norm(title)
    if not title_n:
        return 0
    best = 0
    for alias in (spec.display_name, *spec.aliases):
        a = _norm(alias)
        if not a:
            continue
        if title_n == a:
            best = max(best, 1000)
        elif a in title_n:
            best = max(best, 900)
    return best


def _find_windows(spec: AppSpec) -> list[_WindowInfo]:
    if os.name != "nt":
        raise RuntimeError("Window control supports Windows only.")
    user32 = ctypes.windll.user32
    windows: list[_WindowInfo] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @WNDENUMPROC
    def callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_name = _query_process_name(int(pid.value))
        score = _window_match_score(spec, title, process_name)
        if score > 0:
            windows.append(_WindowInfo(int(hwnd), int(pid.value), title, process_name, score))
        return True

    user32.EnumWindows(callback, 0)
    windows.sort(key=lambda item: (-item.score, item.pid, item.hwnd))
    return windows


def _default_window_action(spec: AppSpec, operation: ComputerOperation) -> str:
    if os.name != "nt":
        raise RuntimeError("Window control supports Windows only.")
    windows = _find_windows(spec)
    if not windows:
        raise RuntimeError(f"No visible window for {spec.display_name} was found.")
    best_score = windows[0].score
    matches = [item for item in windows if item.score == best_score]
    user32 = ctypes.windll.user32

    if operation is ComputerOperation.CLOSE_APP:
        WM_CLOSE = 0x0010
        for item in matches:
            user32.PostMessageW(item.hwnd, WM_CLOSE, 0, 0)
        time.sleep(0.35)
        return f"Close requested for {spec.display_name}."

    target = matches[0]
    if operation is ComputerOperation.FOCUS_WINDOW:
        SW_RESTORE = 9
        user32.ShowWindow(target.hwnd, SW_RESTORE)
        user32.BringWindowToTop(target.hwnd)
        user32.SetForegroundWindow(target.hwnd)
        return f"Focused {spec.display_name}."
    if operation is ComputerOperation.MINIMIZE_WINDOW:
        SW_MINIMIZE = 6
        user32.ShowWindow(target.hwnd, SW_MINIMIZE)
        return f"Minimized {spec.display_name}."
    if operation is ComputerOperation.MAXIMIZE_WINDOW:
        SW_MAXIMIZE = 3
        user32.ShowWindow(target.hwnd, SW_MAXIMIZE)
        return f"Maximized {spec.display_name}."
    raise InvalidAction(f"Unsupported window operation: {operation!r}.")


def _default_gui_action(spec: AppSpec, operation: ComputerOperation) -> str:
    """Run reviewed, coordinate-free templates in one verified app window."""
    # Client-relative coordinates do not identify the actual Paint canvas or
    # selected tool. Keep native input disabled until grounding, cancellation
    # and postcondition verification are implemented and tested together.
    raise RuntimeError(
        "Paint drawing requires verified canvas/tool grounding; native input is not enabled yet."
    )
