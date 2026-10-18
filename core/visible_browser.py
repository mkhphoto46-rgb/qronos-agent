"""
Open one validated public web link in the user's own visible browser.

This is the browser half of the project's internet rule: public web content
is reached only by showing it to the person in a real, visible browser
window. Qronos fetches nothing itself, sends no request, reads no page and
drives no page; it starts the person's default browser with exactly one URL
and checks that a visible window of that browser exists afterwards.

It matches ``core.library_executor.BrowserLauncher`` (``Callable[[str], None]``)
so the Library's existing permission-gated, audited "open link" flow can use
it. The permission decision and the audit record stay in that flow; this
module adds the browser-side guarantees:

    - the URL passes ``core.link_policy.validate_web_url`` again (public http(s)
      only, no credentials, no local or numeric hosts, no spaces/controls);
    - the default https handler is resolved through Windows
      (``AssocQueryStringW`` on the https protocol) and must be an existing,
      non-link executable of a known browser; anything else is refused instead
      of being launched;
    - the browser is started directly with ``[browser, url]`` as an argument
      list (no shell, no ``ShellExecute`` on the URL, so no other protocol
      handler can be reached);
    - success is reported only when a visible top-level window of that browser
      is present; otherwise the caller is told visibility was not confirmed.

The repository does not contain the user-supplied browser workflow the
project documents mention. This launcher is a narrowly scoped visible
adapter for that contract and is not wired into the runtime until it is
accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import ntpath
import os
from pathlib import Path
import subprocess
import time
from typing import Callable, Sequence

from core.link_policy import UnsafeLink, validate_web_url


KNOWN_BROWSERS = frozenset({
    "msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "vivaldi.exe", "opera.exe", "librewolf.exe",
})
DEFAULT_VISIBLE_TIMEOUT = 15.0


class BrowserUnavailable(RuntimeError):
    """No acceptable visible browser could be used; nothing was opened."""


class BrowserVisibilityUnconfirmed(RuntimeError):
    """The browser was started but no visible window could be confirmed."""


@dataclass(frozen=True)
class BrowserIdentity:
    executable: str

    @property
    def process_name(self) -> str:
        return ntpath.basename(self.executable).casefold()


ASSOCF_IS_PROTOCOL = 0x1000
ASSOCF_NOTRUNCATE = 0x20
ASSOCSTR_EXECUTABLE = 2


def _default_https_handler() -> str | None:
    """The executable Windows associates with the https protocol."""
    if os.name != "nt":
        return None
    shlwapi = ctypes.WinDLL("shlwapi", use_last_error=True)
    function = shlwapi.AssocQueryStringW
    function.argtypes = [ctypes.c_uint, ctypes.c_uint, wintypes.LPCWSTR, wintypes.LPCWSTR,
                         wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    function.restype = ctypes.c_long
    size = wintypes.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(size.value)
    result = function(ASSOCF_IS_PROTOCOL | ASSOCF_NOTRUNCATE, ASSOCSTR_EXECUTABLE, "https", "open",
                      buffer, ctypes.byref(size))
    if result != 0 or not buffer.value:
        return None
    return buffer.value


def _is_link(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & 0x400)


def resolve_default_browser(handler_fn: Callable[[], str | None] = _default_https_handler) -> BrowserIdentity:
    executable = str(handler_fn() or "").strip().strip('"')
    if not executable:
        raise BrowserUnavailable("Windows has no default browser for https links.")
    path = Path(executable)
    if not path.is_absolute():
        raise BrowserUnavailable("The default browser is not registered with a full path.")
    name = path.name.casefold()
    if name not in KNOWN_BROWSERS:
        raise BrowserUnavailable("The default https handler is not a known web browser; nothing was opened.")
    if not path.is_file():
        raise BrowserUnavailable("The default browser executable is missing.")
    if _is_link(path) or any(_is_link(parent) for parent in path.parents if parent.exists()):
        raise BrowserUnavailable("The default browser executable is reached through a link.")
    return BrowserIdentity(executable=str(path))


def _visible_browser_window(process_name: str) -> bool:
    """True when a visible, non-minimised window of that browser is in front.

    Browsers bring the window holding a newly opened tab to the foreground,
    so an older browser window left in the background does not count.
    """
    from core.window_binding import enumerate_windows

    for snapshot in enumerate_windows():
        if (
            snapshot.process_name == process_name
            and snapshot.visible
            and snapshot.foreground
            and not snapshot.minimized
            and snapshot.window_rect.usable
        ):
            return True
    return False


class VisibleBrowserLauncher:
    """A ``BrowserLauncher`` for ``LibraryExecutor``: one URL, one visible browser."""

    def __init__(
        self,
        *,
        handler_fn: Callable[[], str | None] = _default_https_handler,
        popen_fn: Callable[..., object] = subprocess.Popen,
        visible_fn: Callable[[str], bool] = _visible_browser_window,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        visible_timeout: float = DEFAULT_VISIBLE_TIMEOUT,
    ) -> None:
        self._handler_fn = handler_fn
        self._popen = popen_fn
        self._visible = visible_fn
        self._clock = clock
        self._sleep = sleep
        self._timeout = float(visible_timeout)

    def __call__(self, url: str) -> None:
        """The Library BrowserLauncher contract."""
        self.launch(url)

    def launch(self, url: str) -> BrowserIdentity:
        """Open one URL; return the browser that is now showing it in front."""
        try:
            checked = validate_web_url(url)
        except UnsafeLink as error:
            raise BrowserUnavailable(str(error)) from None
        if '"' in checked or checked.startswith("-"):
            raise BrowserUnavailable("The link cannot be passed to a browser safely.")
        browser = resolve_default_browser(self._handler_fn)
        arguments: Sequence[str] = (browser.executable, checked)
        try:
            self._popen(
                list(arguments),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as error:
            raise BrowserUnavailable(f"The browser could not be started ({type(error).__name__}).") from None
        deadline = self._clock() + self._timeout
        while True:
            if self._visible(browser.process_name):
                return browser
            if self._clock() >= deadline:
                raise BrowserVisibilityUnconfirmed(
                    "The browser was asked to open the link, but no visible browser window "
                    "could be confirmed."
                )
            self._sleep(0.25)
