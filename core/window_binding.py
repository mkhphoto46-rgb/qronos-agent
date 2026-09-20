from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import hashlib
import ntpath
import os
from typing import Callable


class WindowBindingUnavailable(RuntimeError):
    """A trustworthy Windows binding could not be created."""


class WindowBindingAmbiguous(LookupError):
    """More than one visible window matched strongly enough to stop."""


class WindowBindingStale(RuntimeError):
    """A previously verified binding no longer identifies the same window."""


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def usable(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass(frozen=True)
class WindowTarget:
    """The application identity Qronos expects before touching a window."""

    app_id: str
    display_name: str
    aliases: tuple[str, ...] = ()
    executable: str = ""
    process_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class WindowSnapshot:
    """One immutable observation of a top-level Windows window."""

    hwnd: int
    pid: int
    thread_id: int
    title: str
    process_path: str
    process_start_time_100ns: int
    window_rect: WindowRect
    client_rect: WindowRect
    dpi: int
    visible: bool
    minimized: bool
    foreground: bool

    @property
    def process_name(self) -> str:
        return ntpath.basename(self.process_path).lower()


@dataclass(frozen=True)
class WindowBinding:
    """
    A short-lived capability that binds later Vision/input work to one window.

    identity_token changes if Windows reuses the HWND/PID for another process.
    layout_token also changes if the window/client geometry or DPI changes.
    Coordinates produced by Vision must carry layout_token and expire with it.
    """

    app_id: str
    display_name: str
    snapshot: WindowSnapshot
    identity_token: str
    layout_token: str

    @property
    def hwnd(self) -> int:
        return self.snapshot.hwnd


EnumerateFn = Callable[[], tuple[WindowSnapshot, ...]]
SnapshotFn = Callable[[int], WindowSnapshot | None]


def _normalise_text(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _normalise_windows_path(value: str) -> str:
    cleaned = str(value or "").strip().strip('"')
    return ntpath.normcase(ntpath.normpath(cleaned)) if cleaned else ""


def _identity_tuple(snapshot: WindowSnapshot) -> tuple[object, ...]:
    return (
        int(snapshot.hwnd),
        int(snapshot.pid),
        int(snapshot.thread_id),
        int(snapshot.process_start_time_100ns),
        _normalise_windows_path(snapshot.process_path),
    )


def _identity_token(snapshot: WindowSnapshot) -> str:
    raw = "\0".join(str(part) for part in _identity_tuple(snapshot))
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _layout_token(snapshot: WindowSnapshot) -> str:
    w = snapshot.window_rect
    c = snapshot.client_rect
    raw = (
        f"{_identity_token(snapshot)}\0"
        f"{w.left},{w.top},{w.right},{w.bottom}\0"
        f"{c.left},{c.top},{c.right},{c.bottom}\0"
        f"{int(snapshot.dpi)}"
    )
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _candidate_score(target: WindowTarget, snapshot: WindowSnapshot) -> int:
    if not snapshot.visible or snapshot.minimized:
        return 0
    if not snapshot.window_rect.usable or not snapshot.client_rect.usable:
        return 0
    if not snapshot.process_path or snapshot.process_start_time_100ns <= 0:
        return 0

    actual_path = _normalise_windows_path(snapshot.process_path)
    actual_name = snapshot.process_name
    expected_exe = _normalise_windows_path(target.executable)
    expected_name = ntpath.basename(expected_exe).lower() if expected_exe else ""
    allowed_names = {
        str(name).lower()
        for name in target.process_names
        if str(name).strip()
    }
    if expected_name:
        allowed_names.add(expected_name)

    identity_score = 0
    if expected_exe and (":" in expected_exe or "\\" in expected_exe):
        if actual_path == expected_exe:
            identity_score = 1500
        else:
            return 0
    elif allowed_names:
        if actual_name not in allowed_names:
            return 0
        identity_score = 1250
    else:
        # A title is not application identity. Start-menu-only entries without
        # a known process remain unavailable until their shortcut target is
        # resolved to a concrete executable.
        return 0

    title = _normalise_text(snapshot.title)
    aliases = tuple(
        _normalise_text(value)
        for value in (target.display_name, *target.aliases)
        if _normalise_text(value)
    )
    title_score = 0
    for alias in aliases:
        if title == alias:
            title_score = max(title_score, 200)
        elif alias and alias in title:
            title_score = max(title_score, 100)

    return identity_score + title_score


class WindowBinder:
    """Create and revalidate fail-closed bindings to visible top-level windows."""

    def __init__(
        self,
        *,
        enumerate_fn: EnumerateFn | None = None,
        snapshot_fn: SnapshotFn | None = None,
    ) -> None:
        self._enumerate = enumerate_fn if enumerate_fn is not None else enumerate_windows
        self._snapshot = snapshot_fn if snapshot_fn is not None else snapshot_window

    def bind(
        self,
        target: WindowTarget,
        *,
        require_foreground: bool = False,
        expected_pid: int | None = None,
    ) -> WindowBinding:
        if expected_pid is not None and int(expected_pid) <= 0:
            raise ValueError("expected_pid must be a positive process id when provided.")

        candidates: list[tuple[int, WindowSnapshot]] = []
        for snapshot in self._enumerate():
            if expected_pid is not None and snapshot.pid != int(expected_pid):
                continue
            score = _candidate_score(target, snapshot)
            if score > 0 and (not require_foreground or snapshot.foreground):
                candidates.append((score, snapshot))

        if not candidates:
            pid_detail = (
                f" for pid {int(expected_pid)}" if expected_pid is not None else ""
            )
            raise WindowBindingUnavailable(
                f"No trustworthy visible window for {target.display_name}{pid_detail} was found."
            )

        candidates.sort(key=lambda item: (-item[0], item[1].pid, item[1].hwnd))
        best_score = candidates[0][0]
        best = [snapshot for score, snapshot in candidates if score == best_score]
        if len(best) != 1:
            raise WindowBindingAmbiguous(
                f"More than one visible window matched {target.display_name}; "
                "Qronos will not guess which one to control."
            )

        selected = best[0]
        fresh = self._snapshot(selected.hwnd)
        if fresh is None or _identity_tuple(fresh) != _identity_tuple(selected):
            raise WindowBindingStale(
                "The selected window changed while Qronos was binding to it."
            )
        if expected_pid is not None and fresh.pid != int(expected_pid):
            raise WindowBindingStale(
                "The selected window no longer belongs to the process Qronos launched."
            )
        if not fresh.visible or fresh.minimized or not fresh.client_rect.usable:
            raise WindowBindingStale(
                "The selected window stopped being usable while Qronos was binding to it."
            )
        if require_foreground and not fresh.foreground:
            raise WindowBindingStale(
                "The selected window lost focus while Qronos was binding to it."
            )

        return WindowBinding(
            app_id=target.app_id,
            display_name=target.display_name,
            snapshot=fresh,
            identity_token=_identity_token(fresh),
            layout_token=_layout_token(fresh),
        )

    def revalidate(
        self,
        binding: WindowBinding,
        *,
        require_foreground: bool = True,
        require_same_layout: bool = True,
    ) -> WindowSnapshot:
        fresh = self._snapshot(binding.hwnd)
        if fresh is None:
            raise WindowBindingStale("The bound window no longer exists.")
        if _identity_token(fresh) != binding.identity_token:
            raise WindowBindingStale(
                "The HWND/PID now belongs to a different process or process instance."
            )
        if not fresh.visible or fresh.minimized or not fresh.client_rect.usable:
            raise WindowBindingStale("The bound window is no longer visible and usable.")
        if require_foreground and not fresh.foreground:
            raise WindowBindingStale("The bound window lost foreground focus.")
        if require_same_layout and _layout_token(fresh) != binding.layout_token:
            raise WindowBindingStale(
                "The bound window moved, resized, or changed DPI; grounded coordinates expired."
            )
        return fresh


# Canonical Win32 geometry types shared with other native modules. This matters
# because ``ctypes.windll`` caches functions process-wide: different-but-
# identical Structure subclasses are not pointer-compatible once argtypes are set.
_RECT = wintypes.RECT
_POINT = wintypes.POINT


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]




def _configure_user32(user32) -> None:
    """Declare pointer-sized HWND signatures before any direct Win32 call."""
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    try:
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = ctypes.c_uint
    except AttributeError:
        pass


def _configure_kernel32(kernel32) -> None:
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

def _filetime_value(value: _FILETIME) -> int:
    return (int(value.high) << 32) | int(value.low)


def _process_identity(pid: int) -> tuple[str, int]:
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        return "", 0

    kernel32 = ctypes.windll.kernel32
    _configure_kernel32(kernel32)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return "", 0

    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        path = ""
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            path = buffer.value

        created = _FILETIME()
        exited = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        started = 0
        if kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            started = _filetime_value(created)
        return path, started
    finally:
        kernel32.CloseHandle(handle)


def _window_rect(user32, hwnd: int) -> WindowRect | None:
    rect = _RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return WindowRect(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _client_rect(user32, hwnd: int) -> WindowRect | None:
    rect = _RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    top_left = _POINT(rect.left, rect.top)
    bottom_right = _POINT(rect.right, rect.bottom)
    if not user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
        return None
    if not user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
        return None
    return WindowRect(
        int(top_left.x),
        int(top_left.y),
        int(bottom_right.x),
        int(bottom_right.y),
    )


def _window_dpi(user32, hwnd: int) -> int:
    try:
        get_dpi = user32.GetDpiForWindow
    except AttributeError:
        return 96
    try:
        value = int(get_dpi(hwnd))
    except Exception:
        return 96
    return value if value > 0 else 96


def snapshot_window(hwnd: int) -> WindowSnapshot | None:
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        raise WindowBindingUnavailable("Window binding is available on Windows only.")

    user32 = ctypes.windll.user32
    _configure_user32(user32)
    hwnd = int(hwnd)
    handle = wintypes.HWND(hwnd)
    if hwnd <= 0 or not user32.IsWindow(handle):
        return None

    pid = wintypes.DWORD()
    thread_id = int(user32.GetWindowThreadProcessId(handle, ctypes.byref(pid)))
    if int(pid.value) <= 0 or thread_id <= 0:
        return None

    length = int(user32.GetWindowTextLengthW(handle))
    title = ""
    if length > 0:
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        title = buffer.value

    process_path, started = _process_identity(int(pid.value))
    outer = _window_rect(user32, handle)
    client = _client_rect(user32, handle)
    if outer is None or client is None:
        return None

    return WindowSnapshot(
        hwnd=hwnd,
        pid=int(pid.value),
        thread_id=thread_id,
        title=title,
        process_path=process_path,
        process_start_time_100ns=started,
        window_rect=outer,
        client_rect=client,
        dpi=_window_dpi(user32, handle),
        visible=bool(user32.IsWindowVisible(handle)),
        minimized=bool(user32.IsIconic(handle)),
        foreground=int(user32.GetForegroundWindow() or 0) == hwnd,
    )


def enumerate_windows() -> tuple[WindowSnapshot, ...]:
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        raise WindowBindingUnavailable("Window binding is available on Windows only.")

    user32 = ctypes.windll.user32
    _configure_user32(user32)
    found: list[WindowSnapshot] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        snapshot = snapshot_window(int(hwnd))
        if snapshot is not None:
            found.append(snapshot)
        return True

    user32.EnumWindows(visit, 0)
    return tuple(found)
