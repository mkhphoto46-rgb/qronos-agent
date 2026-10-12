"""
Telegram: download the newest incoming file from one exactly named contact.

The workflow has two approved phases, because what a person agrees to changes
once Qronos has looked:

    1. Inspect (GUI automation, voice confirmation or stronger). Bind the one
       verified Telegram window, search the exact contact name, stop unless
       exactly one result has exactly that name, open it, confirm the chat
       header is that name, and read the loaded messages newest first. The
       first *incoming document attachment* is the candidate. Nothing is
       downloaded, sent or typed into a chat.

    2. Download (file creation, UI confirmation). The person sees the exact
       contact, file name, size and destination folder. The approval is bound
       to that plan and to the window it was shown for. Qronos re-reads the
       chat, refuses if the candidate is no longer the newest incoming
       document, records the attempt in the side-effect journal, invokes the
       download control once, and verifies that exactly one new, size-stable
       file with that name appeared in the destination folder.

The adapter never sends a message, never selects a contact by similarity,
never downloads more than one attachment, and never retries a download whose
outcome is unclear: an unclear outcome is reported and blocks a repeat of the
same attachment until a person has checked.

The destination folder is explicit: it must be the folder Telegram is set to
save into, it is shown in the approval, and a file that appears anywhere else
is reported as uncertain rather than searched for. ``user_downloads_folder``
resolves the user's real (possibly redirected) Downloads folder for callers
that build that path.

Telegram is driven only through Windows UI Automation on the verified window
(:class:`UiaTelegramUi`), using the same exact-RuntimeId, gate-authorized
actuators as other semantic GUI actions. The element recognition rules are
explicit and fail closed: if the accessibility tree does not show exactly one
search field, an exact header or a download control inside the candidate
message, the workflow refuses rather than guessing. Those rules have not yet
been validated against a live Telegram for Windows build (live acceptance is
deferred), which is why they refuse rather than fall back to pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
import math
import ntpath
import os
from pathlib import Path
import re
import time
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from core.action_cancellation import ActionCancelled, CancellationToken
from core.actions import ActionRequest
from core.app_workflow import (
    DuplicateSideEffect,
    SideEffectJournal,
    SideEffectState,
    StepGuard,
    WindowEvidence,
    WorkflowAmbiguous,
    WorkflowApproval,
    WorkflowError,
    WorkflowRefused,
    WorkflowReport,
    WorkflowStopped,
    ApprovalMismatch,
    content_key,
    grant_for,
    operation_key,
    plan_digest,
)
from core.persian_text import normalise
from core.window_binding import WindowBinder, WindowBinding
from security.gate import ActionGrant
from security.permissions import ActionCategory, PermissionDecision


TELEGRAM_APP_ID = "telegram"
TELEGRAM_PROCESS_NAMES = frozenset({"telegram.exe"})
MAX_INSPECTED_MESSAGES = 60
MAX_CONTACT_NAME = 128
DEFAULT_VERIFY_TIMEOUT_SECONDS = 180.0
DEFAULT_POLL_SECONDS = 1.0

class TelegramActionNotStarted(WorkflowRefused):
    """The UI refused before Windows was asked to act; nothing happened."""


# --------------------------------------------------------------- UI model


@dataclass(frozen=True)
class ContactCandidate:
    element_id: str
    display_name: str


@dataclass(frozen=True)
class MessageView:
    """One loaded chat message, as read from the accessibility tree."""

    element_id: str
    incoming: bool
    kind: str  # "document", "media", "text", "service" or "unknown"
    file_name: str = ""
    size_text: str = ""
    download_element_id: str = ""
    downloaded: bool = False


class TelegramUi(Protocol):
    """What the workflow needs from Telegram; production is UiaTelegramUi."""

    def bind(self) -> WindowBinding: ...

    def search_contacts(
        self, binding: WindowBinding, query: str, *, grant: ActionGrant
    ) -> tuple[ContactCandidate, ...]: ...

    def open_chat(
        self, binding: WindowBinding, candidate: ContactCandidate, *, grant: ActionGrant
    ) -> None: ...

    def chat_title(self, binding: WindowBinding) -> str: ...

    def recent_messages(
        self, binding: WindowBinding, *, limit: int
    ) -> tuple[MessageView, ...]: ...

    def start_download(
        self, binding: WindowBinding, message: MessageView, *, grant: ActionGrant
    ) -> None: ...


class DownloadFolder(Protocol):
    @property
    def path(self) -> Path: ...

    def listing(self) -> Mapping[str, tuple[int, int]]:
        """File name -> (size, modified time ns) for regular files only."""
        ...


# ---------------------------------------------------------------- request


@dataclass(frozen=True)
class TelegramDownloadRequest:
    contact: str

    def __post_init__(self) -> None:
        cleaned = normalise(self.contact, for_search=False).strip()
        if not cleaned:
            raise ValueError("A Telegram download needs the exact contact name.")
        if len(cleaned) > MAX_CONTACT_NAME or any(ord(ch) < 32 for ch in cleaned):
            raise ValueError("The contact name is not a plain display name.")
        object.__setattr__(self, "contact", cleaned)


def name_key(value: str) -> str:
    """Comparison form for display names: Persian/Arabic letters unified, case and
    spacing folded. Exact after folding; never a similarity score."""
    return normalise(str(value or ""), for_search=True).casefold()


@dataclass(frozen=True)
class PreparedTelegramDownload:
    request: TelegramDownloadRequest
    plan: Mapping[str, object]
    digest: str
    binding: WindowBinding
    evidence: WindowEvidence
    message: MessageView
    destination: Path
    inspect_steps: tuple = ()


# ----------------------------------------------------------------- grants


def _gui_request(contact: str, purpose: str) -> ActionRequest:
    return ActionRequest(
        category=ActionCategory.GUI_AUTOMATION,
        target=f"app:{TELEGRAM_APP_ID}",
        summary=purpose,
        parameters={"contact": contact},
    )


# ---------------------------------------------------------------- workflow


_DUPLICATE_SUFFIX = re.compile(r"^(?P<stem>.*?)(?: \((?P<n>[0-9]{1,4})\))?$")


def _matches_attachment(candidate: str, expected: str) -> bool:
    """Telegram keeps the original name, adding " (n)" when the name exists."""
    stem, ext = os.path.splitext(expected)
    got_stem, got_ext = os.path.splitext(candidate)
    if got_ext.casefold() != ext.casefold():
        return False
    match = _DUPLICATE_SUFFIX.match(got_stem)
    return bool(match) and match.group("stem").casefold() == stem.casefold()


_SIZE = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(B|KB|MB|GB|bytes?|کیلوبایت|مگابایت|گیگابایت|بایت)\s*$", re.IGNORECASE)
_UNITS = {
    "b": 1, "byte": 1, "bytes": 1, "بایت": 1,
    "kb": 1024, "کیلوبایت": 1024,
    "mb": 1024 ** 2, "مگابایت": 1024 ** 2,
    "gb": 1024 ** 3, "گیگابایت": 1024 ** 3,
}


def approximate_size(text: str) -> tuple[int, int] | None:
    """(low, high) byte bounds for a displayed size such as "2.1 MB"."""
    match = _SIZE.match(normalise(text or "", for_search=True))
    if not match:
        return None
    number = match.group(1).replace(",", ".")
    value = float(number)
    unit = _UNITS[match.group(2).casefold()]
    decimals = len(number.split(".", 1)[1]) if "." in number else 0
    half_step = 0.5 * (10 ** -decimals) * unit
    return (max(0, math.floor(value * unit - half_step)), math.ceil(value * unit + half_step))


class TelegramDownloadWorkflow:
    def __init__(
        self,
        ui: TelegramUi,
        *,
        binder: WindowBinder,
        journal: SideEffectJournal,
        folder: DownloadFolder,
        clock: Callable[[], float] = time.monotonic,
        verify_timeout: float = DEFAULT_VERIFY_TIMEOUT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        sleep: Callable[[CancellationToken | None, float], None] | None = None,
    ) -> None:
        self._ui = ui
        self._binder = binder
        self._journal = journal
        self._folder = folder
        self._clock = clock
        self._verify_timeout = float(verify_timeout)
        self._poll = float(poll_seconds)
        self._sleep = sleep or _cancellable_sleep

    # ------------------------------------------------------------ phase 1

    def inspect(
        self,
        request: TelegramDownloadRequest,
        *,
        approved: PermissionDecision,
        token: CancellationToken | None,
        approval_valid: Callable[[], bool],
    ) -> PreparedTelegramDownload:
        grant = grant_for(
            _gui_request(request.contact, "Find one exact Telegram chat and read its recent messages."),
            approved,
        )
        binding = self._ui.bind()
        if binding.snapshot.process_name not in TELEGRAM_PROCESS_NAMES:
            raise WorkflowRefused("The bound window is not the Telegram app.")
        evidence = WindowEvidence.from_binding(binding, clock=self._clock)
        guard = StepGuard(
            token=token, approval_valid=approval_valid, binder=self._binder,
            binding=binding, evidence=evidence, clock=self._clock,
        )

        guard.checkpoint("search contact")
        candidates = self._ui.search_contacts(binding, request.contact, grant=grant)
        wanted = name_key(request.contact)
        exact = [item for item in candidates if name_key(item.display_name) == wanted]
        if not exact:
            raise WorkflowRefused("No Telegram chat has exactly that name.")
        if len(exact) > 1:
            raise WorkflowAmbiguous(
                f"{len(exact)} Telegram chats are named exactly like that; Qronos will not choose one."
            )
        if len({item.element_id for item in exact}) != 1:
            raise WorkflowAmbiguous("The matching chat could not be identified uniquely.")

        guard.checkpoint("open chat")
        self._ui.open_chat(binding, exact[0], grant=grant)
        guard.checkpoint("verify chat")
        if name_key(self._ui.chat_title(binding)) != wanted:
            raise WorkflowRefused("The opened chat is not the requested contact.")

        guard.checkpoint("read messages")
        message = self._newest_incoming_document(binding)
        destination = self._folder.path
        plan = {
            "workflow": "telegram_download",
            "contact": request.contact,
            "chatElement": exact[0].element_id,
            "messageElement": message.element_id,
            "fileName": message.file_name,
            "sizeText": message.size_text,
            "destination": str(destination),
        }
        return PreparedTelegramDownload(
            request=request,
            plan=plan,
            digest=plan_digest(plan),
            binding=binding,
            evidence=evidence,
            message=message,
            destination=destination,
            inspect_steps=guard.steps,
        )

    def _newest_incoming_document(self, binding: WindowBinding) -> MessageView:
        messages = self._ui.recent_messages(binding, limit=MAX_INSPECTED_MESSAGES)
        for message in messages[:MAX_INSPECTED_MESSAGES]:
            if not message.incoming or message.kind != "document":
                continue
            if message.kind == "document" and not _safe_file_name(message.file_name):
                raise WorkflowRefused("The newest incoming file has a name Qronos cannot save safely.")
            if message.downloaded:
                raise WorkflowRefused(
                    "The newest incoming file from this contact is already downloaded; "
                    "Qronos will not download it again."
                )
            if not message.download_element_id:
                raise WorkflowRefused("The newest incoming file shows no download control.")
            return message
        raise WorkflowRefused("No incoming file was found in this chat's loaded messages.")

    # ------------------------------------------------------------ phase 2

    def download(
        self,
        prepared: PreparedTelegramDownload,
        approval: WorkflowApproval,
        *,
        token: CancellationToken | None,
        approval_valid: Callable[[], bool],
    ) -> WorkflowReport:
        approval.require_for(
            plan=prepared.plan,
            required=PermissionDecision.REQUIRE_UI_CONFIRMATION,
            evidence=prepared.evidence,
        )
        file_grant_request = ActionRequest(
            category=ActionCategory.CREATE_OR_EDIT_FILE,
            target=str(prepared.destination / prepared.message.file_name),
            summary="Download one exact Telegram attachment.",
            parameters={"contact": prepared.request.contact, "fileName": prepared.message.file_name},
        )
        grant_for(file_grant_request, approval.decision)
        gui_grant = grant_for(
            _gui_request(prepared.request.contact, "Start one Telegram attachment download."),
            approval.decision,
        )
        evidence = WindowEvidence.from_binding(prepared.binding, clock=self._clock)
        if evidence.identity_token != prepared.evidence.identity_token:
            raise WorkflowStopped("window_changed", "Telegram was replaced since it was inspected.")
        guard = StepGuard(
            token=token, approval_valid=approval_valid, binder=self._binder,
            binding=prepared.binding, evidence=evidence, clock=self._clock,
        )

        # Postcondition of phase 1 is the precondition of phase 2: the same
        # chat is open and the candidate is still the newest incoming file.
        guard.checkpoint("re-verify chat")
        if name_key(self._ui.chat_title(prepared.binding)) != name_key(prepared.request.contact):
            raise WorkflowRefused("Another chat is open now; nothing was downloaded.")
        current = self._newest_incoming_document(prepared.binding)
        if (
            current.element_id != prepared.message.element_id
            or current.file_name != prepared.message.file_name
            or current.size_text != prepared.message.size_text
        ):
            raise WorkflowRefused(
                "The newest incoming file changed since it was shown for approval; nothing was downloaded."
            )

        if not _real_directory(prepared.destination):
            raise WorkflowRefused("The download folder is missing or is a link; nothing was downloaded.")
        before = dict(self._folder.listing())
        op = operation_key(approval.approval_id, prepared.digest)
        content = content_key(
            "telegram-download",
            name_key(prepared.request.contact),
            prepared.message.file_name.casefold(),
            prepared.message.size_text,
            str(prepared.destination).casefold(),
        )
        guard.checkpoint("download")
        self._journal.begin(op, content, f"Telegram {prepared.message.file_name}")
        try:
            guard.checkpoint("download")
            self._ui.start_download(prepared.binding, current, grant=gui_grant)
        except (WorkflowStopped, TelegramActionNotStarted) as not_started:
            self._journal.settle(op, SideEffectState.NOT_SUBMITTED, str(not_started))
            raise
        except Exception as error:  # noqa: BLE001 - outcome unknown once Windows was asked
            self._journal.settle(op, SideEffectState.UNCERTAIN, type(error).__name__)
            return WorkflowReport(
                outcome="uncertain",
                detail=(
                    "Qronos asked Telegram to download the file but could not confirm "
                    "whether it started. It will not try again; check Telegram."
                ),
                steps=guard.steps,
                result={"fileName": prepared.message.file_name},
            )

        try:
            return self._verify(prepared, op, before, token, guard)
        except Exception as error:  # noqa: BLE001 - the download may have happened
            self._journal.settle_if_unresolved(op, SideEffectState.UNCERTAIN, type(error).__name__)
            return WorkflowReport(
                outcome="uncertain",
                detail=(
                    "The download was started but could not be checked. Qronos will not "
                    "download it again; check the folder."
                ),
                steps=guard.steps,
                result={"fileName": prepared.message.file_name},
            )

    def _verify(self, prepared, op, before, token, guard) -> WorkflowReport:
        bounds = approximate_size(prepared.message.size_text)
        deadline = self._clock() + self._verify_timeout
        stable: dict[str, int] = {}
        while True:
            listing = dict(self._folder.listing())
            new = {
                name: size
                for name, (size, mtime) in listing.items()
                if name not in before and _matches_attachment(name, prepared.message.file_name)
            }
            if len(new) > 1:
                self._journal.settle(op, SideEffectState.UNCERTAIN, "several matching files appeared")
                return WorkflowReport(
                    outcome="uncertain",
                    detail="Several new files with that name appeared; Qronos cannot tell which is this download.",
                    steps=guard.steps,
                    result={"candidates": sorted(new)},
                )
            if len(new) == 1:
                (name, size), = new.items()
                plausible = bounds is None or bounds[0] <= size <= bounds[1]
                if stable.get(name) == size and size > 0 and plausible:
                    path = prepared.destination / name
                    if not _real_regular_file(path):
                        self._journal.settle(op, SideEffectState.UNCERTAIN, "result is not a regular file")
                        return WorkflowReport(
                            outcome="uncertain",
                            detail="The downloaded item is not a regular file; Qronos did not use it.",
                            steps=guard.steps,
                        )
                    self._journal.settle(op, SideEffectState.CONFIRMED, name)
                    return WorkflowReport(
                        outcome="succeeded",
                        detail="The file was downloaded.",
                        steps=guard.steps,
                        result={"path": str(path), "size": size, "fileName": name},
                    )
                stable = {name: size}
            if self._clock() >= deadline:
                self._journal.settle(op, SideEffectState.UNCERTAIN, "no complete file before timeout")
                return WorkflowReport(
                    outcome="uncertain",
                    detail=(
                        "The download did not finish in time or was saved elsewhere. "
                        "Qronos will not download it again; check Telegram's downloads."
                    ),
                    steps=guard.steps,
                )
            try:
                self._sleep(token, self._poll)
            except ActionCancelled:
                self._journal.settle(op, SideEffectState.UNCERTAIN, "stopped while verifying")
                return WorkflowReport(
                    outcome="uncertain",
                    detail=(
                        "Stopped while the download was being checked. Telegram may still "
                        "finish it; Qronos will not start it again."
                    ),
                    steps=guard.steps,
                )


def _cancellable_sleep(token: CancellationToken | None, seconds: float) -> None:
    if token is None:
        time.sleep(seconds)
    else:
        token.sleep(seconds)


_FORBIDDEN_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _safe_file_name(name: str) -> bool:
    if not name or len(name) > 255 or name in {".", ".."} or name != name.strip() or name.endswith("."):
        return False
    if _FORBIDDEN_NAME.search(name):
        return False
    return os.path.splitext(name)[0].casefold() not in _RESERVED


def _is_link(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & 0x400)


def _real_directory(path: Path) -> bool:
    return path.is_dir() and not any(_is_link(item) for item in (path, *path.parents) if item.exists())


def _real_regular_file(path: Path) -> bool:
    return path.is_file() and not _is_link(path)


# ------------------------------------------------------- production folder


class LocalDownloadFolder:
    """A real local folder, listed without following links."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def listing(self) -> Mapping[str, tuple[int, int]]:
        result: dict[str, tuple[int, int]] = {}
        with os.scandir(self._path) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    result[entry.name] = (int(stat.st_size), int(stat.st_mtime_ns))
        return result


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


# FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
_FOLDERID_DOWNLOADS = (0x374DE290, 0x123F, 0x4565, (0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))


def user_downloads_folder() -> Path:
    """The user's (possibly redirected) Downloads folder via SHGetKnownFolderPath."""
    if os.name != "nt":
        raise WorkflowRefused("The Windows Downloads folder is only available on Windows.")
    guid = _GUID(_FOLDERID_DOWNLOADS[0], _FOLDERID_DOWNLOADS[1], _FOLDERID_DOWNLOADS[2],
                 (ctypes.c_ubyte * 8)(*_FOLDERID_DOWNLOADS[3]))
    pointer = ctypes.c_wchar_p()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    function = shell32.SHGetKnownFolderPath
    function.argtypes = [ctypes.POINTER(_GUID), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    function.restype = ctypes.c_long
    result = function(ctypes.byref(guid), 0, None, ctypes.byref(pointer))
    try:
        if result != 0 or not pointer.value:
            raise WorkflowRefused("Windows did not report a Downloads folder.")
        return Path(pointer.value)
    finally:
        ole32.CoTaskMemFree(pointer)


# ------------------------------------------------- production UI adapter


_SEARCH_NAMES = {"search", "جستجو"}
_DOWNLOAD_NAMES = {"download", "دانلود"}
_OPEN_NAMES = {"open", "open file", "باز کردن"}
_DOCUMENT_LINE = re.compile(r"^(?P<name>[^\n]+?\.[A-Za-z0-9]{1,8})\s*[,·]?\s*(?P<size>[0-9][0-9.,]*\s*[A-Za-z؀-ۿ]+)?$")


def _chat_list_right(binding: WindowBinding, lists) -> float | None:
    """Right edge of the chat list: list items wholly inside the left 45% of the
    client area (Telegram's dialog column). Message bubbles extend further right."""
    client = binding.snapshot.client_rect
    boundary = client.left + 0.45 * client.width
    left_column = [item.right for item in lists if item.right <= boundary]
    return max(left_column) if left_column else None


class UiaTelegramUi:
    """
    Telegram for Windows through Windows UI Automation on one verified window.

    Reading uses ``actionable_elements_in_window``. Acting goes only through
    the reviewed :class:`core.accessibility_action_executor.AccessibilityActionExecutor`
    exact-RuntimeId paths (``set_exact_text_value`` for the search query,
    ``invoke_exact_element`` for opening a chat and starting a download): the
    element this adapter selected by its own exact rules is re-read by
    RuntimeId, must keep its name, role and control type, and is acted on with
    an authorization narrowed to that single element. No locator or model
    chooses anything.
    """

    def __init__(
        self,
        *,
        binder: WindowBinder,
        target_fn: Callable[[], object],
        actuator,
        elements_fn: Callable[[int], Sequence[object]] | None = None,
        settle_seconds: float = 0.6,
    ) -> None:
        from core import windows_accessibility

        self._binder = binder
        self._target_fn = target_fn
        self._actuator = actuator
        self._elements = elements_fn or (
            lambda hwnd: windows_accessibility.actionable_elements_in_window(hwnd, max_elements=2048)
        )
        self._settle = float(settle_seconds)
        self._by_id: dict[str, object] = {}

    def bind(self) -> WindowBinding:
        return self._binder.bind(self._target_fn())

    def _read(self, binding: WindowBinding):
        elements = tuple(self._elements(binding.hwnd))
        self._by_id = {item.runtime_id: item for item in elements if item.runtime_id}
        return elements

    def _element(self, runtime_id: str):
        element = self._by_id.get(runtime_id)
        if element is None:
            raise TelegramActionNotStarted("The selected Telegram control is no longer shown.")
        return element

    def search_contacts(self, binding, query, *, grant):
        fields = [
            item for item in self._read(binding)
            if item.role == "text_input" and name_key(item.name) in _SEARCH_NAMES
        ]
        if len(fields) != 1:
            raise TelegramActionNotStarted(
                "Telegram's search field could not be identified uniquely; nothing was typed."
            )
        self._actuator.set_exact_text_value(binding, fields[0], query, grant=grant)
        time.sleep(self._settle)
        field_bottom = fields[0].bottom
        return tuple(
            ContactCandidate(element_id=item.runtime_id, display_name=item.name.strip())
            for item in self._read(binding)
            if item.role == "list_item" and item.top >= field_bottom and item.name.strip()
        )

    def open_chat(self, binding, candidate, *, grant):
        self._actuator.invoke_exact_element(binding, self._element(candidate.element_id), grant=grant)
        time.sleep(self._settle)

    def chat_title(self, binding) -> str:
        # The chat header is the topmost text element right of the chat list.
        elements = self._read(binding)
        lists = [item for item in elements if item.role == "list_item"]
        list_right = _chat_list_right(binding, lists)
        if list_right is None:
            return ""
        headers = sorted(
            (
                item for item in elements
                if item.control_type.casefold().endswith(".text")
                and item.left >= list_right and item.name.strip()
            ),
            key=lambda item: (item.top, item.left),
        )
        return headers[0].name.strip() if headers else ""

    def recent_messages(self, binding, *, limit):
        elements = self._read(binding)
        lists = [item for item in elements if item.role == "list_item"]
        list_right = _chat_list_right(binding, lists)
        if list_right is None:
            return ()
        chat_items = [item for item in lists if item.left >= list_right]
        buttons = [item for item in elements if item.role == "button"]
        center_x = (list_right + binding.snapshot.client_rect.right) / 2.0
        messages = []
        for item in sorted(chat_items, key=lambda value: -value.bottom)[:limit]:
            inside = [
                button for button in buttons
                if item.left <= button.center[0] <= item.right and item.top <= button.center[1] <= item.bottom
            ]
            download = [button for button in inside if name_key(button.name) in _DOWNLOAD_NAMES]
            opened = [button for button in inside if name_key(button.name) in _OPEN_NAMES]
            line = _DOCUMENT_LINE.match(item.name.strip().splitlines()[0] if item.name.strip() else "")
            kind = "document" if line and (download or opened) else "unknown"
            messages.append(MessageView(
                element_id=item.runtime_id,
                # Incoming messages sit left of the chat area's centre line.
                incoming=(item.left + item.width / 2.0) < center_x,
                kind=kind,
                file_name=line.group("name").strip() if line else "",
                size_text=(line.group("size") or "").strip() if line else "",
                download_element_id=download[0].runtime_id if len(download) == 1 else "",
                downloaded=bool(opened) and not download,
            ))
        return tuple(messages)

    def start_download(self, binding, message, *, grant):
        if not message.download_element_id:
            raise TelegramActionNotStarted("The message has no download control.")
        element = self._element(message.download_element_id)
        self._actuator.invoke_exact_element(binding, element, grant=grant)
