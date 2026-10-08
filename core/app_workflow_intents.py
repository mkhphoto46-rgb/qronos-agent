"""
Deterministic parsing of the two application-workflow requests.

    "آخرین فایل «سارا احمدی» را در تلگرام دانلود کن"
    "download the latest file from "Sara Ahmadi" on Telegram"
    "فایل report.pdf را با چاپگر «Office Laser» چاپ کن"
    "print report.pdf on printer Office Laser"

The parser extracts entities; it never resolves them. A contact name is
matched later against Telegram exactly, a file reference goes to the file
resolver, and a printer name is matched exactly against installed printers.
When an entity is missing or the sentence could mean more than one thing, the
result is a :class:`WorkflowClarification` with a Persian question, never a
guess. Quoted names (« », " ", “ ”) are taken verbatim and are the reliable way
to name a contact or printer whose name contains words such as "on" or "در".
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from core.persian_text import normalise


@dataclass(frozen=True)
class TelegramDownloadIntent:
    contact: str


@dataclass(frozen=True)
class PrintIntent:
    file_reference: str
    printer: str | None  # None: the person did not name one; the default must be confirmed
    copies: int = 1


@dataclass(frozen=True)
class WorkflowClarification:
    question: str  # Persian, shown to the person


WorkflowIntent = TelegramDownloadIntent | PrintIntent | WorkflowClarification


_QUOTED = re.compile(r"[«\"“]([^«»\"“”]{1,128})[»\"”]")

_TELEGRAM = re.compile(r"(?:\btelegram\b|تلگرام)", re.IGNORECASE)
_DOWNLOAD = re.compile(r"(?:\bdownload\b|\bsave\b|دانلود|ذخیره|بگیر|بارگیری)", re.IGNORECASE)
_LATEST = re.compile(r"(?:\blatest\b|\bnewest\b|\blast\b|\bmost recent\b|آخرین|جدیدترین|تازه‌ترین|تازه ترین)", re.IGNORECASE)
_FILE_WORD = re.compile(r"(?:\bfile\b|\battachment\b|\bdocument\b|فایل|پیوست|سند)", re.IGNORECASE)
_SEND_WORDS = re.compile(r"(?:\bsend\b|\breply\b|\bforward\b|بفرست|ارسال کن|جواب بده|فوروارد)", re.IGNORECASE)
_ALL_WORDS = re.compile(r"(?:\ball\b|\bevery\b|همه|تمام)", re.IGNORECASE)

# A print *request*: the English verb, or the Persian noun followed by a verb
# ("چاپ کن"). "پرینتر" (printer) alone is not a request.
_PRINT = re.compile(r"(?:\bprint\b(?!er)|(?:چاپ|پرینت)\s*(?:کن|کنید|بگیر|بگیرید)(?!\w))", re.IGNORECASE)
_PRINTER_WORD = re.compile(r"(?:\bprinter\b|چاپگر|پرینتر)", re.IGNORECASE)
_COPIES = re.compile(r"(?:\b([0-9]{1,3})\s*(?:copies|copy)\b|([0-9]{1,3})\s*(?:نسخه|بار)|(دو|سه|چهار|پنج)\s*(?:نسخه|بار))", re.IGNORECASE)
_PERSIAN_NUMBERS = {"دو": 2, "سه": 3, "چهار": 4, "پنج": 5}

# Unquoted contact: the words after "from"/"از" up to "on/in Telegram"/"در تلگرام"/"را"/"توی".
_EN_CONTACT = re.compile(r"\bfrom\s+(?P<name>.+?)\s+(?:on|in)\s+telegram\b", re.IGNORECASE)
_FA_CONTACT = re.compile(r"(?:^|\s)از\s+(?P<name>.+?)\s+(?:را\s+)?(?:در|توی|تو|از)\s+تلگرام")
_FA_CONTACT_GENITIVE = re.compile(r"(?:فایل|پیوست)\s+(?P<name>.+?)\s+(?:را\s+)?(?:در|توی|تو|از)\s+تلگرام")

_EN_PRINT = re.compile(r"\bprint\s+(?P<file>.+?)(?:\s+(?:on|to|with|using)\s+(?:the\s+)?(?:printer\s+)?(?P<printer>.+?))?\s*[.!]?$", re.IGNORECASE)
_FA_PRINT = re.compile(r"(?:فایل\s+)?(?P<file>.+?)\s+را\s+(?:با|روی|توسط|به)\s+(?:چاپگر|پرینتر)\s+(?P<printer>.+?)\s+(?:چاپ|پرینت)")
_FA_PRINT_DEFAULT = re.compile(r"(?:فایل\s+)?(?P<file>.+?)\s+را\s+(?:\S+\s+){0,3}?(?:چاپ|پرینت)")


# Function words that can land where a name is expected.
_NOT_A_NAME = frozenset({"را", "این", "آن", "اون", "it", "this", "that", "them"})


def _clean(value: str) -> str:
    return " ".join(str(value or "").strip(" \t\"'«»“”.,،؟?!").split())


def _strip_leading(value: str, words: tuple[str, ...]) -> str:
    text = value
    changed = True
    while changed:
        changed = False
        for word in words:
            if text.casefold().startswith(word + " "):
                text = text[len(word) + 1:]
                changed = True
    return text


def parse_workflow_intent(text: str) -> WorkflowIntent | None:
    """Return a workflow intent, a clarification, or None when neither applies."""
    raw = normalise(str(text or ""), for_search=False)
    if not raw:
        return None
    if _TELEGRAM.search(raw) and (_DOWNLOAD.search(raw) or (_LATEST.search(raw) and _FILE_WORD.search(raw))):
        return _telegram(raw)
    if _PRINT.search(raw):
        return _print(raw)
    return None


def _telegram(raw: str) -> WorkflowIntent:
    if _SEND_WORDS.search(raw):
        return WorkflowClarification(
            "این درخواست هم دانلود و هم ارسال پیام دارد. Qronos پیام نمی‌فرستد؛ "
            "لطفاً فقط دانلود را درخواست کنید."
        )
    if _ALL_WORDS.search(raw):
        return WorkflowClarification(
            "Qronos فقط جدیدترین فایل دریافتی یک مخاطب را دانلود می‌کند، نه همه فایل‌ها را."
        )
    quoted = [_clean(item) for item in _QUOTED.findall(raw) if _clean(item)]
    if len(quoted) > 1:
        return WorkflowClarification("چند نام در درخواست آمده است. نام دقیق یک مخاطب را بگویید.")
    if quoted:
        return TelegramDownloadIntent(contact=quoted[0])
    for pattern in (_EN_CONTACT, _FA_CONTACT, _FA_CONTACT_GENITIVE):
        match = pattern.search(raw)
        if match:
            name = _clean(_strip_leading(_clean(match.group("name")), (
                "the", "latest", "newest", "last", "file", "attachment", "آخرین", "جدیدترین", "فایل", "پیوست", "از",
            )))
            if name and name not in _NOT_A_NAME and not _FILE_WORD.fullmatch(name) and not _LATEST.fullmatch(name):
                return TelegramDownloadIntent(contact=name)
    return WorkflowClarification(
        "نام دقیق مخاطب تلگرام را بگویید؛ بهتر است آن را داخل گیومه «» بنویسید."
    )


def _copies(raw: str) -> int | None:
    match = _COPIES.search(raw)
    if not match:
        return 1
    number = match.group(1) or match.group(2)
    if number:
        return int(number)
    return _PERSIAN_NUMBERS.get(match.group(3))


def _print(raw: str) -> WorkflowIntent:
    copies = _copies(raw)
    quoted = [_clean(item) for item in _QUOTED.findall(raw) if _clean(item)]
    file_reference = printer = None
    if len(quoted) == 2:
        file_reference, printer = quoted
    elif len(quoted) > 2:
        return WorkflowClarification("چند نام در درخواست آمده است. یک فایل و یک چاپگر را مشخص کنید.")
    else:
        match = _FA_PRINT.search(raw) or None
        if match:
            file_reference, printer = match.group("file"), match.group("printer")
        else:
            match = _EN_PRINT.search(raw)
            if match:
                file_reference, printer = match.group("file"), match.group("printer")
            else:
                match = _FA_PRINT_DEFAULT.search(raw)
                if match:
                    file_reference = match.group("file")
        if len(quoted) == 1:
            # One quoted value: it names whichever side it appears on.
            if printer and quoted[0] in printer:
                printer = quoted[0]
            elif file_reference and quoted[0] in file_reference:
                file_reference = quoted[0]
    file_reference = _clean(_strip_leading(_clean(file_reference or ""), ("the", "file", "فایل")))
    printer = _clean(_strip_leading(_clean(printer or ""), ("printer", "چاپگر", "پرینتر"))) or None
    if file_reference:
        file_reference = _COPIES.sub("", file_reference).strip()
    if not file_reference or _FILE_WORD.fullmatch(file_reference):
        return WorkflowClarification("کدام فایل را چاپ کنم؟ نام یا مسیر دقیق آن را بگویید.")
    if copies is None:
        return WorkflowClarification("تعداد نسخه‌ها را با عدد بگویید.")
    return PrintIntent(file_reference=file_reference, printer=printer, copies=copies)
