from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import uuid
import wave
from pathlib import Path

import numpy as np
import time
from dataclasses import dataclass
from typing import Any, Callable


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="backslashreplace",
    )

if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(
        encoding="utf-8",
        errors="backslashreplace",
    )

from core.foreground_lifecycle import ForegroundBusy, ForegroundLifecycle
from core.inference_cancellation import LocalInferenceCancelled, inference_checkpoint
from core.action_audit import ActionAuditLog
from core import activity_history
from core.config import CONFIG
from core.actions import ActionOutcome, ActionResult
from core.app_workflow_runtime import (
    APP_WORKFLOW_DECISION_ACTION,
    APP_WORKFLOW_UNCERTAIN_ACTION,
    AppWorkflowCoordinator,
)
from core.file_action_parser import FileActionParseError, parse_file_action
from core.file_executor import FileAction, FileExecutor, FileOperation, PreparedFileAction
from core.file_resolver import (
    KnownFolderFileResolver,
    NamedFileAmbiguous,
    NamedFileNotFound,
    NamedFileParseError,
    NamedFileSearchLimit,
    NamedLocationNotFound,
    NamedLocationParseError,
    is_computer_capability_query,
    parse_location_request,
    parse_named_file_request,
)
from core.computer_action_parser import (
    ComputerActionParseError,
    parse_computer_action,
)
from core.computer_executor import (
    ComputerAction,
    ComputerExecutor,
    ComputerOperation,
    resolve_application_id,
)
from core.paint_draw_integration import (
    EnvironmentPaintReferenceProvider,
    PaintDrawExecutor,
    PaintReferenceProvider,
    PaintUndoExecution,
    PreparedPaintDrawAction,
    PreparedPaintUndo,
    paint_draw_failure_response_fa,
)
from core.paint_draw_parser import (
    PAINT_UNDO_AMBIGUOUS,
    PaintDrawParseError,
    classify_paint_undo_request,
    parse_paint_draw_action,
)
from core.audio_input import AudioConfig, AudioInput, resolve_input_device
from core.chatterbox_runtime import ChatterboxRuntime
from core.command_recorder import CommandRecorder, CommandRecorderConfig
from core.conversation_session import ConversationSession
from core.conversation_history import ConversationHistory
from core.hard_floor import required_vram_mb
from core.load_signal import LoadSample, SustainedLoadMonitor
from core.resource_context import build_sustained_load_monitor
from core.model_registry import MODELS
from core.openwakeword_engine import OpenWakeWordEngine
from core.orchestrator import Orchestrator
from core.spoken_normalizer import normalise_spoken_text
from core.settings_service import DEFAULT_DEFINITIONS, DEFAULT_SETTINGS_PATH, SettingsService
from core.screen_capture import (
    Capture,
    CaptureRefused,
    CaptureUnavailable,
    ScreenCapture,
    foreground_window,
)
from core.queue_scheduler import (
    DEFAULT_SCHEDULER_CONFIG,
    QueueEvent,
    QueueEventType,
    QueueScheduler,
    SchedulerConfig,
    WorkOutcome,
    WorkResult,
)
from core.queued_conversation import (
    ConversationPhase,
    ConversationQueueExecutor,
    ConversationRequest,
    QueuedConversationWork,
    build_request as build_queued_conversation_request,
)
from core.queue_journal import (
    QueueJournal,
    QueueRecord,
    RecordState,
    StorageProblem,
    StorageStatus,
    QueueStatePathError,
    default_queue_state_path,
)
from core.safe_queue import TaskState
from core.brain_runtime import BrainMessage, BrainMessageRole
from core.resource_governor import ResourceGovernor, Weight
from core.resource_policy import ResourceDecision
from core.safe_queue import SafeQueue
from core.task_plan import TaskPlan
from core.arithmetic_fast_path import solve_simple_arithmetic
from core.compute_estimator import ComputeEstimator
from core.intent_gate import IntentGate
from core.runtime_hooks import run_startup_status_hooks
from core.routing_input import RoutingInput
from core.task_router import TaskRouter, TaskType
from core.undo_journal import Reversal, UndoJournal
from core.approval_ledger import (
    STALE_SUPERSEDED,
    ApprovalKind,
    ApprovalLedger,
    ApprovalTicket,
    StaleApproval,
)
from core.action_cancellation import (
    CANCEL_REASON_PAUSE,
    CANCEL_REASON_SHUTDOWN,
    CANCEL_REASON_USER_STOP,
    ActiveActionRegistry,
)
from core.vision_ocr import read_screen_text
from core.vision_worker import build_vision_worker
from core.whisper_cpp_runtime import WhisperCppRuntime
from core.whisper_hybrid_runtime import WhisperHybridRuntime
from core.whisper_cpp_vad_runtime import WhisperCppVADRuntime
from security.gate import set_default_audit_sink
from security.permissions import PermissionDecision
from core.voice_trigger import VoiceTriggerService


PUSH_TO_TALK_ACTION = "qronos.push_to_talk"
LOOK_AT_SCREEN_ACTION = "qronos.look_at_screen"
WAKE_LISTENER_START_ACTION = "qronos.wake_listener_start"
WAKE_LISTENER_STOP_ACTION = "qronos.wake_listener_stop"
SETTINGS_RELOAD_ACTION = "qronos.settings_reload"
INPUT_METER_START_ACTION = "qronos.input_meter_start"
INPUT_METER_STOP_ACTION = "qronos.input_meter_stop"
TEXT_MESSAGE_COMMAND = "text_message"
_FOREGROUND_INITIALIZATION_LOCK = threading.Lock()
RUNTIME_SET_PAUSED_COMMAND = "runtime_set_paused"
DESKTOP_WAKE_BOOTSTRAP_ENV = "QRONOS_DESKTOP_WAKE_BOOTSTRAP"
CONVERSATION_HISTORY_REQUEST_ACTION = "qronos.conversation_history_request"
FILE_ACTION_DECISION_ACTION = "qronos.file_action_decision"
# The desktop's activity history (core/activity_history.py): a read-only snapshot, and undo of one supported file
# change behind a single-use history confirmation (plus the typed secret when the gate requires it).
HISTORY_REQUEST_ACTION = "qronos.history_request"
HISTORY_UNDO_REQUEST_ACTION = "qronos.history_undo_request"
HISTORY_UNDO_DECISION_ACTION = "qronos.history_undo_decision"
FILE_WRITE_APPROVAL_LEASE_SECONDS = 10 * 60.0
MAX_TEXT_MESSAGE_LENGTH = 16_000
MAX_TEXT_REQUEST_ID_LENGTH = 128


@dataclass(frozen=True)
class PendingFileTextAction:
    token: str
    prepared: PreparedFileAction
    request_id: str
    started_at: float


@dataclass(frozen=True)
class PendingHistoryUndo:
    action_id: str
    needs_secret: bool


@dataclass(frozen=True)
class PendingPaintDrawAction:
    prepared: PreparedPaintDrawAction
    gui_approval: PermissionDecision | None
    transcript: str


@dataclass(frozen=True)
class PendingPaintUndoAction:
    prepared: PreparedPaintUndo
    gui_approval: PermissionDecision | None
    transcript: str




VOICE_PLAYBACK_STARTED_ACTION = "qronos.voice_playback_started"
VOICE_PLAYBACK_COMPLETE_ACTION = "qronos.voice_playback_complete"
STOP_RESPONSE_ACTION = "qronos.stop_response"
# The desktop's Device Link page (docs/QRONOS_DEVICE_LINK.md 5.7).
LINK_STATUS_ACTION = "qronos.link_status"
LINK_DEVICES_ACTION = "qronos.link_devices"
LINK_PAIR_OPEN_ACTION = "qronos.link_pair_open"
LINK_PAIR_CANCEL_ACTION = "qronos.link_pair_cancel"
LINK_REVOKE_ACTION = "qronos.link_revoke"
LINK_REMOTE_ACTION = "qronos.link_remote"
LINK_RELAY_SET_ACTION = "qronos.link_relay_set"
# Console-only: clears the Security Monitor's containment/quarantine (Link D9).
LINK_SECURITY_RESET_ACTION = "qronos.link_security_reset"
# The media product route (qronos_media.product): typed {op, requestId, ...} operations on the one media host.
MEDIA_ACTION = "qronos.media"
# The Device Hub (core/device_hub.py): typed {op, requestId, ...} operations on local spooler/driver devices.
DEVICE_HUB_ACTION = "qronos.device_hub"
#: How long a brain/voice media request waits for its typed result before answering honestly that it did not.
MEDIA_INTENT_WAIT_SECONDS = 12.0
LINK_ACTIONS = frozenset({
    LINK_STATUS_ACTION, LINK_DEVICES_ACTION, LINK_PAIR_OPEN_ACTION, LINK_PAIR_CANCEL_ACTION,
    LINK_REVOKE_ACTION, LINK_REMOTE_ACTION, LINK_RELAY_SET_ACTION, LINK_SECURITY_RESET_ACTION,
})

# How long shutdown waits for cancelled GUI actions to release input state.
COMPUTER_ACTION_SHUTDOWN_GRACE_SECONDS = 2.0


WAKE_PLAYBACK_GUARD_SECONDS = 0.35
FOLLOWUP_START_TIMEOUT_SECONDS = 12.0
FOLLOWUP_PLAYBACK_GUARD_SECONDS = 0.25
WAKE_STARTUP_RETRY_DELAYS_SECONDS = (0.35, 0.8, 1.6)
WAKE_COMMAND_PREP_BUFFER_LIMIT_SECONDS = 30.0
WAKE_COMMAND_PREP_RETRY_DELAYS_SECONDS = (0.0, 0.5, 1.5)

# Desktop owns actual audio playback. Python waits for an explicit playback
# completion acknowledgement instead of guessing from WAV duration.
#
# This limit is only a deadlock failsafe for a crashed/disconnected desktop.
# It does NOT close the conversation.
PLAYBACK_ACK_FAILSAFE_SECONDS = 300.0

# A wake-word candidate needs stronger speech evidence than an
# already-established follow-up turn. This prevents short wind/noise
# bursts from becoming phantom commands after a false wake.
WAKE_MIN_COMMAND_SPEECH_SECONDS = 0.40

VOICE_RETRY_RESPONSE = (
    "این درخواست اجرا نشد. دوباره بگو."
)
VOICE_STT_RETRY_RESPONSE = (
    "متوجه نشدم. دوباره بگو."
)
VOICE_COMPUTER_DEFERRED_RESPONSE = (
    "این فرمان هنوز در Windows Control فعلی پشتیبانی نمی‌شود."
)

AUDIO_SPECTRUM_BANDS = 32

# The accepted production Wake threshold is 0.660167. The UI sensitivity slider
# is intentionally constrained to a conservative band around it rather than
# exposing 0.0..1.0 directly. Higher user sensitivity means a lower threshold.
WAKE_SENSITIVITY_DEFAULT = 56
WAKE_THRESHOLD_ACCEPTED = 0.660167
WAKE_THRESHOLD_LOW_SENSITIVITY = 0.72
WAKE_THRESHOLD_HIGH_SENSITIVITY = 0.62


def wake_sensitivity_to_threshold(
    sensitivity: int | float,
) -> float:
    value = max(
        0.0,
        min(
            100.0,
            float(sensitivity),
        ),
    )

    if value <= WAKE_SENSITIVITY_DEFAULT:
        span = float(WAKE_SENSITIVITY_DEFAULT)
        ratio = 0.0 if span == 0.0 else value / span
        threshold = (
            WAKE_THRESHOLD_LOW_SENSITIVITY
            + (
                WAKE_THRESHOLD_ACCEPTED
                - WAKE_THRESHOLD_LOW_SENSITIVITY
            )
            * ratio
        )
    else:
        span = 100.0 - float(WAKE_SENSITIVITY_DEFAULT)
        ratio = (
            value - float(WAKE_SENSITIVITY_DEFAULT)
        ) / span
        threshold = (
            WAKE_THRESHOLD_ACCEPTED
            + (
                WAKE_THRESHOLD_HIGH_SENSITIVITY
                - WAKE_THRESHOLD_ACCEPTED
            )
            * ratio
        )

    return round(
        threshold,
        6,
    )


def _runtime_settings_path() -> Path:
    override = os.environ.get(
        "QRONOS_SETTINGS_PATH",
        "",
    ).strip()

    return (
        Path(override)
        if override
        else DEFAULT_SETTINGS_PATH
    )


def _desktop_wake_bootstrap_enabled() -> bool:
    """
    Return True only for the real desktop runtime process.

    Raw bridge tests intentionally remain side-effect free. The Tauri desktop
    sets this flag so Wake becomes a startup invariant before runtime_ready.
    """
    return (
        os.environ.get(
            DESKTOP_WAKE_BOOTSTRAP_ENV,
            "",
        ).strip()
        == "1"
    )


def _preload_openwakeword_for_desktop() -> None:
    """
    Import OpenWakeWord on the main startup thread before Wake is launched.

    openwakeword.__init__ imports sklearn/scipy. Doing that work before the
    stdin command loop prevents the first Wake thread from competing with
    desktop startup work and keeps the wake gate on the highest-priority path.
    """
    from openwakeword.model import Model as _WakeModel

    _ = _WakeModel


EMIT_LOCK = threading.Lock()

CONVERSATION_END_PHRASES = {
    "تمام",
    "تموم",
    "تمام شد",
    "تموم شد",
    "خداحافظ",
    "بسه",
    "ممنون تمومه",
    "ممنون تمامه",
    "مرسی تمومه",
}

VOICE_LATENCY_LOG_PATH = (
    CONFIG.paths.voice_temp
    / "voice_latency_latest.json"
)

VOICE_PLAYBACK_TELEMETRY_PATH = (
    CONFIG.paths.voice_temp
    / "voice_playback_latest.json"
)


def normalise_qronos_invocation(transcript: str) -> str:
    """Correct measured Whisper variants only when Qronos is addressed."""
    return re.sub(
        r"^(?P<greeting>\s*\u0633\u0644\u0627\u0645\s+)?(?:\u06a9\u0631\u0648\u0646\u0633|\u062e\u0631\u0648\u0646\u0633|\u06a9\u0631\u0648\u0646\u0632)(?=\s|[\u060c,\u061f?]|$)",
        lambda match: f"{match.group('greeting') or ''}\u06a9\u0631\u0648\u0646\u0648\u0633",
        transcript,
        count=1,
    )




def _voice_action_transcript_quality(text: str) -> int:
    """Score action-command structure without trying to repair arbitrary prose."""
    n = re.sub(r"[\s\u200c]+", " ", (text or "").strip().lower())
    if not n:
        return 0

    score = 0
    if any(marker in n for marker in (
        "باز کن", "بازش کن", "برو ", "ببند", "بیار جلو", "مینیمایز", "ماکسیمایز",
        "open ", "launch ", "close ", "focus ", "minimize ", "maximize ", "go to",
        "بکش", "رسم کن", "نقاشی کن", "draw ", "sketch ",
    )):
        score += 6
    if any(marker in n for marker in (
        "دسکتاپ", "دسکتاب", "desktop", "دانلود", "downloads",
        "documents", "اسناد", "درایو", "drive",
    )):
        score += 4
    if any(marker in n for marker in (
        "فایل", "نوت", "یادداشت", "file", "note", "document",
        "داکیومنت", "دایکیومنت", "folder", "فولدر", "پوشه",
    )):
        score += 3
    if any(marker in n for marker in (
        "notepad", "نوت پد", "calculator", "paint", "پینت", "پاینت", "explorer", "telegram", "تلگرام", "premiere", "پریمیر",
    )):
        score += 3
    if is_computer_capability_query(text):
        score -= 8
    return score


def _voice_transcript_looks_actionish(text: str) -> bool:
    n = re.sub(r"[\s\u200c]+", " ", (text or "").strip().lower())
    return any(marker in n for marker in (
        "دسکتاپ", "دسکتاب", "desktop", "دانلود", "download",
        "documents", "اسناد", "درایو", "drive", "فایل", "file",
        "document", "داکیومنت", "دایکیومنت", "folder", "فولدر",
        "notepad", "نوت پد", "calculator", "paint", "پینت", "پاینت", "explorer", "telegram", "تلگرام", "premiere", "پریمیر",
    ))


def _windows_control_route_promotion(text: str) -> bool:
    """Conservatively promote only explicit Windows-control commands."""
    n = re.sub(r"[\s\u200c]+", " ", (text or "").strip().lower())
    if not n:
        return False
    has_action = any(marker in n for marker in (
        "باز کن", "بازش کن", "اجرا کن", "برو ", "ببند", "بیار جلو",
        "مینیمایز", "ماکسیمایز", "فوکوس",
        "open ", "launch ", "start ", "run ", "go to", "close ",
        "focus ", "minimize ", "minimise ", "maximize ", "maximise ",
        "بکش", "رسم کن", "نقاشی کن", "draw ", "sketch ",
    ))
    has_target = any(marker in n for marker in (
        "دسکتاپ", "دسکتاب", "desktop", "دانلود", "downloads", "download",
        "documents", "اسناد", "درایو", "drive", "فایل", "file", "folder",
        "فولدر", "پوشه", "notepad", "نوت پد", "calculator", "paint", "پینت", "پاینت",
        "explorer", "telegram", "تلگرام", "premiere", "پریمیر", "برنامه",
        "نرم افزار", "نرم‌افزار", "app", "application",
    ))
    if not has_action:
        return False
    if has_target:
        return True

    # Installed applications are dynamic and cannot all be hard-coded into
    # TaskRouter.  Before allowing an explicit open/close/focus command to fall
    # through to conversational FAST, verify that its target resolves in the
    # local Windows catalog.  Parsing also strips polite Persian fillers and
    # keeps file/folder ownership out of this branch.
    try:
        parse_computer_action(
            text,
            app_resolver=lambda query: resolve_application_id(
                query,
                dynamic=True,
            ),
        )
    except ComputerActionParseError:
        return False
    return True



def _calculator_action_candidate(
    text: str,
    last_app_id: str | None = None,
) -> str:
    value = str(
        text
        or ""
    )

    value = (
        value
        .replace("\u200c", " ")
        .replace("\u064a", "\u06cc")
        .replace("\u0643", "\u06a9")
        .lower()
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    aliases = (
        "\u06a9\u0644\u06a9\u0648\u0644\u06cc\u062a\u0631",
        "\u06a9\u0627\u0644\u06a9\u0648\u0644\u06cc\u062a\u0631",
        "\u06a9\u0644\u06a9\u06cc\u0648\u0644\u06cc\u062a\u0648\u0631",
        "\u06a9\u0644\u06a9\u06cc\u0644\u06cc\u062a\u0648\u0631",
        "\u06a9\u0644\u06a9\u0648\u0644\u0627\u062a\u0648\u0631",
    )

    for alias in aliases:
        value = value.replace(
            alias,
            "\u0645\u0627\u0634\u06cc\u0646 \u062d\u0633\u0627\u0628",
        )

    replacements = (
        (
            "\u0628\u0647 \u0627\u0636\u0627\u0641\u0647",
            "\u0628\u0647 \u0639\u0644\u0627\u0648\u0647",
        ),
        (
            "\u0632\u0628\u062f\u0627\u0631",
            "\u0636\u0631\u0628\u062f\u0631",
        ),
        (
            "\u0632\u0628\u062f\u0631\u0647",
            "\u0636\u0631\u0628\u062f\u0631",
        ),
        (
            "\u0632\u0628\u062f\u0631",
            "\u0636\u0631\u0628\u062f\u0631",
        ),
        (
            "\u0632\u0628 \u062f\u0631",
            "\u0636\u0631\u0628\u062f\u0631",
        ),
        (
            "\u0632 \u0628 \u062f\u0631",
            "\u0636\u0631\u0628\u062f\u0631",
        ),
        (
            "\u0630\u0631 \u0628\u0647 \u062f\u0631",
            "\u0636\u0631\u0628\u062f\u0631",
        ),
        (
            "\u0636\u0631\u0628 \u062f\u0631",
            "\u0636\u0631\u0628\u062f\u0631",
        ),
        (
            "\u062f\u0648\u0631 \u0648",
            "\u062f\u0648 \u0631\u0648",
        ),
    )

    for source, target in replacements:
        value = value.replace(
            source,
            target,
        )

    explicit = any(
        marker in value
        for marker in (
            "\u0645\u0627\u0634\u06cc\u0646 \u062d\u0633\u0627\u0628",
            "calculator",
            "calc",
        )
    )

    if (
        not explicit
        and last_app_id == "calculator"
    ):
        value = (
            "\u0645\u0627\u0634\u06cc\u0646 \u062d\u0633\u0627\u0628 "
            + value
        )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def _looks_like_calculator_semantic_action(
    text: str,
    last_app_id: str | None = None,
) -> bool:
    candidate = (
        _calculator_action_candidate(
            text,
            last_app_id,
        )
    )

    explicit = any(
        marker in candidate
        for marker in (
            "\u0645\u0627\u0634\u06cc\u0646 \u062d\u0633\u0627\u0628",
            "calculator",
            "calc",
        )
    )

    operators = (
        "\u0628\u0647 \u0639\u0644\u0627\u0648\u0647",
        "\u0628\u0639\u0644\u0627\u0648\u0647",
        "\u062c\u0645\u0639",
        "\u0645\u0646\u0647\u0627\u06cc",
        "\u0636\u0631\u0628",
        "\u0636\u0631\u0628\u062f\u0631",
        "\u062a\u0642\u0633\u06cc\u0645",
        "plus",
        "minus",
        "multiply",
        "times",
        "divide",
        "\u00d7",
    )

    number_words = (
        "\u0635\u0641\u0631",
        "\u06cc\u06a9",
        "\u062f\u0648",
        "\u0633\u0647",
        "\u0686\u0647\u0627\u0631",
        "\u067e\u0646\u062c",
        "\u0634\u0634",
        "\u0647\u0641\u062a",
        "\u0647\u0634\u062a",
        "\u0646\u0647",
    )

    has_operator = any(
        marker in candidate
        for marker in operators
    )

    has_number = (
        any(
            char.isdigit()
            for char in candidate
        )
        or any(
            word in candidate
            for word in number_words
        )
    )

    imperative = any(
        marker in candidate
        for marker in (
            " \u06a9\u0646",
            " \u0628\u06a9\u0646",
            " \u0628\u06a9\u0646\u06cc",
            " \u0628\u06a9\u0646\u06cc\u0645",
            " \u0628\u0632\u0646",
            "\u0645\u0633\u0627\u0648\u06cc",
            "\u062d\u0633\u0627\u0628 \u06a9\u0646",
        )
    )

    if not has_operator or not has_number:
        return False

    if explicit:
        return True

    return (
        last_app_id == "calculator"
        and imperative
    )

def _computer_capability_response() -> str:
    return (
        "می‌توانم پوشه‌ها و Driveهای محلی را باز کنم، فایل را با بخشی از اسمش "
        "پیدا و باز کنم، و برنامه‌های نصب‌شده‌ای را که از Start Menu یا App Paths "
        "شناخته می‌شوند باز کنم. برای پنجرهٔ برنامه هم Open، Close، Focus، "
        "Minimize و Maximize پشتیبانی می‌شود. فرمان‌های Shell و Terminal همچنان "
        "خارج از Windows Control هستند."
    )


COMPUTER_ACTION_CANCELLED_RESPONSE = "عملیات با دستور توقف متوقف شد و ادامه نیافت."


PAINT_UNDO_RESPONSES = {
    "ambiguous": (
        "درخواست هم بازگردانی و هم کشیدن در Paint را داشت؛ هیچ کاری انجام نشد. "
        "لطفاً فقط یکی را بگو."
    ),
    "nothing": "هیچ نقاشی قابل بازگردانی از Qronos ثبت نشده است.",
    "not_paint": (
        "آخرین تغییر قابل بازگردانی Qronos مربوط به Paint نیست؛ برای اینکه "
        "کار دیگری برگردانده نشود، در Paint هیچ کلیدی ارسال نشد."
    ),
    "unavailable": (
        "بازگردانی خودکار برای آخرین نقاشی Qronos در دسترس نیست؛ "
        "هیچ Ctrl+Z ای ارسال نشد."
    ),
    "pending": "یک درخواست Paint دیگر هنوز منتظر تأیید صفحه است.",
    "refused": "این عملیات طبق تنظیمات مجوز فعلی اجازه اجرا ندارد.",
    "other_approval": "بازگردانی Paint به نوع دیگری از تأیید نیاز دارد و اجرا نشد.",
    "voice_needed": (
        "بازگردانی نقاشی در Paint طبق مجوز فعلی به تأیید صوتی نیاز دارد. "
        "همین فرمان را با Voice بگو؛ برای مشاهدهٔ محدود بوم نیز تأیید روی "
        "صفحه خواسته می‌شود."
    ),
}


def _paint_undo_response(execution: PaintUndoExecution) -> str:
    """Never claims success unless the canvas was verified restored."""
    outcome = execution.result.outcome
    steps = execution.steps_sent
    if outcome is ActionOutcome.SUCCEEDED and execution.restored:
        return (
            "انجام شد. فقط تغییراتی که Qronos در Paint کشیده بود برگردانده شد "
            "و بوم با حالت پیش از نقاشی تطبیق داده شد."
        )
    if outcome is ActionOutcome.AWAITING_APPROVAL:
        return "مجوزها هنگام اجرا تغییر کردند و بازگردانی انجام نشد."
    if outcome is ActionOutcome.CANCELLED:
        if not steps:
            return "بازگردانی پیش از ارسال هر کلیدی متوقف شد."
        return (
            f"بازگردانی با دستور توقف نیمه‌کاره ماند؛ {steps} مرحله برگشت و "
            "Qronos دوباره تلاش نمی‌کند."
        )
    if steps:
        return (
            f"بازگردانی کامل نشد؛ {steps} مرحله برگشت اما بوم با حالت پیش از "
            "نقاشی تطبیق ندارد. Qronos دوباره تلاش نمی‌کند؛ Redo در خود Paint "
            "می‌تواند آن مراحل را برگرداند."
        )
    return (
        "بازگردانی انجام نشد و هیچ کلیدی ارسال نشد؛ شرایط امن برقرار نبود "
        "(مثلاً بوم بعد از نقاشی تغییر کرده، پنجره عوض شده یا شواهد آن دیگر "
        "نگه داشته نمی‌شود)."
    )


def _calculator_cancelled_response(cancelled: Any) -> str:
    """Honest summary of a Calculator sequence stopped part-way."""
    if not cancelled.steps_invoked:
        return COMPUTER_ACTION_CANCELLED_RESPONSE
    return (
        "محاسبه با دستور توقف متوقف شد؛ "
        f"{cancelled.steps_invoked} از {cancelled.steps_total} دکمه زده شد "
        "و ممکن است عبارت نیمه‌کاره در Calculator مانده باشد."
    )


def _paint_draw_cancelled_response(execution: Any) -> str:
    """Honest user-facing summary of a Paint drawing stopped by the user."""
    effects = execution.side_effects
    if not effects.any:
        return "نقاشی پیش از هر تغییری در Paint متوقف شد."
    if not effects.touched_canvas:
        if effects.toolbar_clicks:
            return (
                "نقاشی پیش از کشیدن روی بوم متوقف شد؛ "
                "ابزار، رنگ یا ضخامت انتخاب‌شدهٔ Paint ممکن است تغییر کرده باشد."
            )
        return "نقاشی پیش از کشیدن روی بوم متوقف شد؛ فقط Paint باز یا فعال شد."
    response = (
        "نقاشی با دستور توقف متوقف شد و دکمهٔ ماوس رها شد؛ "
        f"{effects.strokes_completed} خط از {effects.strokes_planned} خط کامل شد "
        "و هیچ ادامه یا تلاش دوباره‌ای انجام نمی‌شود."
    )
    if not execution.undo_available:
        response += " بازگردانی خودکار این تغییرات در دسترس نیست."
    return response


def _computer_action_success_response(
    operation: ComputerOperation,
    display_name: str,
) -> str:
    """Render an operation-specific acknowledgement using a display name."""
    verbs = {
        ComputerOperation.OPEN_APP: "باز شد",
        ComputerOperation.CLOSE_APP: "بسته شد",
        ComputerOperation.FOCUS_WINDOW: "به جلو آورده شد",
        ComputerOperation.MINIMIZE_WINDOW: "مینیمایز شد",
        ComputerOperation.MAXIMIZE_WINDOW: "ماکسیمایز شد",
        ComputerOperation.OPEN_LOCATION: "باز شد",
        ComputerOperation.DRAW_BASIC_HOUSE: "یک خانه‌ی ساده در آن رسم شد",
    }
    return f"انجام شد. {display_name} {verbs[operation]}."


def _normalise_conversation_control_text(
    text: str,
) -> str:
    cleaned = re.sub(
        r"[\s\u200c]+",
        " ",
        (text or "").strip().lower(),
    )

    cleaned = re.sub(
        r"[.!?؟،,؛;:]+$",
        "",
        cleaned,
    ).strip()

    return cleaned


def _is_conversation_end_phrase(
    text: str,
) -> bool:
    return (
        _normalise_conversation_control_text(
            text
        )
        in CONVERSATION_END_PHRASES
    )


VOICE_SPECTRUM_FRAME_SECONDS = 0.08
VOICE_SPECTRUM_BANDS = 32


def _write_voice_spectrum_sidecar(
    audio_path: str | Path,
) -> Path:
    """
    Precompute Qronos playback spectrum with the same analyser used for
    microphone input.

    The microphone path calls CommandRecorder._analyze_audio_frame() on
    80 ms PCM frames. Reusing that exact function here keeps playback and
    microphone visuals on the same normalization, RMS envelope, Hann window,
    logarithmic frequency bands, and per-frame band scaling.
    """
    source = Path(audio_path)

    with wave.open(
        str(source),
        "rb",
    ) as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        raw = wav_file.readframes(
            wav_file.getnframes()
        )

    if sample_width != 2:
        raise RuntimeError(
            "Qronos spectrum sidecar requires 16-bit PCM WAV audio."
        )

    samples = np.frombuffer(
        raw,
        dtype=np.int16,
    )

    if channels > 1:
        usable = (
            samples.size
            // channels
            * channels
        )

        samples = (
            samples[:usable]
            .reshape(-1, channels)
            .astype(np.float32)
            .mean(axis=1)
            .clip(-32768, 32767)
            .astype(np.int16)
        )

    frame_size = max(
        1,
        round(
            sample_rate
            * VOICE_SPECTRUM_FRAME_SECONDS
        ),
    )

    frames: list[dict[str, object]] = []

    for start in range(
        0,
        samples.size,
        frame_size,
    ):
        frame = samples[
            start:start + frame_size
        ]

        if frame.size < frame_size:
            padded = np.zeros(
                frame_size,
                dtype=np.int16,
            )
            padded[:frame.size] = frame
            frame = padded

        level, bands = (
            CommandRecorder._analyze_audio_frame(
                frame.tobytes(),
                sample_rate,
                VOICE_SPECTRUM_BANDS,
            )
        )

        frames.append(
            {
                "level": round(
                    float(level),
                    4,
                ),
                "bands": [
                    round(
                        float(value),
                        4,
                    )
                    for value in bands
                ],
            }
        )

    sidecar = source.with_suffix(
        ".spectrum.json"
    )

    sidecar.write_text(
        json.dumps(
            {
                "frameSeconds":
                    VOICE_SPECTRUM_FRAME_SECONDS,
                "frames": frames,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    return sidecar


def _analyze_voice_wav_quality(
    audio_path: str | Path,
) -> dict[str, float | int]:
    """
    Record objective PCM indicators that help separate generated-WAV artifacts
    from playback-device artifacts. These metrics do not attempt to "score"
    subjective voice quality.
    """
    source = Path(audio_path)

    with wave.open(
        str(source),
        "rb",
    ) as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
        raw = wav_file.readframes(
            frame_count
        )

    if sample_width != 2:
        return {
            "sampleRate": int(sample_rate),
            "channels": int(channels),
            "sampleWidthBytes": int(sample_width),
        }

    samples = np.frombuffer(
        raw,
        dtype=np.int16,
    ).astype(np.float64)

    if samples.size == 0:
        return {
            "sampleRate": int(sample_rate),
            "channels": int(channels),
            "sampleWidthBytes": int(sample_width),
            "sampleCount": 0,
        }

    if channels > 1:
        usable = (
            samples.size
            // channels
            * channels
        )
        samples = (
            samples[:usable]
            .reshape(-1, channels)
            .mean(axis=1)
        )

    normalized = (
        samples / 32768.0
    )

    peak = float(
        np.max(
            np.abs(normalized)
        )
    )
    rms = float(
        np.sqrt(
            np.mean(
                normalized * normalized
            )
        )
    )

    clipped_fraction = float(
        np.mean(
            np.abs(samples) >= 32760.0
        )
    )
    dc_offset = float(
        np.mean(normalized)
    )

    peak_dbfs = (
        20.0 * math.log10(peak)
        if peak > 0.0
        else -120.0
    )
    rms_dbfs = (
        20.0 * math.log10(rms)
        if rms > 0.0
        else -120.0
    )

    # A large share of energy above ~8 kHz can correlate with hiss/static,
    # though speech content and sample rate matter. Keep this diagnostic only.
    high_frequency_ratio = 0.0

    if (
        sample_rate >= 20_000
        and normalized.size >= 1024
    ):
        window = np.hanning(
            normalized.size
        )
        spectrum = np.fft.rfft(
            normalized * window
        )
        power = (
            np.abs(spectrum) ** 2
        )
        freqs = np.fft.rfftfreq(
            normalized.size,
            d=1.0 / sample_rate,
        )
        total_power = float(
            np.sum(power)
        )

        if total_power > 0.0:
            high_frequency_ratio = float(
                np.sum(
                    power[
                        freqs >= 8_000.0
                    ]
                )
                / total_power
            )

    return {
        "sampleRate": int(sample_rate),
        "channels": int(channels),
        "sampleWidthBytes": int(sample_width),
        "sampleCount": int(
            normalized.size
        ),
        "peakDbfs": round(
            peak_dbfs,
            3,
        ),
        "rmsDbfs": round(
            rms_dbfs,
            3,
        ),
        "clippedSamplePercent": round(
            clipped_fraction * 100.0,
            5,
        ),
        "dcOffset": round(
            dc_offset,
            6,
        ),
        "highFrequencyEnergyPercent": round(
            high_frequency_ratio * 100.0,
            3,
        ),
    }


TTS_FIRST_CHUNK_TARGET_CHARACTERS = 260
TTS_LATER_CHUNK_TARGET_CHARACTERS = 440
TTS_MIN_FIRST_CHUNK_CHARACTERS = 230
TTS_MIN_CHUNK_CHARACTERS = 96
TTS_MAX_CHUNK_CHARACTERS = 520


def _split_text_to_word_budget(
    text: str,
    max_characters: int,
) -> tuple[str, str]:
    """Split text at the largest word boundary that fits max_characters."""
    cleaned = re.sub(
        r"\s+",
        " ",
        (text or "").strip(),
    )

    if not cleaned:
        return "", ""

    if len(cleaned) <= max_characters:
        return cleaned, ""

    words = cleaned.split(" ")
    prefix_words: list[str] = []
    used = 0

    for word in words:
        added = len(word) if not prefix_words else len(word) + 1

        if prefix_words and used + added > max_characters:
            break

        prefix_words.append(word)
        used += added

        if used >= max_characters:
            break

    if not prefix_words:
        # A single pathological token can exceed the budget. Split it rather
        # than allowing an unbounded TTS request.
        return (
            cleaned[:max_characters],
            cleaned[max_characters:].lstrip(),
        )

    prefix = " ".join(prefix_words)
    remainder = cleaned[len(prefix):].lstrip()

    return prefix, remainder


def _split_semantic_tts_chunks(
    text: str,
) -> list[str]:
    """
    Split one finished Brain response into bounded speech chunks.

    Continuity matters more than preserving every punctuation boundary.
    R5 showed that a semantically neat but only ~145-character first chunk
    could end before Chatterbox finished chunk 2, producing audible buffering.
    R7 therefore:
    - fills the first chunk to a real minimum before playback can start,
    - still prefers punctuation when the buffer is already healthy,
    - splits overlong clauses at word boundaries,
    - caps every chunk so very long TTS requests do not degrade quality.
    """
    cleaned = re.sub(
        r"\s+",
        " ",
        (text or "").strip(),
    )

    if not cleaned:
        return []

    semantic_pieces = [
        piece.strip()
        for piece in re.split(
            r"(?<=[.!?؟؛])\s+|(?<=[،,:])\s+",
            cleaned,
        )
        if piece.strip()
    ]

    if not semantic_pieces:
        semantic_pieces = [cleaned]

    # No individual semantic piece may bypass the hard quality cap.
    pieces: list[str] = []

    for piece in semantic_pieces:
        remainder = piece

        while remainder:
            prefix, remainder = (
                _split_text_to_word_budget(
                    remainder,
                    TTS_MAX_CHUNK_CHARACTERS,
                )
            )

            if prefix:
                pieces.append(prefix)

    chunks: list[str] = []
    current = ""
    pending = list(pieces)

    while pending:
        piece = pending.pop(0)

        target = (
            TTS_FIRST_CHUNK_TARGET_CHARACTERS
            if not chunks
            else TTS_LATER_CHUNK_TARGET_CHARACTERS
        )
        minimum = (
            TTS_MIN_FIRST_CHUNK_CHARACTERS
            if not chunks
            else TTS_MIN_CHUNK_CHARACTERS
        )

        if not current:
            current = piece

        else:
            candidate = f"{current} {piece}"

            if len(candidate) <= target:
                current = candidate
            elif len(current) < minimum:
                # R5's main failure mode: the first semantic sentence could
                # be much shorter than the target. Borrow only enough words
                # from the next piece to create a useful playback buffer
                # instead of swallowing that whole next sentence.
                needed = max(
                    1,
                    min(
                        target - len(current) - 1,
                        TTS_MAX_CHUNK_CHARACTERS
                        - len(current)
                        - 1,
                    ),
                )

                prefix, remainder = (
                    _split_text_to_word_budget(
                        piece,
                        needed,
                    )
                )

                if prefix:
                    current = (
                        f"{current} {prefix}"
                    ).strip()

                if remainder:
                    pending.insert(
                        0,
                        remainder,
                    )
            else:
                chunks.append(current)
                current = piece

        if len(current) >= TTS_MAX_CHUNK_CHARACTERS:
            prefix, remainder = (
                _split_text_to_word_budget(
                    current,
                    TTS_MAX_CHUNK_CHARACTERS,
                )
            )
            chunks.append(prefix)
            current = ""

            if remainder:
                pending.insert(
                    0,
                    remainder,
                )

    if current:
        chunks.append(current)

    # Merge only a tiny final fragment when doing so stays below the hard cap.
    if (
        len(chunks) >= 2
        and len(chunks[-1]) < TTS_MIN_CHUNK_CHARACTERS
        and (
            len(chunks[-2])
            + 1
            + len(chunks[-1])
            <= TTS_MAX_CHUNK_CHARACTERS
        )
    ):
        chunks[-2] = (
            f"{chunks[-2]} {chunks[-1]}"
        ).strip()
        chunks.pop()

    return chunks


@dataclass(frozen=True)
class RuntimeEvent:
    event_type: str
    status: str
    message: str


def _telegram_download_folder() -> Path | None:
    """The exact folder Telegram saves into, when configured.

    Telegram decides where a download is saved; Qronos verifies it there and
    never searches elsewhere. The folder is an explicit, absolute, existing
    destination, shown in the download approval; Qronos never guesses it.
    """
    value = os.environ.get("QRONOS_TELEGRAM_DOWNLOAD_FOLDER", "").strip()
    if not value:
        return None
    folder = Path(value)
    if not folder.is_absolute() or not folder.is_dir():
        return None
    return folder


def _text_event_message(
    request_id: str,
    **fields: Any,
) -> str:
    payload: dict[str, Any] = {
        "requestId": request_id,
    }
    payload.update(fields)

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class QronosRuntime:
    def __init__(
        self,
        scheduler: QueueScheduler | None = None,
        notify: Callable[[str, str, str], None] | None = None,
        settings: SettingsService | None = None,
        paint_reference_provider: PaintReferenceProvider | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._foreground = ForegroundLifecycle()
        # Global pause is deliberately process-local. It is never persisted,
        # so a Qronos runtime restart always returns to the active state.
        self._runtime_paused = False
        # Remember whether Wake was alive before the active -> paused
        # transition so a later Resume can restore what Pause suspended.
        # This is also process-local and therefore resets on restart.
        self._resume_wake_after_pause = False

        # Resolved here rather than as a default argument, because emit() is
        # defined below this class and a default is evaluated at definition
        # time — which would be a NameError at import.
        self._notify = notify if notify is not None else emit
        self._scheduler = scheduler
        self._conversation_executor: ConversationQueueExecutor | None = None

        # The queue's durable record. Opened with the scheduler, not before:
        # a bridge nobody has asked about the queue touches no queue file.
        self._queue_journal: QueueJournal | None = None
        self._queue_setup_lock = threading.RLock()
        self._last_storage_status: StorageStatus | None = None

        self.settings = (
            settings
            if settings is not None
            else SettingsService(
                DEFAULT_DEFINITIONS,
                path=_runtime_settings_path(),
                on_problem=lambda message: self._notify(
                    "settings_warning",
                    "warning",
                    message,
                ),
            )
        )
        self._pending_wake_settings_reconcile = False
        self._pending_audio_input_reconcile = False
        self._active_input_device_id = ""
        self._input_meter_enabled = False
        self._input_meter_last_emit = 0.0
        self._input_meter_thread: threading.Thread | None = None
        self._input_meter_stop = threading.Event()

        self.audio_input: AudioInput | None = None
        self.vad_runtime: WhisperCppVADRuntime | None = None
        self.command_recorder: CommandRecorder | None = None
        self.speech_runtime: (
            WhisperHybridRuntime
            | WhisperCppRuntime
            | None
        ) = None

        # One Voice turn can have nested cleanup paths: _run_voice_turn()
        # releases STT before Brain execution, while push_to_talk() keeps a
        # final safety cleanup for failures that happen before transcription.
        # Track whether the current warmed STT generation has already yielded
        # so the second cleanup is an idempotent no-op rather than a duplicate
        # shutdown.
        self._speech_runtime_released = False

        self.voice_output: ChatterboxRuntime | None = None
        self._voice_output_prewarm_thread: threading.Thread | None = None
        self._voice_output_prewarm_error: str | None = None
        self.task_router: TaskRouter | None = None

        # Intent classification remains diagnostic. ComputeEstimator has one
        # narrow production authority: it may promote an otherwise-FAST
        # language request to HEAVY when multiple high-confidence complexity
        # signals agree. Action / browser / vision routes are never overridden.
        # They classify the real transcript and record what the future routing
        # stack would choose, while TaskRouter remains the only component that
        # controls the current production path.
        self.intent_gate = IntentGate()
        self.compute_estimator = ComputeEstimator()

        # Pay the classifiers' one-time Python/regex/cache setup cost before
        # the user sends the first command.  Live shadow measurements showed
        # a repeatable ~8-10 ms first-use spike while warm calls stayed near
        # or below 1 ms.  Synchronous startup warmup is preferable here:
        # it happens before runtime_ready, costs only once, and guarantees
        # the first real transcript never races an unfinished background warmup.
        self._prewarm_routing_classifiers()

        self.action_audit: ActionAuditLog | None = None
        self.undo_journal: UndoJournal | None = None
        self.file_executor: FileExecutor | None = None
        self.file_resolver: KnownFolderFileResolver | None = None
        self.computer_executor: ComputerExecutor | None = None
        self._last_computer_app_id: str | None = None
        self.paint_reference_provider: PaintReferenceProvider = (
            paint_reference_provider
            if paint_reference_provider is not None
            else EnvironmentPaintReferenceProvider()
        )
        self.paint_draw_executor: PaintDrawExecutor | None = None
        # Every prompt that waits on a person (Paint draw/undo, screen read)
        # is an open ticket here with a single-use backend identity. The
        # desktop must answer with that identity; Stop/Pause/shutdown revoke
        # it. pending_paint_draw/pending_paint_undo/pending_look are views.
        self._approvals = ApprovalLedger()
        # Makes "check nothing is pending, then issue" atomic.
        self._approval_lock = threading.Lock()
        # The in-process media host (qronos_media.host), built on first use only:
        # nothing about media runs, probes or opens at startup.
        self._media_host: Any = None
        self._media_host_lock = threading.Lock()
        # Telegram download / print workflows; prompts share the ledger above.
        self._app_workflows: AppWorkflowCoordinator | None = None
        # One explicit UI approval grants a short, process-local lease for
        # ordinary reversible file-write actions. The window is fixed: auto-
        # approved actions never extend it. Restart and global Pause revoke it.
        self._file_write_approval_lease_until = 0.0
        self._file_approval_clock = time.monotonic
        self.screen_capture: ScreenCapture | None = None
        self.last_foreground_window: int | None = None
        self.orchestrator: Orchestrator | None = None
        self.conversation_session: ConversationSession | None = None
        self.conversation_history = ConversationHistory()

        self.wake_engine: OpenWakeWordEngine | None = None
        self.voice_trigger: VoiceTriggerService | None = None
        self._wake_thread: threading.Thread | None = None
        self._wake_stop = threading.Event()

        # Thread liveness is not Wake readiness. This Event is set only
        # after OpenWakeWord and the selected microphone are genuinely accepting
        # wake frames; command VAD/STT warm-up is explicitly outside this gate.
        self._wake_ready = threading.Event()
        self._wake_start_retry_count = 0
        self._wake_retry_thread: threading.Thread | None = None

        # Wake detection and command recognition have different cold-start
        # requirements. OpenWakeWord + microphone must become available first.
        # The heavier VAD/recorder/STT capture layer warms after Wake is live.
        # A dedicated event/lock makes this split deterministic and prevents
        # duplicate VAD preparation when PTT or a very early wake races it.
        self._wake_command_prepare_lock = threading.Lock()
        self._wake_command_ready = threading.Event()
        self._wake_command_prepare_thread: threading.Thread | None = None
        self._wake_command_prepare_error: str | None = None

        # Playback lifecycle is synchronized with the desktop media engine.
        # One ID identifies one complete Qronos response, even when that
        # response contains several sequential TTS chunks.
        self._voice_playback_complete = threading.Event()
        self._voice_playback_id = 0
        self._pending_playback_id: int | None = None
        self._voice_playback_metrics: dict[int, dict[str, Any]] = {}

        # User-visible Stop is separate from runtime Pause. Stop cancels only
        # the current response/playback while leaving Wake and the conversation
        # session alive. A running model HTTP request is not force-killed, but
        # later response/TTS phases are suppressed at deterministic checkpoints.
        self._voice_response_stop_requested = threading.Event()

        # In-flight user-visible computer/Paint actions. Stop cancels exactly
        # the actions registered here (cooperatively, releasing any held mouse
        # button); queue tasks, file actions and other processes are never in
        # this registry. Cancelling grants nothing.
        self._computer_actions = ActiveActionRegistry()

        # A real spoken turn must be distinguished from a wake candidate
        # that never produced valid speech. Brain/resource/TTS failures
        # after genuine speech must never be treated as false wakes.
        self._last_voice_turn_had_valid_speech_evidence = False

    def ensure_scheduler(self) -> QueueScheduler:
        """
        The queue, built the first time anybody asks for it.

        Deliberately not built in ``prepare()``, which opens the microphone —
        the queue has to work without any of that. Deliberately not built at
        startup either: four tests in
        ``tests/test_runtime_bridge_process.py`` depend on a freshly started
        bridge having no side effects, and a sampler thread launching
        ``nvidia-smi`` is a side effect. The desktop asks for the queue as
        soon as it sees ``runtime_ready``, so in practice the delay is
        imperceptible.
        """
        with self._queue_setup_lock:
            with self._lock:
                existing = self._scheduler

            if existing is not None:
                return existing

            monitor = build_sustained_load_monitor()
            monitor.prime()

            scheduler = QueueScheduler(
                queue=SafeQueue(),
                governor=ResourceGovernor(),
                monitor=monitor,
                runner=self._run_queue_work_in_thread,
                notify=self._on_queue_event,
                config=_scheduler_config_from_environment(),
                # The user's own voice/text/screen turn owns the model phase.
                # Read without the runtime lock held by the caller: the pump
                # thread asks this on every tick.
                foreground_busy=lambda: self.is_busy,
            )

            with self._lock:
                self._scheduler = scheduler
                runtime_paused = self._runtime_paused

            if runtime_paused:
                scheduler.set_runtime_paused(True)

            # Before the pump starts, so nothing restored can be admitted
            # until all of it is back and every interrupted mark is on disk.
            try:
                self._recover_queue(scheduler)
            except Exception as exc:
                self._notify(
                    "queue_storage_problem",
                    "warning",
                    _compact(
                        {
                            "durable": False,
                            "problem": StorageProblem.UNAVAILABLE.value,
                            "detail": f"Queue recovery failed: {exc}",
                        }
                    ),
                )

            scheduler.start()

            return scheduler

    # ------------------------------------------------------ media host

    def _media_foreground_busy(self) -> bool:
        """The person's own turn (voice, text, screen) is in progress: media yields to it."""
        with self._lock:
            return bool(self._busy or self._foreground.active)

    def ensure_media_host(self) -> Any:
        """
        The one in-process media host, built the first time media work is asked for.

        It is bound to THIS runtime's objects only: the queue scheduler's
        ResourceGovernor, the shared ResourceLedger, the ApprovalLedger and the
        action audit, plus the live foreground and Pause state. Its model
        authority is issued here, never from anything in a request.
        """
        with self._media_host_lock:
            if self._media_host is not None:
                return self._media_host
            from core.resource_ownership import GLOBAL_RESOURCE_LEDGER
            from qronos_media.host import build_bridge_media_host

            from core.library_executor import WorkflowSecretCheck

            scheduler = self.ensure_scheduler()
            audit = self.action_audit.record_verdict if self.action_audit is not None else None
            self._media_host = build_bridge_media_host(
                governor=scheduler.governor,
                ledger=GLOBAL_RESOURCE_LEDGER,
                foreground_busy=self._media_foreground_busy,
                paused=lambda: self.runtime_paused,
                approvals=self._approvals,
                audit=audit,
                # The owner and THAT generation's lifecycle capability, read together (one lock) per admission.
                embedding_generation=self._memory_sidecar_generation,
                # The bridge never opens the secret store itself; media borrows the narrow typed-secret check.
                secrets=WorkflowSecretCheck(),
            )
            return self._media_host

    def _memory_sidecar_owner(self) -> Any:
        """The owned bge-m3 memory sidecar process, if the memory runtime runs one (else None: unavailable)."""
        memory = getattr(self.orchestrator, "memory_runtime", None) if self.orchestrator is not None else None
        sidecar = getattr(memory, "sidecar", None)
        return getattr(sidecar, "owned_process", None) if sidecar is not None else None

    def _memory_sidecar_generation(self) -> Any:
        """(owner, lifecycle capability) of the owned bge-m3 sidecar's CURRENT generation, atomically, or None.

        ``MemorySidecarManager.owned_generation`` reads both under one lock; media binds that pair per admission and
        frees a slot only on THAT capability's whole-tree proof. Off or missing: None (unavailable).
        """
        memory = getattr(self.orchestrator, "memory_runtime", None) if self.orchestrator is not None else None
        sidecar = getattr(memory, "sidecar", None)
        owned_generation = getattr(sidecar, "owned_generation", None) if sidecar is not None else None
        if not callable(owned_generation):
            return None
        try:
            return owned_generation()
        except Exception:  # an unreadable generation is no generation
            return None

    def device_hub(self) -> Any:
        """The Device Hub (core/device_hub.py), built on first use; spooler/driver calls happen only per request."""
        with self._media_host_lock:
            hub = getattr(self, "_device_hub", None)
        if hub is not None:
            return hub
        from core.device_hub import DeviceHub, default_source
        from core.library_executor import WorkflowSecretCheck

        workflows = self._ensure_app_workflows()
        secret_check = WorkflowSecretCheck()  # the bridge never opens the secret store itself
        with self._media_host_lock:
            hub = getattr(self, "_device_hub", None)
            if hub is None:
                hub = DeviceHub(
                    source=default_source,
                    ledger=self._approvals,
                    emit=lambda event, status, payload: self._notify(event, status, _compact(payload)),
                    paused=lambda: self.runtime_paused,
                    begin_print=workflows.begin_print,
                    verify_secret=secret_check.verify,
                    audit=self.action_audit,
                )
                self._device_hub = hub
        return hub

    def media_product(self) -> Any:
        """The typed media operations (qronos_media.product) on the one media host, built with it."""
        with self._media_host_lock:
            product = getattr(self, "_media_product", None)
        if product is not None:
            return product
        host = self.ensure_media_host()
        from qronos_media.product import MediaProduct

        with self._media_host_lock:
            product = getattr(self, "_media_product", None)
            if product is None:
                product = MediaProduct(
                    host,
                    emit=lambda event, status, payload: self._notify(event, status, _compact(payload)),
                    paused=lambda: self.runtime_paused,
                )
                self._media_product = product
        return product

    def _run_media_intent(self, intent: Any) -> str:
        """Run a brain/voice media request through the SAME typed product operation; a short Persian answer."""
        from qronos_media.intents import MediaSearchIntent, spoken_answer

        request_id = f"brain_{uuid.uuid4().hex[:16]}"
        if isinstance(intent, MediaSearchIntent):
            data = {"op": "search", "requestId": request_id, "query": intent.query, "mode": intent.mode, "limit": 10}
        else:
            data = {"op": "status", "requestId": request_id}
        answered: dict[str, Any] = {}
        done = threading.Event()

        def reply(result: dict[str, Any]) -> None:
            answered.update(result)
            done.set()

        self.media_product().handle(data, reply=reply)
        done.wait(MEDIA_INTENT_WAIT_SECONDS)
        return spoken_answer(intent, answered if done.is_set() else None)

    def _begin_text_media_request(self, text: str, request_id: str, started_at: float) -> bool:
        """A deterministic media search/status request goes to the media product, not to a model."""
        from qronos_media.intents import parse_media_intent

        intent = parse_media_intent(text)
        if intent is None:
            return False
        response = self._run_media_intent(intent)
        self._finish_text_file_action(request_id=request_id, response=response, started_at=started_at)
        return True

    def _preempt_media(self, reason: str) -> None:
        host = self._media_host
        if host is not None:
            try:
                host.preempt(reason)
            except Exception:
                pass

    # ------------------------------------------------------ durable queue

    def _ensure_queue_journal(self) -> QueueJournal:
        if self._queue_journal is None:
            reason = ""

            try:
                path = default_queue_state_path()
            except QueueStatePathError as exc:
                # A configured location that cannot be used safely. Not
                # silently replaced by the default: the override was a choice.
                path = None
                reason = str(exc)
            except Exception:
                # No writable per-user location at all. The journal runs in
                # memory and says so; it never pretends to be durable.
                path = None
                reason = "No per-user location for the queue is available."

            self._queue_journal = QueueJournal(
                path,
                unavailable_detail=reason,
            )

        return self._queue_journal

    def _recover_queue(self, scheduler: QueueScheduler) -> None:
        """
        Put back what an earlier run of Qronos accepted, safely.

        Queued and paused requests come back as they were; each is validated
        afresh — route, resources, policy — when it comes to run, exactly as
        new work is. Anything that was running comes back interrupted (or
        done, if its answer had already reached the conversation) and waits
        for the user's explicit resume.
        """
        journal = self._ensure_queue_journal()
        recovered = journal.open(
            answered=self.conversation_history.has_assistant_reply,
        )

        if recovered.paused:
            scheduler.set_paused(True)

        states = {
            RecordState.QUEUED: TaskState.QUEUED,
            RecordState.PAUSED: TaskState.PAUSED,
            RecordState.INTERRUPTED: TaskState.INTERRUPTED,
        }

        for record in recovered.records:
            state = states.get(record.state)

            if state is None:
                continue

            try:
                work = self._work_from_record(record)
                weight = Weight(record.weight)
            except ValueError as exc:
                journal.update(
                    record.task_id,
                    RecordState.FAILED,
                    detail=(
                        "This saved request can no longer be queued: "
                        f"{exc}"
                    ),
                )
                continue

            scheduler.restore(
                work=work,
                weight=weight,
                summary=record.summary,
                required_vram_mb=record.required_vram_mb,
                task_id=record.task_id,
                state=state,
                queued_at=record.submitted_at,
                attempts=record.attempts,
                detail=record.detail,
            )

            if state is TaskState.INTERRUPTED:
                self._notify_queued_conversation(
                    work,
                    ConversationPhase.INTERRUPTED,
                    {"resumable": True, "detail": record.detail},
                )

        for task_id in recovered.answered:
            record = journal.get(task_id)

            if record is not None:
                self._notify(
                    "queue_conversation_update",
                    "ready",
                    json.dumps(
                        {
                            "taskId": record.task_id,
                            "requestId": record.request_id,
                            "route": record.route,
                            "phase": ConversationPhase.ANSWERED.value,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )

        self._report_storage_status(force=True)

    def _work_from_record(self, record: QueueRecord) -> QueuedConversationWork:
        request = build_queued_conversation_request(
            request_id=record.request_id,
            text=record.text,
            route=TaskType(record.route),
            context=tuple(
                BrainMessage(
                    role=BrainMessageRole(role),
                    content=content,
                )
                for role, content in record.context
                if content.strip()
            ),
        )
        work = QueuedConversationWork(
            request,
            self._conversation_queue_executor(),
        )
        work.task_id = record.task_id

        return work

    def _sync_queue_journal(self, tasks) -> None:
        """
        Make the durable record agree with the queue as it now stands.

        Called on every queue event, so every path that moves a task — start,
        finish, failure, deferral, cancel, the scheduler's own pause after
        too many attempts — reaches the disk without each one having to
        remember to. A start is written before the work is handed out, so a
        crash mid-run reads back as interrupted rather than queued.
        """
        journal = self._queue_journal

        if journal is None:
            return

        for task in tasks:
            if task.get("kind") != "conversation":
                continue

            try:
                state = RecordState(task.get("state"))
            except ValueError:
                continue

            # A waiting task's hold detail changes with every reading of the
            # machine. Only the detail of an outcome is worth a write.
            keep_detail = state.finished or state in (
                RecordState.PAUSED,
                RecordState.INTERRUPTED,
            )

            journal.update(
                str(task.get("taskId", "")),
                state,
                attempts=int(task.get("attempts", 0)),
                detail=str(task.get("detail", "")) if keep_detail else None,
            )

        self._report_storage_status()

    def _report_storage_status(self, force: bool = False) -> None:
        journal = self._queue_journal

        if journal is None:
            return

        status = journal.status()

        if not force and status == self._last_storage_status:
            return

        self._last_storage_status = status

        # Said once per change, loudly when it is a problem: a queue that is
        # not being saved must never look like one that is.
        self._notify(
            "queue_storage_status" if status.durable else "queue_storage_problem",
            "ready" if status.durable else "warning",
            _compact(status.to_public_dict()),
        )

    def _queue_view_payload(self, view) -> dict[str, Any]:
        journal = self._queue_journal
        payload = _queue_view_dict(view)
        live = {task.get("taskId") for task in view.tasks}

        if journal is None:
            payload["storage"] = None
            payload["recent"] = []
            return payload

        payload["storage"] = journal.status().to_public_dict()
        payload["recent"] = [
            {
                "taskId": record.task_id,
                "requestId": record.request_id,
                "summary": record.summary,
                "route": record.route,
                "state": record.state.value,
                "detail": record.detail,
                "finishedAt": record.updated_at,
            }
            for record in sorted(
                journal.records(),
                key=lambda item: item.updated_at,
                reverse=True,
            )
            if record.state.finished and record.task_id not in live
        ][:20]

        return payload

    def emit_queue_view(self) -> None:
        scheduler = self._scheduler

        if scheduler is not None:
            self._notify(
                "queue_changed",
                "ready",
                _compact(self._queue_view_payload(scheduler.view())),
            )

    def _run_queue_work_in_thread(self, entry) -> None:
        """
        Hand queued work to its own thread and report back when it returns.

        A two-minute generation on the pump thread would stop the pump from
        renewing the work's lease or noticing that the machine got busy.
        Returning None tells the scheduler the outcome will be reported
        later, by this thread, through ``report_outcome``.
        """
        task_id = entry.task_id
        work = entry.work

        def run() -> None:
            try:
                outcome = work.run()
            except Exception as exc:
                outcome = WorkResult(WorkOutcome.FAILED, f"{exc}")

            scheduler = self._scheduler

            if scheduler is not None:
                scheduler.report_outcome(task_id, outcome)

        threading.Thread(
            target=run,
            name="qronos-queue-work",
            daemon=True,
        ).start()

        return None

    # ------------------------------------------------ queued conversations

    def _conversation_queue_executor(self) -> ConversationQueueExecutor:
        if self._conversation_executor is None:
            self._conversation_executor = ConversationQueueExecutor(
                orchestrator=self._orchestrator_for_queue,
                route=self._route_for_queue,
                record_answer=self._record_queued_answer,
                notify=self._notify_queued_conversation,
                foreground_busy=lambda: self.is_busy,
                runtime_paused=lambda: self.runtime_paused,
                release_brains=self._release_queue_brains,
                resident_brains=self._queue_resident_brains,
                already_answered=self._queued_request_already_answered,
                storage_blocks_start=self._queue_storage_blocks_start,
            )

        return self._conversation_executor

    def _queue_resident_brains(self) -> frozenset[str] | None:
        """Brains resident before a queued run, or None when unknown."""
        resident = getattr(self.orchestrator, "resident_brain_names", None)

        if not callable(resident):
            return None

        return frozenset(resident())

    def _release_queue_brains(
        self,
        resident_before: frozenset[str] | None,
    ) -> None:
        """
        Unload what a queued run loaded, and only that.

        When residency before the run is unknown, nothing is unloaded: an
        idle model left loaded expires by itself, while evicting a model the
        user's conversation had warm cannot be undone. An orchestrator with
        no notion of residency gets the older, all-Qronos release.
        """
        release = getattr(
            self.orchestrator,
            "release_brains_loaded_since",
            None,
        )

        if not callable(release):
            self._release_brains_for_voice_phase()
            return

        if resident_before is None:
            return

        release(resident_before)

    def _queued_request_already_answered(self, request_id: str) -> bool:
        """
        Whether the conversation already holds an answer for this request.

        A history that cannot be read right now is not evidence of an
        answer; the request runs, as it would have before this check.
        """
        try:
            return self.conversation_history.has_assistant_reply(request_id)
        except Exception:
            return False

    def _queue_storage_blocks_start(self) -> str | None:
        journal = self._queue_journal

        if journal is None:
            return None

        blocked = journal.start_blocker()
        self._report_storage_status()

        return blocked

    def _orchestrator_for_queue(self):
        self._ensure_text_runtime()

        if self.orchestrator is None:
            raise RuntimeError(
                "Qronos text runtime did not prepare correctly."
            )

        return self.orchestrator

    def _route_for_queue(self, text: str) -> TaskType:
        """
        The production route for a request, as a direct text turn would pick.

        Only the deterministic router and the Fast-to-Heavy promotion: no
        brain is loaded to decide. The Calculator action is checked as the
        text path checks it, before routing, because it is an action.
        """
        if _looks_like_calculator_semantic_action(
            text,
            self._last_computer_app_id,
        ):
            return TaskType.COMPUTER

        if self.task_router is None:
            self.task_router = TaskRouter()

        routed = self.task_router.route(text).task_type
        selected, _details = self._select_production_task_type(
            text,
            routed,
        )

        return selected

    def _record_queued_answer(
        self,
        request: ConversationRequest,
        response: str,
    ) -> None:
        self._record_conversation_message(
            role="assistant",
            text=response,
            source="text",
            request_id=request.request_id,
        )

    def _notify_queued_conversation(
        self,
        work: QueuedConversationWork,
        phase: ConversationPhase,
        fields: dict[str, Any],
    ) -> None:
        """
        Tell the conversation where its queued request is.

        Correlated by the original request id, so the answer — or the reason
        there is none — lands on the request it belongs to rather than as a
        count in a queue panel.
        """
        payload: dict[str, Any] = {
            "taskId": work.task_id,
            "requestId": work.request.request_id,
            "route": work.request.route.value,
            "phase": phase.value,
        }
        payload.update(fields)

        self._notify(
            "queue_conversation_update",
            "error" if phase is ConversationPhase.FAILED else "ready",
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def submit_queued_conversation(
        self,
        *,
        text: str,
        request_id: str,
        context: tuple = (),
        user_message_recorded: bool,
        route: TaskType | None = None,
        heavy_requested: bool = False,
        required_mb: int | None = None,
        reason: str = "",
    ) -> QueuedConversationWork:
        """
        Put one typed request in the queue as real, answerable work.

        Refuses anything that is not plain Fast/Heavy reasoning before it
        reaches the queue. Records the user's message now, unless the text
        turn that deferred it already has, so the conversation shows the
        request at once and the eventual answer attaches to it.
        """
        selected = route if route is not None else self._route_for_queue(text)
        request = build_queued_conversation_request(
            request_id=request_id,
            text=text,
            route=selected,
            context=context,
        )

        scheduler = self.ensure_scheduler()
        waiting = [
            task
            for task in scheduler.view().tasks
            if task.get("kind") == "conversation"
            and task.get("state")
            in ("queued", "paused", "running", "interrupted")
        ]

        if len(waiting) >= MAX_QUEUED_CONVERSATIONS:
            raise ValueError(
                "The queue already holds "
                f"{MAX_QUEUED_CONVERSATIONS} waiting requests."
            )

        journal = self._ensure_queue_journal()

        if any(
            task.get("requestId") == request_id for task in waiting
        ) or journal.holds_request(request_id):
            raise ValueError(f"{request_id} is already queued.")

        heavy = selected is TaskType.HEAVY or heavy_requested
        weight = Weight.HEAVY if heavy else Weight.LIGHT

        if required_mb is None:
            profile = MODELS["heavy" if heavy else "fast"]
            required_mb = required_vram_mb(profile.estimated_vram_gb)

        if not user_message_recorded:
            self._record_conversation_message(
                role="user",
                text=text,
                source="text",
                request_id=request_id,
            )

        work = QueuedConversationWork(
            request,
            self._conversation_queue_executor(),
        )
        # Identified, and announced, before the scheduler can see it: the
        # pump may start it the instant it is submitted, and its progress
        # must never arrive ahead of the news that it was queued.
        work.task_id = f"q-{uuid.uuid4().hex[:8]}"

        # On disk before it is announced or admitted. If the disk refuses,
        # the request is still accepted for this session, and says plainly
        # that it would not survive a restart.
        now = time.time()
        durable = journal.add(
            QueueRecord(
                task_id=work.task_id,
                request_id=request_id,
                summary=request.summary(),
                route=selected.value,
                weight=weight.value,
                required_vram_mb=int(required_mb),
                state=RecordState.QUEUED,
                submitted_at=now,
                updated_at=now,
                text=text,
                context=tuple(
                    (message.role.value, message.content)
                    for message in request.context
                    if message.role
                    in (BrainMessageRole.USER, BrainMessageRole.ASSISTANT)
                ),
            )
        )

        fields: dict[str, Any] = {"durable": durable}

        if reason:
            fields["reason"] = reason

        self._notify_queued_conversation(
            work,
            ConversationPhase.QUEUED,
            fields,
        )
        self._report_storage_status()

        scheduler.submit(
            work=work,
            weight=weight,
            summary=request.summary(),
            required_vram_mb=required_mb,
            task_id=work.task_id,
        )

        return work

    @property
    def scheduler(self) -> QueueScheduler | None:
        """The queue if one has been built, without building one."""
        return self._scheduler

    def _on_queue_event(self, event: QueueEvent) -> None:
        """
        Put one queue event on the wire, then the whole queue after it.

        Whole state rather than a delta, and for a concrete reason: the Rust
        reader re-emits any line it cannot parse as a log entry rather than
        dropping it, so a single mangled line would desynchronise a delta
        stream permanently and silently. A few hundred bytes of full state
        repairs itself on the next event.
        """
        if event.event is not QueueEventType.CHANGED:
            event_type, status = _QUEUE_EVENT_NAMES[event.event]
            self._notify(event_type, status, _queue_event_message(event))

        scheduler = self._scheduler

        if scheduler is not None:
            view = scheduler.view()

            try:
                self._sync_queue_journal(view.tasks)
            except Exception:
                # The journal reports its own failures as storage status; an
                # exception here must not cost the user their queue view.
                pass

            self._notify(
                "queue_changed",
                "ready",
                _compact(self._queue_view_payload(view)),
            )

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def runtime_paused(self) -> bool:
        with self._lock:
            return self._runtime_paused

    @property
    def wake_word_enabled(self) -> bool:
        return bool(
            self.settings.get(
                "voice.wake_word_enabled"
            )
        )

    @property
    def voice_language(self) -> str:
        value = str(
            self.settings.get(
                "voice.language"
            )
        ).strip().lower()

        return (
            value
            if value in {
                "auto",
                "fa",
                "en",
            }
            else "fa"
        )

    @property
    def wake_sensitivity(self) -> int:
        return int(
            self.settings.get(
                "voice.wake_sensitivity"
            )
        )

    @property
    def wake_threshold(self) -> float:
        return wake_sensitivity_to_threshold(
            self.wake_sensitivity
        )

    @property
    def input_device_id(self) -> str:
        value = self.settings.get(
            "voice.input_device_id"
        )
        return str(value or "").strip()

    def _resolved_input_device_index(
        self,
        device_id: str | None = None,
    ) -> int | None:
        requested = (
            self.input_device_id
            if device_id is None
            else str(device_id or "").strip()
        )

        try:
            return resolve_input_device(
                requested
            )
        except LookupError as exc:
            self._notify(
                "microphone_selection_warning",
                "warning",
                str(exc),
            )
            return None

    def _new_audio_input_from_settings(
        self,
    ) -> AudioInput:
        requested = self.input_device_id
        device_index = (
            self._resolved_input_device_index(
                requested
            )
        )
        self._active_input_device_id = (
            requested
        )
        return AudioInput(
            AudioConfig(
                device=device_index,
            )
        )

    def _replace_audio_input_from_settings(
        self,
    ) -> None:
        old_audio = self.audio_input

        if old_audio is not None:
            try:
                old_audio.stop()
            except Exception:
                pass

        audio_input = (
            self._new_audio_input_from_settings()
        )
        self.audio_input = audio_input

        if self.vad_runtime is not None:
            self.command_recorder = (
                CommandRecorder(
                    audio_input=audio_input,
                    vad_runtime=self.vad_runtime,
                    config=CommandRecorderConfig(
                        silence_seconds=0.96,
                        start_timeout_seconds=(
                            FOLLOWUP_START_TIMEOUT_SECONDS
                        ),
                    ),
                )
            )

        self._pending_audio_input_reconcile = False

    def _reconcile_audio_input_without_wake(
        self,
    ) -> None:
        # A Settings meter may temporarily own the selected microphone while
        # Wake is disabled. Stop that exclusive stream, swap the configured
        # AudioInput, then resume the meter on the newly selected device.
        meter_was_enabled = (
            self._input_meter_enabled
        )
        self._stop_standalone_input_meter()
        self._replace_audio_input_from_settings()

        if (
            meter_was_enabled
            and not self.runtime_paused
        ):
            self._start_standalone_input_meter()

    def _reconcile_audio_input_in_wake_thread(
        self,
    ) -> None:
        if not self._pending_audio_input_reconcile:
            return

        if self.audio_input is None:
            self._replace_audio_input_from_settings()
            return

        was_running = self.audio_input.is_running()

        self._replace_audio_input_from_settings()

        if (
            was_running
            and not self._wake_stop.is_set()
        ):
            self.audio_input.start()

        self._notify(
            "microphone_selection_applied",
            "ready",
            json.dumps(
                {
                    "deviceId":
                        self._active_input_device_id,
                    "usingSystemDefault":
                        self.audio_input.config.device
                        is None,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

    def _reconcile_wake_setting(self) -> None:
        if self.runtime_paused:
            with self._lock:
                self._resume_wake_after_pause = (
                    self.wake_word_enabled
                )
            return

        if self.wake_word_enabled:
            if not self.wake_listener_running:
                self.start_wake_listener()
            return

        if self.wake_listener_running:
            self.stop_wake_listener()

    def reload_settings(
        self,
        settings: SettingsService | None = None,
    ) -> None:
        previous_input_device_id = (
            self.input_device_id
        )

        next_settings = (
            settings
            if settings is not None
            else SettingsService(
                DEFAULT_DEFINITIONS,
                path=_runtime_settings_path(),
                on_problem=lambda message: self._notify(
                    "settings_warning",
                    "warning",
                    message,
                ),
            )
        )

        if next_settings.write_blocked_reason is not None:
            self._notify(
                "settings_apply_failed",
                "warning",
                next_settings.write_blocked_reason,
            )
            return

        with self._lock:
            self.settings = next_settings
            busy = self._busy

            if busy:
                self._pending_wake_settings_reconcile = True

        input_changed = (
            previous_input_device_id
            != self.input_device_id
        )

        if input_changed:
            self._pending_audio_input_reconcile = True

        engine = self.wake_engine
        if engine is not None:
            engine.threshold = self.wake_threshold

        if (
            input_changed
            and not busy
            and not self.wake_listener_running
        ):
            self._reconcile_audio_input_without_wake()

        if not busy:
            self._reconcile_wake_setting()

        self._notify(
            "settings_applied",
            "ready",
            json.dumps(
                {
                    "wakeWordEnabled":
                        self.wake_word_enabled,
                    "wakeSensitivity":
                        self.wake_sensitivity,
                    "wakeThreshold":
                        self.wake_threshold,
                    "voiceLanguage":
                        self.voice_language,
                    "inputDeviceId":
                        self.input_device_id,
                    "inputDeviceReconcileDeferred":
                        bool(
                            self._pending_audio_input_reconcile
                            and self.wake_listener_running
                        ),
                    "wakeReconcileDeferred":
                        busy,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

    def _foreground_lifecycle(self) -> ForegroundLifecycle:
        # Legacy bare-runtime helpers bypass __init__ and may omit _lock.
        # Creation is still serialized, with the same real lifecycle rules.
        lifecycle = getattr(self, "_foreground", None)
        if lifecycle is None:
            with _FOREGROUND_INITIALIZATION_LOCK:
                lifecycle = getattr(self, "_foreground", None)
                if lifecycle is None:
                    lifecycle = self._foreground = ForegroundLifecycle()
        return lifecycle

    def _foreground_state(self):
        return self.runtime_paused, self.is_busy

    def _report_foreground_cancelled(self, kind, reason, *, request_id="", turn=None):
        lifecycle = self._foreground_lifecycle()
        with lifecycle.transition():
            if not lifecycle.claim_cancellation_report(turn):
                return
            if kind == "text":
                self._notify(
                    "text_error", "cancelled",
                    _text_event_message(request_id, status="cancelled", reason=reason,
                                        error="Current Qronos request was cancelled."),
                )
            else:
                self._last_voice_turn_had_valid_speech_evidence = False
                session = getattr(self, "conversation_session", None)
                if session is not None:
                    session.close()
                self._notify("voice_turn_stopped", "ready", _compact(
                    {"status": "cancelled", "reason": reason, "channel": kind}))

    def _set_busy(self, value: bool) -> None:
        reconcile = False

        with self._lock:
            self._busy = value
            scheduler = self._scheduler

            if (
                not value
                and self._pending_wake_settings_reconcile
            ):
                self._pending_wake_settings_reconcile = False
                reconcile = True

        if scheduler is not None:
            if value:
                # The user's turn takes the model phase. Queued work already
                # running is told so: if the turn costs it its brain, it goes
                # back in line instead of failing, and it does not unload
                # models out from under this turn when it returns.
                for work in scheduler.running_work():
                    preempt = getattr(work, "preempt", None)

                    if callable(preempt):
                        preempt()
            else:
                scheduler.wake()

        if reconcile:
            self._reconcile_wake_setting()

        if (
            not value
            and self._pending_audio_input_reconcile
            and not self.wake_listener_running
        ):
            self._reconcile_audio_input_without_wake()

    def _notify_runtime_pause_state(self) -> None:
        self._notify(
            "runtime_pause_changed",
            "paused" if self.runtime_paused else "ready",
            _compact(
                {
                    "paused": self.runtime_paused,
                    "temporary": True,
                    "resetsOnRestart": True,
                }
            ),
        )

    def _reject_if_runtime_paused(self, channel: str) -> bool:
        if not self.runtime_paused:
            return False

        self._notify(
            "runtime_paused_rejected",
            "paused",
            _compact(
                {
                    "channel": channel,
                    "temporary": True,
                }
            ),
        )
        return True

    def _require_runtime_active(self, channel: str) -> None:
        inference_checkpoint()
        if self.runtime_paused:
            raise RuntimeError(
                f"Qronos runtime is temporarily paused; {channel} is unavailable."
            )

    def set_runtime_paused(self, paused: bool) -> None:
        if not isinstance(paused, bool):
            raise ValueError("runtime_set_paused needs paused to be true or false.")
        lifecycle = self._foreground_lifecycle()
        with lifecycle.transition():
            if paused:
                lifecycle.cancel_current(CANCEL_REASON_PAUSE)
            self._set_runtime_paused(paused)

    def _set_runtime_paused(self, paused: bool) -> None:
        if not isinstance(paused, bool):
            raise ValueError(
                "runtime_set_paused needs paused to be true or false."
            )

        # Capture the Wake state before the transition. Repeated Pause commands
        # must not erase the original resume intent after Wake has been stopped.
        wake_was_running = self.wake_listener_running if paused else False

        with self._lock:
            was_paused = self._runtime_paused

            if paused and not was_paused:
                self._resume_wake_after_pause = wake_was_running

            self._runtime_paused = paused
            busy = self._busy
            scheduler = self._scheduler

            resume_wake = (
                not paused
                and was_paused
                and self._resume_wake_after_pause
            )

            if not paused:
                self._resume_wake_after_pause = False

        # The queue follows the global runtime state, but this command never
        # creates the queue merely to pause it — and it never touches the
        # user's own hold on the queue, which is separate and remembered.
        if scheduler is not None:
            set_runtime_paused = getattr(scheduler, "set_runtime_paused", None)

            if callable(set_runtime_paused):
                set_runtime_paused(paused)
            else:
                scheduler.set_paused(paused)

        if paused:
            self._discard_pending_file_approvals(CANCEL_REASON_PAUSE)
            self._discard_pending_media_approvals(CANCEL_REASON_PAUSE)
            self._discard_pending_device_approvals(CANCEL_REASON_PAUSE)
            self._discard_pending_history_undo(CANCEL_REASON_PAUSE)
            # Pause stops GUI work the same way Stop does: in-flight actions
            # are cancelled cooperatively (releasing held buttons), turns in
            # progress cannot start new actions, and pending approvals are
            # discarded so Resume can never revive them.
            self._computer_actions.cancel_all(CANCEL_REASON_PAUSE)
            self._discard_pending_approvals(CANCEL_REASON_PAUSE)
            # Stop accepting new voice input immediately and release idle voice
            # resources. The foreground token already closed its model HTTP
            # connection; downstream checkpoints suppress later phases.
            self._stop_standalone_input_meter()
            self.stop_wake_listener()
            self._release_speech_runtime()

            if not busy:
                self._release_brains_for_voice_phase()
                self._release_voice_output_runtime()

        elif resume_wake:
            # Resume is owned by the runtime itself rather than a second
            # frontend action. This makes Pause -> Active a single lifecycle
            # transition and prevents Wake from remaining stopped after Resume.
            self.start_wake_listener()

        elif self._input_meter_enabled:
            self._start_standalone_input_meter()

        self._notify_runtime_pause_state()

    def _prewarm_routing_classifiers(self) -> None:
        """Warm deterministic routing before the first user command.

        The Gate and Estimator remain shadow-only at this stage.  Warmup is
        deliberately best-effort so a diagnostic classifier can never block
        Qronos startup while the legacy TaskRouter is still authoritative.
        """
        samples = (
            "سلام",
            "پایتخت کانادا چیه؟",
            "چرا آسمان آبیه؟",
            "دو به علاوه دو چند میشه؟",
        )

        try:
            for sample in samples:
                routing_input = RoutingInput.from_text(
                    sample
                )
                self.intent_gate.classify(
                    routing_input
                )
                self.compute_estimator.estimate(
                    routing_input
                )
        except Exception:
            # Shadow diagnostics are non-authoritative.  Runtime startup must
            # remain available even if warmup itself ever regresses.
            pass

    def _select_production_task_type(
        self,
        text: str,
        routed_task_type: TaskType,
        routing_shadow: dict[str, Any] | None = None,
    ) -> tuple[TaskType, dict[str, Any]]:
        """
        Apply one conservative production promotion on top of TaskRouter.

        TaskRouter remains authoritative for every non-FAST route. The compute
        estimator may only promote FAST -> HEAVY, and only when complexity
        evidence is strong enough to avoid turning ordinary short "why"
        questions into expensive Heavy calls.
        """
        details: dict[str, Any] = {
            "legacyRoute": routed_task_type.value,
            "selectedRoute": routed_task_type.value,
            "applied": False,
        }

        if routed_task_type is not TaskType.FAST:
            return routed_task_type, details

        # Windows Control v2: location/app action language must not fall through
        # to FAST merely because legacy TaskRouter lacks a particular noun such
        # as Drive/Downloads.  This promotion is deterministic and happens
        # before any model runs.
        if _windows_control_route_promotion(text):
            details.update(
                {
                    "selectedRoute": TaskType.COMPUTER.value,
                    "applied": True,
                    "windowsControlPromotion": True,
                }
            )
            return TaskType.COMPUTER, details

        try:
            shadow = routing_shadow or self._routing_shadow(
                text
            )

            level = str(
                shadow.get("computeLevel", "")
            ).strip().lower()
            score = int(
                shadow.get("computeScore", 0)
            )
            confidence = float(
                shadow.get("computeConfidence", 0.0)
            )
            factors = tuple(
                str(item)
                for item in shadow.get(
                    "computeFactors",
                    (),
                )
            )

            strong_normal_or_deep = (
                level
                in {
                    "heavy_normal",
                    "heavy_deep",
                }
                and confidence >= 0.88
            )

            strong_eco = (
                level == "heavy_eco"
                and score >= 16
                and confidence >= 0.88
                and len(factors) >= 2
            )

            if (
                strong_normal_or_deep
                or strong_eco
            ):
                details.update(
                    {
                        "selectedRoute":
                            TaskType.HEAVY.value,
                        "applied": True,
                        "computeLevel": level,
                        "computeScore": score,
                        "computeConfidence":
                            round(
                                confidence,
                                6,
                            ),
                        "computeFactors":
                            list(factors),
                    }
                )
                return TaskType.HEAVY, details

            details.update(
                {
                    "computeLevel": level,
                    "computeScore": score,
                    "computeConfidence":
                        round(
                            confidence,
                            6,
                        ),
                    "computeFactors":
                        list(factors),
                }
            )

        except Exception as exc:
            details["error"] = str(exc)

        return routed_task_type, details

    def _ensure_voice_output_instance(
        self,
    ) -> ChatterboxRuntime:
        """
        Construct the Chatterbox runtime without forcing a model load.

        The assignment is protected because Wake/background prewarm and the
        ordinary prepare() path can race on the first voice turn.
        """
        with self._lock:
            voice = self.voice_output

            if voice is None:
                voice = ChatterboxRuntime()

                if not voice.health_check():
                    raise RuntimeError(
                        "Qronos TTS runtime is not ready."
                    )

                self.voice_output = voice

            return voice

    def _prewarm_voice_output_async(
        self,
    ) -> None:
        """
        Hide Chatterbox cold-start behind command capture / STT.

        This is best-effort and never delays listening. Chatterbox itself has
        single-flight startup, so speak_to_file() safely joins an unfinished
        prewarm instead of launching a duplicate server.
        """
        try:
            voice = self._ensure_voice_output_instance()
        except Exception as exc:
            self._voice_output_prewarm_error = str(
                exc
            )
            return

        if bool(
            getattr(
                voice,
                "is_loaded",
                False,
            )
        ):
            self._voice_output_prewarm_error = None
            return

        with self._lock:
            current = (
                self._voice_output_prewarm_thread
            )

            if (
                current is not None
                and current.is_alive()
            ):
                return

            self._voice_output_prewarm_error = None

            def worker() -> None:
                try:
                    voice.prewarm()
                except Exception as exc:
                    self._voice_output_prewarm_error = (
                        str(exc)
                    )

            thread = threading.Thread(
                target=worker,
                name="qronos-tts-prewarm",
                daemon=True,
            )
            self._voice_output_prewarm_thread = (
                thread
            )

        thread.start()

    def _yield_voice_output_before_brain(
        self,
        timeout_seconds: float = 8.0,
    ) -> bool:
        """
        A model-backed Brain must not share the 8 GB card with hot TTS.

        Chatterbox prewarm starts while the user is speaking so routes that do
        not need a Brain (for example Computer or local arithmetic) can answer
        quickly. Once routing proves that Fast/Heavy/Browser will load a Brain,
        wait for any in-flight prewarm to settle and then release TTS before the
        Brain is admitted. A timeout fails closed instead of risking VRAM
        overcommit or an Ollama runner failure.
        """
        thread = self._voice_output_prewarm_thread

        if (
            thread is not None
            and thread.is_alive()
        ):
            thread.join(
                timeout=max(
                    0.0,
                    float(
                        timeout_seconds
                    ),
                )
            )

        if (
            thread is not None
            and thread.is_alive()
        ):
            return False

        return self._release_voice_output_runtime()

    def _yield_voice_output_before_heavy(
        self,
        timeout_seconds: float = 8.0,
    ) -> bool:
        """Compatibility seam for the earlier Heavy-only handoff tests."""
        return self._yield_voice_output_before_brain(
            timeout_seconds=timeout_seconds,
        )

    def _routing_shadow(
        self,
        transcript: str,
    ) -> dict[str, Any]:
        """Classify one real transcript without changing its production route.

        This is diagnostic-only. Any failure is captured in the returned payload
        instead of escaping into the voice turn, and TaskRouter remains the sole
        authority for selected_task_type until the Resolver is separately approved.
        """
        started = time.perf_counter()

        try:
            routing_input = RoutingInput.from_text(
                transcript
            )
            intent = self.intent_gate.classify(
                routing_input
            )
            compute = self.compute_estimator.estimate(
                routing_input
            )

            return {
                "primaryIntent": intent.primary_intent.value,
                "requiredIntents": [
                    item.value
                    for item in intent.required_intents
                ],
                "intentConfidence": round(
                    float(intent.confidence),
                    6,
                ),
                "accuracyRisk": intent.accuracy_risk.value,
                "intentSignals": list(
                    intent.signals
                ),
                "computeLevel": compute.level.value,
                "computeScore": int(
                    compute.score
                ),
                "computeConfidence": round(
                    float(compute.confidence),
                    6,
                ),
                "computeFactors": list(
                    compute.factors
                ),
                "elapsedMs": round(
                    (time.perf_counter() - started)
                    * 1000.0,
                    6,
                ),
                "authoritative": False,
            }
        except Exception as exc:
            return {
                "error": str(exc),
                "elapsedMs": round(
                    (time.perf_counter() - started)
                    * 1000.0,
                    6,
                ),
                "authoritative": False,
            }

    def _emit_audio_spectrum(
        self,
        level: float,
        bands: tuple[float, ...],
    ) -> None:
        payload = {
            "level": round(
                max(0.0, min(1.0, level)),
                4,
            ),
            "bands": [
                round(
                    max(0.0, min(1.0, value)),
                    4,
                )
                for value in bands[:AUDIO_SPECTRUM_BANDS]
            ],
        }

        emit(
            "voice_audio_spectrum",
            "listening",
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

        if self._input_meter_enabled:
            self._emit_input_meter_level(
                payload["level"]
            )

    def _clear_audio_spectrum(self) -> None:
        self._emit_audio_spectrum(
            0.0,
            tuple(
                0.0
                for _ in range(AUDIO_SPECTRUM_BANDS)
            ),
        )

    def _emit_input_meter_level(
        self,
        level: float,
        *,
        active: bool = True,
        force: bool = False,
    ) -> None:
        if (
            not self._input_meter_enabled
            and not force
        ):
            return

        now = time.monotonic()

        if (
            not force
            and now - self._input_meter_last_emit
            < 0.10
        ):
            return

        self._input_meter_last_emit = now

        self._notify(
            "microphone_input_level",
            "listening" if active else "ready",
            json.dumps(
                {
                    "level": round(
                        max(
                            0.0,
                            min(
                                1.0,
                                float(level),
                            ),
                        ),
                        4,
                    ),
                    "active": bool(active),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _frame_input_level(
        frame: bytes,
    ) -> float:
        samples = np.frombuffer(
            frame,
            dtype=np.int16,
        ).astype(
            np.float32,
            copy=False,
        )

        if samples.size == 0:
            return 0.0

        normalized = samples / 32768.0
        rms = float(
            np.sqrt(
                np.mean(
                    normalized *
                    normalized
                )
            )
        )

        # Speech RMS is naturally much lower than full-scale PCM. A small
        # display-only gain makes ordinary speech visible without changing the
        # audio sent to Wake/VAD/STT.
        return max(
            0.0,
            min(
                1.0,
                rms * 4.0,
            ),
        )

    def _maybe_emit_input_meter_frame(
        self,
        frame: bytes,
    ) -> None:
        if not self._input_meter_enabled:
            return

        self._emit_input_meter_level(
            self._frame_input_level(
                frame
            )
        )

    def _standalone_input_meter_loop(
        self,
    ) -> None:
        audio: AudioInput | None = None

        try:
            audio = (
                self._new_audio_input_from_settings()
            )
            audio.start()

            while (
                self._input_meter_enabled
                and not
                self._input_meter_stop.is_set()
                and not
                self.wake_listener_running
                and not
                self.runtime_paused
            ):
                frame = audio.read_frame()
                self._maybe_emit_input_meter_frame(
                    frame
                )

        except Exception as exc:
            if (
                self._input_meter_enabled
                and not self.runtime_paused
            ):
                self._notify(
                    "microphone_meter_warning",
                    "warning",
                    str(exc),
                )

        finally:
            if audio is not None:
                try:
                    audio.stop()
                except Exception:
                    pass

            self._emit_input_meter_level(
                0.0,
                active=False,
                force=True,
            )

            if (
                self._input_meter_thread
                is threading.current_thread()
            ):
                self._input_meter_thread = None

    def _start_standalone_input_meter(
        self,
    ) -> None:
        if (
            not self._input_meter_enabled
            or self.runtime_paused
            or self.wake_listener_running
        ):
            return

        thread = self._input_meter_thread
        if (
            thread is not None
            and thread.is_alive()
        ):
            return

        self._input_meter_stop.clear()

        thread = threading.Thread(
            target=self._standalone_input_meter_loop,
            name="qronos-input-meter",
            daemon=True,
        )
        self._input_meter_thread = thread
        thread.start()

    def _stop_standalone_input_meter(
        self,
    ) -> None:
        self._input_meter_stop.set()

        thread = self._input_meter_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not
            threading.current_thread()
        ):
            thread.join(timeout=0.75)

        if (
            thread is not
            threading.current_thread()
        ):
            self._input_meter_thread = None

    def start_input_meter(self) -> None:
        self._input_meter_enabled = True
        self._input_meter_last_emit = 0.0

        if not self.wake_listener_running:
            self._start_standalone_input_meter()

    def stop_input_meter(self) -> None:
        self._input_meter_enabled = False
        self._stop_standalone_input_meter()
        self._emit_input_meter_level(
            0.0,
            active=False,
            force=True,
        )

    def _record_conversation_message(
        self,
        *,
        role: str,
        text: str,
        source: str,
        request_id: str = "",
    ) -> None:
        message = self.conversation_history.append(
            role=role,
            text=text,
            source=source,
            request_id=request_id,
        )

        self._notify(
            "conversation_message_added",
            "ready",
            json.dumps(
                message.to_public_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def emit_conversation_history_snapshot(
        self,
    ) -> None:
        self._notify(
            "conversation_history_snapshot",
            "ready",
            self.conversation_history.snapshot_json(),
        )

    # ------------------------------------------------------------ activity history

    def _history_records(self) -> tuple[ActionAuditLog, UndoJournal]:
        """The trail and journal the executors already write (created the same way they are)."""
        if self.action_audit is None:
            self.action_audit = ActionAuditLog()
            set_default_audit_sink(self.action_audit.record_verdict)
        if self.undo_journal is None:
            self.undo_journal = UndoJournal()
        return self.action_audit, self.undo_journal

    def _history_json(self, event_type: str, status: str, payload: dict[str, Any]) -> None:
        self._notify(event_type, status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def emit_history_snapshot(self) -> None:
        """The bounded, redacted activity history (read only; nothing is collected or replayed)."""
        audit, journal = self._history_records()
        self._history_json("history_snapshot", "ready", activity_history.snapshot(audit, journal))

    def _history_undo_request(self, entry):
        from core.actions import ActionRequest
        from security.permissions import ActionCategory

        return ActionRequest(
            category=ActionCategory.CREATE_OR_EDIT_FILE,
            target=activity_history.target_for(entry),
            summary=activity_history.UNDO_REQUEST_SUMMARY,
            parameters={"undoesActionId": entry.action_id, "reversal": entry.reversal.value},
        )

    def _history_source_read_verdict(self, entry, audit):
        """The CURRENT Files verdict for reading a created copy's SOURCE (the undo reads it to prove removing the copy
        loses nothing), or None when the undo reads no source. Recorded under the undo request's summary. The full
        verdict is kept: a refusal means no read; a required confirmation or typed secret must be satisfied first."""
        from core.actions import ActionRequest
        from security.gate import evaluate
        from security.permissions import ActionCategory

        source = activity_history.source_read_path(entry)
        if source is None:
            return None
        request = ActionRequest(
            category=ActionCategory.READ_NON_SENSITIVE,
            target=source,
            summary=activity_history.UNDO_REQUEST_SUMMARY,
            parameters={"undoesActionId": entry.action_id, "reads": "copy_source"},
        )
        return evaluate(request, audit=audit.record_verdict)

    def request_history_undo(self, action_id: str) -> None:
        """Prepare the undo of one history entry: exact checks, the gate, then a single-use confirmation."""
        from security.gate import evaluate

        action_id = str(action_id or "").strip()[:128]
        if self.runtime_paused:
            self._history_json("history_undo_refused", "ready", {"actionId": action_id, "reason": "paused"})
            return
        audit, journal = self._history_records()
        entry, state = activity_history.undoable_entry(journal, action_id)
        if entry is None or self.file_executor is None or self.file_executor.journal is not journal:
            self._history_json("history_undo_refused", "ready",
                               {"actionId": action_id, "reason": state if entry is None else "unsupported"})
            return
        verdict = evaluate(self._history_undo_request(entry), audit=audit.record_verdict)
        read_verdict = self._history_source_read_verdict(entry, audit)
        if verdict.refused or (read_verdict is not None and read_verdict.refused):
            self._history_json("history_undo_refused", "ready", {"actionId": action_id, "reason": "permission_denied"})
            return
        # Only now, with the gates' permission, is file content read: the copy's hash, and its source's only when
        # reading the source is allowed outright (a required confirmation or secret is satisfied at the answer first).
        source_now = read_verdict is None or read_verdict.allowed
        entry, state = activity_history.undoable_entry(journal, action_id, content=True, source=source_now)
        if entry is None:
            self._history_json("history_undo_refused", "ready", {"actionId": action_id, "reason": state})
            return
        needs_secret = verdict.decision is PermissionDecision.REQUIRE_TYPED_SECRET or (
            read_verdict is not None and read_verdict.decision is PermissionDecision.REQUIRE_TYPED_SECRET)
        with self._approval_lock:
            self._approvals.revoke_kinds((ApprovalKind.HISTORY_UNDO,), STALE_SUPERSEDED)
            ticket = self._approvals.issue(
                ApprovalKind.HISTORY_UNDO,
                "history_undo",
                payload=PendingHistoryUndo(action_id=entry.action_id, needs_secret=needs_secret),
            )
        self._history_json("history_undo_approval_required", "approval_required", {
            "actionToken": ticket.approval_id,
            "actionId": entry.action_id,
            "reversal": entry.reversal.value,
            "name": activity_history._name(activity_history.target_for(entry)),
            "needsSecret": needs_secret,
            # The confirmation also covers reading the copy's original once more (shown to the person).
            "readsSource": read_verdict is not None,
        })

    def _verify_history_secret(self, secret: str | None) -> bool:
        verifier = getattr(self, "_history_secret_verifier", None)
        if verifier is None:
            # The bridge never opens the secret store itself: it borrows the Library's narrow check (the same
            # per-profile store and persisted lockout as every other typed-secret approval).
            from core.library_executor import WorkflowSecretCheck

            verifier = WorkflowSecretCheck().verify
        try:
            return bool(isinstance(secret, str) and secret and verifier(secret))
        except Exception:  # an unverifiable secret is a refusal, never a pass
            return False

    def resolve_history_undo(self, action_token: str, *, approved: bool, secret: str | None = None) -> None:
        """Answer one history undo confirmation, exactly once. Stale, late or unknown answers do nothing."""
        from core.action_audit import AuditEvent
        from core.approval_ledger import StaleApproval
        from security.gate import evaluate

        token = str(action_token or "").strip()
        if self.runtime_paused:
            self._history_json("history_undo_stale", "ready", {"actionToken": token, "reason": "paused"})
            return
        try:
            ticket = self._approvals.take(token, kinds=(ApprovalKind.HISTORY_UNDO,))
        except StaleApproval as exc:
            self._history_json("history_undo_stale", "ready", {"actionToken": token, "reason": exc.reason})
            return
        pending: PendingHistoryUndo = ticket.payload
        audit, journal = self._history_records()
        if not approved:
            self._history_json("history_undo_declined", "ready", {"actionToken": token, "actionId": pending.action_id})
            return
        entry, state = activity_history.undoable_entry(journal, pending.action_id)
        outcome = state
        if entry is not None and self.file_executor is not None and self.file_executor.journal is journal:
            request = self._history_undo_request(entry)
            verdict = evaluate(request, audit=audit.record_verdict)
            # Both CURRENT policies decide again: the removal, and reading the copy's source.
            read_verdict = self._history_source_read_verdict(entry, audit)
            secret_needed = pending.needs_secret or verdict.decision is PermissionDecision.REQUIRE_TYPED_SECRET or (
                read_verdict is not None and read_verdict.decision is PermissionDecision.REQUIRE_TYPED_SECRET)
            if verdict.refused or (read_verdict is not None and read_verdict.refused):
                outcome = "refused"  # nothing is read or removed
            elif secret_needed and not self._verify_history_secret(secret):
                self._history_json("history_undo_secret_rejected", "ready",
                                   {"actionToken": token, "actionId": pending.action_id})
                return
            # The person's yes to this single-use confirmation is the confirmation both policies may require.
            elif activity_history.undoable_entry(journal, pending.action_id, content=True)[0] is None:
                outcome = "changed_since"  # checked again, with permission, immediately before the undo
            else:
                audit.record(AuditEvent.APPROVED, request, decision=verdict.decision)
                try:
                    self.file_executor.undo(entry.action_id)
                    outcome = "undone"
                except Exception:
                    outcome = "failed"
        elif entry is not None:
            outcome = "unsupported"
        self._history_json("history_undo_result", "ready",
                           {"actionToken": token, "actionId": pending.action_id, "outcome": outcome})
        self.emit_history_snapshot()

    def _discard_pending_history_undo(self, reason: str) -> int:
        """Revoke an open history undo confirmation; its late answer is stale and does nothing."""
        return len(self._approvals.revoke_kinds((ApprovalKind.HISTORY_UNDO,), reason))

    def _conversation_brain_context(
        self,
    ):
        return (
            self.conversation_history.brain_context()
        )

    def _ensure_text_runtime(self) -> None:
        """
        Prepare only the collaborators needed for typed Fast/Heavy turns.

        Text Chat must not warm the microphone, Whisper, VAD, or Chatterbox.
        If the full voice runtime is already prepared, reuse its router and
        orchestrator instead of constructing parallel Brain state.
        """
        if self.task_router is None:
            self.task_router = TaskRouter()

        if self.orchestrator is None:
            self.orchestrator = Orchestrator()

    # ------------------------------------------------------------ device link

    def _link_runtime_settings(self):
        """The link's switches and files, read now (tests move CONFIG's roots)."""
        from core.link_runtime import LinkRuntimeSettings

        return LinkRuntimeSettings(
            link_enabled=self.settings.get("link.enabled") is True,
            # Off until the Link worker's WAN gate is accepted: no relay is
            # dialled and no phone outside this network is served.
            remote_access_enabled=False,
            device_path=Path(CONFIG.paths.data) / "link_devices.json",
            audit_path=Path(CONFIG.paths.logs) / "link_audit.jsonl",
            relay_path=Path(CONFIG.paths.data) / "link_relay.json",
            # The Security Monitor's latches and incident log (Link D9); a
            # quarantine keeps the whole link off until reset at this PC.
            security_path=Path(CONFIG.paths.data) / "link_security.json",
            incident_path=Path(CONFIG.paths.logs) / "link_incidents.jsonl",
        )

    def _device_link(self):
        """The one LinkRuntime, built on first use and never started here."""
        link = getattr(self, "_link_runtime", None)

        if link is None:
            from core.link_runtime import LinkRuntime, runtime_services

            # Phone questions reach only LinkRuntime's ToolFreeAsk over the
            # orchestrator's prompt-only Fast-brain call: no router, workers,
            # tools, file actions or queue.
            link = LinkRuntime(self._link_runtime_settings(), services=runtime_services(self))
            self._link_runtime = link

        return link

    def start_device_link(self) -> bool:
        """Start the link when the user turned it on. Never raises.

        Called once the bridge is ready, in text-only mode too: it needs the
        text runtime, never the microphone or the wake model. Off means
        nothing is built: no device file, listener, socket or thread.
        """
        settings = self._link_runtime_settings()

        if not settings.link_enabled:
            return False

        try:
            self._ensure_text_runtime()
            link = self._device_link()
            link.settings = settings
            running = link.start()
        except Exception as exc:  # the link must never stop the runtime
            self._notify("link_error", "warning", json.dumps(
                {"action": "start", "code": "failed", "detail": type(exc).__name__},
            ))
            return False

        self._publish_link_status()
        return running

    def stop_device_link(self) -> None:
        link = getattr(self, "_link_runtime", None)

        if link is not None:
            link.stop()

    def apply_device_link_settings(self) -> None:
        """After a settings reload: restart only when the link's own settings changed."""
        wanted = self._link_runtime_settings()
        link = getattr(self, "_link_runtime", None)

        if link is None or link.settings == wanted:
            if link is None and wanted.link_enabled:
                self.start_device_link()
            return

        # Every open phone session ends here, before anything restarts.
        link.stop()
        link.settings = wanted

        if wanted.link_enabled:
            self.start_device_link()
        else:
            self._publish_link_status()

    def _publish_link_status(self) -> None:
        link = self._device_link()
        self._notify("link_status", "ready", json.dumps(link.status(), ensure_ascii=True))
        self._notify("link_devices", "ready", json.dumps(
            [device.to_json() for device in link.devices()], ensure_ascii=True,
        ))

    def _ensure_file_runtime(self) -> None:
        """Prepare only the trusted local file-action collaborators."""
        if self.action_audit is None:
            self.action_audit = ActionAuditLog()
            set_default_audit_sink(
                self.action_audit.record_verdict
            )

        if self.undo_journal is None:
            self.undo_journal = UndoJournal()

        if self.file_executor is None:
            self.file_executor = FileExecutor(
                audit=self.action_audit,
                journal=self.undo_journal,
            )

        if self.file_resolver is None:
            self.file_resolver = KnownFolderFileResolver(use_index=True)

    def _ensure_app_workflows(self) -> AppWorkflowCoordinator:
        """Prepare the Telegram/print workflow coordinator with real Windows adapters.

        Native collaborators are created only when a workflow step runs, so a
        recognised request never touches Telegram or the spooler before its
        approval. Tests inject their own coordinator instead.
        """
        existing = getattr(self, "_app_workflows", None)
        if existing is not None:
            return existing
        self._ensure_file_runtime()
        from core.app_workflow import SideEffectJournal, WorkflowError, WorkflowRefused
        from core.file_resolver import NamedFileRequest
        from core.print_workflow import PrintWorkflow, ShellPrintToSubmitter, WinSpooler, default_renderer
        from core.telegram_workflow import LocalDownloadFolder, TelegramDownloadWorkflow, UiaTelegramUi

        journal = SideEffectJournal(Path(CONFIG.paths.data) / "workflows" / "side-effects.json")
        # Durable, shared by every workflow in this runtime; kept for inspection.
        self._app_workflow_journal = journal
        try:
            # Attempts left SUBMITTING by a process that no longer runs become
            # UNCERTAIN: still blocking repeats, and visible for a person to check.
            journal.reconcile_interrupted()
        except WorkflowError:
            pass  # a damaged journal refuses every side effect on use anyway

        def telegram_workflow() -> TelegramDownloadWorkflow:
            self._ensure_computer_runtime()
            executor = self.computer_executor
            folder = _telegram_download_folder()
            if folder is None:
                raise WorkflowRefused(
                    "Telegram's download folder is not configured (QRONOS_TELEGRAM_DOWNLOAD_FOLDER)."
                )

            def target():
                from core.computer_executor import get_app_spec
                from core.window_binding import WindowTarget

                spec = get_app_spec(executor.resolve_app_id("telegram"))
                return WindowTarget(
                    app_id=spec.app_id, display_name=spec.display_name, aliases=spec.aliases,
                    executable=spec.executable, process_names=spec.process_names,
                )

            from core.accessibility_action_executor import AccessibilityActionExecutor
            from core.accessibility_targeting import AccessibilityTargetLocator

            binder = executor.window_binder
            # Exact-RuntimeId actions only; the locator has no model and is unused.
            actuator = AccessibilityActionExecutor(binder, executor, AccessibilityTargetLocator(None))
            return TelegramDownloadWorkflow(
                UiaTelegramUi(binder=binder, target_fn=target, actuator=actuator),
                binder=binder,
                journal=journal,
                folder=LocalDownloadFolder(folder),
            )

        def browser_workflow():
            from core.browser_research import (
                BrowserResearchWorkflow,
                UiaPageReader,
                foreground_window_snapshot,
            )
            from core.visible_browser import VisibleBrowserLauncher
            from core.window_binding import WindowBinder

            return BrowserResearchWorkflow(
                launcher=VisibleBrowserLauncher(),
                reader=UiaPageReader(),
                binder=WindowBinder(),
                foreground_fn=foreground_window_snapshot,
            )

        def print_workflow() -> PrintWorkflow:
            return PrintWorkflow(
                spooler=WinSpooler(), submitter=ShellPrintToSubmitter(), journal=journal,
                renderer=default_renderer(),
            )

        def resolve_file(reference: str) -> Path:
            exact = reference if Path(reference).is_absolute() else None
            return self.file_resolver.resolve(NamedFileRequest(name_hint=reference, exact_path=exact)).path

        def default_printer() -> str | None:
            return next((item.name for item in WinSpooler().printers() if item.is_default), None)

        from core.library_executor import WorkflowSecretCheck

        # The per-profile approval secret and its shared lockout (Library store).
        secret_check = WorkflowSecretCheck()

        self._app_workflows = AppWorkflowCoordinator(
            ledger=self._approvals,
            notify=self._notify,
            actions=self._computer_actions,
            telegram_workflow=telegram_workflow,
            print_workflow=print_workflow,
            resolve_file=resolve_file,
            default_printer=default_printer,
            # The bridge never opens the secret store itself
            # (tests/test_approval_secret.py); it borrows the Library's check.
            verify_secret=secret_check.verify,
            secret_status=secret_check.status,
            paused=lambda: self.runtime_paused,
            audit=self.action_audit,
            journal=journal,
            browser_workflow=browser_workflow,
        )
        return self._app_workflows

    def _ensure_elevated_actions(self):
        """The elevated-action coordinator (core/elevated_actions.py), built on first use."""
        coordinator = getattr(self, "_elevated_actions", None)
        if coordinator is None:
            from core.elevated_actions import ElevatedActionCoordinator
            from core.library_executor import WorkflowSecretCheck

            # The bridge never opens the secret store itself; like printing,
            # it borrows the Library's per-profile check and shared lockout.
            secret_check = WorkflowSecretCheck()
            coordinator = ElevatedActionCoordinator(
                notify=self._notify,
                verify_secret=secret_check.verify,
                secret_status=secret_check.status,
                paused=lambda: self.runtime_paused,
            )
            self._elevated_actions = coordinator
        return coordinator

    def _begin_text_elevated_action(self, text: str, request_id: str, started_at: float) -> bool:
        """
        Route the person's own request to restart the print spooler.

        Only desktop chat turns come here; Device Link text goes to the
        tool-free chat path and can never reach an elevated action.
        """
        from core.elevated_actions import parse_spooler_restart

        if not parse_spooler_restart(text):
            return False
        if self._computer_actions.stopped_since_turn_start():
            self._finish_text_file_action(
                request_id=request_id,
                response="لغو شد؛ هیچ کاری شروع نشد.",
                started_at=started_at,
            )
            return True
        reply = self._ensure_elevated_actions().try_begin(text, request_id)
        if reply is None:
            return False
        self._finish_text_file_action(request_id=request_id, response=reply, started_at=started_at)
        return True

    def _begin_text_app_workflow(self, text: str, request_id: str, started_at: float) -> bool:
        """Route a Telegram-download or print request to its approval flow."""
        from core.app_workflow_intents import parse_workflow_intent
        from core.browser_research import plan_navigation

        if self._begin_text_elevated_action(text, request_id, started_at):
            return True

        if parse_workflow_intent(text) is None and plan_navigation(text) is None:
            return False
        if self._computer_actions.stopped_since_turn_start():
            self._finish_text_file_action(
                request_id=request_id,
                response="لغو شد؛ هیچ کاری شروع نشد.",
                started_at=started_at,
            )
            return True
        reply = self._ensure_app_workflows().try_begin(text, request_id)
        if reply is None:
            return False
        self._finish_text_file_action(request_id=request_id, response=reply, started_at=started_at)
        return True

    def resolve_app_workflow_decision(
        self,
        action_token: str,
        *,
        approved: bool,
        secret: str | None = None,
    ) -> None:
        """Answer one workflow prompt by its single-use identity."""
        elevated = getattr(self, "_elevated_actions", None)
        if elevated is not None and elevated.owns(action_token):
            elevated.resolve(action_token, approved=approved, secret=secret)
            return
        coordinator = getattr(self, "_app_workflows", None)
        if coordinator is None:
            self._notify(
                "app_workflow_stale",
                "ready",
                json.dumps({"actionToken": str(action_token or ""), "reason": "unknown"}, separators=(",", ":")),
            )
            return
        coordinator.resolve(action_token, approved=approved, secret=secret)

    def _ensure_computer_runtime(self) -> None:
        """Prepare the trusted Windows computer-action collaborator."""
        if self.action_audit is None:
            self.action_audit = ActionAuditLog()
            set_default_audit_sink(
                self.action_audit.record_verdict
            )

        if self.undo_journal is None:
            self.undo_journal = UndoJournal()

        if self.computer_executor is None:
            self.computer_executor = ComputerExecutor(
                audit=self.action_audit,
                journal=self.undo_journal,
                dynamic_apps=True,
            )

        if self.paint_draw_executor is None:
            self.paint_draw_executor = PaintDrawExecutor(
                self.computer_executor,
                reference_provider=self.paint_reference_provider,
                audit=self.action_audit,
                journal=self.undo_journal,
            )

    @staticmethod
    def _computer_operation_label(
        operation: ComputerOperation,
    ) -> str:
        labels = {
            ComputerOperation.OPEN_APP: "باز کردن برنامه",
            ComputerOperation.CLOSE_APP: "بستن برنامه",
            ComputerOperation.FOCUS_WINDOW: "آوردن پنجره به جلو",
            ComputerOperation.MINIMIZE_WINDOW: "مینیمایز کردن پنجره",
            ComputerOperation.MAXIMIZE_WINDOW: "ماکسیمایز کردن پنجره",
            ComputerOperation.OPEN_LOCATION: "باز کردن پوشه یا درایو",
            ComputerOperation.DRAW_BASIC_HOUSE: "کشیدن خانه‌ی ساده در Paint",
        }
        return labels.get(
            operation,
            operation.value,
        )

    def _file_write_approval_lease_remaining(self) -> float:
        return max(
            0.0,
            self._file_write_approval_lease_until
            - float(self._file_approval_clock()),
        )

    def _file_write_approval_lease_active(self) -> bool:
        return self._file_write_approval_lease_remaining() > 0.0

    def _grant_file_write_approval_lease(self) -> None:
        now = float(self._file_approval_clock())
        self._file_write_approval_lease_until = (
            now + FILE_WRITE_APPROVAL_LEASE_SECONDS
        )
        self._notify(
            "file_action_approval_lease_started",
            "ready",
            json.dumps(
                {
                    "seconds": int(FILE_WRITE_APPROVAL_LEASE_SECONDS),
                    "expiresInSeconds": int(FILE_WRITE_APPROVAL_LEASE_SECONDS),
                    "scope": "reversible_file_writes",
                    "resetsOnRestart": True,
                    "clearsOnPause": True,
                    "sliding": False,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def _clear_file_write_approval_lease(self, reason: str) -> None:
        was_active = self._file_write_approval_lease_active()
        self._file_write_approval_lease_until = 0.0
        if was_active:
            self._notify(
                "file_action_approval_lease_cleared",
                "ready",
                json.dumps(
                    {
                        "reason": str(reason or "cleared"),
                        "scope": "reversible_file_writes",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )

    @staticmethod
    def _file_operation_label(
        operation: FileOperation,
    ) -> str:
        labels = {
            FileOperation.COPY_FILE: "کپی فایل",
            FileOperation.MOVE_FILE: "انتقال فایل",
            FileOperation.RENAME_FILE: "تغییر نام فایل",
            FileOperation.CREATE_FOLDER: "ساخت پوشه",
            FileOperation.OPEN_FILE: "باز کردن فایل",
        }
        return labels.get(
            operation,
            operation.value,
        )

    def _finish_text_file_action(
        self,
        *,
        request_id: str,
        response: str,
        started_at: float,
    ) -> None:
        with self._foreground_lifecycle().publication(action_outcome=True, terminal=True):
            self._record_conversation_message(
                role="assistant",
                text=response,
                source="text",
                request_id=request_id,
            )
            self._notify(
                "text_response",
                "ready",
                _text_event_message(
                    request_id,
                    text=response,
                    route=TaskType.COMPUTER.value,
                    elapsedSeconds=round(
                        time.perf_counter() - started_at,
                        3,
                    ),
                ),
            )

    def _fail_text_file_action(
        self,
        *,
        request_id: str,
        error: str,
        started_at: float,
    ) -> None:
        lifecycle = self._foreground_lifecycle()
        with lifecycle.publication(action_outcome=True, terminal=True):
            turn = lifecycle.current
            if turn is not None and turn.token.cancelled:
                self._notify("text_error", "cancelled", _text_event_message(
                    request_id, status="cancelled", reason=turn.token.reason,
                    error="Current native action was cancelled. Completed changes may remain."))
                return
            self._notify(
                "text_error",
                "error",
                _text_event_message(
                    request_id,
                    error=error,
                    route=TaskType.COMPUTER.value,
                    elapsedSeconds=round(
                        time.perf_counter() - started_at,
                        3,
                    ),
                ),
            )

    @staticmethod
    def _named_file_ambiguity_message(
        names: tuple[str, ...],
    ) -> str:
        shown = "، ".join(names[:3])
        if shown:
            return (
                "چند فایل با این بخش از اسم پیدا کردم: "
                f"{shown}. چند کلمه بیشتر از اسم فایل بگو."
            )
        return (
            "چند فایل شبیه به این اسم پیدا کردم. "
            "چند کلمه بیشتر از اسم فایل بگو."
        )

    def _resolve_named_file_action(
        self,
        text: str,
    ):
        request = parse_named_file_request(text)
        self._ensure_file_runtime()

        if self.file_resolver is None:
            raise RuntimeError(
                "Qronos file resolver did not prepare correctly."
            )

        resolved = self.file_resolver.resolve(request)
        action = FileAction(
            FileOperation.OPEN_FILE,
            source=str(resolved.path),
        )
        return request, resolved, action

    def _begin_text_named_file_action(
        self,
        text: str,
        request_id: str,
        started_at: float,
    ) -> bool:
        """
        Resolve a natural partial filename in Desktop/Documents/Downloads.

        Text respects the existing OPEN_APPLICATION permission policy. With the
        default policy it explains that the exact same request must be spoken;
        it never silently treats typed text as voice confirmation.
        """
        try:
            request, resolved, action = self._resolve_named_file_action(text)
        except NamedFileParseError:
            return False
        except NamedFileAmbiguous as exc:
            self._finish_text_file_action(
                request_id=request_id,
                response=self._named_file_ambiguity_message(exc.names),
                started_at=started_at,
            )
            return True
        except NamedFileSearchLimit:
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    "جست‌وجوی این محل خیلی گسترده شد. اسم فایل را کمی دقیق‌تر "
                    "یا یک پوشه محدودتر بگو."
                ),
                started_at=started_at,
            )
            return True
        except NamedFileNotFound:
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    "فایلی با این بخش از اسم در محلی که گفتی پیدا نکردم."
                ),
                started_at=started_at,
            )
            return True

        if self.file_executor is None:
            raise RuntimeError(
                "Qronos file executor did not prepare correctly."
            )

        prepared = self.file_executor.prepare(action)
        verdict = prepared.verdict

        if verdict.refused:
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    "فایل پیدا شد، اما تنظیمات مجوز فعلی اجازه باز کردنش را نمی‌دهد."
                ),
                started_at=started_at,
            )
            return True

        if verdict.allowed:
            execution = self.file_executor.execute_prepared(
                prepared,
                reevaluate=False,
            )
            if execution.result.outcome is not ActionOutcome.SUCCEEDED:
                self._fail_text_file_action(
                    request_id=request_id,
                    error=(
                        execution.result.detail
                        or "Open-file action failed."
                    ),
                    started_at=started_at,
                )
                return True

            self._finish_text_file_action(
                request_id=request_id,
                response=f"انجام شد. فایل {resolved.display_name} باز شد.",
                started_at=started_at,
            )
            return True

        if (
            verdict.decision
            is PermissionDecision.REQUIRE_VOICE_CONFIRMATION
        ):
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    f"فایل {resolved.display_name} را پیدا کردم. "
                    "طبق مجوز فعلی برای باز کردنش همین فرمان را با Voice بگو."
                ),
                started_at=started_at,
            )
            return True

        self._finish_text_file_action(
            request_id=request_id,
            response=(
                f"فایل {resolved.display_name} را پیدا کردم، اما باز کردنش "
                "به نوع دیگری از تأیید نیاز دارد."
            ),
            started_at=started_at,
        )
        return True

    def _execute_voice_named_file_action(
        self,
        transcript: str,
        report: dict[str, Any],
    ) -> str | None:
        try:
            request, resolved, action = self._resolve_named_file_action(
                transcript
            )
        except NamedFileParseError:
            return None
        except NamedFileAmbiguous as exc:
            report["voiceNamedFileAmbiguous"] = True
            report["voiceComputerDeferred"] = False
            return self._named_file_ambiguity_message(exc.names)
        except NamedFileSearchLimit:
            report["voiceNamedFileSearchLimit"] = True
            report["voiceComputerDeferred"] = False
            return (
                "جست‌وجوی این محل خیلی گسترده شد. اسم فایل را کمی دقیق‌تر "
                "یا یک پوشه محدودتر بگو."
            )
        except NamedFileNotFound:
            report["voiceNamedFileNotFound"] = True
            report["voiceComputerDeferred"] = False
            return (
                "فایلی با این بخش از اسم در محلی که گفتی پیدا نکردم."
            )

        if self.file_executor is None:
            raise RuntimeError(
                "Qronos file executor did not prepare correctly."
            )

        prepared = self.file_executor.prepare(action)
        verdict = prepared.verdict

        report["fileOperation"] = action.operation.value
        report["namedFileFolder"] = resolved.folder_key
        report["namedFileScore"] = int(resolved.score)
        report["namedFileName"] = resolved.display_name

        if verdict.refused:
            report["voiceNamedFileRefused"] = True
            report["voiceComputerDeferred"] = False
            return (
                "فایل پیدا شد، اما تنظیمات مجوز فعلی اجازه باز کردنش را نمی‌دهد."
            )

        approval = None
        if verdict.needs_approval:
            if (
                verdict.decision
                is PermissionDecision.REQUIRE_VOICE_CONFIRMATION
            ):
                approval = verdict.decision
            else:
                report["voiceNamedFileNeedsOtherApproval"] = (
                    verdict.decision.value
                )
                report["voiceComputerDeferred"] = False
                return (
                    "فایل پیدا شد، اما باز کردنش به نوع دیگری از تأیید نیاز دارد."
                )

        execution = self.file_executor.execute_prepared(
            prepared,
            approval=approval,
            reevaluate=False,
        )
        report["fileActionId"] = execution.request.action_id

        if execution.result.outcome is not ActionOutcome.SUCCEEDED:
            report["voiceNamedFileFailed"] = True
            report["voiceNamedFileFailure"] = execution.result.detail
            report["voiceComputerDeferred"] = False
            return (
                "فایل را پیدا کردم، اما Windows نتوانست آن را باز کند. "
                "جزئیات در گزارش اجرای کرونوس ثبت شد."
            )

        report["voiceNamedFileExecuted"] = True
        report["voiceComputerExecuted"] = True
        report["voiceComputerDeferred"] = False
        return f"انجام شد. فایل {resolved.display_name} باز شد."

    def _resolve_named_location_action(
        self,
        text: str,
    ):
        request = parse_location_request(text)
        self._ensure_file_runtime()
        self._ensure_computer_runtime()
        if self.file_resolver is None:
            raise RuntimeError("Qronos file resolver did not prepare correctly.")
        resolved = self.file_resolver.resolve_location(request)
        action = ComputerAction(
            ComputerOperation.OPEN_LOCATION,
            location=str(resolved.path),
        )
        return request, resolved, action

    def _begin_text_named_location_action(
        self,
        text: str,
        request_id: str,
        started_at: float,
    ) -> bool:
        try:
            _, resolved, action = self._resolve_named_location_action(text)
        except NamedLocationParseError:
            return False
        except NamedLocationNotFound:
            self._finish_text_file_action(
                request_id=request_id,
                response="پوشه یا درایوی که گفتی روی این سیستم در دسترس نیست.",
                started_at=started_at,
            )
            return True

        if self.computer_executor is None:
            raise RuntimeError("Qronos computer executor did not prepare correctly.")
        prepared = self.computer_executor.prepare(action)
        verdict = prepared.verdict
        if verdict.refused:
            self._finish_text_file_action(
                request_id=request_id,
                response="محل را پیدا کردم، اما تنظیمات مجوز اجازه باز کردنش را نمی‌دهد.",
                started_at=started_at,
            )
            return True
        if verdict.allowed:
            with self._computer_actions.begin("computer_action") as cancel_token:
                execution = self.computer_executor.execute_prepared(
                    prepared, reevaluate=False, cancel_token=cancel_token,
                )
            if execution.result.outcome is ActionOutcome.CANCELLED:
                self._finish_text_file_action(
                    request_id=request_id,
                    response=COMPUTER_ACTION_CANCELLED_RESPONSE,
                    started_at=started_at,
                )
                return True
            if execution.result.outcome is not ActionOutcome.SUCCEEDED:
                self._fail_text_file_action(
                    request_id=request_id,
                    error=execution.result.detail or "Open-location action failed.",
                    started_at=started_at,
                )
                return True
            self._finish_text_file_action(
                request_id=request_id,
                response=f"انجام شد. {resolved.display_name} باز شد.",
                started_at=started_at,
            )
            return True
        if verdict.decision is PermissionDecision.REQUIRE_VOICE_CONFIRMATION:
            self._finish_text_file_action(
                request_id=request_id,
                response=f"{resolved.display_name} را پیدا کردم. طبق مجوز فعلی همین فرمان را با Voice بگو.",
                started_at=started_at,
            )
            return True
        self._finish_text_file_action(
            request_id=request_id,
            response=f"{resolved.display_name} را پیدا کردم، اما باز کردنش به نوع دیگری از تأیید نیاز دارد.",
            started_at=started_at,
        )
        return True

    def _execute_voice_named_location_action(
        self,
        transcript: str,
        report: dict[str, Any],
    ) -> str | None:
        try:
            _, resolved, action = self._resolve_named_location_action(transcript)
        except NamedLocationParseError:
            return None
        except NamedLocationNotFound:
            report["voiceNamedLocationNotFound"] = True
            report["voiceComputerDeferred"] = False
            return "پوشه یا درایوی که گفتی روی این سیستم در دسترس نیست."

        if self.computer_executor is None:
            raise RuntimeError("Qronos computer executor did not prepare correctly.")
        prepared = self.computer_executor.prepare(action)
        verdict = prepared.verdict
        report["computerOperation"] = action.operation.value
        report["namedLocation"] = str(resolved.path)
        report["namedLocationKey"] = resolved.folder_key
        if verdict.refused:
            report["voiceNamedLocationRefused"] = True
            report["voiceComputerDeferred"] = False
            return "محل را پیدا کردم، اما تنظیمات مجوز اجازه باز کردنش را نمی‌دهد."
        approval = None
        if verdict.needs_approval:
            if verdict.decision is PermissionDecision.REQUIRE_VOICE_CONFIRMATION:
                approval = verdict.decision
            else:
                report["voiceNamedLocationNeedsOtherApproval"] = verdict.decision.value
                report["voiceComputerDeferred"] = False
                return "محل را پیدا کردم، اما باز کردنش به نوع دیگری از تأیید نیاز دارد."
        with self._computer_actions.begin("computer_action") as cancel_token:
            execution = self.computer_executor.execute_prepared(
                prepared, approval=approval, reevaluate=False,
                cancel_token=cancel_token,
            )
        if execution.result.outcome is ActionOutcome.CANCELLED:
            report["voiceComputerCancelled"] = True
            report["voiceComputerDeferred"] = False
            return COMPUTER_ACTION_CANCELLED_RESPONSE
        if execution.result.outcome is not ActionOutcome.SUCCEEDED:
            report["voiceNamedLocationFailed"] = True
            report["voiceNamedLocationFailure"] = execution.result.detail
            report["voiceComputerDeferred"] = False
            return "محل را پیدا کردم، اما Windows نتوانست آن را باز کند."
        report["voiceNamedLocationExecuted"] = True
        report["voiceComputerExecuted"] = True
        report["voiceComputerDeferred"] = False
        return f"انجام شد. {resolved.display_name} باز شد."

    def _select_paint_undo_target(self) -> tuple[str, str]:
        """
        Return (action_id, "") for the exact drawing a Paint undo may reverse,
        or ("", reason_key) explaining why there is none.

        Only the most recent reversible Qronos action qualifies (undo is a
        stack), and only when it is an exact app-level Paint reversal.
        """
        journal = self.undo_journal
        if journal is None:
            return "", "nothing"
        top = journal.next_undoable()
        if top is not None:
            if top.target == "app:paint" and top.reversal is Reversal.APP_UNDO_STEPS:
                return top.action_id, ""
            return "", "not_paint"
        if any(entry.target == "app:paint" for entry in journal.entries()):
            return "", "unavailable"
        return "", "nothing"

    def _prepare_paint_undo(
        self,
        text: str,
        report: dict[str, Any],
    ) -> tuple[PreparedPaintUndo | None, str]:
        """Shared recognition for text/voice; (None, "") means not an undo."""
        kind = classify_paint_undo_request(text)
        if not kind:
            return None, ""
        report["computerOperation"] = "undo_paint_drawing"
        report["computerAppId"] = "paint"
        report["voiceComputerDeferred"] = False
        if kind == PAINT_UNDO_AMBIGUOUS:
            return None, PAINT_UNDO_RESPONSES["ambiguous"]
        self._ensure_computer_runtime()
        if self.paint_draw_executor is None:
            raise RuntimeError("Qronos Paint draw executor did not prepare correctly.")
        action_id, reason = self._select_paint_undo_target()
        if not action_id:
            report["paintUndoUnavailable"] = reason
            return None, PAINT_UNDO_RESPONSES[reason]
        report["paintUndoTarget"] = action_id
        return self.paint_draw_executor.prepare_undo(action_id), ""

    def _execute_paint_undo(
        self,
        prepared: PreparedPaintUndo,
        *,
        gui_approval: PermissionDecision | None,
        screen_approval: PermissionDecision | None,
        report: dict[str, Any],
    ) -> str:
        with self._computer_actions.begin("paint_undo") as cancel_token:
            execution = self.paint_draw_executor.execute_undo(
                prepared,
                gui_approval=gui_approval,
                screen_approval=screen_approval,
                reevaluate=True,
                cancel_token=cancel_token,
            )
        report["paintUndoOutcome"] = execution.result.outcome.value
        report["paintUndoRestored"] = execution.restored
        report["paintUndoStepsSent"] = execution.steps_sent
        self._notify(
            "paint_undo_completed" if execution.restored else "paint_undo_not_completed",
            "ready",
            json.dumps(
                {
                    "undoesActionId": execution.undoes_action_id,
                    "outcome": execution.result.outcome.value,
                    "restored": execution.restored,
                    "stepsSent": execution.steps_sent,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )
        return _paint_undo_response(execution)

    def _prepare_voice_paint_undo_action(
        self,
        transcript: str,
        report: dict[str, Any],
    ) -> str | None:
        """
        Voice route for "undo my Paint drawing".

        ``None``: not an undo request. ``""``: waiting for the on-screen
        approval (same prompt, Stop/Pause/stale rules as drawing). Anything
        else is the final spoken answer. Nothing here sends a keystroke
        unless execute_undo proves the reversal is exact.
        """
        prepared, message = self._prepare_paint_undo(transcript, report)
        if prepared is None:
            return message or None

        gui = prepared.gui_verdict
        screen = prepared.screen_verdict
        if gui.refused or screen.refused:
            report["voiceComputerRefused"] = True
            return PAINT_UNDO_RESPONSES["refused"]

        gui_approval: PermissionDecision | None = None
        if gui.needs_approval:
            if gui.decision is not PermissionDecision.REQUIRE_VOICE_CONFIRMATION:
                report["voiceComputerNeedsOtherApproval"] = gui.decision.value
                return PAINT_UNDO_RESPONSES["other_approval"]
            gui_approval = gui.decision

        screen_approval: PermissionDecision | None = None
        if screen.needs_approval:
            if screen.decision is PermissionDecision.REQUIRE_VOICE_CONFIRMATION:
                screen_approval = screen.decision
            elif screen.decision is PermissionDecision.REQUIRE_UI_CONFIRMATION:
                stopped = self._computer_actions.stopped_since_turn_start()
                if stopped:
                    self.paint_draw_executor.discard_pending(prepared, reason=stopped)
                    report["voiceComputerCancelled"] = True
                    return COMPUTER_ACTION_CANCELLED_RESPONSE
                ticket = self._issue_paint_prompt(
                    ApprovalKind.PAINT_UNDO,
                    transcript,
                    PendingPaintUndoAction(
                        prepared=prepared,
                        gui_approval=gui_approval,
                        transcript=transcript,
                    ),
                )
                if ticket is None:
                    return PAINT_UNDO_RESPONSES["pending"]
                report["paintDrawScreenApprovalPending"] = True
                report["screenApprovalId"] = ticket.approval_id
                return ""
            else:
                report["voiceComputerNeedsOtherApproval"] = screen.decision.value
                return PAINT_UNDO_RESPONSES["other_approval"]

        return self._execute_paint_undo(
            prepared,
            gui_approval=gui_approval,
            screen_approval=screen_approval,
            report=report,
        )

    def _begin_text_paint_undo_action(
        self,
        text: str,
        request_id: str,
        started_at: float,
    ) -> bool:
        """Text route: runs only when policy needs no voice/UI approval."""
        report: dict[str, Any] = {}
        prepared, message = self._prepare_paint_undo(text, report)
        if prepared is None:
            if not message:
                return False
            self._finish_text_file_action(
                request_id=request_id,
                response=message,
                started_at=started_at,
            )
            return True

        gui = prepared.gui_verdict
        screen = prepared.screen_verdict
        if gui.refused or screen.refused:
            response = PAINT_UNDO_RESPONSES["refused"]
        elif gui.allowed and screen.allowed:
            response = self._execute_paint_undo(
                prepared,
                gui_approval=None,
                screen_approval=None,
                report=report,
            )
        elif (
            gui.needs_approval
            and gui.decision is PermissionDecision.REQUIRE_VOICE_CONFIRMATION
        ):
            response = PAINT_UNDO_RESPONSES["voice_needed"]
        else:
            response = PAINT_UNDO_RESPONSES["other_approval"]
        self._finish_text_file_action(
            request_id=request_id,
            response=response,
            started_at=started_at,
        )
        return True

    def _begin_text_paint_draw_action(
        self,
        text: str,
        request_id: str,
        started_at: float,
    ) -> bool:
        """Recognize Paint drawing before the narrower Windows verb parser."""
        try:
            action = parse_paint_draw_action(text)
        except PaintDrawParseError:
            return False

        self._ensure_computer_runtime()
        if self.paint_draw_executor is None:
            raise RuntimeError("Qronos Paint draw executor did not prepare correctly.")

        prepared = self.paint_draw_executor.prepare(action)
        gui = prepared.gui_verdict
        screen = prepared.screen_verdict

        if gui.refused or screen.refused:
            self._finish_text_file_action(
                request_id=request_id,
                response="این عملیات طبق تنظیمات مجوز فعلی اجازه اجرا ندارد.",
                started_at=started_at,
            )
            return True

        if gui.allowed and screen.allowed:
            with self._computer_actions.begin("paint_draw") as cancel_token:
                execution = self.paint_draw_executor.execute_prepared(
                    prepared,
                    reevaluate=True,
                    cancel_token=cancel_token,
                )
            if execution.result.outcome is ActionOutcome.CANCELLED:
                self._finish_text_file_action(
                    request_id=request_id,
                    response=_paint_draw_cancelled_response(execution),
                    started_at=started_at,
                )
            elif execution.result.outcome is ActionOutcome.SUCCEEDED:
                self._finish_text_file_action(
                    request_id=request_id,
                    response=(
                        "انجام شد. نقاشی در Paint اجرا و نتیجهٔ واقعی آن "
                        "تأیید شد."
                    ),
                    started_at=started_at,
                )
            else:
                self._fail_text_file_action(
                    request_id=request_id,
                    error=paint_draw_failure_response_fa(execution),
                    started_at=started_at,
                )
            return True

        if (
            gui.needs_approval
            and gui.decision is PermissionDecision.REQUIRE_VOICE_CONFIRMATION
        ):
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    "کشیدن در Paint طبق مجوز فعلی به تأیید صوتی نیاز دارد. "
                    "همین فرمان را با Voice بگو؛ برای مشاهدهٔ محدود پنجرهٔ Paint "
                    "و تأیید نتیجه نیز تأیید روی صفحه خواسته می‌شود."
                ),
                started_at=started_at,
            )
            return True

        self._finish_text_file_action(
            request_id=request_id,
            response=(
                "این عملیات به تأییدی نیاز دارد که مسیر Text فعلی آن را "
                "خودکار انجام نمی‌دهد."
            ),
            started_at=started_at,
        )
        return True

    def _prepare_voice_paint_draw_action(
        self,
        transcript: str,
        report: dict[str, Any],
    ) -> str | None:
        """
        Prepare a natural Paint drawing command.

        ``None`` means the transcript was not a Paint draw request and the
        normal Computer parser should continue. An empty string means the
        request is waiting for the existing screen-approval UI and the current
        voice turn must return before TTS/model work starts.
        """
        try:
            action = parse_paint_draw_action(transcript)
        except PaintDrawParseError:
            return None

        self._ensure_computer_runtime()
        if self.paint_draw_executor is None:
            raise RuntimeError("Qronos Paint draw executor did not prepare correctly.")

        prepared = self.paint_draw_executor.prepare(action)
        gui = prepared.gui_verdict
        screen = prepared.screen_verdict

        report["computerOperation"] = "draw_in_paint"
        report["computerAppId"] = "paint"
        report["paintDrawPrepared"] = True

        if gui.refused or screen.refused:
            report["voiceComputerRefused"] = True
            report["voiceComputerDeferred"] = False
            return "این عملیات طبق تنظیمات مجوز فعلی اجازه اجرا ندارد."

        gui_approval: PermissionDecision | None = None
        if gui.needs_approval:
            if gui.decision is PermissionDecision.REQUIRE_VOICE_CONFIRMATION:
                gui_approval = gui.decision
            else:
                report["voiceComputerNeedsOtherApproval"] = gui.decision.value
                report["voiceComputerDeferred"] = False
                return "کنترل Paint به نوع دیگری از تأیید نیاز دارد و اجرا نشد."

        screen_approval: PermissionDecision | None = None
        if screen.needs_approval:
            if screen.decision is PermissionDecision.REQUIRE_VOICE_CONFIRMATION:
                screen_approval = screen.decision
            elif screen.decision is PermissionDecision.REQUIRE_UI_CONFIRMATION:
                stopped = self._computer_actions.stopped_since_turn_start()
                if stopped:
                    # Stop/Pause/shutdown arrived while this turn was still
                    # routing; it must not open a prompt that could later
                    # approve the stopped request.
                    self.paint_draw_executor.discard_pending(prepared, reason=stopped)
                    report["voiceComputerCancelled"] = True
                    report["voiceComputerDeferred"] = False
                    return COMPUTER_ACTION_CANCELLED_RESPONSE
                ticket = self._issue_paint_prompt(
                    ApprovalKind.PAINT_DRAW,
                    transcript,
                    PendingPaintDrawAction(
                        prepared=prepared,
                        gui_approval=gui_approval,
                        transcript=transcript,
                    ),
                )
                if ticket is None:
                    report["voiceComputerDeferred"] = False
                    return "یک نقاشی دیگر هنوز منتظر تأیید صفحه است."
                report["paintDrawScreenApprovalPending"] = True
                report["screenApprovalId"] = ticket.approval_id
                report["voiceComputerDeferred"] = False
                return ""
            else:
                report["voiceComputerNeedsOtherApproval"] = screen.decision.value
                report["voiceComputerDeferred"] = False
                return "مشاهدهٔ تأییدی Paint به نوع دیگری از تأیید نیاز دارد."

        with self._computer_actions.begin("paint_draw") as cancel_token:
            execution = self.paint_draw_executor.execute_prepared(
                prepared,
                gui_approval=gui_approval,
                screen_approval=screen_approval,
                reevaluate=True,
                cancel_token=cancel_token,
            )
        report["computerActionId"] = execution.request.action_id
        report["paintDrawUndoAvailable"] = execution.undo_available
        if execution.result.outcome is ActionOutcome.CANCELLED:
            report["voiceComputerCancelled"] = True
            report["voiceComputerDeferred"] = False
            report["paintDrawCanvasEdits"] = (
                execution.side_effects.canvas_marks_committed
            )
            return _paint_draw_cancelled_response(execution)
        if execution.result.outcome is ActionOutcome.SUCCEEDED:
            report["voiceComputerExecuted"] = True
            report["voiceComputerDeferred"] = False
            if execution.run is not None:
                report["paintDrawStrokes"] = execution.run.stroke_count
                report["paintDrawFidelityF1"] = round(
                    execution.run.verification.f1_score,
                    4,
                )
            return "انجام شد. نقاشی در Paint اجرا و نتیجهٔ واقعی آن تأیید شد."

        if execution.result.outcome is ActionOutcome.AWAITING_APPROVAL:
            report["voiceComputerNeedsOtherApproval"] = True
        else:
            report["voiceComputerFailed"] = True
            report["voiceComputerFailure"] = execution.result.detail
        report["voiceComputerDeferred"] = False
        if execution.result.outcome is ActionOutcome.FAILED:
            return paint_draw_failure_response_fa(execution)
        return "نقاشی در Paint کامل یا تأیید نشد؛ هیچ retry خودکاری انجام نشد."

    def _begin_text_calculator_semantic_action(
        self,
        text: str,
        request_id: str,
        started_at: float,
    ) -> bool:
        if not _looks_like_calculator_semantic_action(
            text,
            self._last_computer_app_id,
        ):
            return False

        candidate = (
            _calculator_action_candidate(
                text,
                self._last_computer_app_id,
            )
        )

        try:
            from core.calculator_semantic import (
                CalculatorSemanticExecutor,
                CalculatorSemanticCancelled,
                CalculatorSemanticParseError,
                CalculatorSemanticPermissionRequired,
                parse_calculator_semantic_command,
            )
        except Exception:
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    "کنترل ماشین حساب در این نسخهٔ Qronos در دسترس نیست."
                ),
                started_at=started_at,
            )
            return True

        try:
            plan = (
                parse_calculator_semantic_command(
                    candidate
                )
            )

        except CalculatorSemanticParseError:
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    "دستور ماشین حساب را متوجه نشدم؛ دو عدد و یک عمل مثل جمع، منها، ضرب یا تقسیم را بگو."
                ),
                started_at=started_at,
            )
            return True

        self._ensure_computer_runtime()

        if self.computer_executor is None:
            raise RuntimeError(
                "Qronos computer executor did not prepare correctly."
            )

        executor = CalculatorSemanticExecutor(
            self.computer_executor
        )

        try:
            with self._computer_actions.begin("calculator") as cancel_token:
                run = executor.execute(
                    plan,
                    voice_confirmed=False,
                    cancel_token=cancel_token,
                )

        except CalculatorSemanticCancelled as cancelled:
            self._finish_text_file_action(
                request_id=request_id,
                response=_calculator_cancelled_response(cancelled),
                started_at=started_at,
            )
            return True

        except CalculatorSemanticPermissionRequired:
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    "این محاسبه در ماشین حساب طبق مجوز فعلی به تأیید صوتی نیاز دارد؛ همین فرمان را با Voice بگو."
                ),
                started_at=started_at,
            )
            return True

        except Exception:
            self._fail_text_file_action(
                request_id=request_id,
                error=(
                    "محاسبه در ماشین حساب انجام یا تأیید نشد."
                ),
                started_at=started_at,
            )
            return True

        self._last_computer_app_id = (
            "calculator"
        )

        self._finish_text_file_action(
            request_id=request_id,
            response=(
                "انجام شد. نتیجه "
                f"{run.result_text} است."
            ),
            started_at=started_at,
        )

        return True


    def _execute_voice_calculator_semantic_action(
        self,
        transcript: str,
        report: dict[str, Any],
    ) -> str | None:
        if not _looks_like_calculator_semantic_action(
            transcript,
            self._last_computer_app_id,
        ):
            return None

        candidate = (
            _calculator_action_candidate(
                transcript,
                self._last_computer_app_id,
            )
        )

        try:
            from core.calculator_semantic import (
                CalculatorSemanticExecutor,
                CalculatorSemanticCancelled,
                CalculatorSemanticParseError,
                CalculatorSemanticPermissionRequired,
                parse_calculator_semantic_command,
            )
        except Exception:
            report[
                "voiceComputerDeferred"
            ] = False

            return (
                "کنترل ماشین حساب در این نسخهٔ Qronos در دسترس نیست."
            )

        try:
            plan = (
                parse_calculator_semantic_command(
                    candidate
                )
            )

        except CalculatorSemanticParseError:
            report[
                "voiceComputerDeferred"
            ] = False

            return (
                "دستور ماشین حساب را متوجه نشدم؛ دو عدد و یک عمل مثل جمع، منها، ضرب یا تقسیم را بگو."
            )

        self._ensure_computer_runtime()

        if self.computer_executor is None:
            raise RuntimeError(
                "Qronos computer executor did not prepare correctly."
            )

        executor = CalculatorSemanticExecutor(
            self.computer_executor
        )

        try:
            with self._computer_actions.begin("calculator") as cancel_token:
                run = executor.execute(
                    plan,
                    voice_confirmed=True,
                    cancel_token=cancel_token,
                )

        except CalculatorSemanticCancelled as cancelled:
            report["voiceComputerCancelled"] = True
            report["voiceComputerDeferred"] = False
            report["calculatorStepsInvoked"] = cancelled.steps_invoked
            return _calculator_cancelled_response(cancelled)

        except CalculatorSemanticPermissionRequired:
            report[
                "voiceComputerNeedsOtherApproval"
            ] = True

            report[
                "voiceComputerDeferred"
            ] = False

            return (
                "این محاسبه به نوع دیگری از تأیید نیاز دارد و اجرا نشد."
            )

        except Exception:
            report[
                "voiceComputerFailed"
            ] = True

            report[
                "voiceComputerFailure"
            ] = (
                "Calculator semantic execution failed."
            )

            report[
                "voiceComputerDeferred"
            ] = False

            return (
                "محاسبه در ماشین حساب انجام یا تأیید نشد."
            )

        self._last_computer_app_id = (
            "calculator"
        )

        report[
            "voiceComputerExecuted"
        ] = True

        report[
            "voiceComputerDeferred"
        ] = False

        report[
            "computerOperation"
        ] = "calculator_uia_sequence"

        report[
            "computerAppId"
        ] = "calculator"

        report[
            "calculatorExpression"
        ] = run.expression

        report[
            "calculatorResult"
        ] = run.result_text

        report[
            "calculatorDisplay"
        ] = run.display_name

        return (
            "انجام شد. نتیجه "
            f"{run.result_text} است."
        )


    def _begin_text_computer_action(
        self,
        text: str,
        request_id: str,
        started_at: float,
    ) -> bool:
        """
        Try the narrow Windows-control vocabulary before the File-Control path.

        Returns True when this request was a recognized computer action, even
        if policy refused it. False means the caller should try another
        COMPUTER sub-parser such as exact-path File Control.
        """
        if self._begin_text_paint_undo_action(
            text,
            request_id,
            started_at,
        ):
            return True

        if self._begin_text_paint_draw_action(
            text,
            request_id,
            started_at,
        ):
            return True

        self._ensure_computer_runtime()

        if self.computer_executor is None:
            raise RuntimeError(
                "Qronos computer executor did not prepare correctly."
            )

        try:
            action = parse_computer_action(
                text,
                app_resolver=self.computer_executor.resolve_app_id,
            )
        except ComputerActionParseError:
            return False

        if self.computer_executor is None:
            raise RuntimeError(
                "Qronos computer executor did not prepare correctly."
            )

        prepared = self.computer_executor.prepare(action)
        verdict = prepared.verdict

        if verdict.refused:
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    "این عملیات طبق تنظیمات مجوز فعلی اجازه اجرا ندارد."
                ),
                started_at=started_at,
            )
            return True

        if verdict.allowed:
            with self._computer_actions.begin("computer_action") as cancel_token:
                execution = self.computer_executor.execute_prepared(
                    prepared,
                    reevaluate=False,
                    cancel_token=cancel_token,
                )
            if execution.result.outcome is ActionOutcome.CANCELLED:
                self._finish_text_file_action(
                    request_id=request_id,
                    response=COMPUTER_ACTION_CANCELLED_RESPONSE,
                    started_at=started_at,
                )
                return True
            if execution.result.outcome is not ActionOutcome.SUCCEEDED:
                self._fail_text_file_action(
                    request_id=request_id,
                    error=(
                        execution.result.detail
                        or "Computer action failed."
                    ),
                    started_at=started_at,
                )
                return True

            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    f"انجام شد. {self._computer_operation_label(action.operation)} "
                    "با موفقیت اجرا شد."
                ),
                started_at=started_at,
            )
            return True

        if (
            verdict.decision
            is PermissionDecision.REQUIRE_VOICE_CONFIRMATION
        ):
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    "این عملیات طبق سیاست فعلی به تأیید صوتی نیاز دارد. "
                    "از Voice همان فرمان را بگو."
                ),
                started_at=started_at,
            )
            self._notify(
                "computer_action_voice_confirmation_required",
                "ready",
                json.dumps(
                    {
                        "requestId": request_id,
                        "actionId": prepared.request.action_id,
                        "operation": action.operation.value,
                        "appId": action.app_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            return True

        self._finish_text_file_action(
            request_id=request_id,
            response=(
                "این عملیات به نوع دیگری از تأیید نیاز دارد و "
                "Windows Control v1 آن را خودکار اجرا نمی‌کند."
            ),
            started_at=started_at,
        )
        return True

    def _execute_voice_computer_action(
        self,
        transcript: str,
        report: dict[str, Any],
    ) -> str:
        """
        Execute one explicit, allowlisted Windows action from the active voice
        command. The spoken command itself satisfies VOICE_CONFIRMATION only;
        it never substitutes for UI_CONFIRMATION or TYPED_SECRET.
        """
        if is_computer_capability_query(transcript):
            report["voiceComputerCapabilityQuery"] = True
            report["voiceComputerDeferred"] = False
            return _computer_capability_response()

        from qronos_media.intents import parse_media_intent

        media_intent = parse_media_intent(transcript)
        if media_intent is not None:
            # The same typed media product operation as the desktop (no shell, model or database call).
            report["voiceMediaRequest"] = type(media_intent).__name__
            report["voiceComputerDeferred"] = False
            return self._run_media_intent(media_intent)

        paint_undo_response = self._prepare_voice_paint_undo_action(
            transcript,
            report,
        )
        if paint_undo_response is not None:
            return paint_undo_response

        paint_draw_response = self._prepare_voice_paint_draw_action(
            transcript,
            report,
        )
        if paint_draw_response is not None:
            return paint_draw_response

        self._ensure_computer_runtime()

        if self.computer_executor is None:
            raise RuntimeError(
                "Qronos computer executor did not prepare correctly."
            )

        calculator_response = (
            self._execute_voice_calculator_semantic_action(
                transcript,
                report,
            )
        )

        if calculator_response is not None:
            return calculator_response

        try:
            action = parse_computer_action(
                transcript,
                app_resolver=self.computer_executor.resolve_app_id,
            )
        except ComputerActionParseError:
            named_file_response = self._execute_voice_named_file_action(
                transcript,
                report,
            )
            if named_file_response is not None:
                return named_file_response

            named_location_response = self._execute_voice_named_location_action(
                transcript,
                report,
            )
            if named_location_response is not None:
                return named_location_response

            report["voiceComputerDeferred"] = True
            report["voiceComputerReason"] = "unsupported_windows_control_v2_action"
            return VOICE_COMPUTER_DEFERRED_RESPONSE

        if self.computer_executor is None:
            raise RuntimeError(
                "Qronos computer executor did not prepare correctly."
            )

        prepared = self.computer_executor.prepare(action)
        verdict = prepared.verdict

        if verdict.refused:
            report["voiceComputerRefused"] = True
            report["computerOperation"] = action.operation.value
            report["computerAppId"] = action.app_id
            return (
                "این عملیات طبق تنظیمات مجوز فعلی اجازه اجرا ندارد."
            )

        approval = None
        if verdict.needs_approval:
            if (
                verdict.decision
                is PermissionDecision.REQUIRE_VOICE_CONFIRMATION
            ):
                approval = verdict.decision
            else:
                report["voiceComputerNeedsOtherApproval"] = (
                    verdict.decision.value
                )
                report["computerOperation"] = action.operation.value
                report["computerAppId"] = action.app_id
                return (
                    "این عملیات به نوع دیگری از تأیید نیاز دارد و "
                    "با فرمان صوتی فعلی اجرا نشد."
                )

        with self._computer_actions.begin("computer_action") as cancel_token:
            execution = self.computer_executor.execute_prepared(
                prepared,
                approval=approval,
                reevaluate=False,
                cancel_token=cancel_token,
            )

        report["computerOperation"] = action.operation.value
        report["computerAppId"] = action.app_id
        report["computerActionId"] = execution.request.action_id

        if execution.result.outcome is ActionOutcome.CANCELLED:
            report["voiceComputerCancelled"] = True
            report["voiceComputerDeferred"] = False
            return COMPUTER_ACTION_CANCELLED_RESPONSE

        if execution.result.outcome is not ActionOutcome.SUCCEEDED:
            report["voiceComputerFailed"] = True
            report["voiceComputerFailure"] = (
                execution.result.detail
            )
            return (
                "باز کردن برنامه انجام نشد. "
                "جزئیات در گزارش اجرای کرونوس ثبت شد."
            )

        report["voiceComputerExecuted"] = True
        report["voiceComputerDeferred"] = False
        app_name = self.computer_executor.display_name_for_app_id(
            action.app_id,
        )
        return _computer_action_success_response(
            action.operation,
            app_name,
        )

    def _begin_text_file_action(
        self,
        text: str,
        request_id: str,
        started_at: float,
    ) -> None:
        stopped = self._computer_actions.stopped_since_turn_start()
        if stopped:
            # Stop/Pause/shutdown arrived while this turn was still routing:
            # never prepare, auto-approve or prompt for a file write.
            self._finish_text_file_action(
                request_id=request_id,
                response="لغو شد؛ هیچ تغییری روی فایل‌ها انجام نشد.",
                started_at=started_at,
            )
            return

        if self.pending_file_actions:
            raise RuntimeError(
                "Another file action is already awaiting approval."
            )

        try:
            action = parse_file_action(text)
        except FileActionParseError as exc:
            raise RuntimeError(
                "برای کنترل فایل در Gate 1B مسیر دقیق را داخل کوتیشن بنویس. "
                "عملیات پشتیبانی‌شده: Copy، Move، Rename و Create Folder. "
                f"جزئیات: {exc}"
            ) from exc

        self._ensure_file_runtime()

        if self.file_executor is None:
            raise RuntimeError(
                "Qronos file executor did not prepare correctly."
            )

        prepared = self.file_executor.prepare(action)
        verdict = prepared.verdict

        if self._computer_actions.stopped_since_turn_start():
            # Stop landed while the request was being parsed/prepared: no
            # auto-approved write, no lease use, no prompt.
            self._finish_text_file_action(
                request_id=request_id,
                response="لغو شد؛ هیچ تغییری روی فایل‌ها انجام نشد.",
                started_at=started_at,
            )
            return

        if verdict.refused:
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    "این عملیات طبق تنظیمات مجوز فعلی اجازه اجرا ندارد."
                ),
                started_at=started_at,
            )
            self._notify(
                "file_action_refused",
                "ready",
                json.dumps(
                    {
                        "requestId": request_id,
                        "actionId": prepared.request.action_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            return

        if verdict.allowed:
            execution = self.file_executor.execute_prepared(
                prepared,
                reevaluate=False,
            )
            if execution.result.outcome is not ActionOutcome.SUCCEEDED:
                self._fail_text_file_action(
                    request_id=request_id,
                    error=(
                        execution.result.detail
                        or "File action failed."
                    ),
                    started_at=started_at,
                )
                return

            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    f"انجام شد. {self._file_operation_label(action.operation)} "
                    "با موفقیت اجرا شد."
                ),
                started_at=started_at,
            )
            self._notify(
                "file_action_completed",
                "ready",
                json.dumps(
                    {
                        "requestId": request_id,
                        "actionId": prepared.request.action_id,
                        "operation": action.operation.value,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            return

        if (
            verdict.decision
            is not PermissionDecision.REQUIRE_UI_CONFIRMATION
        ):
            self._finish_text_file_action(
                request_id=request_id,
                response=(
                    "این عملیات به نوع دیگری از تأیید نیاز دارد و "
                    "Gate 1B متنی آن را خودکار اجرا نمی‌کند."
                ),
                started_at=started_at,
            )
            return

        if self._file_write_approval_lease_active():
            ticket = self._approvals.issue(
                ApprovalKind.FILE_ACTION,
                request_id,
                payload_fn=lambda approval_id: PendingFileTextAction(
                    token=approval_id,
                    prepared=prepared,
                    request_id=request_id,
                    started_at=started_at,
                ),
            )
            token = ticket.approval_id
            self._notify(
                "file_action_approval_lease_used",
                "ready",
                json.dumps(
                    {
                        "requestId": request_id,
                        "actionId": prepared.request.action_id,
                        "remainingSeconds": int(
                            self._file_write_approval_lease_remaining()
                        ),
                        "scope": "reversible_file_writes",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            self.resolve_file_action_decision(
                token,
                approved=True,
                grant_lease=False,
            )
            return

        ticket = self._approvals.issue(
            ApprovalKind.FILE_ACTION,
            request_id,
            payload_fn=lambda approval_id: PendingFileTextAction(
                token=approval_id,
                prepared=prepared,
                request_id=request_id,
                started_at=started_at,
            ),
        )
        token = ticket.approval_id

        action_payload = {
            "actionToken": token,
            "actionId": prepared.request.action_id,
            "requestId": request_id,
            "permission": verdict.decision.value,
            "operation": action.operation.value,
            "source": action.source,
            "destination": action.destination,
            "newName": action.new_name,
        }

        self._notify(
            "file_action_approval_required",
            "approval_required",
            json.dumps(
                action_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def resolve_file_action_decision(
        self,
        action_token: str,
        *,
        approved: bool,
        grant_lease: bool = True,
    ) -> None:
        token = (action_token or "").strip()
        if not token:
            raise ValueError(
                "File action decision needs an action token."
            )

        if self.runtime_paused:
            # Pause revoked every pending file action already; never write
            # while paused, whatever the answer names.
            self._notify_file_stale(token, CANCEL_REASON_PAUSE)
            return

        try:
            ticket = self._approvals.take(
                token,
                kinds=(ApprovalKind.FILE_ACTION,),
            )
        except StaleApproval as exc:
            self._notify_file_stale(token, exc.reason)
            return
        pending: PendingFileTextAction = ticket.payload

        if self.file_executor is None:
            self._fail_text_file_action(
                request_id=pending.request_id,
                error="Qronos file executor is not available.",
                started_at=pending.started_at,
            )
            return

        if not approved:
            self.file_executor.decline_prepared(
                pending.prepared
            )
            self._notify(
                "file_action_declined",
                "ready",
                json.dumps(
                    {
                        "actionToken": token,
                        "requestId": pending.request_id,
                        "actionId": pending.prepared.request.action_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            self._finish_text_file_action(
                request_id=pending.request_id,
                response="لغو شد؛ هیچ تغییری روی فایل‌ها انجام نشد.",
                started_at=pending.started_at,
            )
            return

        execution = self.file_executor.execute_prepared(
            pending.prepared,
            approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
            reevaluate=True,
        )

        if (
            grant_lease
            and execution.result.outcome
            not in {
                ActionOutcome.REFUSED,
                ActionOutcome.AWAITING_APPROVAL,
            }
        ):
            self._grant_file_write_approval_lease()

        if execution.result.outcome is ActionOutcome.SUCCEEDED:
            self._notify(
                "file_action_completed",
                "ready",
                json.dumps(
                    {
                        "actionToken": token,
                        "requestId": pending.request_id,
                        "actionId": execution.request.action_id,
                        "operation": pending.prepared.action.operation.value,
                        "undoActionId": (
                            execution.undo_entry.action_id
                            if execution.undo_entry is not None
                            else ""
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            self._finish_text_file_action(
                request_id=pending.request_id,
                response=(
                    f"انجام شد. "
                    f"{self._file_operation_label(pending.prepared.action.operation)} "
                    "با موفقیت اجرا شد."
                ),
                started_at=pending.started_at,
            )
            return

        if execution.result.outcome is ActionOutcome.REFUSED:
            self._notify(
                "file_action_refused",
                "ready",
                json.dumps(
                    {
                        "actionToken": token,
                        "requestId": pending.request_id,
                        "actionId": execution.request.action_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            self._finish_text_file_action(
                request_id=pending.request_id,
                response=(
                    "مجوز این عملیات تغییر کرده یا اکنون اجازه اجرا نمی‌دهد؛ "
                    "هیچ تغییری انجام نشد."
                ),
                started_at=pending.started_at,
            )
            return

        if execution.result.outcome is ActionOutcome.AWAITING_APPROVAL:
            self._notify(
                "file_action_refused",
                "ready",
                json.dumps(
                    {
                        "actionToken": token,
                        "requestId": pending.request_id,
                        "actionId": execution.request.action_id,
                        "reason": "approval_mechanism_changed",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            self._finish_text_file_action(
                request_id=pending.request_id,
                response=(
                    "نوع مجوز این عملیات در زمان تأیید تغییر کرده است؛ "
                    "برای امنیت، عملیات اجرا نشد."
                ),
                started_at=pending.started_at,
            )
            return

        self._notify(
            "file_action_failed",
            "error",
            json.dumps(
                {
                    "actionToken": token,
                    "requestId": pending.request_id,
                    "actionId": execution.request.action_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        self._fail_text_file_action(
            request_id=pending.request_id,
            error=(
                execution.result.detail
                or "File action failed."
            ),
            started_at=pending.started_at,
        )

    def _defer_text_turn_to_queue(
        self,
        *,
        text: str,
        request_id: str,
        context: tuple,
        route: TaskType,
        reason: str,
        started_at: float,
    ) -> None:
        """
        Turn a resource refusal into a queued request rather than a "no".

        The user's message is already in the conversation, so it is not
        recorded twice; the context is the one this turn captured before
        recording it. If the queue cannot take it, the user gets the original
        refusal plus why, exactly as before this path existed.
        """
        try:
            work = self.submit_queued_conversation(
                text=text,
                request_id=request_id,
                context=tuple(context or ()),
                user_message_recorded=True,
                route=route,
                reason="resource_pressure",
            )
        except Exception as exc:
            self._notify(
                "text_error",
                "error",
                _text_event_message(
                    request_id,
                    error=f"{reason} The queue could not hold it: {exc}",
                    route=route.value,
                    elapsedSeconds=round(
                        time.perf_counter() - started_at,
                        3,
                    ),
                ),
            )
            return

        self._notify(
            "text_deferred",
            "queued",
            _text_event_message(
                request_id,
                taskId=work.task_id,
                route=route.value,
                reason="resource_pressure",
                detail=reason,
            ),
        )

    def run_text_turn(self, text: str, request_id: str) -> None:
        lifecycle = self._foreground_lifecycle()
        turn = None
        try:
            with lifecycle.begin("text", state=self._foreground_state, on_cancel=
                    lambda turn, exc: self._report_foreground_cancelled(
                        "text", exc.reason, request_id=request_id, turn=turn)) as turn:
                self._run_text_turn(text, request_id)
        except ForegroundBusy as exc:
            self._notify("text_error", "busy", _text_event_message(request_id, error=str(exc)))
        except LocalInferenceCancelled as exc:
            self._report_foreground_cancelled("text", exc.reason, request_id=request_id, turn=turn)

    def _run_text_turn(
        self,
        text: str,
        request_id: str,
    ) -> None:
        """
        Execute one typed request through the production TaskRouter/Orchestrator.

        V1 intentionally supports only FAST and HEAVY. Vision, browser, and
        computer-action text turns need their own permission/approval contracts
        and are not silently executed through the chat box.
        """
        cleaned_text = (text or "").strip()
        cleaned_request_id = (request_id or "").strip()

        if self.runtime_paused:
            self._notify(
                "text_error",
                "paused",
                _text_event_message(
                    cleaned_request_id,
                    error="Qronos runtime is temporarily paused.",
                ),
            )
            return

        if self.is_busy:
            self._notify(
                "text_error",
                "busy",
                _text_event_message(
                    cleaned_request_id,
                    error=(
                        "Qronos is already processing another request. "
                        "Try again after the current turn finishes."
                    ),
                ),
            )
            return

        self._set_busy(True)
        self._computer_actions.mark_turn_start()
        started = time.perf_counter()
        route_name = ""

        try:
            if not cleaned_text:
                raise ValueError(
                    "Text message must not be empty."
                )

            if (
                len(cleaned_text)
                > MAX_TEXT_MESSAGE_LENGTH
            ):
                raise ValueError(
                    "Text message exceeds the "
                    f"{MAX_TEXT_MESSAGE_LENGTH} character limit."
                )

            if not cleaned_request_id:
                raise ValueError(
                    "Text request id must not be empty."
                )

            if (
                len(cleaned_request_id)
                > MAX_TEXT_REQUEST_ID_LENGTH
            ):
                raise ValueError(
                    "Text request id exceeds the "
                    f"{MAX_TEXT_REQUEST_ID_LENGTH} character limit."
                )

            self._ensure_text_runtime()

            if (
                self.task_router is None
                or self.orchestrator is None
            ):
                raise RuntimeError(
                    "Qronos text runtime did not prepare correctly."
                )

            conversation_context = (
                self._conversation_brain_context()
            )

            self._record_conversation_message(
                role="user",
                text=cleaned_text,
                source="text",
                request_id=cleaned_request_id,
            )

            self._notify(
                "text_request_started",
                "processing",
                _text_event_message(
                    cleaned_request_id,
                    text=cleaned_text,
                ),
            )

            if self._begin_text_calculator_semantic_action(
                cleaned_text,
                cleaned_request_id,
                started,
            ):
                return

            if self._begin_text_media_request(
                cleaned_text,
                cleaned_request_id,
                started,
            ):
                return

            if self._begin_text_app_workflow(
                cleaned_text,
                cleaned_request_id,
                started,
            ):
                return

            route = self.task_router.route(
                cleaned_text
            )
            routed_task_type = route.task_type
            text_routing_shadow = (
                self._routing_shadow(
                    cleaned_text
                )
            )
            (
                selected_task_type,
                text_compute_promotion,
            ) = self._select_production_task_type(
                cleaned_text,
                routed_task_type,
                text_routing_shadow,
            )
            route_name = (
                selected_task_type.value
            )

            self._notify(
                "text_routed",
                "processing",
                _text_event_message(
                    cleaned_request_id,
                    route=route_name,
                ),
            )

            if selected_task_type is TaskType.COMPUTER:
                if is_computer_capability_query(cleaned_text):
                    self._finish_text_file_action(
                        request_id=cleaned_request_id,
                        response=_computer_capability_response(),
                        started_at=started,
                    )
                    return

                if self._begin_text_computer_action(
                    cleaned_text,
                    cleaned_request_id,
                    started,
                ):
                    return

                if self._begin_text_named_file_action(
                    cleaned_text,
                    cleaned_request_id,
                    started,
                ):
                    return

                if self._begin_text_named_location_action(
                    cleaned_text,
                    cleaned_request_id,
                    started,
                ):
                    return

                self._begin_text_file_action(
                    cleaned_text,
                    cleaned_request_id,
                    started,
                )
                return

            if selected_task_type not in (
                TaskType.FAST,
                TaskType.HEAVY,
            ):
                raise RuntimeError(
                    "Text Chat currently supports Fast and Heavy, plus the "
                    "permission-gated exact-path File Control path. Vision "
                    "and browser actions remain behind dedicated paths."
                )

            arithmetic_answer = (
                solve_simple_arithmetic(
                    cleaned_text
                )
                if selected_task_type
                is TaskType.FAST
                else None
            )

            if arithmetic_answer is not None:
                response = (
                    arithmetic_answer.spoken_text
                )
            else:
                plan = TaskPlan(
                    goal=cleaned_text
                )
                plan.add_step(
                    task_type=selected_task_type,
                    description=cleaned_text,
                )

                self._require_runtime_active("text execution")

                results = (
                    self.orchestrator.execute_plan(
                        plan,
                        conversation_messages=(
                            conversation_context
                        ),
                    )
                )

                self._require_runtime_active("text response")

                self._require_runtime_active("voice response")

                if not results:
                    raise RuntimeError(
                        "Qronos did not return an execution result."
                    )

                result = results[-1]

                if (
                    not result.success
                    and getattr(result, "resource_blocked", False)
                ):
                    # Resource policy refused the brain, not the request.
                    # Hand it to the queue, which answers it when the machine
                    # allows, instead of telling the user no.
                    with self._foreground_lifecycle().publication(terminal=True):
                        self._defer_text_turn_to_queue(
                            text=cleaned_text,
                            request_id=cleaned_request_id,
                            context=conversation_context,
                            route=selected_task_type,
                            reason=result.error or "",
                            started_at=started,
                        )
                    return

                if not result.success:
                    raise RuntimeError(
                        result.error
                        or "Qronos task failed."
                    )

                response = (
                    result.output.strip()
                )

            if not response:
                raise RuntimeError(
                    "Qronos returned an empty response."
                )

            elapsed = (
                time.perf_counter()
                - started
            )

            with self._foreground_lifecycle().publication(terminal=True):
                self._record_conversation_message(
                    role="assistant",
                    text=response,
                    source="text",
                    request_id=cleaned_request_id,
                )

                inference_checkpoint()
                self._notify(
                    "text_response",
                    "ready",
                    _text_event_message(
                        cleaned_request_id,
                        text=response,
                        route=route_name,
                        elapsedSeconds=round(
                            elapsed,
                            3,
                        ),
                    ),
                )

        except LocalInferenceCancelled:
            raise
        except Exception as exc:
            inference_checkpoint()
            elapsed = (
                time.perf_counter()
                - started
            )

            with self._foreground_lifecycle().publication(terminal=True):
                self._notify(
                    "text_error",
                    "error",
                    _text_event_message(
                        cleaned_request_id,
                        error=str(exc),
                        route=route_name,
                        elapsedSeconds=round(
                            elapsed,
                            3,
                        ),
                    ),
                )

        finally:
            self._set_busy(False)

    def prepare_wake_gate(self) -> None:
        """Prepare only what is required to hear the Qronos wake word."""
        if self.audio_input is None:
            self.audio_input = (
                self._new_audio_input_from_settings()
            )

        if self.conversation_session is None:
            self.conversation_session = (
                ConversationSession()
            )

    def prepare_wake_capture(self) -> None:
        """
        Prepare command capture without making Wake readiness depend on it.

        This path is intentionally heavier than ``prepare_wake_gate`` because
        Whisper.cpp VAD can have a noticeable cold start. It is serialized so
        a background Wake warm-up, PTT, and an early spoken turn cannot create
        duplicate VAD/recorder instances.
        """
        self.prepare_wake_gate()

        if (
            self.vad_runtime is not None
            and self.command_recorder is not None
            and self.speech_runtime is not None
        ):
            self._wake_command_prepare_error = None
            self._wake_command_ready.set()
            return

        with self._wake_command_prepare_lock:
            if (
                self.vad_runtime is not None
                and self.command_recorder is not None
                and self.speech_runtime is not None
            ):
                self._wake_command_prepare_error = None
                self._wake_command_ready.set()
                return

            try:
                if self.vad_runtime is None:
                    vad_runtime = (
                        WhisperCppVADRuntime()
                    )

                    if not vad_runtime.health_check():
                        raise RuntimeError(
                            "Qronos VAD runtime is not ready."
                        )

                    vad_runtime.prepare()
                    self.vad_runtime = vad_runtime

                if self.command_recorder is None:
                    self.command_recorder = (
                        CommandRecorder(
                            audio_input=self.audio_input,
                            vad_runtime=self.vad_runtime,
                            config=CommandRecorderConfig(
                                silence_seconds=0.96,
                                start_timeout_seconds=(
                                    FOLLOWUP_START_TIMEOUT_SECONDS
                                ),
                            ),
                        )
                    )

                if self.speech_runtime is None:
                    self.speech_runtime = (
                        WhisperHybridRuntime()
                    )

            except Exception as exc:
                self._wake_command_prepare_error = str(exc)
                self._wake_command_ready.clear()
                raise

            self._wake_command_prepare_error = None
            self._wake_command_ready.set()

    def _warm_wake_command_capture_async(self) -> None:
        """Warm VAD/recorder/STT after the wake gate is already listening."""
        if self._wake_command_ready.is_set():
            return

        thread = self._wake_command_prepare_thread
        if thread is not None and thread.is_alive():
            return

        def worker() -> None:
            last_error: Exception | None = None

            for delay in WAKE_COMMAND_PREP_RETRY_DELAYS_SECONDS:
                if self._wake_stop.is_set():
                    return

                if delay > 0 and self._wake_stop.wait(delay):
                    return

                try:
                    self.prepare_wake_capture()
                    self._notify(
                        "voice_capture_ready",
                        "ready",
                        "Qronos command capture is warmed.",
                    )
                    return
                except Exception as exc:
                    last_error = exc

            if last_error is not None and not self._wake_stop.is_set():
                self._notify(
                    "runtime_warning",
                    "warning",
                    "Command capture warm-up failed while Wake remains active: "
                    + str(last_error),
                )

        worker_thread = threading.Thread(
            target=worker,
            name="qronos-wake-command-prewarm",
            daemon=True,
        )
        self._wake_command_prepare_thread = worker_thread
        worker_thread.start()

    def _buffer_wake_command_audio_until_capture_ready(
        self,
    ) -> tuple[bytes, ...]:
        """
        Keep microphone ownership and buffer post-wake speech while VAD warms.

        Normally the background warm-up has already completed by the time the
        user says Qronos. This fallback is what makes a *very early* wake safe:
        the microphone keeps being drained instead of losing the first command
        words while the heavier VAD initialization finishes.
        """
        if self._wake_command_ready.is_set():
            return ()

        if self.audio_input is None:
            raise RuntimeError(
                "Qronos microphone is unavailable while command capture warms."
            )

        frame_duration = (
            self.audio_input.config.frame_size
            / self.audio_input.config.sample_rate
        )
        max_frames = max(
            1,
            int(
                WAKE_COMMAND_PREP_BUFFER_LIMIT_SECONDS
                / frame_duration
            ),
        )

        buffered: list[bytes] = []
        deadline = (
            time.perf_counter()
            + WAKE_COMMAND_PREP_BUFFER_LIMIT_SECONDS
        )

        while not self._wake_command_ready.is_set():
            if self._wake_stop.is_set():
                raise RuntimeError(
                    "Wake listener stopped while command capture was warming."
                )

            if time.perf_counter() >= deadline:
                raise TimeoutError(
                    "Qronos command capture did not warm before the audio buffer limit."
                )

            thread = self._wake_command_prepare_thread
            if (
                self._wake_command_prepare_error
                and (thread is None or not thread.is_alive())
            ):
                raise RuntimeError(
                    "Qronos command capture warm-up failed: "
                    + self._wake_command_prepare_error
                )

            frame = self.audio_input.read_frame()
            buffered.append(bytes(frame))
            self._maybe_emit_input_meter_frame(frame)

            if len(buffered) >= max_frames:
                raise TimeoutError(
                    "Qronos command capture exceeded the buffered-audio safety limit."
                )

        return tuple(buffered)

    def prepare(self) -> None:
        """
        Prepare the full post-transcription processing/output runtime.

        This deliberately calls ``prepare_wake_capture`` first but never makes
        Wake readiness wait for TTS/Router/Orchestrator initialization.
        """
        self.prepare_wake_capture()

        full_ready = all(
            getattr(
                self,
                name,
            )
            is not None
            for name in (
                "voice_output",
                "task_router",
                "action_audit",
                "screen_capture",
                "orchestrator",
            )
        )

        if full_ready:
            return

        emit(
            "runtime_preparing",
            "preparing",
            "Preparing Qronos processing runtime.",
        )

        if self.voice_output is None:
            self._ensure_voice_output_instance()

        if self.task_router is None:
            self.task_router = TaskRouter()

        if self.action_audit is None:
            self.action_audit = (
                ActionAuditLog()
            )
            set_default_audit_sink(
                self.action_audit.record_verdict
            )

        if self.screen_capture is None:
            self.screen_capture = (
                ScreenCapture(
                    read_text=read_screen_text
                )
            )

        if self.orchestrator is None:
            orchestrator = Orchestrator()
            # Public web access requires the browser adapter. Legacy direct
            # HTTP research is intentionally unavailable in production.
            orchestrator.workers.register(
                build_vision_worker(
                    orchestrator.runtime
                )
            )
            self.orchestrator = (
                orchestrator
            )

        emit(
            "runtime_prepared",
            "ready",
            "Qronos processing runtime is ready.",
        )

    def _require_wake_capture_prepared(
        self,
    ) -> None:
        missing = [
            name
            for name in (
                "audio_input",
                "vad_runtime",
                "command_recorder",
                "speech_runtime",
                "conversation_session",
            )
            if getattr(
                self,
                name,
            )
            is None
        ]

        if missing:
            raise RuntimeError(
                "Qronos voice capture runtime is not prepared: "
                + ", ".join(missing)
                + " missing."
            )

    def _require_prepared(self) -> None:
        missing = [
            name
            for name in (
                "audio_input",
                "vad_runtime",
                "command_recorder",
                "speech_runtime",
                "voice_output",
                "task_router",
                "action_audit",
                "screen_capture",
                "orchestrator",
                "conversation_session",
            )
            if getattr(self, name) is None
        ]

        if missing:
            raise RuntimeError(
                "Qronos voice runtime is not prepared: "
                + ", ".join(missing)
                + " missing."
            )

    def _warm_speech_runtime_async(
        self,
    ) -> None:
        speech = self.speech_runtime

        if speech is None:
            return

        warm = getattr(
            speech,
            "warm_async",
            None,
        )

        if callable(warm):
            # A new warm request starts a new STT residency generation. The
            # next release must be allowed to shut that generation down.
            self._speech_runtime_released = False

            try:
                warm()

            except Exception:
                # Warm-up is an optimization only.
                #
                # The hybrid runtime keeps the validated CLI fallback,
                # so warm failure must never cancel a user's voice turn.
                pass

    def _release_speech_runtime(
        self,
    ) -> bool:
        """
        Release persistent STT resources exactly once per warm generation.

        _run_voice_turn() must yield Whisper before Brain execution on the
        8 GB GPU. push_to_talk() also owns a final safety cleanup so failures
        that happen before transcription cannot leak STT residency. Those two
        cleanup paths intentionally converge here and are idempotent.
        """
        if self._speech_runtime_released:
            return True

        speech = self.speech_runtime

        if speech is None:
            self._speech_runtime_released = True
            return True

        shutdown = getattr(
            speech,
            "shutdown",
            None,
        )

        if callable(shutdown):
            try:
                shutdown()

            except Exception:
                return False

        try:
            server_running = getattr(
                speech,
                "server_running",
                None,
            )

            if (
                server_running is not None
                and bool(server_running)
            ):
                return False

        except Exception:
            pass

        self._speech_runtime_released = True
        return True

    def _release_brains_for_voice_phase(
        self,
    ) -> None:
        """
        Yield configured Qronos Brain residency before another GPU voice phase.

        Older/fake orchestrators used by tests may not expose the public seam;
        in that case this is intentionally a no-op.
        """
        orchestrator = self.orchestrator

        if orchestrator is None:
            return

        release = getattr(
            orchestrator,
            "release_brains_for_voice_phase",
            None,
        )

        if callable(release):
            release()

    def _release_voice_output_runtime(
        self,
    ) -> bool:
        """
        Release TTS model residency after WAV generation.

        Desktop playback consumes generated files, not the TTS model itself,
        so keeping Chatterbox resident during playback only steals VRAM from
        the next STT phase.
        """
        voice = self.voice_output

        if voice is None:
            return True

        release = getattr(
            voice,
            "release",
            None,
        )

        if not callable(release):
            return True

        try:
            release()
            return not bool(
                getattr(
                    voice,
                    "is_loaded",
                    False,
                )
            )

        except Exception:
            return False

    @property
    def wake_listener_running(self) -> bool:
        thread = self._wake_thread
        return thread is not None and thread.is_alive()

    @property
    def wake_listener_ready(self) -> bool:
        return (
            self.wake_listener_running
            and self._wake_ready.is_set()
        )

    def _schedule_wake_start_retry(
        self,
        error: Exception,
    ) -> bool:
        """Retry a failed initial Wake startup without requiring a Settings write."""
        if (
            self._wake_stop.is_set()
            or self.runtime_paused
            or not self.wake_word_enabled
        ):
            return False

        with self._lock:
            if (
                self._wake_start_retry_count
                >= len(WAKE_STARTUP_RETRY_DELAYS_SECONDS)
            ):
                return False

            attempt = self._wake_start_retry_count + 1
            delay = WAKE_STARTUP_RETRY_DELAYS_SECONDS[
                self._wake_start_retry_count
            ]
            self._wake_start_retry_count = attempt

        self._notify(
            "wake_word_retrying",
            "warning",
            json.dumps(
                {
                    "attempt": attempt,
                    "delaySeconds": delay,
                    "reason": str(error),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

        failed_wake_thread = threading.current_thread()

        def retry() -> None:
            if self._wake_stop.wait(delay):
                return

            if (
                self.runtime_paused
                or not self.wake_word_enabled
            ):
                return

            # A startup exception is raised on the Wake thread itself. The old
            # implementation could fire this retry while that failed thread was
            # still inside its finally/cleanup path. start_wake_listener() then
            # saw an alive _wake_thread and correctly refused a duplicate start,
            # but no later retry remained; touching Wake sensitivity happened to
            # start it again. Wait until the failed owner has completely exited
            # before replacing it. This keeps one microphone owner at a time.
            while failed_wake_thread.is_alive():
                if self._wake_stop.wait(0.05):
                    return

            with self._lock:
                if self._wake_thread is failed_wake_thread:
                    self._wake_thread = None

            if (
                self.runtime_paused
                or not self.wake_word_enabled
                or self._wake_stop.is_set()
            ):
                return

            self.start_wake_listener()

        worker = threading.Thread(
            target=retry,
            name=f"qronos-wake-retry-{attempt}",
            daemon=True,
        )
        self._wake_retry_thread = worker
        worker.start()
        return True

    def start_wake_listener(self) -> None:

        """
        Start the low-cost Qronos wake-word loop.

        Thread liveness is deliberately not reported as listening readiness.
        A duplicate start can arrive while the first thread is still preparing
        the real Wake engine and microphone. During that startup window, return
        silently and let _wake_listener_loop emit wake_word_listening only after
        trigger.start() and audio_input.start() have both completed.

        The listener uses the same AudioInput object as command recording, but
        never reads from it concurrently. After detection, this thread pauses
        wake-word inference and immediately hands the already-open microphone
        to CommandRecorder, preserving the first phoneme of the user's command.
        """
        if not self.wake_word_enabled:
            self._wake_ready.clear()
            self._notify(
                "wake_word_disabled",
                "ready",
                "Wake Word is disabled in Qronos Settings.",
            )
            return

        if self.runtime_paused:
            self._reject_if_runtime_paused(
                "wake_listener_start"
            )
            return

        # The Settings meter may own the microphone only while Wake is absent.
        # Stop that temporary owner before the persistent Wake stream opens.
        self._stop_standalone_input_meter()

        with self._lock:
            if self._wake_thread is not None and self._wake_thread.is_alive():
                if self._wake_ready.is_set():
                    self._notify(
                        "wake_word_listening",
                        "listening",
                        "Qronos wake-word listener is already active.",
                    )
                return

            self._wake_stop.clear()
            self._wake_ready.clear()

            thread = threading.Thread(
                target=self._wake_listener_loop,
                name="qronos-wake-word",
                daemon=True,
            )
            self._wake_thread = thread


        thread.start()


    def stop_wake_listener(self) -> None:
        self._wake_stop.set()
        self._wake_ready.clear()
        with self._lock:
            self._wake_start_retry_count = 0

        # Unblock a wake thread that may currently be waiting for the desktop
        # to finish speaking.
        self._voice_playback_complete.set()

        audio = self.audio_input
        if audio is not None:
            try:
                audio.stop()
            except Exception:
                pass

        trigger = self.voice_trigger
        if trigger is not None:
            try:
                trigger.stop()
            except Exception:
                pass

        thread = self._wake_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=0.75)

        self._wake_thread = None

        # Stopping wake listening also releases persistent STT resources.
        self._release_speech_runtime()

        if (
            self._input_meter_enabled
            and not self.runtime_paused
        ):
            self._start_standalone_input_meter()

    def _write_voice_playback_telemetry(
        self,
        playback_id: int,
        *,
        archive: bool = False,
    ) -> None:
        """Persist desktop playback timing without affecting a voice turn."""
        try:
            with self._lock:
                source = dict(
                    self._voice_playback_metrics.get(
                        playback_id,
                        {},
                    )
                )

            if not source:
                return

            def delta(
                later: str,
                earlier: str,
            ) -> float | None:
                try:
                    return round(
                        float(source[later])
                        - float(source[earlier]),
                        3,
                    )
                except (KeyError, TypeError, ValueError):
                    return None

            rendered: dict[str, Any] = {
                key: value
                for key, value in source.items()
                if not key.endswith("Perf")
            }

            rendered.update(
                {
                    "responseReadyToFirstAudioReadySeconds": delta(
                        "firstAudioReadyPerf",
                        "responseReadyPerf",
                    ),
                    "firstAudioReadyToDesktopPlaybackStartSeconds": delta(
                        "desktopPlaybackStartedPerf",
                        "firstAudioReadyPerf",
                    ),
                    "responseReadyToDesktopPlaybackStartSeconds": delta(
                        "desktopPlaybackStartedPerf",
                        "responseReadyPerf",
                    ),
                    "desktopPlaybackDurationSeconds": delta(
                        "desktopPlaybackCompletePerf",
                        "desktopPlaybackStartedPerf",
                    ),
                }
            )

            VOICE_PLAYBACK_TELEMETRY_PATH.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            payload = json.dumps(
                rendered,
                ensure_ascii=False,
                indent=2,
            )
            VOICE_PLAYBACK_TELEMETRY_PATH.write_text(
                payload,
                encoding="utf-8",
            )

            if archive:
                archive_path = (
                    VOICE_PLAYBACK_TELEMETRY_PATH.parent
                    / (
                        "voice_playback_"
                        f"{playback_id}_"
                        f"{time.time_ns()}.json"
                    )
                )
                archive_path.write_text(
                    payload,
                    encoding="utf-8",
                )
        except Exception:
            # Diagnostics must never affect voice playback.
            pass

    def _begin_voice_playback_wait(self) -> int:
        """Start one response-level desktop playback timing group."""
        now_perf = time.perf_counter()
        now_epoch = time.time()

        with self._lock:
            self._voice_playback_id += 1
            playback_id = self._voice_playback_id
            self._pending_playback_id = playback_id
            self._voice_playback_complete.clear()
            self._voice_playback_metrics[playback_id] = {
                "playbackId": playback_id,
                "status": "response_ready",
                "responseReadyPerf": now_perf,
                "responseReadyAtEpoch": round(now_epoch, 6),
            }

        self._write_voice_playback_telemetry(
            playback_id
        )
        return playback_id

    def set_voice_playback_context(
        self,
        playback_id: int,
        *,
        trigger_source: str,
        transcript: str,
        response: str,
    ) -> None:
        with self._lock:
            metrics = self._voice_playback_metrics.get(
                playback_id
            )
            if metrics is None:
                return
            metrics.update(
                {
                    "triggerSource": trigger_source,
                    "transcript": transcript,
                    "response": response,
                }
            )

        self._write_voice_playback_telemetry(
            playback_id
        )

    def mark_voice_first_audio_ready(
        self,
        playback_id: int,
    ) -> None:
        now_perf = time.perf_counter()
        now_epoch = time.time()

        with self._lock:
            metrics = self._voice_playback_metrics.get(
                playback_id
            )
            if metrics is None:
                return
            if "firstAudioReadyPerf" in metrics:
                return
            metrics.update(
                {
                    "status": "audio_ready",
                    "firstAudioReadyPerf": now_perf,
                    "firstAudioReadyAtEpoch": round(
                        now_epoch,
                        6,
                    ),
                }
            )

        self._write_voice_playback_telemetry(
            playback_id
        )

    def set_voice_playback_audio_metadata(
        self,
        playback_id: int,
        *,
        total_audio_seconds: float,
        chunk_count: int,
    ) -> None:
        with self._lock:
            metrics = self._voice_playback_metrics.get(
                playback_id
            )
            if metrics is None:
                return
            metrics.update(
                {
                    "generatedAudioSeconds": round(
                        float(total_audio_seconds),
                        3,
                    ),
                    "chunkCount": int(chunk_count),
                }
            )

        self._write_voice_playback_telemetry(
            playback_id
        )

    def mark_voice_playback_started(
        self,
        playback_id: int,
    ) -> bool:
        """Record the first real Desktop media-engine playback start."""
        now_perf = time.perf_counter()
        now_epoch = time.time()

        with self._lock:
            if (
                self._pending_playback_id is None
                or playback_id != self._pending_playback_id
            ):
                return False

            metrics = self._voice_playback_metrics.get(
                playback_id
            )
            if metrics is None:
                return False

            if "desktopPlaybackStartedPerf" not in metrics:
                metrics.update(
                    {
                        "status": "playing",
                        "desktopPlaybackStartedPerf": now_perf,
                        "desktopPlaybackStartedAtEpoch": round(
                            now_epoch,
                            6,
                        ),
                    }
                )

        self._write_voice_playback_telemetry(
            playback_id
        )
        return True

    @property
    def pending_paint_draw(self) -> PendingPaintDrawAction | None:
        ticket = self._approvals.open_ticket(ApprovalKind.PAINT_DRAW)
        return ticket.payload if ticket is not None else None

    @property
    def pending_paint_undo(self) -> PendingPaintUndoAction | None:
        ticket = self._approvals.open_ticket(ApprovalKind.PAINT_UNDO)
        return ticket.payload if ticket is not None else None

    @property
    def pending_look(self) -> str | None:
        ticket = self._approvals.open_ticket(ApprovalKind.SCREEN_READ)
        return ticket.question if ticket is not None else None

    @pending_look.setter
    def pending_look(self, question: str | None) -> None:
        """Replace the open screen-read prompt (a new identity every time)."""
        with self._approval_lock:
            self._approvals.revoke_kinds(
                (ApprovalKind.SCREEN_READ,),
                STALE_SUPERSEDED,
            )
            if question:
                self._approvals.issue(
                    ApprovalKind.SCREEN_READ,
                    question,
                    window=self.last_foreground_window,
                )

    @property
    def pending_file_actions(self) -> dict[str, PendingFileTextAction]:
        """Open file approvals by their single-use identity (a view)."""
        return {
            ticket.approval_id: ticket.payload
            for ticket in self._approvals.open_tickets()
            if ticket.kind is ApprovalKind.FILE_ACTION
        }

    def _discard_pending_file_approvals(self, reason: str) -> int:
        """
        Revoke every file action still waiting for its UI answer and end the
        reversible-write lease. A late "approve" for a revoked identity is
        refused (file_action_stale) and cannot write. Each discarded request
        is audited as cancelled and its text request is answered. Idempotent.
        """
        revoked = self._approvals.revoke_kinds(
            (ApprovalKind.FILE_ACTION,),
            reason,
        )
        for ticket in revoked:
            pending: PendingFileTextAction = ticket.payload
            audit = getattr(self.file_executor, "audit", None)
            if audit is not None:
                try:
                    audit.record_result(
                        pending.prepared.request,
                        ActionResult(
                            action_id=pending.prepared.request.action_id,
                            outcome=ActionOutcome.CANCELLED,
                            detail=f"Discarded before approval ({reason}); nothing was written.",
                        ),
                    )
                except Exception as exc:
                    self._notify(
                        "runtime_warning",
                        "warning",
                        f"Could not audit a discarded file approval: {type(exc).__name__}",
                    )
            self._notify(
                "file_action_cancelled",
                "ready",
                json.dumps(
                    {
                        "actionToken": ticket.approval_id,
                        "requestId": pending.request_id,
                        "actionId": pending.prepared.request.action_id,
                        "reason": reason,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            self._finish_text_file_action(
                request_id=pending.request_id,
                response="لغو شد؛ هیچ تغییری روی فایل‌ها انجام نشد.",
                started_at=pending.started_at,
            )
        self._clear_file_write_approval_lease(reason)
        return len(revoked)

    def _discard_pending_media_approvals(self, reason: str) -> int:
        """
        Revoke every media library question (MEDIA_ACTION) at the same Pause,
        Stop and shutdown points as the file approvals. Their payloads belong
        to the media route and are never read here; a late answer is stale
        there. Idempotent.
        """
        return len(self._approvals.revoke_kinds((ApprovalKind.MEDIA_ACTION,), reason))

    def _discard_pending_device_approvals(self, reason: str) -> int:
        """Revoke every Device Hub job-control question (DEVICE_ACTION) at Pause, Stop and shutdown. Payloads
        belong to core/device_hub.py and are never read here; a late answer is stale there. Idempotent."""
        return len(self._approvals.revoke_kinds((ApprovalKind.DEVICE_ACTION,), reason))

    def _notify_file_stale(self, action_token: str, reason: str) -> None:
        self._notify(
            "file_action_stale",
            "ready",
            json.dumps(
                {"actionToken": action_token, "reason": reason},
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

    def _issue_paint_prompt(
        self,
        kind: ApprovalKind,
        transcript: str,
        payload: Any,
    ) -> ApprovalTicket | None:
        """Issue one Paint prompt identity, or None while another is open."""
        with self._approval_lock:
            if self._approvals.open_ticket(
                ApprovalKind.PAINT_DRAW,
                ApprovalKind.PAINT_UNDO,
            ) is not None:
                return None
            return self._approvals.issue(kind, transcript, payload=payload)

    def _notify_screen_prompt(self, ticket: ApprovalTicket) -> None:
        """Ask the desktop for an on-screen answer bound to ``ticket``."""
        self._notify(
            "voice_needs_screen",
            "ready",
            json.dumps(
                {
                    "approvalId": ticket.approval_id,
                    "kind": ticket.kind.value,
                    "question": ticket.question,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def _discard_pending_approvals(self, reason: str) -> int:
        """
        Revoke every computer/Paint/screen prompt still waiting for an answer.

        The prompt may already be on screen. Its identity is revoked, so its
        later answer is refused even if an identical question is asked again
        (that new prompt gets a new identity). Nothing ran, and the discard is
        audited for a prepared Paint drawing/undo. Idempotent.
        """
        revoked = self._approvals.revoke_kinds(
            (
                ApprovalKind.PAINT_DRAW,
                ApprovalKind.PAINT_UNDO,
                ApprovalKind.SCREEN_READ,
            ),
            reason,
        )
        for ticket in revoked:
            pending = ticket.payload
            if pending is not None and self.paint_draw_executor is not None:
                try:
                    self.paint_draw_executor.discard_pending(
                        pending.prepared,
                        reason=reason,
                    )
                except Exception:
                    pass
        app_workflows = getattr(self, "_app_workflows", None)
        if app_workflows is not None:
            # Telegram/print prompts and running steps stop with everything else.
            app_workflows.discard(reason)
        elevated = getattr(self, "_elevated_actions", None)
        if elevated is not None:
            # Open elevation prompts too. A broker already started is not
            # interrupted: it performs one action or none, and reports.
            elevated.discard(reason)
        if revoked:
            self._notify(
                "computer_approval_discarded",
                "ready",
                json.dumps(
                    {
                        "reason": reason,
                        "discarded": len(revoked),
                        "approvalIds": [t.approval_id for t in revoked],
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            )
        return len(revoked)

    def request_stop_response(self) -> dict[str, Any]:
        lifecycle = self._foreground_lifecycle()
        with lifecycle.transition():
            cancelled = lifecycle.cancel_current(CANCEL_REASON_USER_STOP)
            self._preempt_media(CANCEL_REASON_USER_STOP)
            result = self._request_stop_response()
            result["foregroundInferenceCancelled"] = cancelled
            return result

    def _request_stop_response(
        self,
    ) -> dict[str, Any]:
        """
        Stop the current user-visible response without pausing Qronos.

        Desktop audio is stopped independently at the media layer. This method
        releases the backend playback wait immediately and asks any in-flight
        voice turn to suppress remaining response/TTS phases. The foreground
        token closes its HTTP connection before this Stop acknowledgement;
        downstream checks discard late results without retrying the provider.

        In-flight computer/Paint actions are cancelled cooperatively: each
        stops at its next checkpoint (between native steps or pointer moves)
        after releasing any mouse button it holds. Only actions registered
        before this call are affected; the next command starts fresh.
        """
        now_perf = time.perf_counter()
        now_epoch = time.time()

        computer_actions_cancelled = len(
            self._computer_actions.cancel_all(CANCEL_REASON_USER_STOP)
        )
        pending_approvals_discarded = self._discard_pending_approvals(
            CANCEL_REASON_USER_STOP
        )
        pending_file_approvals_discarded = self._discard_pending_file_approvals(
            CANCEL_REASON_USER_STOP
        )
        self._discard_pending_media_approvals(CANCEL_REASON_USER_STOP)
        self._discard_pending_device_approvals(CANCEL_REASON_USER_STOP)
        self._discard_pending_history_undo(CANCEL_REASON_USER_STOP)

        with self._lock:
            busy = bool(self._busy or self._foreground.active)

            if busy:
                self._voice_response_stop_requested.set()

            playback_id = self._pending_playback_id
            playback_cancelled = playback_id is not None

            if playback_id is not None:
                metrics = self._voice_playback_metrics.get(
                    playback_id
                )
                if metrics is not None:
                    metrics.update(
                        {
                            "status": "cancelled",
                            "desktopPlaybackCompletePerf": now_perf,
                            "desktopPlaybackCompleteAtEpoch": round(
                                now_epoch,
                                6,
                            ),
                            "cancelledByUser": True,
                        }
                    )

                # Clear the pending identity now so a stale later `ended`
                # callback cannot complete a response the user already stopped.
                self._pending_playback_id = None

                # Wake any playback waiter immediately.
                self._voice_playback_complete.set()

        if playback_id is not None:
            self._write_voice_playback_telemetry(
                playback_id,
                archive=True,
            )

        self._notify(
            "voice_response_stopped",
            "ready",
            json.dumps(
                {
                    "busyTurnStopRequested": busy,
                    "playbackCancelled": playback_cancelled,
                    "playbackId": playback_id,
                    "computerActionsCancelled": computer_actions_cancelled,
                    "pendingApprovalsDiscarded": pending_approvals_discarded,
                    "pendingFileApprovalsDiscarded": pending_file_approvals_discarded,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

        return {
            "busyTurnStopRequested": busy,
            "playbackCancelled": playback_cancelled,
            "playbackId": playback_id,
            "computerActionsCancelled": computer_actions_cancelled,
            "pendingApprovalsDiscarded": pending_approvals_discarded,
            "pendingFileApprovalsDiscarded": pending_file_approvals_discarded,
        }


    def mark_voice_playback_complete(
        self,
        playback_id: int,
    ) -> bool:
        """Accept Desktop completion and persist final practical timing."""
        now_perf = time.perf_counter()
        now_epoch = time.time()

        with self._lock:
            if (
                self._pending_playback_id is None
                or playback_id != self._pending_playback_id
            ):
                return False

            metrics = self._voice_playback_metrics.get(
                playback_id
            )
            if metrics is not None:
                metrics.update(
                    {
                        "status": "complete",
                        "desktopPlaybackCompletePerf": now_perf,
                        "desktopPlaybackCompleteAtEpoch": round(
                            now_epoch,
                            6,
                        ),
                    }
                )

            self._voice_playback_complete.set()

        self._write_voice_playback_telemetry(
            playback_id,
            archive=True,
        )
        return True

    def _wait_for_voice_playback_complete(
        self,
    ) -> bool:
        """Wait for Desktop completion; timeout remains only a deadlock guard."""
        with self._lock:
            playback_id = self._pending_playback_id

        if playback_id is None:
            return True

        deadline = (
            time.monotonic()
            + PLAYBACK_ACK_FAILSAFE_SECONDS
        )

        while not self._wake_stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            if self._voice_playback_complete.wait(
                timeout=min(0.25, remaining)
            ):
                with self._lock:
                    if self._pending_playback_id == playback_id:
                        self._pending_playback_id = None
                    self._voice_playback_complete.clear()

                    metrics = self._voice_playback_metrics.get(
                        playback_id
                    )
                    if (
                        metrics is not None
                        and metrics.get("status")
                        != "cancelled"
                    ):
                        metrics["status"] = "acknowledged"

                    # Keep a bounded amount of in-memory diagnostic state.
                    if len(self._voice_playback_metrics) > 20:
                        for old_id in sorted(
                            self._voice_playback_metrics
                        )[:-20]:
                            self._voice_playback_metrics.pop(
                                old_id,
                                None,
                            )

                self._write_voice_playback_telemetry(
                    playback_id
                )
                return True

        if self._wake_stop.is_set():
            return False

        with self._lock:
            if self._pending_playback_id == playback_id:
                self._pending_playback_id = None
            self._voice_playback_complete.clear()

            metrics = self._voice_playback_metrics.get(
                playback_id
            )
            if metrics is not None:
                metrics["status"] = "ack_timeout"

        self._write_voice_playback_telemetry(
            playback_id,
            archive=True,
        )

        self._notify(
            "voice_playback_ack_timeout",
            "warning",
            (
                "Desktop did not confirm voice playback completion "
                "before the playback acknowledgement failsafe expired."
            ),
        )

        return False

    def _should_continue_followup_session(self) -> bool:
        """
        Return True while the current interactive conversation should continue.

        Conversation lifetime is deliberately independent of generated audio
        duration. A turn may fail before producing audio while the conversation
        itself is still valid and should return to follow-up listening.

        The session ends only when another lifecycle path explicitly closes it,
        such as the no-speech follow-up timeout or runtime shutdown.
        """
        return (
            not self._wake_stop.is_set()
            and self.conversation_session is not None
            and self.conversation_session.is_active
            and self.pending_look is None
            and self.pending_paint_draw is None
        )

    def _wake_listener_loop(self) -> None:

        """
        Run the wake gate and the interactive multi-turn voice session.

        A wake word is required only to open a session. After Qronos finishes
        speaking, the microphone reopens for a bounded follow-up window and the
        user may continue without saying Qronos again.

        Full command capture remains off while Qronos is speaking. True
        mid-speech barge-in is intentionally not implemented here because the
        current desktop playback path has no acoustic echo cancellation.
        """
        try:

            wake_start_perf = time.perf_counter()

            self.prepare_wake_gate()

            if self.audio_input is None:
                raise RuntimeError(
                    "Qronos microphone is not prepared for wake-word listening."
                )


            engine = OpenWakeWordEngine(
                threshold=self.wake_threshold,
            )


            trigger = VoiceTriggerService(
                wake_word="Qronos",
                engine=engine,
            )

            self.wake_engine = engine
            self.voice_trigger = trigger


            trigger.start()


            self.audio_input.start()


            self._wake_ready.set()
            with self._lock:
                self._wake_start_retry_count = 0

            self._notify(
                "wake_word_listening",
                "listening",
                json.dumps(
                    {
                        "message": "Say Qronos to start a voice session.",
                        "coldStartSeconds": round(
                            time.perf_counter() - wake_start_perf,
                            3,
                        ),
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            )

            # Do not put VAD/recorder/STT on the wake critical path. Wake is
            # already listening at this point; the heavier capture layer warms
            # behind it and an early command is buffered if necessary.
            self._warm_wake_command_capture_async()

            while not self._wake_stop.is_set():
                if self._pending_audio_input_reconcile:
                    self._reconcile_audio_input_in_wake_thread()

                try:
                    frame = self.audio_input.read_frame()
                except Exception:
                    if self._wake_stop.is_set():
                        break
                    raise

                self._maybe_emit_input_meter_frame(
                    frame
                )

                event = trigger.process_audio(
                    frame,
                    timestamp=time.time(),
                )

                if event is None:
                    continue

                wake_detected_perf = (
                    time.perf_counter()
                )

                self._wake_ready.clear()
                trigger.pause()

                # The 8 GB GPU has one Qronos high-VRAM phase slot.
                # Yield any warm Fast/Heavy Brain before Whisper starts so
                # STT does not overlap physically with Brain residency.
                self._release_brains_for_voice_phase()

                # If the user wakes Qronos before the command VAD has finished
                # its background cold start, keep draining the open microphone
                # into a bounded buffer. Those frames are consumed first by the
                # recorder, so early commands are not clipped or discarded.
                initial_command_frames = (
                    self._buffer_wake_command_audio_until_capture_ready()
                )

                # Load persistent Whisper while the user is speaking.
                # Recording time hides most or all of model startup latency.
                self._warm_speech_runtime_async()

                self._notify(
                    "wake_word_detected",
                    "listening",
                    json.dumps(
                        {
                            "wakeWord": event.wake_word,
                            "timestamp": round(
                                event.timestamp,
                                6,
                            ),
                            "score": round(
                                engine.last_score,
                                6,
                            ),
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )

                if (
                    self.conversation_session
                    is not None
                    and not
                    self.conversation_session.is_active
                ):
                    self.conversation_session.start()
                    self._notify(
                        "conversation_session_started",
                        "listening",
                        "Interactive voice session started.",
                    )

                playback_seconds = (
                    self._run_voice_turn(
                        trigger_source="wake_word",
                        detected_at_perf=(
                            wake_detected_perf
                        ),
                        wake_score=float(
                            engine.last_score
                        ),
                        initial_frames=initial_command_frames,
                    )
                )

                # A wake-word candidate is not enough to establish an
                # interactive conversation. If no valid spoken turn was
                # completed after the wake event, close any session that
                # may have been opened and return directly to wake-word
                # listening. This prevents wind/noise false wakes from
                # falling through into unrestricted follow-up listening.
                if (
                    playback_seconds <= 0
                    and not
                    self._last_voice_turn_had_valid_speech_evidence
                    and self.conversation_session
                    is not None
                    and self.conversation_session.is_active
                ):
                    self.conversation_session.close()

                    self._notify(
                        "conversation_session_closed",
                        "ready",
                        (
                            "Wake candidate cancelled because "
                            "no valid speech turn was completed."
                        ),
                    )

                while self._should_continue_followup_session():
                    if playback_seconds > 0:
                        playback_completed = (
                            self._wait_for_voice_playback_complete()
                        )

                        if (
                            playback_completed
                            and not self._wake_stop.is_set()
                        ):
                            # WASAPI/device buffers can leave a tiny acoustic
                            # tail after the Desktop process reports complete.
                            # Keep the microphone closed through that tail so
                            # follow-up capture cannot ingest Qronos's own last
                            # syllable as the next user command.
                            time.sleep(
                                FOLLOWUP_PLAYBACK_GUARD_SECONDS
                            )

                    if self._wake_stop.is_set():
                        break

                    if (
                        not
                        self.conversation_session.is_active
                    ):
                        break

                    self.conversation_session.begin_listening()

                    self._notify(
                        "conversation_followup_listening",
                        "listening",
                        json.dumps(
                            {
                                "timeoutSeconds":
                                    FOLLOWUP_START_TIMEOUT_SECONDS,
                                "wakeWordRequired":
                                    False,
                            },
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ),
                    )

                    playback_seconds = (
                        self._run_voice_turn(
                            trigger_source="followup",
                        )
                    )

                if self._wake_stop.is_set():
                    break

                # The interactive session is finished. Release
                # persistent STT VRAM before returning to idle wake listening.
                self._release_speech_runtime()

                if self._pending_audio_input_reconcile:
                    self._reconcile_audio_input_in_wake_thread()

                # Resume the existing wake-word pipeline.
                # OpenWakeWord resume() resets its rolling
                # prediction/audio buffers and performs its
                # warm-up path without rebuilding the engine.
                self.audio_input.start()
                trigger.resume()
                self._wake_ready.set()

                self._notify(
                    "wake_word_listening",
                    "listening",
                    "Say Qronos to start a voice session.",
                )

        except Exception as exc:

            if not self._wake_stop.is_set():
                retry_scheduled = (
                    self._schedule_wake_start_retry(
                        exc
                    )
                )

                if not retry_scheduled:
                    self._notify(
                        "runtime_error",
                        "error",
                        f"Wake-word listener failed: {exc}",
                    )

        finally:
            self._wake_ready.clear()

            trigger = self.voice_trigger
            if trigger is not None:
                try:
                    trigger.stop()
                except Exception:
                    pass

            if self.audio_input is not None:
                try:
                    self.audio_input.stop()
                except Exception:
                    pass

            self.voice_trigger = None
            self.wake_engine = None

    def _synthesize_response(self, response: str):
        """
        Generate the spoken form of one assistant response.

        This seam lets the production response-to-TTS path be tested without
        opening the microphone or invoking ASR/LLM work.
        """
        if self.voice_output is None:
            raise RuntimeError(
                "Qronos TTS runtime is not prepared."
            )

        self._notify(
            "voice_synthesizing",
            "processing",
            "Generating Qronos voice response.",
        )

        spoken_response = normalise_spoken_text(
            response
        )

        utterance = self.voice_output.speak_to_file(
            spoken_response
        )

        def build_spectrum_sidecar() -> None:
            try:
                _write_voice_spectrum_sidecar(
                    utterance.audio_path
                )
            except Exception:
                # Playback spectrum is visual-only. A sidecar failure must
                # never affect speech playback or the runtime event contract.
                pass

        threading.Thread(
            target=build_spectrum_sidecar,
            name="qronos-spectrum-sidecar",
            daemon=True,
        ).start()

        self._notify(
            "voice_audio_ready",
            "ready",
            json.dumps(
                {
                    "path": str(utterance.audio_path),
                    "audioSeconds": round(
                        utterance.audio_seconds,
                        3,
                    ),
                    "generationSeconds": round(
                        utterance.took_seconds,
                        3,
                    ),
                    "rtf": round(
                        utterance.real_time_factor,
                        3,
                    ),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

        return utterance


    def _synthesize_response_chunks(
        self,
        response: str,
        playback_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Synthesize one complete Brain response as sequential semantic chunks.

        There is still exactly one Chatterbox generation at a time. After each
        chunk is ready its path is emitted immediately, allowing the desktop
        player to begin playback while the next chunk is generated.
        """
        if self.voice_output is None:
            raise RuntimeError(
                "Qronos TTS runtime is not prepared."
            )

        chunks = _split_semantic_tts_chunks(
            response
        )

        if not chunks:
            raise RuntimeError(
                "Qronos response produced no TTS chunks."
            )

        self._notify(
            "voice_synthesizing",
            "processing",
            "Generating Qronos voice response.",
        )

        total_audio_seconds = 0.0
        total_model_seconds = 0.0
        first_audio_ready_perf: float | None = None
        first_chunk_model_seconds = 0.0
        first_chunk_audio_seconds = 0.0
        spoken_chunks: list[str] = []
        partial_tts_error: str | None = None
        chunk_metrics: list[dict[str, Any]] = []
        predicted_buffer_seconds = 0.0
        predicted_underrun_seconds = 0.0

        for index, chunk in enumerate(
            chunks
        ):
            if self._voice_response_stop_requested.is_set():
                break

            spoken_chunk = normalise_spoken_text(
                chunk
            )

            try:
                utterance = (
                    self.voice_output.speak_to_file(
                        spoken_chunk
                    )
                )
            except Exception as exc:
                inference_checkpoint()
                # If an early chunk is already playing, a later TTS failure
                # must not invalidate that healthy audio. Degrade to the
                # successfully generated prefix, keep the playback identity
                # alive, and let Desktop finish it normally.
                if spoken_chunks:
                    partial_tts_error = str(exc)
                    self._notify(
                        "runtime_warning",
                        "warning",
                        (
                            "Voice continuation stopped after "
                            f"{len(spoken_chunks)} generated chunk(s): {exc}"
                        ),
                    )
                    break

                # Nothing usable was generated, so this remains a genuine
                # voice-turn failure and the established recovery path applies.
                raise

            # Chatterbox generation itself is synchronous. If Stop arrived
            # during this one chunk, discard the generated file instead of
            # emitting more speech to Desktop.
            if self._voice_response_stop_requested.is_set():
                break

            spoken_chunks.append(
                spoken_chunk
            )

            if first_audio_ready_perf is None:
                first_audio_ready_perf = (
                    time.perf_counter()
                )
                first_chunk_model_seconds = (
                    float(
                        utterance.took_seconds
                    )
                )
                first_chunk_audio_seconds = (
                    float(
                        utterance.audio_seconds
                    )
                )

                if playback_id is not None:
                    self.mark_voice_first_audio_ready(
                        playback_id
                    )

            audio_seconds = float(
                utterance.audio_seconds
            )
            generation_seconds = float(
                utterance.took_seconds
            )

            predicted_gap_before_chunk = 0.0

            if index == 0:
                # Playback cannot consume anything before the first WAV exists.
                predicted_buffer_seconds = audio_seconds
            else:
                # While Chatterbox generates chunk N, Desktop is consuming the
                # already-buffered audio from earlier chunks. If generation
                # takes longer than that buffer, the listener hears a gap.
                predicted_gap_before_chunk = max(
                    0.0,
                    generation_seconds
                    - predicted_buffer_seconds,
                )
                predicted_underrun_seconds += (
                    predicted_gap_before_chunk
                )
                predicted_buffer_seconds = (
                    max(
                        0.0,
                        predicted_buffer_seconds
                        - generation_seconds,
                    )
                    + audio_seconds
                )

            try:
                audio_quality = (
                    _analyze_voice_wav_quality(
                        utterance.audio_path
                    )
                )
            except Exception as exc:
                audio_quality = {
                    "analysisError": str(
                        exc
                    )
                }

            chunk_metrics.append(
                {
                    "audioPath": str(
                        utterance.audio_path
                    ),
                    "audioQuality":
                        audio_quality,
                    "chunkNumber": index + 1,
                    "characterCount": len(
                        spoken_chunk
                    ),
                    "audioSeconds": round(
                        audio_seconds,
                        3,
                    ),
                    "generationSeconds": round(
                        generation_seconds,
                        3,
                    ),
                    "rtf": round(
                        float(
                            utterance.real_time_factor
                        ),
                        3,
                    ),
                    "predictedGapBeforeChunkSeconds": round(
                        predicted_gap_before_chunk,
                        3,
                    ),
                    "predictedBufferedAudioAfterChunkSeconds": round(
                        predicted_buffer_seconds,
                        3,
                    ),
                }
            )

            total_audio_seconds += audio_seconds
            total_model_seconds += generation_seconds

            def build_spectrum_sidecar(
                audio_path=utterance.audio_path,
            ) -> None:
                try:
                    _write_voice_spectrum_sidecar(
                        audio_path
                    )
                except Exception:
                    # Spectrum is visual-only and must never delay speech.
                    pass

            threading.Thread(
                target=build_spectrum_sidecar,
                name=(
                    "qronos-spectrum-sidecar-"
                    f"{index + 1}"
                ),
                daemon=True,
            ).start()

            audio_payload: dict[str, Any] = {
                "path": str(
                    utterance.audio_path
                ),
                "audioSeconds": round(
                    utterance.audio_seconds,
                    3,
                ),
                "generationSeconds": round(
                    utterance.took_seconds,
                    3,
                ),
                "rtf": round(
                    utterance.real_time_factor,
                    3,
                ),
                "chunkIndex": index,
                "chunkNumber": index + 1,
                "chunkCount": len(chunks),
                "isFirstChunk": index == 0,
                "isLastChunk": (
                    index
                    == len(chunks) - 1
                ),
            }

            if playback_id is not None:
                audio_payload[
                    "playbackId"
                ] = playback_id

            with self._foreground_lifecycle().publication():
                self._notify(
                    "voice_audio_ready",
                    "ready",
                    json.dumps(
                        audio_payload,
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )

        if playback_id is not None:
            self.set_voice_playback_audio_metadata(
                playback_id,
                total_audio_seconds=total_audio_seconds,
                chunk_count=len(spoken_chunks),
            )

            # Chunk playback is a streamed response. Desktop must not infer
            # "whole response finished" merely because its queue is
            # temporarily empty between chunks. This explicit finalization
            # event closes the producer side only after TTS generation has
            # genuinely finished (successfully, partially, or by Stop).
            with self._foreground_lifecycle().publication():
                self._notify(
                    "voice_audio_stream_complete",
                    "ready",
                    json.dumps(
                        {
                            "playbackId": playback_id,
                            "generatedChunkCount": len(
                                spoken_chunks
                            ),
                            "plannedChunkCount": len(
                                chunks
                            ),
                            "stoppedByUser":
                                self._voice_response_stop_requested.is_set(),
                            "partialTtsError":
                                partial_tts_error,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )

        return {
            "chunks": chunks,
            "spokenChunks": spoken_chunks,
            "chunkCount": len(spoken_chunks),
            "plannedChunkCount": len(chunks),
            "totalAudioSeconds":
                total_audio_seconds,
            "totalModelSeconds":
                total_model_seconds,
            "firstAudioReadyPerf":
                first_audio_ready_perf,
            "firstChunkModelSeconds":
                first_chunk_model_seconds,
            "firstChunkAudioSeconds":
                first_chunk_audio_seconds,
            "stoppedByUser":
                self._voice_response_stop_requested.is_set(),
            "partialTtsError":
                partial_tts_error,
            "chunkMetrics": chunk_metrics,
            "predictedUnderrunSeconds": round(
                predicted_underrun_seconds,
                3,
            ),
        }


    def _write_latency_report(
        self,
        payload: dict[str, Any],
    ) -> None:
        """
        Persist voice diagnostics without losing earlier reports.

        ``voice_latency_latest.json`` remains the convenient latest-event
        snapshot. Every write is also archived under a unique filename so
        later follow-up timeouts, runtime errors, or successful turns can
        never erase earlier diagnostic evidence.

        Diagnostics must never break a voice turn.
        """
        try:
            VOICE_LATENCY_LOG_PATH.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            rendered = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            )

            VOICE_LATENCY_LOG_PATH.write_text(
                rendered,
                encoding="utf-8",
            )

            archive_path = (
                VOICE_LATENCY_LOG_PATH.parent
                / (
                    "voice_latency_"
                    f"{time.time_ns()}.json"
                )
            )

            archive_path.write_text(
                rendered,
                encoding="utf-8",
            )

        except Exception:
            # Diagnostics must never change the runtime event contract or
            # interfere with a voice turn.
            pass


    def _cancel_pending_voice_playback_wait(
        self,
    ) -> None:
        """
        Forget an unfinished playback group after a failed voice turn.

        This prevents a TTS/runtime failure from leaving a stale playback ID
        that could interfere with the next valid desktop acknowledgement.
        """
        with self._lock:
            self._pending_playback_id = None
            self._voice_playback_complete.clear()


    def _recover_valid_voice_turn_failure(
        self,
        report: dict[str, Any],
        *,
        user_message_added: bool,
        assistant_message_added: bool,
        rewarm_stt_after_tts: bool,
    ) -> float:
        """
        Speak a short local recovery message after genuine user speech fails
        before a usable assistant response has been produced.

        No Brain is called here. The purpose is to stop recoverable
        Brain/resource failures from becoming a silent follow-up window and
        then an apparently random session close.
        """
        inference_checkpoint()
        if (
            not self._last_voice_turn_had_valid_speech_evidence
            or assistant_message_added
            or self.conversation_session is None
            or not self.conversation_session.is_active
            or self.voice_output is None
        ):
            return 0.0

        recovery_response = (
            VOICE_RETRY_RESPONSE
            if user_message_added
            else VOICE_STT_RETRY_RESPONSE
        )

        playback_id: int | None = None

        try:
            with self._foreground_lifecycle().publication():
                self.conversation_session.begin_responding()

                if user_message_added:
                    self.conversation_session.add_assistant_message(
                        recovery_response
                    )

                self._notify(
                    "voice_response",
                    "ready",
                    recovery_response,
                )

            # A failed Brain turn may still have left a keep-alive model
            # resident. The local recovery voice must get the GPU phase slot.
            self._release_brains_for_voice_phase()

            playback_id = (
                self._begin_voice_playback_wait()
            )

            chunked = (
                self._synthesize_response_chunks(
                    recovery_response,
                    playback_id=playback_id,
                )
            )

            tts_released = (
                self._release_voice_output_runtime()
            )
            report[
                "recoveryTtsReleasedAfterSynthesis"
            ] = bool(
                tts_released
            )

            if (
                tts_released
                and rewarm_stt_after_tts
            ):
                self._warm_speech_runtime_async()

            playback_seconds = max(
                0.0,
                float(
                    chunked[
                        "totalAudioSeconds"
                    ]
                ),
            )

            report[
                "recoveryResponse"
            ] = recovery_response

            report[
                "recoveryGeneratedAudioSeconds"
            ] = round(
                playback_seconds,
                3,
            )

            return playback_seconds

        except LocalInferenceCancelled:
            raise
        except Exception as recovery_exc:
            inference_checkpoint()
            self._cancel_pending_voice_playback_wait()

            report[
                "recoveryError"
            ] = str(
                recovery_exc
            )

            return 0.0


    def _run_voice_turn(self, trigger_source: str, detected_at_perf=None,
                        wake_score=None, initial_frames=(), *, prepare_push_to_talk=False) -> float:
        lifecycle = self._foreground_lifecycle()
        turn = None
        try:
            with lifecycle.begin("voice", state=self._foreground_state, on_cancel=
                    lambda turn, exc: self._report_foreground_cancelled(
                        "voice", exc.reason, turn=turn)) as turn:
                if prepare_push_to_talk:
                    return self._prepared_push_to_talk_turn()
                return self._run_voice_turn_scoped(trigger_source, detected_at_perf,
                                                   wake_score, initial_frames)
        except ForegroundBusy as exc:
            self._notify("runtime_busy", "busy", str(exc))
        except LocalInferenceCancelled as exc:
            self._report_foreground_cancelled("voice", exc.reason, turn=turn)
        return 0.0

    def _run_voice_turn_scoped(
        self,
        trigger_source: str,
        detected_at_perf: float | None = None,
        wake_score: float | None = None,
        initial_frames: tuple[bytes, ...] = (),
    ) -> float:
        """
        Capture one user command, run the existing Brain path, synthesize it,
        and return the generated audio duration.

        This version also instruments every material latency segment without
        changing the execution order. That gives us a factual baseline before
        introducing semantic chunking or streaming.
        """
        if self._reject_if_runtime_paused("voice"):
            return 0.0

        try:
            self.last_foreground_window = foreground_window()
        except Exception:
            self.last_foreground_window = None

        if self.is_busy:
            self._notify(
                "runtime_busy",
                "busy",
                "Qronos is already processing a voice request.",
            )
            return 0.0

        self._set_busy(True)
        self._voice_response_stop_requested.clear()
        self._computer_actions.mark_turn_start()
        playback_seconds = 0.0
        user_message_added = False
        assistant_message_added = False
        voice_request_id = ""
        conversation_context = ()
        self._last_voice_turn_had_valid_speech_evidence = False

        turn_started_perf = (
            detected_at_perf
            if detected_at_perf is not None
            else time.perf_counter()
        )

        report: dict[str, Any] = {
            "triggerSource": trigger_source,
            "startedAtEpoch": round(
                time.time(),
                6,
            ),
        }

        if wake_score is not None:
            report["wakeScore"] = round(
                float(wake_score),
                6,
            )

        try:
            self.prepare_wake_capture()
            self._require_wake_capture_prepared()

            # Chatterbox cold-start is ~2-3 seconds on this machine. Hide it
            # behind the user's recording/STT time instead of paying it after
            # the assistant text has already appeared.
            self._prewarm_voice_output_async()
            report["ttsPrewarmRequested"] = True

            self._notify(
                "voice_listening",
                "listening",
                "Listening for your command.",
            )

            self.audio_input.start()

            recording_started = time.perf_counter()

            recording_path = (
                self.speech_runtime.temp_dir
                / "qronos_voice_command.wav"
            )

            # Preserve the established recorder call contract for ordinary
            # PTT/follow-up turns. Only the very-early Wake path needs the new
            # buffered-frame argument. This keeps existing recorder adapters
            # and tests compatible while still protecting speech spoken before
            # command VAD warm-up finishes.
            if initial_frames:
                recording = self.command_recorder.record_to_file(
                    recording_path,
                    on_audio_spectrum=self._emit_audio_spectrum,
                    initial_frames=initial_frames,
                )
            else:
                recording = self.command_recorder.record_to_file(
                    recording_path,
                    on_audio_spectrum=self._emit_audio_spectrum,
                )

            recording_finished = time.perf_counter()

            self.audio_input.stop()
            self._clear_audio_spectrum()

            report.update(
                {
                    "recordingAudioSeconds": round(
                        float(
                            recording.duration_seconds
                        ),
                        3,
                    ),
                    "recordingSpeechSeconds": round(
                        float(
                            recording.speech_seconds
                        ),
                        3,
                    ),
                    "peakSpeechProbability": round(
                        float(
                            recording.peak_speech_probability
                        ),
                        6,
                    ),
                }
            )

            # A false wake must never turn a tiny burst of wind/noise into
            # an LLM request. Follow-up turns intentionally keep the
            # recorder's normal minimum so short replies such as "yes",
            # "no", or a person's name remain usable once a conversation
            # has already been established.
            if (
                trigger_source == "wake_word"
                and float(recording.speech_seconds)
                < WAKE_MIN_COMMAND_SPEECH_SECONDS
            ):
                report["wakeSpeechEvidenceRejected"] = True
                report["recordingAudioSeconds"] = round(
                    float(recording.duration_seconds),
                    3,
                )
                report["recordingSpeechSeconds"] = round(
                    float(recording.speech_seconds),
                    3,
                )
                report["peakSpeechProbability"] = round(
                    float(recording.peak_speech_probability),
                    6,
                )
                report["closedAtEpoch"] = round(
                    time.time(),
                    6,
                )

                self._write_latency_report(
                    report
                )

                if (
                    self.conversation_session
                    is not None
                    and self.conversation_session.is_active
                ):
                    self.conversation_session.close()

                self._notify(
                    "conversation_session_closed",
                    "ready",
                    (
                        "Wake candidate rejected because "
                        "speech evidence was too short."
                    ),
                )

                return 0.0

            self._last_voice_turn_had_valid_speech_evidence = True

            self._notify(
                "voice_transcribing",
                "processing",
                "Transcribing voice command.",
            )

            stt_started = time.perf_counter()

            primary_stt_language = self.voice_language
            transcript = (
                self.speech_runtime.transcribe_file(
                    recording.audio_path,
                    language=self.voice_language,
                )
                .strip()
            )

            inference_checkpoint()
            primary_action_score = _voice_action_transcript_quality(transcript)
            if (
                _voice_transcript_looks_actionish(transcript)
                and primary_action_score < 8
            ):
                retry_language = (
                    "fa"
                    if primary_stt_language != "fa"
                    else "auto"
                )
                try:
                    inference_checkpoint()
                    retry_transcript = (
                        self.speech_runtime.transcribe_file(
                            recording.audio_path,
                            language=retry_language,
                        )
                        .strip()
                    )
                    retry_action_score = _voice_action_transcript_quality(
                        retry_transcript
                    )
                    report["sttActionRetry"] = True
                    report["sttActionRetryLanguage"] = retry_language
                    report["sttPrimaryActionScore"] = primary_action_score
                    report["sttRetryActionScore"] = retry_action_score
                    report["sttPrimaryTranscript"] = transcript
                    if retry_transcript and retry_action_score > primary_action_score:
                        transcript = retry_transcript
                        report["sttActionRetrySelected"] = True
                    else:
                        report["sttActionRetrySelected"] = False
                except Exception as retry_exc:
                    inference_checkpoint()
                    report["sttActionRetry"] = True
                    report["sttActionRetryFailure"] = str(retry_exc)

            inference_checkpoint()
            stt_finished = time.perf_counter()

            stt_release_started = time.perf_counter()

            if not self._release_speech_runtime():
                raise RuntimeError(
                    "Qronos could not release persistent STT GPU resources "
                    "before Brain execution."
                )

            stt_release_finished = time.perf_counter()
            report["sttReleasedBeforeBrain"] = True
            report["sttReleaseSeconds"] = round(
                stt_release_finished
                - stt_release_started,
                3,
            )

            # Heavy post-transcription collaborators are intentionally delayed
            # until after the user's speech is already safely captured.
            self.prepare()
            self._require_prepared()

            self._require_runtime_active("voice brain execution")

            transcript = normalise_qronos_invocation(transcript)

            if not transcript:
                raise RuntimeError(
                    "Speech recognition returned an empty command."
                )

            report["transcript"] = transcript

            conversation_context = (
                self._conversation_brain_context()
            )
            voice_request_id = (
                f"voice-{time.time_ns()}"
            )

            self._record_conversation_message(
                role="user",
                text=transcript,
                source="voice",
                request_id=voice_request_id,
            )

            self._notify(
                "voice_transcript",
                "processing",
                transcript,
            )

            if not self.conversation_session.is_active:
                self.conversation_session.start()

            self.conversation_session.add_user_message(
                transcript
            )
            user_message_added = True
            self.conversation_session.begin_processing()

            # Observe the future Gate + Estimator on the exact real STT transcript.
            # This result is written only to diagnostics; it cannot select a worker.
            routing_shadow = self._routing_shadow(
                transcript
            )
            report["routingShadow"] = routing_shadow

            route_started = time.perf_counter()

            route = self.task_router.route(
                transcript
            )

            routed_task_type = route.task_type
            (
                selected_task_type,
                compute_promotion,
            ) = self._select_production_task_type(
                transcript,
                routed_task_type,
                routing_shadow,
            )

            if _looks_like_calculator_semantic_action(
                transcript,
                self._last_computer_app_id,
            ):
                routed_task_type = (
                    TaskType.COMPUTER
                )

                selected_task_type = (
                    TaskType.COMPUTER
                )

                compute_promotion = dict(
                    compute_promotion
                )

                compute_promotion[
                    "calculatorLocalSemanticRoute"
                ] = True

            from qronos_media.intents import parse_media_intent as _parse_media_intent

            if _parse_media_intent(transcript) is not None:
                # A media search/status request runs the typed media operation (voice computer branch).
                routed_task_type = TaskType.COMPUTER
                selected_task_type = TaskType.COMPUTER

            report["route"] = (
                routed_task_type.value
            )
            report["selectedRoute"] = (
                selected_task_type.value
            )
            report["computePromotion"] = (
                compute_promotion
            )

            routing_shadow["legacyRoute"] = (
                routed_task_type.value
            )
            routing_shadow["selectedRoute"] = (
                selected_task_type.value
            )
            routing_shadow[
                "computePromotionApplied"
            ] = bool(
                compute_promotion.get(
                    "applied",
                    False,
                )
            )

            inference_checkpoint()
            route_finished = time.perf_counter()

            self._notify(
                "voice_routed",
                "processing",
                selected_task_type.value,
            )

            if selected_task_type is TaskType.VISION:
                if self._computer_actions.stopped_since_turn_start():
                    # Stopped while routing: never open a screen prompt
                    # whose later answer would act on the stopped request.
                    report["visionApprovalCancelled"] = True
                    self._notify(
                        "voice_turn_stopped",
                        "ready",
                        "Current Qronos response was stopped by the user.",
                    )
                    self._write_latency_report(report)
                    return 0.0
                with self._approval_lock:
                    # A newer spoken question replaces an unanswered one; the
                    # old prompt's identity is revoked, never reassigned.
                    self._approvals.revoke_kinds(
                        (ApprovalKind.SCREEN_READ,),
                        STALE_SUPERSEDED,
                    )
                    ticket = self._approvals.issue(
                        ApprovalKind.SCREEN_READ,
                        transcript,
                        window=self.last_foreground_window,
                    )
                self.conversation_session.begin_listening()

                self._notify_screen_prompt(ticket)

                report["route"] = route.task_type.value
                report["visionApprovalPending"] = True
                self._write_latency_report(report)
                return 0.0

            voice_computer_response: str | None = None
            voice_computer_deferred = False

            if selected_task_type is TaskType.COMPUTER:
                voice_computer_response = (
                    self._execute_voice_computer_action(
                        transcript,
                        report,
                    )
                )
                voice_computer_deferred = bool(
                    report.get(
                        "voiceComputerDeferred",
                        False,
                    )
                )
                if report.get(
                    "paintDrawScreenApprovalPending",
                    False,
                ):
                    self.conversation_session.begin_listening()
                    pending_ticket = self._approvals.open_ticket(
                        ApprovalKind.PAINT_DRAW,
                        ApprovalKind.PAINT_UNDO,
                    )
                    if (
                        pending_ticket is not None
                        and pending_ticket.approval_id
                        == report.get("screenApprovalId")
                    ):
                        self._notify_screen_prompt(pending_ticket)
                    report["visionApprovalPending"] = True
                    self._write_latency_report(report)
                    return 0.0
                arithmetic_answer = None
            else:
                arithmetic_answer = (
                    solve_simple_arithmetic(
                        transcript
                    )
                    if selected_task_type is TaskType.FAST
                    else None
                )

            brain_route_needs_model = (
                selected_task_type
                in {
                    TaskType.FAST,
                    TaskType.HEAVY,
                    TaskType.BROWSER,
                }
                and arithmetic_answer is None
            )

            if brain_route_needs_model:
                brain_tts_yield_started = (
                    time.perf_counter()
                )
                brain_tts_yielded = (
                    self._yield_voice_output_before_brain()
                )
                report[
                    "ttsReleasedBeforeBrain"
                ] = bool(
                    brain_tts_yielded
                )
                report[
                    "ttsBrainYieldSeconds"
                ] = round(
                    time.perf_counter()
                    - brain_tts_yield_started,
                    3,
                )

                # Preserve the established Heavy telemetry keys for existing
                # diagnostics while the generic keys cover every Brain route.
                if selected_task_type is TaskType.HEAVY:
                    report[
                        "ttsReleasedBeforeHeavyBrain"
                    ] = bool(
                        brain_tts_yielded
                    )
                    report[
                        "ttsHeavyYieldSeconds"
                    ] = report[
                        "ttsBrainYieldSeconds"
                    ]

                if not brain_tts_yielded:
                    raise RuntimeError(
                        "Qronos could not safely yield TTS before "
                        f"{selected_task_type.value} Brain execution."
                    )

            brain_started = time.perf_counter()

            if selected_task_type is TaskType.COMPUTER:
                response = (
                    voice_computer_response
                    or VOICE_COMPUTER_DEFERRED_RESPONSE
                )
            elif arithmetic_answer is not None:
                response = (
                    arithmetic_answer.spoken_text
                )
            else:
                plan = TaskPlan(
                    goal=transcript
                )
                plan.add_step(
                    task_type=selected_task_type,
                    description=transcript,
                )

                self._require_runtime_active("voice brain execution")

                results = self.orchestrator.execute_plan(
                    plan,
                    conversation_messages=(
                        conversation_context
                    ),
                )

                inference_checkpoint()
                if not results:
                    raise RuntimeError(
                        "Qronos did not return an execution result."
                    )

                result = results[-1]

                if not result.success:
                    raise RuntimeError(
                        result.error
                        or "Qronos task failed."
                    )

                response = result.output.strip()

            brain_finished = time.perf_counter()

            if self._voice_response_stop_requested.is_set():
                if selected_task_type is TaskType.COMPUTER and voice_computer_response:
                    with self._foreground_lifecycle().publication(action_outcome=True):
                        self._notify("voice_response", "ready", voice_computer_response)
                inference_checkpoint()
                report["stoppedByUserBeforeTts"] = True
                report["stoppedAtEpoch"] = round(
                    time.time(),
                    6,
                )

                self._release_brains_for_voice_phase()

                self._notify(
                    "voice_turn_stopped",
                    "ready",
                    "Current Qronos response was stopped by the user.",
                )

                self._write_latency_report(
                    report
                )

                return 0.0

            if not response:
                raise RuntimeError(
                    "Qronos returned an empty response."
                )

            brain_release_started = time.perf_counter()
            self._release_brains_for_voice_phase()
            brain_release_finished = time.perf_counter()

            report["brainReleasedBeforeTts"] = True
            report["brainReleaseSeconds"] = round(
                brain_release_finished
                - brain_release_started,
                3,
            )

            with self._foreground_lifecycle().publication():
                self.conversation_session.begin_responding()
                self.conversation_session.add_assistant_message(
                    response
                )
                assistant_message_added = True

                self._record_conversation_message(
                    role="assistant",
                    text=response,
                    source="voice",
                    request_id=voice_request_id,
                )

                inference_checkpoint()
                self._notify(
                    "voice_response",
                    "ready",
                    response,
                )

            self._require_runtime_active("voice output")

            tts_started = time.perf_counter()

            report["ttsWasHotAtResponse"] = bool(
                self.voice_output is not None
                and getattr(
                    self.voice_output,
                    "is_loaded",
                    False,
                )
            )
            if self._voice_output_prewarm_error:
                report["ttsPrewarmError"] = (
                    self._voice_output_prewarm_error
                )

            playback_id = (
                self._begin_voice_playback_wait()
            )

            self.set_voice_playback_context(
                playback_id,
                trigger_source=trigger_source,
                transcript=transcript,
                response=response,
            )

            chunked = (
                self._synthesize_response_chunks(
                    response,
                    playback_id=playback_id,
                )
            )

            inference_checkpoint()
            all_audio_ready = (
                time.perf_counter()
            )

            if chunked.get("stoppedByUser"):
                report["stoppedByUserDuringTts"] = True

            partial_tts_error = chunked.get(
                "partialTtsError"
            )
            if partial_tts_error:
                report["partialTtsError"] = str(
                    partial_tts_error
                )
                report["ttsPartialPlaybackPreserved"] = True

            tts_release_started = time.perf_counter()
            tts_released = (
                self._release_voice_output_runtime()
            )
            tts_release_finished = time.perf_counter()

            report["ttsReleasedAfterSynthesis"] = bool(
                tts_released
            )
            report["ttsReleaseSeconds"] = round(
                tts_release_finished
                - tts_release_started,
                3,
            )

            # Once TTS has yielded the GPU, warm STT while desktop playback
            # consumes the already-generated WAV files. This restores the
            # follow-up latency optimization without overlapping two large
            # Qronos GPU phases.
            if (
                tts_released
                and trigger_source != "push_to_talk"
            ):
                self._warm_speech_runtime_async()

            first_audio_ready = (
                chunked[
                    "firstAudioReadyPerf"
                ]
                or all_audio_ready
            )

            # Reliability-first playback guard.
            #
            # The desktop player owns actual audio playback. The backend only
            # knows when generated WAV chunks become available; it does not
            # know the exact moment the desktop media engine starts playing
            # them or whether there are small gaps between queued chunks.
            #
            # Do not subtract TTS generation time from the playback duration.
            # Doing so can open the follow-up microphone before desktop
            # playback has actually finished, and the desktop intentionally
            # stops active Qronos audio when a new voice-listening turn begins.
            #
            # Until an explicit desktop -> runtime playback-complete
            # acknowledgement is implemented, wait conservatively for the
            # complete generated audio duration.
            playback_seconds = max(
                0.0,
                float(
                    chunked[
                        "totalAudioSeconds"
                    ]
                ),
            )

            report.update(
                {
                    "transcript": transcript,
                    "route": routed_task_type.value,
                    "selectedRoute": selected_task_type.value,
                    "response": response,
                    "recordingAudioSeconds": round(
                        float(
                            recording.duration_seconds
                        ),
                        3,
                    ),
                    "recordingSpeechSeconds": round(
                        float(
                            recording.speech_seconds
                        ),
                        3,
                    ),
                    "generatedAudioSeconds": round(
                        float(
                            chunked[
                                "totalAudioSeconds"
                            ]
                        ),
                        3,
                    ),
                    "ttsModelSeconds": round(
                        float(
                            chunked[
                                "totalModelSeconds"
                            ]
                        ),
                        3,
                    ),
                    "ttsChunkCount": int(
                        chunked[
                            "chunkCount"
                        ]
                    ),
                    "ttsChunks": list(
                        chunked[
                            "chunks"
                        ]
                    ),
                    "ttsSpokenChunks": list(
                        chunked[
                            "spokenChunks"
                        ]
                    ),
                    "ttsChunkMetrics": list(
                        chunked[
                            "chunkMetrics"
                        ]
                    ),
                    "predictedTtsUnderrunSeconds": round(
                        float(
                            chunked[
                                "predictedUnderrunSeconds"
                            ]
                        ),
                        3,
                    ),
                    "ttsContinuityChunkTargets": {
                        "firstCharacters":
                            TTS_FIRST_CHUNK_TARGET_CHARACTERS,
                        "laterCharacters":
                            TTS_LATER_CHUNK_TARGET_CHARACTERS,
                        "minimumFirstCharacters":
                            TTS_MIN_FIRST_CHUNK_CHARACTERS,
                        "minimumCharacters":
                            TTS_MIN_CHUNK_CHARACTERS,
                        "maximumCharacters":
                            TTS_MAX_CHUNK_CHARACTERS,
                    },
                    "firstChunkAudioSeconds": round(
                        float(
                            chunked[
                                "firstChunkAudioSeconds"
                            ]
                        ),
                        3,
                    ),
                    "firstChunkModelSeconds": round(
                        float(
                            chunked[
                                "firstChunkModelSeconds"
                            ]
                        ),
                        3,
                    ),
                    "timingsSeconds": {
                        "wakeToRecordingStart": round(
                            recording_started
                            - turn_started_perf,
                            3,
                        ),
                        "recording": round(
                            recording_finished
                            - recording_started,
                            3,
                        ),
                        "stt": round(
                            stt_finished
                            - stt_started,
                            3,
                        ),
                        "routing": round(
                            route_finished
                            - route_started,
                            3,
                        ),
                        "brain": round(
                            brain_finished
                            - brain_started,
                            3,
                        ),
                        "ttsFirstChunk": round(
                            first_audio_ready
                            - tts_started,
                            3,
                        ),
                        "ttsAllChunks": round(
                            all_audio_ready
                            - tts_started,
                            3,
                        ),
                        "wakeToFirstAudioReady": round(
                            first_audio_ready
                            - turn_started_perf,
                            3,
                        ),
                        "wakeToAllAudioReady": round(
                            all_audio_ready
                            - turn_started_perf,
                            3,
                        ),
                    },
                }
            )

            self._write_latency_report(
                report
            )

            with self._foreground_lifecycle().publication(terminal=True):
                self._notify(
                    "voice_turn_complete",
                    "ready",
                    (
                        "Wake-word turn completed."
                        if trigger_source == "wake_word"
                        else "Push-to-talk turn completed."
                    ),
                )

        except LocalInferenceCancelled as exc:
            report["status"] = "cancelled"
            report["cancellationReason"] = exc.reason
            self._cancel_pending_voice_playback_wait()
            self._write_latency_report(report)
            raise
        except TimeoutError as exc:
            inference_checkpoint()
            if (
                trigger_source == "followup"
                and str(exc)
                == "No speech was detected before the start timeout."
            ):
                report["followupTimedOut"] = True
                report["closedAtEpoch"] = round(
                    time.time(),
                    6,
                )

                self._write_latency_report(
                    report
                )

                if (
                    self.conversation_session
                    is not None
                    and self.conversation_session.is_active
                ):
                    self.conversation_session.close()

                self._notify(
                    "conversation_session_closed",
                    "ready",
                    "Follow-up window expired.",
                )

                return 0.0

            report["error"] = str(exc)
            report["failedAtEpoch"] = round(
                time.time(),
                6,
            )

            with self._foreground_lifecycle().publication():
                self._notify(
                    "runtime_error",
                    "error",
                    str(exc),
                )

            self._cancel_pending_voice_playback_wait()

            playback_seconds = (
                self._recover_valid_voice_turn_failure(
                    report,
                    user_message_added=(
                        user_message_added
                    ),
                    assistant_message_added=(
                        assistant_message_added
                    ),
                    rewarm_stt_after_tts=(
                        trigger_source
                        != "push_to_talk"
                    ),
                )
            )

            self._write_latency_report(
                report
            )

        except Exception as exc:
            inference_checkpoint()
            report["error"] = str(exc)
            report["failedAtEpoch"] = round(
                time.time(),
                6,
            )

            with self._foreground_lifecycle().publication():
                self._notify(
                    "runtime_error",
                    "error",
                    str(exc),
                )

            self._cancel_pending_voice_playback_wait()

            playback_seconds = (
                self._recover_valid_voice_turn_failure(
                    report,
                    user_message_added=(
                        user_message_added
                    ),
                    assistant_message_added=(
                        assistant_message_added
                    ),
                    rewarm_stt_after_tts=(
                        trigger_source
                        != "push_to_talk"
                    ),
                )
            )

            self._write_latency_report(
                report
            )

        finally:
            turn = self._foreground_lifecycle().current
            if turn is not None and turn.token.cancelled and report.get("status") != "cancelled":
                report["status"] = "cancelled"
                report["cancellationReason"] = turn.token.reason
                report.pop("error", None)
                self._write_latency_report(report)
            try:
                self._clear_audio_spectrum()
            except Exception:
                pass

            if self.audio_input is not None:
                try:
                    self.audio_input.stop()
                except Exception:
                    pass

            self._set_busy(False)
            self._voice_response_stop_requested.clear()

        return playback_seconds

    def push_to_talk(self) -> None:
        self._run_voice_turn(trigger_source="push_to_talk", prepare_push_to_talk=True)

    def _prepared_push_to_talk_turn(self) -> float:
        try:
            # Never compete with the Settings-only meter for the microphone.
            self._stop_standalone_input_meter()
            self.prepare_wake_capture()
            self._require_wake_capture_prepared()

            # Serialize GPU phases before warming Whisper for an explicit turn.
            self._release_brains_for_voice_phase()

            # Warm Whisper concurrently with command recording.
            self._warm_speech_runtime_async()

            inference_checkpoint()
            return self._run_voice_turn_scoped(trigger_source="push_to_talk")
        except LocalInferenceCancelled:
            raise
        except Exception as exc:
            inference_checkpoint()
            with self._foreground_lifecycle().publication():
                self._notify("runtime_error", "error", str(exc))
            return 0.0

        finally:
            # Push-to-talk is one explicit turn, so do not retain VRAM.
            self._release_speech_runtime()

            if (
                not self._foreground_lifecycle().current.token.cancelled
                and self._input_meter_enabled
                and not self.wake_listener_running
                and not self.runtime_paused
            ):
                self._start_standalone_input_meter()


    def _resolve_paint_undo_decision(
        self,
        pending: PendingPaintUndoAction,
        *,
        approved: bool,
    ) -> bool:
        self._ensure_computer_runtime()
        if self.paint_draw_executor is None:
            raise RuntimeError("Qronos Paint draw executor is not available.")

        if not approved:
            self.paint_draw_executor.decline_screen(pending.prepared)
            self._notify(
                "voice_screen_declined",
                "ready",
                "بازگردانی لغو شد؛ Qronos پنجرهٔ Paint را مشاهده یا تغییر نداد.",
            )
            return True

        response = self._execute_paint_undo(
            pending.prepared,
            gui_approval=pending.gui_approval,
            screen_approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
            report={},
        )

        if self.conversation_session is not None:
            if self.conversation_session.is_active:
                self.conversation_session.begin_responding()
                self.conversation_session.add_assistant_message(response)
                self.conversation_session.begin_listening()

        self._notify("voice_response", "ready", response)
        self._notify(
            "voice_turn_complete",
            "ready",
            "Paint undo turn completed.",
        )
        return True

    def _resolve_paint_draw_decision(
        self,
        pending: PendingPaintDrawAction,
        *,
        approved: bool,
    ) -> bool:
        self._ensure_computer_runtime()
        if self.paint_draw_executor is None:
            raise RuntimeError("Qronos Paint draw executor is not available.")

        if not approved:
            self.paint_draw_executor.decline_screen(pending.prepared)
            self._notify(
                "voice_screen_declined",
                "ready",
                "نقاشی لغو شد؛ Qronos پنجرهٔ Paint را مشاهده یا تغییر نداد.",
            )
            return True

        with self._computer_actions.begin("paint_draw") as cancel_token:
            execution = self.paint_draw_executor.execute_prepared(
                pending.prepared,
                gui_approval=pending.gui_approval,
                screen_approval=PermissionDecision.REQUIRE_UI_CONFIRMATION,
                reevaluate=True,
                cancel_token=cancel_token,
            )

        if execution.result.outcome is ActionOutcome.CANCELLED:
            response = _paint_draw_cancelled_response(execution)
            effects = execution.side_effects
            self._notify(
                "paint_draw_cancelled",
                "ready",
                json.dumps(
                    {
                        "actionId": execution.request.action_id,
                        "strokesPlanned": effects.strokes_planned,
                        "strokesCompleted": effects.strokes_completed,
                        "canvasEdits": effects.canvas_marks_committed,
                        "unprovenReleases": effects.canvas_marks_unproven,
                        "toolbarClicks": effects.toolbar_clicks,
                        "undoAvailable": execution.undo_available,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        elif execution.result.outcome is ActionOutcome.SUCCEEDED:
            response = "انجام شد. نقاشی در Paint اجرا و نتیجهٔ واقعی آن تأیید شد."
            payload: dict[str, Any] = {
                "actionId": execution.request.action_id,
                "verified": True,
                "undoAvailable": execution.undo_available,
            }
            if execution.run is not None:
                payload.update(
                    {
                        "strokes": execution.run.stroke_count,
                        "points": execution.run.point_count,
                        "pid": execution.run.pid,
                        "hwnd": execution.run.hwnd,
                        "expectedCoverage": round(
                            execution.run.verification.expected_coverage,
                            4,
                        ),
                        "observedPrecision": round(
                            execution.run.verification.observed_precision,
                            4,
                        ),
                        "fidelityF1": round(
                            execution.run.verification.f1_score,
                            4,
                        ),
                        "semanticConfidence": round(
                            execution.run.verification.semantic_critique.confidence,
                            4,
                        ),
                    }
                )
            self._notify(
                "paint_draw_completed",
                "ready",
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        elif execution.result.outcome is ActionOutcome.AWAITING_APPROVAL:
            response = (
                "مجوزها هنگام اجرا تغییر کردند و نقاشی انجام نشد. "
                "دوباره فرمان را بگو."
            )
        elif execution.result.outcome is ActionOutcome.REFUSED:
            response = "مجوز فعلی اجازهٔ این نقاشی را نمی‌دهد."
        else:
            response = paint_draw_failure_response_fa(execution)
            self._notify(
                "paint_draw_failed",
                "error",
                execution.result.detail,
            )

        if self.conversation_session is not None:
            if self.conversation_session.is_active:
                self.conversation_session.begin_responding()
                self.conversation_session.add_assistant_message(response)
                self.conversation_session.begin_listening()

        self._notify(
            "voice_response",
            "ready",
            response,
        )
        self._notify(
            "voice_turn_complete",
            "ready",
            "Paint drawing turn completed.",
        )
        return True

    def _notify_stale_screen_answer(
        self,
        reason: str,
        *,
        approval_id: str,
        approved: bool,
    ) -> None:
        self._notify(
            "screen_approval_stale",
            "ready",
            json.dumps(
                {
                    "reason": reason,
                    "approvalId": approval_id,
                    "approved": bool(approved),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

    def look_at_screen(
        self, approved=False, question="", window_only=False,
        approval_id="", direct=False,
    ) -> None:
        if self._reject_if_runtime_paused("screen"):
            return
        lifecycle = self._foreground_lifecycle()
        turn = None
        try:
            with lifecycle.begin("screen", state=self._foreground_state, on_cancel=
                    lambda turn, exc: self._report_foreground_cancelled(
                        "screen", exc.reason, turn=turn)) as turn:
                self._look_at_screen(
                    approved, question, window_only,
                    approval_id=approval_id, direct=direct,
                )
        except ForegroundBusy as exc:
            self._notify("runtime_busy", "busy", str(exc))
        except LocalInferenceCancelled as exc:
            if turn is None and exc.reason == CANCEL_REASON_SHUTDOWN:
                self._notify("screen_approval_stale", "ready", _compact(
                    {"reason": exc.reason, "approvalId": approval_id, "approved": bool(approved)}))
            else:
                self._report_foreground_cancelled("screen", exc.reason, turn=turn)

    def _look_at_screen(
        self,
        approved: bool = False,
        question: str = "",
        window_only: bool = False,
        approval_id: str = "",
        direct: bool = False,
    ) -> None:
        """
        Answer one screen/Paint prompt, or perform one explicit direct read.

        A prompt answer must carry the prompt's single-use ``approval_id``;
        the ticket, not the answer, decides what runs (the exact prepared
        Paint action, or the question and window recorded when it was
        asked). A revoked, used, expired or unknown identity is refused and
        never falls back to a direct read. ``direct=True`` is the explicit
        "look now at this question" request; an answer with neither is
        refused.
        """
        if self._reject_if_runtime_paused("screen"):
            return

        if self.is_busy:
            self._notify(
                "runtime_busy",
                "busy",
                "Qronos is already processing a request.",
            )
            return

        self._set_busy(True)
        self._computer_actions.mark_turn_start()

        try:
            window = self.last_foreground_window
            if approval_id:
                try:
                    ticket = self._approvals.take(
                        approval_id,
                        kinds=(
                            ApprovalKind.PAINT_DRAW,
                            ApprovalKind.PAINT_UNDO,
                            ApprovalKind.SCREEN_READ,
                        ),
                    )
                except StaleApproval as exc:
                    self._notify_stale_screen_answer(
                        exc.reason,
                        approval_id=approval_id,
                        approved=approved,
                    )
                    return
                if ticket.kind is ApprovalKind.PAINT_DRAW:
                    self._resolve_paint_draw_decision(ticket.payload, approved=approved)
                    return
                if ticket.kind is ApprovalKind.PAINT_UNDO:
                    self._resolve_paint_undo_decision(ticket.payload, approved=approved)
                    return
                if ticket.kind is not ApprovalKind.SCREEN_READ:
                    self._notify_stale_screen_answer(
                        "wrong_kind",
                        approval_id=approval_id,
                        approved=approved,
                    )
                    return
                # Bound at prompt time: the question asked and the window
                # that was in front then, not whatever is in front now.
                asked = ticket.question.strip()
                window = ticket.window
            elif direct:
                if self._computer_actions.closed:
                    self._notify_stale_screen_answer(
                        CANCEL_REASON_SHUTDOWN,
                        approval_id="",
                        approved=approved,
                    )
                    return
                asked = str(question or "").strip()
            else:
                self._notify_stale_screen_answer(
                    "approval_id_required",
                    approval_id="",
                    approved=approved,
                )
                return

            if not asked:
                raise RuntimeError(
                    "Qronos was asked to look at the screen without being "
                    "told what to look for."
                )

            if not approved:
                self._notify(
                    "voice_screen_declined",
                    "ready",
                    "Qronos did not look at the screen.",
                )
                return

            self.prepare()
            self._require_prepared()

            self._notify(
                "voice_capturing_screen",
                "processing",
                asked,
            )

            inference_checkpoint()
            capture = self._capture_for(window_only, window=window)
            inference_checkpoint()

            self._notify(
                "voice_captured_screen",
                "processing",
                capture.describe(),
            )

            if capture.blank:
                self._notify(
                    "voice_screen_blank",
                    "ready",
                    "There is nothing on the screen to read — it may be "
                    "locked, asleep, or showing protected video.",
                )
                return

            if not self.conversation_session.is_active:
                self.conversation_session.start()

            self.conversation_session.add_user_message(asked)
            self.conversation_session.begin_processing()

            plan = TaskPlan(goal=asked)
            plan.add_step(
                task_type=TaskType.VISION,
                description=asked,
                images=(capture.image,),
            )

            self._require_runtime_active("screen inference")
            results = self.orchestrator.execute_plan(plan)
            inference_checkpoint()

            if not results:
                raise RuntimeError(
                    "Qronos did not return an execution result."
                )

            result = results[-1]

            if not result.success:
                raise RuntimeError(
                    result.error or "Qronos could not read the screen."
                )

            response = result.output.strip()

            if not response:
                raise RuntimeError(
                    "Qronos returned an empty response."
                )

            with self._foreground_lifecycle().publication(terminal=True):
                self.conversation_session.begin_responding()
                self.conversation_session.add_assistant_message(response)
                self.conversation_session.begin_listening()

                self._notify(
                    "voice_response",
                    "ready",
                    response,
                )

                inference_checkpoint()
                self._notify(
                    "voice_turn_complete",
                    "ready",
                    "Screen reading turn completed.",
                )

        except LocalInferenceCancelled:
            raise
        except (CaptureRefused, CaptureUnavailable) as exc:
            inference_checkpoint()
            with self._foreground_lifecycle().publication(terminal=True):
                self._notify(
                    "voice_screen_unavailable",
                    "error",
                    str(exc),
                )

        except Exception as exc:
            inference_checkpoint()
            with self._foreground_lifecycle().publication(terminal=True):
                self._notify(
                    "runtime_error",
                    "error",
                    str(exc),
                )

        finally:
            self._set_busy(False)

    def _capture_for(
        self,
        window_only: bool,
        *,
        window: int | None = None,
    ) -> Capture:
        if self.screen_capture is None:
            raise RuntimeError(
                "Qronos screen capture runtime is not prepared."
            )

        window = window if window_only else None

        return self.screen_capture.capture(
            approved=True,
            window=window,
            reason=(
                "Look at the window that was in front."
                if window
                else "Look at what is on the screen."
            ),
        )

    def _interrupt_queued_conversations(self) -> None:
        """
        Leave the queue honest as this runtime closes.

        Running requests are interrupted: their work is told to stop, so a
        model call still under way cannot attach an answer after the runtime
        has closed, and they are recorded as interrupted, to run again only
        when the user resumes them. Waiting requests stay in the durable
        record and come back on the next start.

        If the queue could not be saved, waiting requests will not come
        back, and their conversations are told so now rather than left
        looking as if an answer might still come.
        """
        scheduler = self._scheduler

        if scheduler is None:
            return

        journal = self._queue_journal
        durable = journal is not None and journal.status().durable
        interrupted = set(scheduler.interrupt_running())
        detail = (
            "Qronos closed while this was running. It will not run again "
            "unless you resume it."
        )

        for task in scheduler.view().tasks:
            if task.get("kind") != "conversation":
                continue

            task_id = str(task.get("taskId", ""))
            work = scheduler.work_for(task_id)

            if not isinstance(work, QueuedConversationWork):
                continue

            if task_id in interrupted:
                if journal is not None:
                    journal.update(
                        task_id,
                        RecordState.INTERRUPTED,
                        detail=detail,
                    )

                self._notify_queued_conversation(
                    work,
                    ConversationPhase.INTERRUPTED,
                    {
                        "whileRunning": True,
                        "resumable": durable,
                        "detail": (
                            detail
                            if durable
                            else "Qronos closed while this was running, and "
                            "the queue could not be saved. Ask again when "
                            "you are ready."
                        ),
                    },
                )
                continue

            if durable or task.get("state") not in ("queued", "paused"):
                continue

            if not work.cancel():
                continue

            # Told it is lost, so it is cancelled - in the queue too. If the
            # disk recovers before this process exits, the saved record then
            # says what the user was told, instead of bringing the request
            # back on the next start.
            try:
                scheduler.cancel(task_id)
            except Exception:
                pass

            self._notify_queued_conversation(
                work,
                ConversationPhase.INTERRUPTED,
                {
                    "whileRunning": False,
                    "resumable": False,
                    "detail": (
                        "Qronos closed and the queue could not be saved, so "
                        "this request will not come back. Ask again when "
                        "you are ready."
                    ),
                },
            )

    def close(self) -> None:
        lifecycle = self._foreground_lifecycle()
        with lifecycle.transition():
            lifecycle.close()
            self._close()

    def _close(self) -> None:
        # Stop in-flight GUI actions first and give them a bounded moment to
        # unwind, so a stroke releases its mouse button before the process
        # exits. Daemon workers that do not finish in time are abandoned.
        # After close() nothing may run: the registry refuses new actions and
        # pending approvals are discarded so a late answer finds nothing.
        # No phone request may arrive while the runtime shuts down.
        if getattr(self, "_link_runtime", None) is not None:
            self.stop_device_link()
        self._computer_actions.close()
        media_host = self._media_host
        if media_host is not None:
            # Before the approvals close: the media stage stops, its model child is
            # torn down and its exit proven (or it stays counted) within a bound.
            try:
                media_host.close()
            except Exception:
                pass
        self._discard_pending_approvals(CANCEL_REASON_SHUTDOWN)
        self._discard_pending_file_approvals(CANCEL_REASON_SHUTDOWN)
        self._discard_pending_media_approvals(CANCEL_REASON_SHUTDOWN)
        self._discard_pending_device_approvals(CANCEL_REASON_SHUTDOWN)
        self._discard_pending_history_undo(CANCEL_REASON_SHUTDOWN)
        self._approvals.close()
        self._computer_actions.wait_idle(COMPUTER_ACTION_SHUTDOWN_GRACE_SECONDS)

        self._input_meter_enabled = False
        self._stop_standalone_input_meter()
        self.stop_wake_listener()
        self._release_speech_runtime()

        # The scheduler first, and before anything that could raise. Its
        # thread writes to stdout, and a write after main() has returned is a
        # traceback on stderr, which the desktop reads as a crash and
        # tests/test_runtime_bridge_process.py asserts against.
        if self._scheduler is not None:
            try:
                self._scheduler.stop()
            except Exception:
                pass

            try:
                self._interrupt_queued_conversations()
            except Exception:
                pass

        if self.audio_input is not None:
            try:
                self.audio_input.stop()
            except Exception:
                pass

        if self.vad_runtime is not None:
            try:
                self.vad_runtime.close()
            except Exception:
                pass

        if self.voice_output is not None:
            try:
                self.voice_output.release()
            except Exception:
                pass

        if self.conversation_session is not None:
            try:
                self.conversation_session.close()
            except Exception:
                pass

        if self.orchestrator is not None:
            try:
                self.orchestrator.workers.close()
            except Exception:
                pass


#: How a scheduler event reaches the desktop: an event name and a status.
#: Statuses are the ones the front end already understands. A refused override
#: is a *warning*, not an error — nothing went wrong, and ``runtime_error``
#: resets the orb to idle.
_QUEUE_EVENT_NAMES: dict[QueueEventType, tuple[str, str]] = {
    QueueEventType.QUEUED: ("queue_task_queued", "queued"),
    QueueEventType.STARTED: ("queue_task_started", "running"),
    QueueEventType.FINISHED: ("queue_task_finished", "ready"),
    QueueEventType.FAILED: ("queue_task_finished", "error"),
    QueueEventType.CANCELLED: ("queue_task_finished", "ready"),
    QueueEventType.DEFERRED: ("queue_task_deferred", "queued"),
    QueueEventType.OVERRIDE_REFUSED: ("queue_override_refused", "warning"),
    QueueEventType.HOLD_STATE: ("queue_hold_state", "busy"),
}


def _compact(payload: dict) -> str:
    """
    A structured payload, encoded into the message field.

    The event envelope is three string keys and has been since the bridge was
    written; ``voice_audio_spectrum`` already carries its spectrum this way.
    Following that rather than widening the envelope keeps the Rust
    deserialiser and every existing test untouched.
    """
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _queue_event_message(event: QueueEvent) -> str:
    payload: dict[str, Any] = {}

    # A hold-state event is about the machine rather than about any one task,
    # so it carries neither. Sending two empty strings with every one of them
    # is noise on a pipe the voice spectrum already keeps busy.
    if event.task_id:
        payload["taskId"] = event.task_id

    if event.summary:
        payload["summary"] = event.summary

    if event.reason is not None:
        payload["reason"] = event.reason.value

    if event.detail:
        payload["detail"] = event.detail

    if event.level is not None:
        payload["level"] = event.level.value

    if event.floor is not None:
        payload["breach"] = (
            None if event.floor.breach is None else event.floor.breach.value
        )
        payload["requiredVramMb"] = event.floor.required_vram_mb
        payload["freeVramMb"] = event.floor.free_vram_mb

    if event.event is QueueEventType.FINISHED:
        payload["success"] = True
    elif event.event is QueueEventType.FAILED:
        payload["success"] = False

    return _compact(payload)


def _queue_view_dict(view) -> dict[str, Any]:
    return {
        "revision": view.revision,
        "paused": view.paused,
        "runtimePaused": getattr(view, "runtime_paused", False),
        "level": view.level.value,
        "holdingSince": view.holding_since,
        "tasks": list(view.tasks),
    }


def _queue_view_message(view) -> str:
    return _compact(_queue_view_dict(view))


def emit(
    event_type: str,
    status: str,
    message: str,
) -> None:
    event = RuntimeEvent(
        event_type=event_type,
        status=status,
        message=message,
    )

    payload = (
        json.dumps(
            {
                "eventType": event.event_type,
                "status": event.status,
                "message": event.message,
            },
            ensure_ascii=True,
        )
        + "\n"
    )

    with EMIT_LOCK:
        if sys.stdout.closed:
            # The queue runs on its own thread and can outlive main() by a few
            # milliseconds during shutdown. Writing to a closed stream raises,
            # and an exception on a background thread prints a traceback to
            # stderr, which the desktop reports to the user as a crash. There
            # is nowhere to send this line, so it is dropped.
            return

        sys.stdout.write(payload)
        sys.stdout.flush()


def parse_payload(
    line: str,
) -> dict[str, Any]:
    payload: Any = json.loads(line)

    if not isinstance(payload, dict):
        raise ValueError(
            "Runtime command must be a JSON object."
        )

    command = str(
        payload.get(
            "command",
            "",
        )
    ).strip()

    if not command:
        raise ValueError(
            "Runtime command is empty."
        )

    payload["command"] = command
    return payload


def handle_action(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    action_id = str(
        payload.get(
            "actionId",
            "",
        )
    ).strip()

    if not action_id:
        raise ValueError(
            "Runtime action id is empty."
        )

    emit(
        "runtime_action_received",
        "ready",
        action_id,
    )

    if action_id == DEVICE_HUB_ACTION:
        # The Device Hub applies its own pause rule per operation (reads stay available while paused).
        handle_device_hub_action(runtime, payload)
        return

    if action_id == MEDIA_ACTION:
        # The media product applies its own pause rule per operation (reads stay available while paused).
        handle_media_action(runtime, payload)
        return

    pause_safe_actions = {
        CONVERSATION_HISTORY_REQUEST_ACTION,
        HISTORY_REQUEST_ACTION,
        HISTORY_UNDO_DECISION_ACTION,
        FILE_ACTION_DECISION_ACTION,
        VOICE_PLAYBACK_STARTED_ACTION,
        VOICE_PLAYBACK_COMPLETE_ACTION,
        WAKE_LISTENER_STOP_ACTION,
        SETTINGS_RELOAD_ACTION,
        INPUT_METER_STOP_ACTION,
    }

    if (
        runtime.runtime_paused
        and action_id not in pause_safe_actions
    ):
        runtime._reject_if_runtime_paused(
            f"action:{action_id}"
        )
        return

    if action_id == CONVERSATION_HISTORY_REQUEST_ACTION:
        runtime.emit_conversation_history_snapshot()
        return

    if action_id == HISTORY_REQUEST_ACTION:
        runtime.emit_history_snapshot()
        return

    if action_id in (HISTORY_UNDO_REQUEST_ACTION, HISTORY_UNDO_DECISION_ACTION):
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("History undo data must be an object.")
        if action_id == HISTORY_UNDO_REQUEST_ACTION:
            runtime.request_history_undo(str(data.get("actionId", "")))
            return
        approved = data.get("approved", False)
        secret = data.get("secret")
        if not isinstance(approved, bool) or not (secret is None or isinstance(secret, str)):
            raise ValueError("History undo decision needs a boolean approved and an optional text secret.")
        runtime.resolve_history_undo(str(data.get("actionToken", "")), approved=approved, secret=secret)
        return

    if action_id == SETTINGS_RELOAD_ACTION:
        runtime.reload_settings()
        runtime.apply_device_link_settings()
        return

    if action_id in LINK_ACTIONS:
        handle_link_action(runtime, action_id, payload)
        return

    if action_id == INPUT_METER_START_ACTION:
        runtime.start_input_meter()
        return

    if action_id == INPUT_METER_STOP_ACTION:
        runtime.stop_input_meter()
        return

    if action_id == WAKE_LISTENER_START_ACTION:

        runtime.start_wake_listener()


        return

    if action_id == WAKE_LISTENER_STOP_ACTION:
        runtime.stop_wake_listener()
        emit(
            "wake_word_stopped",
            "ready",
            "Qronos wake-word listener stopped.",
        )
        return

    if action_id == STOP_RESPONSE_ACTION:
        result = runtime.request_stop_response()

        emit(
            "voice_stop_acknowledged",
            "ready",
            json.dumps(
                result,
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )
        return

    if action_id == VOICE_PLAYBACK_STARTED_ACTION:
        raw_playback_id = payload.get(
            "playbackId",
            0,
        )

        try:
            playback_id = int(
                raw_playback_id
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Playback start needs a valid playbackId."
            ) from exc

        if playback_id <= 0:
            raise ValueError(
                "Playback start needs a positive playbackId."
            )

        accepted = (
            runtime.mark_voice_playback_started(
                playback_id
            )
        )

        if accepted:
            emit(
                "voice_playback_started_acknowledged",
                "ready",
                str(playback_id),
            )
        else:
            emit(
                "runtime_warning",
                "warning",
                (
                    "Ignored stale voice playback start: "
                    f"{playback_id}"
                ),
            )

        return

    if action_id == VOICE_PLAYBACK_COMPLETE_ACTION:
        raw_playback_id = payload.get(
            "playbackId",
            0,
        )

        try:
            playback_id = int(
                raw_playback_id
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Playback completion needs a valid playbackId."
            ) from exc

        if playback_id <= 0:
            raise ValueError(
                "Playback completion needs a positive playbackId."
            )

        accepted = (
            runtime.mark_voice_playback_complete(
                playback_id
            )
        )

        if accepted:
            emit(
                "voice_playback_acknowledged",
                "ready",
                str(playback_id),
            )
        else:
            emit(
                "runtime_warning",
                "warning",
                (
                    "Ignored stale voice playback completion: "
                    f"{playback_id}"
                ),
            )

        return

    if action_id == APP_WORKFLOW_UNCERTAIN_ACTION:
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("Uncertain-attempt request data must be an object.")
        acknowledge = data.get("acknowledge")
        if acknowledge is not None and (not isinstance(acknowledge, str) or len(acknowledge) > 128):
            raise ValueError("acknowledge must be one operation key.")
        runtime._ensure_app_workflows().report_uncertain(acknowledge)
        return

    if action_id == APP_WORKFLOW_DECISION_ACTION:
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("Workflow decision data must be an object.")
        action_token = str(data.get("actionToken", "")).strip()
        approved = data.get("approved", False)
        if not isinstance(approved, bool):
            raise ValueError("Workflow decision approved must be boolean.")
        secret = data.get("secret")
        if secret is not None and (not isinstance(secret, str) or len(secret) > 256):
            # Never echo the value; only its shape is wrong.
            raise ValueError("Workflow decision secret must be a short string.")
        worker = threading.Thread(
            target=lambda: runtime.resolve_app_workflow_decision(
                action_token,
                approved=approved,
                secret=secret,
            ),
            name="qronos-app-workflow-decision",
            daemon=True,
        )
        worker.start()
        return

    if action_id == FILE_ACTION_DECISION_ACTION:
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise ValueError(
                "File action decision data must be an object."
            )

        action_token = str(
            data.get("actionToken", "")
        ).strip()
        approved = data.get("approved", False)

        if not isinstance(approved, bool):
            raise ValueError(
                "File action decision approved must be boolean."
            )

        worker = threading.Thread(
            target=lambda: runtime.resolve_file_action_decision(
                action_token,
                approved=approved,
            ),
            name="qronos-file-action-decision",
            daemon=True,
        )
        worker.start()
        return

    if action_id == PUSH_TO_TALK_ACTION:
        worker = threading.Thread(
            target=runtime.push_to_talk,
            name="qronos-push-to-talk",
            daemon=True,
        )
        worker.start()
        return

    if action_id == LOOK_AT_SCREEN_ACTION:
        raw_data = payload.get(
            "data",
            {},
        )

        if raw_data is None:
            raw_data = {}

        if not isinstance(
            raw_data,
            dict,
        ):
            raise ValueError(
                "Look-at-screen action data must be an object."
            )

        # Backward-compatible fallback for older direct bridge tests.
        source = (
            raw_data
            if raw_data
            else payload
        )

        approval_id = source.get("approvalId", "")
        if approval_id is None:
            approval_id = ""
        if not isinstance(approval_id, str):
            raise ValueError("Look-at-screen approvalId must be a string.")
        direct = source.get("direct", False)
        if not isinstance(direct, bool):
            raise ValueError("Look-at-screen direct must be true or false.")
        if approval_id and direct:
            raise ValueError(
                "Look-at-screen is either a prompt answer or a direct read, not both."
            )

        worker = threading.Thread(
            target=lambda: runtime.look_at_screen(
                approved=bool(
                    source.get(
                        "approved",
                        False,
                    )
                ),
                question=str(
                    source.get(
                        "question",
                        "",
                    )
                ),
                window_only=bool(
                    source.get(
                        "windowOnly",
                        False,
                    )
                ),
                approval_id=approval_id.strip(),
                direct=direct,
            ),
            name="qronos-look-at-screen",
            daemon=True,
        )
        worker.start()
        return

    emit(
        "runtime_warning",
        "warning",
        f"Unsupported runtime action: {action_id}",
    )


class LinkRequestError(Exception):
    """A Device Link request refused before acting; ``code`` is all the desktop sees."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_LINK_REQUEST_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_LINK_DEVICE_ID = re.compile(r"^[0-9a-f]{16}$")
_LINK_ACTION_KEYS = {
    LINK_STATUS_ACTION: frozenset(),
    LINK_DEVICES_ACTION: frozenset(),
    LINK_PAIR_OPEN_ACTION: frozenset({"requestNonce", "name"}),
    LINK_PAIR_CANCEL_ACTION: frozenset(),
    LINK_REVOKE_ACTION: frozenset({"deviceId"}),
    LINK_REMOTE_ACTION: frozenset({"deviceId", "enabled", "days"}),
    LINK_RELAY_SET_ACTION: None,  # refused whole until the WAN gate is accepted
    LINK_SECURITY_RESET_ACTION: frozenset(),
}


def _link_request(action_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data", {})

    if not isinstance(data, dict):
        raise LinkRequestError("invalid_request")

    allowed = _LINK_ACTION_KEYS[action_id]

    if allowed is not None and not set(data) <= allowed:
        raise LinkRequestError("invalid_request")

    return data


def _link_device_id(data: dict[str, Any]) -> str:
    value = data.get("deviceId")

    if type(value) is not str or not _LINK_DEVICE_ID.match(value):
        raise LinkRequestError("invalid_request")

    return value


def _link_device_name(data: dict[str, Any]) -> str:
    value = data.get("name", "")

    if type(value) is not str:
        raise LinkRequestError("invalid_request")

    value = value.strip()

    if len(value) > 40 or any(_refused_name_character(character) for character in value):
        raise LinkRequestError("invalid_request")

    return value


def _refused_name_character(character: str) -> bool:
    """Control, line-break and bidi-override characters; Persian joiners are fine."""
    code = ord(character)
    return (
        code < 0x20
        or 0x7F <= code <= 0x9F
        or code in (0x2028, 0x2029)
        or 0x202A <= code <= 0x202E
        or 0x2066 <= code <= 0x2069
    )


def handle_link_action(
    runtime: QronosRuntime,
    action_id: str,
    payload: dict[str, Any],
) -> None:
    """The desktop's Device Link page: thin, validated calls on the one LinkRuntime.

    Nothing is acted on until the request has exactly the expected shape. A
    refusal is a typed ``link_error`` carrying a code, never the payload or an
    exception's text. The pairing URI holds the new phone's key: it is sent
    once, tagged with the requesting window's nonce, and never logged or kept.
    """
    try:
        data = _link_request(action_id, payload)

        if action_id == LINK_RELAY_SET_ACTION:
            raise LinkRequestError("remote_unavailable")

        link = runtime._device_link()

        if action_id == LINK_PAIR_OPEN_ACTION:
            nonce = data.get("requestNonce")

            if type(nonce) is not str or not _LINK_REQUEST_NONCE.match(nonce):
                raise LinkRequestError("invalid_request")

            name = _link_device_name(data)

            if not link.running:
                raise LinkRequestError("not_running")

            uri = link.open_pairing(name)
            runtime._notify("link_pairing", "ready", json.dumps({"requestNonce": nonce, "uri": uri}))
        elif action_id == LINK_PAIR_CANCEL_ACTION:
            link.cancel_pairing()
        elif action_id == LINK_REVOKE_ACTION:
            # Works with the link off too: a lost phone is revoked at once.
            link.revoke(_link_device_id(data))
        elif action_id == LINK_REMOTE_ACTION:
            device_id = _link_device_id(data)
            enabled = data.get("enabled")
            days = data.get("days")

            if type(enabled) is not bool or days is not None:
                raise LinkRequestError("invalid_request")

            if enabled:
                # Remote access needs the WAN gate; switching it off is always allowed.
                raise LinkRequestError("remote_unavailable")

            link.set_remote_access(device_id, False)
        elif action_id == LINK_SECURITY_RESET_ACTION:
            # The person at this PC clears the monitor's latches. Only this
            # desktop action reaches it; nothing on the link can. A link that
            # is switched on starts again, with phone questions answerable.
            link.settings = runtime._link_runtime_settings()
            if link.settings.link_enabled:
                runtime._ensure_text_runtime()
            link.reset_security()
    except LinkRequestError as refused:
        runtime._notify("link_error", "warning", json.dumps({"action": action_id, "code": refused.code}))
        return
    except Exception as exc:  # noqa: BLE001 - typed for the desktop, details stay here
        from core.link_devices import UnknownDevice

        code = "unknown_device" if isinstance(exc, UnknownDevice) else "failed"
        runtime._notify("link_error", "warning", json.dumps({"action": action_id, "code": code}))
        return

    runtime._publish_link_status()


def handle_text_message(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    text = str(
        payload.get(
            "text",
            "",
        )
    ).strip()
    request_id = str(
        payload.get(
            "requestId",
            "",
        )
    ).strip()

    if not text:
        raise ValueError(
            "Text message must not be empty."
        )

    if len(text) > MAX_TEXT_MESSAGE_LENGTH:
        raise ValueError(
            "Text message exceeds the "
            f"{MAX_TEXT_MESSAGE_LENGTH} character limit."
        )

    if not request_id:
        raise ValueError(
            "Text request id must not be empty."
        )

    if (
        len(request_id)
        > MAX_TEXT_REQUEST_ID_LENGTH
    ):
        raise ValueError(
            "Text request id exceeds the "
            f"{MAX_TEXT_REQUEST_ID_LENGTH} character limit."
        )

    if runtime.runtime_paused:
        runtime._notify(
            "text_error",
            "paused",
            _text_event_message(
                request_id,
                error="Qronos runtime is temporarily paused.",
            ),
        )
        return

    worker = threading.Thread(
        target=runtime.run_text_turn,
        args=(
            text,
            request_id,
        ),
        name="qronos-text-turn",
        daemon=True,
    )
    worker.start()


#: The longest summary the desktop may submit. A queue entry is shown in a
#: narrow panel, and an unbounded string arriving over a pipe is somebody
#: else's memory to spend.
MAX_SUMMARY_LENGTH = 200

#: Larger than any card Qronos targets. A bound rather than a meaningful
#: limit: the number arrives over a pipe and is used in arithmetic.
MAX_REQUIRED_VRAM_MB = 1_048_576

#: How many conversation requests may wait at once. Each one holds its
#: request text and a bounded slice of conversation context in memory.
MAX_QUEUED_CONVERSATIONS = 20

#: Set to "1" to enable the commands that only exist for demonstrating the
#: queue. Off by default, and off in anything a user installs, so a line
#: arriving on stdin cannot reach them.
DEMO_VARIABLE = "QRONOS_QUEUE_DEMO"


def _demo_is_enabled() -> bool:
    return os.environ.get(DEMO_VARIABLE, "").strip() == "1"

#: Overrides the pump's interval. Not a user-facing setting and not part of the
#: protocol — the tick rate is an implementation detail. It exists so a test
#: can make the pump genuinely busy in a second rather than in a minute, which
#: is the only way to exercise the shutdown race. ``resolve_python`` in the
#: Rust bridge reads ``PYTHON`` the same way.
TICK_SECONDS_VARIABLE = "QRONOS_QUEUE_TICK_SECONDS"


def _scheduler_config_from_environment() -> SchedulerConfig:
    raw = os.environ.get(TICK_SECONDS_VARIABLE, "").strip()

    if not raw:
        return DEFAULT_SCHEDULER_CONFIG

    try:
        seconds = float(raw)
    except ValueError:
        return DEFAULT_SCHEDULER_CONFIG

    if seconds <= 0:
        return DEFAULT_SCHEDULER_CONFIG

    return SchedulerConfig(tick_seconds=seconds)

_WEIGHTS = {weight.value: weight for weight in Weight}


def _task_id_from(payload: dict[str, Any]) -> str:
    task_id = str(payload.get("taskId", "")).strip()

    if not task_id:
        raise ValueError("This command needs a taskId.")

    return task_id


def handle_queue_list(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    """Send the queue as it stands. Also what first starts the scheduler."""
    runtime.ensure_scheduler()
    runtime.emit_queue_view()


def handle_queue_submit(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    """
    Queue a conversation request to be answered when the machine allows.

    ``text`` is the request and ``requestId`` correlates the answer with it.
    A payload carrying only ``summary`` — what the desktop's queue command
    has always sent — is the same thing with the summary as the request and
    a generated id, so every queued item is real work with a real answer.

    Only Fast/Heavy reasoning is accepted. Anything routing to an action,
    vision or the browser is refused here, before it is queued: those need
    the user present to approve them, and the queue never runs them.
    """
    if runtime._reject_if_runtime_paused("queue_submit"):
        return

    raw_text = payload.get("text", payload.get("summary", ""))

    if not isinstance(raw_text, str):
        raise ValueError("A queued request must be text.")

    text = raw_text.strip()

    if not text:
        raise ValueError("A queued task must say what it is.")

    limit = (
        MAX_TEXT_MESSAGE_LENGTH
        if "text" in payload
        else MAX_SUMMARY_LENGTH
    )

    if len(text) > limit:
        raise ValueError(
            f"A queued request may be at most {limit} characters."
        )

    raw_request_id = payload.get("requestId")

    if raw_request_id is None:
        request_id = f"queue-{uuid.uuid4().hex[:12]}"
    elif not isinstance(raw_request_id, str) or not raw_request_id.strip():
        raise ValueError("A queued request id must be non-empty text.")
    elif len(raw_request_id.strip()) > MAX_TEXT_REQUEST_ID_LENGTH:
        raise ValueError(
            "A queued request id may be at most "
            f"{MAX_TEXT_REQUEST_ID_LENGTH} characters."
        )
    else:
        request_id = raw_request_id.strip()

    weight_name = str(payload.get("weight", "light")).strip().lower()
    weight = _WEIGHTS.get(weight_name)

    if weight is None:
        raise ValueError(
            f"Unknown task weight {weight_name!r}; "
            f"expected one of {sorted(_WEIGHTS)}."
        )

    needs = payload.get("requiredVramMb")

    if needs is None:
        # A task that did not say assumes it will load the brain its route
        # implies, which is the only kind of work the queue runs.
        needed_mb = None
    elif isinstance(needs, bool) or not isinstance(needs, int):
        raise ValueError("requiredVramMb must be a whole number of megabytes.")
    elif not 0 <= needs <= MAX_REQUIRED_VRAM_MB:
        raise ValueError(
            f"requiredVramMb must be between 0 and {MAX_REQUIRED_VRAM_MB}."
        )
    else:
        needed_mb = needs

    # Captured before the request itself is recorded, as a direct text turn
    # does, so the request is not duplicated into its own context.
    context = runtime._conversation_brain_context()

    runtime.submit_queued_conversation(
        text=text,
        request_id=request_id,
        context=tuple(context),
        user_message_recorded=False,
        # The client may ask for more room than the route implies, never
        # less: a Heavy route always holds the heavy slot.
        heavy_requested=weight is Weight.HEAVY,
        required_mb=needed_mb,
    )


def handle_queue_cancel(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    """
    Cancel a task, and tell its conversation — once, and only if true.

    A request whose answer is already committed cannot be un-sent, so that
    cancel is refused and reported as refused, not as a cancellation.
    """
    task_id = _task_id_from(payload)
    scheduler = runtime.ensure_scheduler()
    work = scheduler.work_for(task_id)
    was_running = work is not None and any(
        running is work for running in scheduler.running_work()
    )

    if not scheduler.cancel(task_id):
        runtime._notify(
            "queue_cancel_refused",
            "warning",
            _compact(
                {
                    "taskId": task_id,
                    "detail": (
                        "This request has already been answered and "
                        "cannot be cancelled."
                    ),
                }
            ),
        )
        return

    if isinstance(work, QueuedConversationWork):
        runtime._notify_queued_conversation(
            work,
            ConversationPhase.CANCELLED,
            {
                "whileRunning": was_running,
                # Close only this work item's local request connection. Late
                # output is discarded; daemon GPU release is asynchronous.
                "detail": (
                    "Cancelled while running. No answer was attached; the "
                    "local model request already under way is closed and "
                    "late output is discarded. No answer will be committed. "
                    "GPU release may take longer."
                    if was_running
                    else "Cancelled before it ran. Nothing was executed."
                ),
            },
        )


def handle_queue_override(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    if runtime._reject_if_runtime_paused("queue_override"):
        return

    """
    Run it anyway — if only politeness was in the way.

    The result is not returned to the caller. It arrives as an event, because
    a refused override has to be visible to whoever is looking at the queue
    and not only to whoever pressed the button.
    """
    runtime.ensure_scheduler().override(_task_id_from(payload))


def handle_queue_set_paused(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    paused = payload.get("paused")

    if not isinstance(paused, bool):
        raise ValueError("queue_set_paused needs paused to be true or false.")

    if runtime.runtime_paused and not paused:
        runtime._reject_if_runtime_paused("queue_resume")
        return

    runtime.ensure_scheduler().set_paused(paused)

    # The user's hold is remembered across restarts; Qronos's own pause is
    # not, and never reaches this record.
    runtime._ensure_queue_journal().set_paused(paused)
    runtime._report_storage_status()
    runtime.emit_queue_view()


def handle_queue_pause_task(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    """Hold one waiting request until the user resumes it."""
    task_id = _task_id_from(payload)
    scheduler = runtime.ensure_scheduler()

    if not scheduler.pause_task(task_id):
        runtime._notify(
            "queue_task_refused",
            "warning",
            _compact(
                {
                    "taskId": task_id,
                    "action": "pause",
                    "detail": "Only a waiting request can be paused.",
                }
            ),
        )
        return

    work = scheduler.work_for(task_id)

    if isinstance(work, QueuedConversationWork):
        runtime._notify_queued_conversation(
            work,
            ConversationPhase.PAUSED,
            {"detail": "Paused by you. It waits until you resume it."},
        )


def handle_queue_resume_task(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    """
    Explicitly run a paused or interrupted request again.

    The only way an interrupted request ever runs again. It goes back in
    line like new work: the route, the machine and the resource policy are
    all checked afresh before it starts.
    """
    task_id = _task_id_from(payload)

    if runtime._reject_if_runtime_paused("queue_resume_task"):
        return

    scheduler = runtime.ensure_scheduler()

    if not scheduler.resume_task(task_id):
        runtime._notify(
            "queue_task_refused",
            "warning",
            _compact(
                {
                    "taskId": task_id,
                    "action": "resume",
                    "detail": (
                        "Only a paused or interrupted request can be resumed."
                    ),
                }
            ),
        )
        return

    work = scheduler.work_for(task_id)

    if isinstance(work, QueuedConversationWork):
        runtime._notify_queued_conversation(
            work,
            ConversationPhase.QUEUED,
            {"reason": "resumed"},
        )


def handle_runtime_set_paused(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    paused = payload.get("paused")

    if not isinstance(paused, bool):
        raise ValueError(
            "runtime_set_paused needs paused to be true or false."
        )

    runtime.set_runtime_paused(paused)


def handle_queue_debug_load(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    if runtime._reject_if_runtime_paused("queue_debug_load"):
        return

    """
    Tell the monitor what to think the machine is doing.

    This exists for one reason. Demonstrating that Qronos *holds* work needs
    only a busy machine, and a busy machine is easy to come by. Demonstrating
    that it *releases* work needs a machine that stops being busy, and the only
    honest ways to arrange that are to wait an unbounded amount of time or to
    close something of the user's — and closing something of the user's is the
    exact thing this whole feature exists to avoid.

    So the sensor is substituted rather than the machine. Everything downstream
    is real: the same monitor, dwell, scheduler, governor, floor, protocol and
    interface. Only the readings are supplied rather than measured, and any
    write-up of a demonstration using this has to say so.

    Off unless QRONOS_QUEUE_DEMO is set to 1.
    """
    if not _demo_is_enabled():
        raise ValueError(
            "queue_debug_load is only available when "
            f"{DEMO_VARIABLE}=1."
        )

    state = str(payload.get("state", "")).strip().lower()

    if state not in ("free", "busy"):
        raise ValueError("queue_debug_load needs state to be free or busy.")

    scheduler = runtime.ensure_scheduler()
    monitor = scheduler.monitor
    loaded = state == "busy"

    # Replace what the monitor believes rather than adding to it. Appending
    # alone does not work: readings from an earlier call are still inside the
    # window, so a later "free" and an earlier "busy" simply argue with each
    # other and the verdict sits in the hysteresis band. Observed exactly that
    # way before this line existed.
    monitor.reset()

    # Enough readings to satisfy the longer of the two dwells, so the verdict
    # actually moves. Dated backwards from now, never forwards: a reading
    # stamped in the future outlives every real one taken after it.
    span = max(
        monitor.config.busy_dwell_seconds,
        monitor.config.free_dwell_seconds,
    )
    interval = monitor.config.sample_interval_seconds
    count = int(span / interval) + 2
    now = monitor.clock()

    for index in range(count):
        monitor.offer(
            LoadSample(
                at=now - (count - 1 - index) * interval,
                decision=(
                    ResourceDecision.BLOCK
                    if loaded
                    else ResourceDecision.ALLOW
                ),
                cpu_percent=95.0 if loaded else 5.0,
                ram_percent=43.0,
                vram_free_mb=256 if loaded else 15_000,
                gpu_utilization_percent=99 if loaded else 1,
                gpu_temperature_c=45,
            )
        )

    emit(
        "queue_hold_state",
        "busy" if loaded else "ready",
        _compact(
            {
                "level": monitor.snapshot().level.value,
                "simulated": True,
            }
        ),
    )


def handle_media_index(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    """Ask the media host for one bounded index pass (only numbers are read from the request)."""
    host = runtime.ensure_media_host()
    answer = host.submit_index(
        max_seconds=float(payload.get("maxSeconds", 120.0)),
        max_stages=int(payload.get("maxStages", 50)),
    )
    runtime._notify("media_index", "ready" if answer.get("accepted") else "warning", _compact(answer))


def handle_media_status(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    host = runtime._media_host
    status = host.status() if host is not None else {"workerStarted": False}
    runtime._notify("media_status", "ready", _compact(status))


def handle_device_hub_action(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    """One typed Device Hub operation (the data object only; validated in core/device_hub.py)."""
    runtime.device_hub().handle(payload.get("data"))


def handle_media_action(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    """One typed media product operation (the data object only; validated in qronos_media.product)."""
    runtime.media_product().handle(payload.get("data"))


MEDIA_COMMANDS: dict[str, Callable[[QronosRuntime, dict[str, Any]], None]] = {
    "media_index": handle_media_index,
    "media_status": handle_media_status,
}


QUEUE_COMMANDS: dict[str, Callable[[QronosRuntime, dict[str, Any]], None]] = {
    "queue_debug_load": handle_queue_debug_load,
    "queue_list": handle_queue_list,
    "queue_submit": handle_queue_submit,
    "queue_cancel": handle_queue_cancel,
    "queue_override": handle_queue_override,
    "queue_set_paused": handle_queue_set_paused,
    "queue_pause_task": handle_queue_pause_task,
    "queue_resume_task": handle_queue_resume_task,
}


SUPERVISED_FLAG = "--supervised"
#: After the supervisor asks for shutdown (or is gone), the command loop ends
#: when its input does. If it has not within this, the process ends anyway: a
#: supervised bridge never outlives its supervisor's request.
SUPERVISED_EXIT_BACKSTOP_SECONDS = 30.0


class CommandLoopHealth:
    """
    Whole seconds the command loop has spent inside the command it is
    handling now (0 while it waits for the next). Sent with each heartbeat,
    so the supervisor can report a loop stuck in one command; the heartbeat
    thread alone would keep beating through that.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._since: float | None = None
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self._since = self._clock()

    def leave(self) -> None:
        with self._lock:
            self._since = None

    def busy_seconds(self) -> int:
        with self._lock:
            since = self._since
        return 0 if since is None else int(self._clock() - since)


COMMAND_LOOP = CommandLoopHealth()


def _command_lines(stream):
    """The command loop's input; marks each command busy until the next is asked for."""
    for raw_line in stream:
        COMMAND_LOOP.enter()
        yield raw_line
        COMMAND_LOOP.leave()


def start_supervision(stream) -> Any:
    """
    Join the supervisor that started this bridge (``core/supervisor_relay.py``).

    The supervisor's bootstrap is the first line of standard input, read here
    before the command loop reads anything. The supervisor's process id is
    checked on the pipe and both sides prove the session key; any failure
    raises, and a supervised bridge then exits rather than run unsupervised.
    """
    from core.supervisor import SupervisedRuntimeClient, read_bootstrap

    bootstrap = read_bootstrap(stream)

    def on_shutdown(reason: str) -> None:
        # The supervisor also sends "shutdown" on standard input, which the
        # command loop handles in order. This is only the backstop.
        backstop = threading.Timer(SUPERVISED_EXIT_BACKSTOP_SECONDS, os._exit, args=(70,))
        backstop.daemon = True
        backstop.start()

    client = SupervisedRuntimeClient(
        bootstrap, on_shutdown=on_shutdown, busy_seconds=COMMAND_LOOP.busy_seconds,
    )
    client.start()
    return client


def main() -> int:
    supervision = None

    # Before ANY store opens (core/startup_admission.py): own the bridge singleton,
    # refuse while an update runs, resolve an interrupted update, then hold the shared
    # writer admission for this process's life. The frozen entry point has already done
    # this before importing this module; then this is a no-op.
    from core import startup_admission

    try:
        startup_admission.enter("bridge", instance=True)
    except startup_admission.StartupRefused as refused:
        emit("runtime_error", "error", f"{refused} ({refused.code})")
        return startup_admission.STARTUP_REFUSED_EXIT

    if SUPERVISED_FLAG in sys.argv[1:]:
        try:
            supervision = start_supervision(sys.stdin.buffer)
        except Exception as exc:
            emit(
                "runtime_error",
                "error",
                f"The Qronos runtime could not join its supervisor ({type(exc).__name__}).",
            )
            return 3

    try:
        return _run()
    finally:
        if supervision is not None:
            supervision.stop("runtime exiting")


def _run() -> int:
    runtime = QronosRuntime()

    # The real desktop must not depend on a later Settings write or a frontend
    # timing race to begin listening. Tauri marks the desktop process with an
    # explicit environment flag; raw bridge tests keep their no-side-effect
    # startup behavior because they do not set it.
    if (
        _desktop_wake_bootstrap_enabled()
        and runtime.wake_word_enabled
    ):
        bootstrap_started = time.perf_counter()

        try:
            _preload_openwakeword_for_desktop()
            runtime.start_wake_listener()

            if runtime._wake_ready.wait(5.0):
                emit(
                    "wake_bootstrap_ready",
                    "ready",
                    json.dumps(
                        {
                            "seconds": round(
                                time.perf_counter()
                                - bootstrap_started,
                                3,
                            ),
                            "beforeRuntimeReady": True,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )
            else:
                emit(
                    "wake_bootstrap_pending",
                    "warning",
                    json.dumps(
                        {
                            "seconds": round(
                                time.perf_counter()
                                - bootstrap_started,
                                3,
                            ),
                            "beforeRuntimeReady": False,
                            "listenerRunning":
                                runtime.wake_listener_running,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )
        except Exception as exc:
            # Wake retry/recovery remains inside QronosRuntime. A bootstrap
            # failure must not prevent the rest of the desktop bridge from
            # becoming usable.
            emit(
                "wake_bootstrap_failed",
                "warning",
                str(exc),
            )

    emit(
        "runtime_ready",
        "ready",
        "Qronos runtime bridge is ready.",
    )

    # Reviewed status hooks (core/runtime_hooks.py), on their own daemon
    # thread with a deadline each: they can never hold up the command loop.
    run_startup_status_hooks(emit)

    # Paired phones, when the user turned the link on. Text-only mode too;
    # off by default, and then nothing is built.
    runtime.start_device_link()

    try:
        for raw_line in _command_lines(sys.stdin):
            line = raw_line.strip()

            if not line:
                continue

            try:
                payload = parse_payload(
                    line
                )
                command = payload[
                    "command"
                ]

                if command == "ping":
                    emit(
                        "runtime_pong",
                        "ready",
                        "Qronos runtime bridge responded.",
                    )
                    continue

                if command == RUNTIME_SET_PAUSED_COMMAND:
                    handle_runtime_set_paused(
                        runtime,
                        payload,
                    )
                    continue

                if command == TEXT_MESSAGE_COMMAND:
                    handle_text_message(
                        runtime,
                        payload,
                    )
                    continue

                if command == "action":
                    handle_action(
                        runtime,
                        payload,
                    )
                    continue

                handler = QUEUE_COMMANDS.get(command) or MEDIA_COMMANDS.get(command)

                if handler is not None:
                    handler(runtime, payload)
                    continue

                if command == "shutdown":
                    emit(
                        "runtime_stopping",
                        "stopping",
                        "Qronos runtime bridge is stopping.",
                    )
                    return 0

                emit(
                    "runtime_warning",
                    "warning",
                    f"Unknown runtime command: {command}",
                )

            except Exception as exc:
                emit(
                    "runtime_error",
                    "error",
                    str(exc),
                )

    finally:
        runtime.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
