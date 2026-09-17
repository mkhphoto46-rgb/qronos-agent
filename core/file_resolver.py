from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import os
from pathlib import Path
import re
import unicodedata


class NamedFileParseError(ValueError):
    """The request was not an unambiguous natural-language OPEN_FILE request."""


class NamedFileNotFound(FileNotFoundError):
    """No safe file in the requested scope matched the spoken name."""


class NamedFileAmbiguous(LookupError):
    """More than one safe file matched strongly enough to require clarification."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        super().__init__("More than one file matched: " + ", ".join(names))


class NamedFileSearchLimit(RuntimeError):
    """A bounded recursive search stopped before the requested scope was exhausted."""


class NamedLocationParseError(ValueError):
    """The request was not an unambiguous open-folder/location command."""


class NamedLocationNotFound(FileNotFoundError):
    """The requested known folder or drive was not available."""


@dataclass(frozen=True)
class NamedFileRequest:
    name_hint: str
    folder_hint: str | None = None
    note_like: bool = False
    suffix_hint: str | None = None


@dataclass(frozen=True)
class ResolvedNamedFile:
    path: Path
    score: int
    folder_key: str
    display_name: str


@dataclass(frozen=True)
class NamedLocationRequest:
    folder_hint: str | None
    name_hint: str | None = None


@dataclass(frozen=True)
class ResolvedLocation:
    path: Path
    folder_key: str
    display_name: str


_QUOTED = re.compile(r'"([^"\r\n]+)"|«([^»\r\n]+)»')

_FILE_MARKERS = (
    "فایل", "نوت", "یادداشت", "note", "file", "document", "text file",
    "داکیومنت", "دایکیومنت",
)

_FOLDER_WORDS = (
    "فولدر", "پوشه", "folder", "directory",
)

_EXPLORER_CONTEXT_ALIASES = (
    "file explorer", "windows explorer", "explorer",
    "فایل اکسپلورر", "فایل‌اکسپلورر", "اکسپلورر",
)

_STRONG_FILE_MARKERS = (
    "فایل", "file", "نوت", "note", "یادداشت", "text file",
)

_KNOWN_FOLDER_ALIASES: dict[str, tuple[str, ...]] = {
    "desktop": (
        "desktop", "دسکتاپ", "دسکتاب", "دسک تاپ",
    ),
    "documents": (
        "documents", "داکیومنتس", "اسناد",
    ),
    "downloads": (
        "downloads", "download", "دانلود", "دانلودها", "دانلودز", "دانلود هام", "دانلود هام",
    ),
}

# H is the user's current target and is deliberately supported in both common
# Persian pronunciations. Latin A-Z works generically via "Drive X".
_PERSIAN_DRIVE_LETTERS: dict[str, tuple[str, ...]] = {
    "H": ("اچ", "ایچ", "اچ", "هِچ"),
}

_BLOCKED_SUFFIXES = frozenset({
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".psd1",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta",
    ".msi", ".msp", ".scr", ".cpl", ".reg", ".lnk", ".url",
    ".py", ".pyw", ".jar",
})

_PERSIAN_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

# Conservative spoken-English aliases that materially affect file lookup.
# This is NOT a general transliterator. It only normalises high-value filename
# tokens we have live evidence for, keeping ambiguity handling in the resolver.
_SPOKEN_TOKEN_ALIASES: dict[str, str] = {
    "نیو": "new",
    "تکس": "text",
    "تکست": "text",
    "دایکیومنت": "document",
    "داکیومنت": "document",
    "داکیومنتس": "documents",
    "لول": "level",
    "آپ": "up",
    "وان": "1",
    "یک": "1",
    "دو": "2",
    "توو": "2",
    "تری": "3",
    "سه": "3",
    "فور": "4",
    "چهار": "4",
    "فایو": "5",
    "پنج": "5",
    "سیکس": "6",
    "شش": "6",
    "سون": "7",
    "هفت": "7",
    "ایت": "8",
    "هشت": "8",
    "ناین": "9",
    "نه": "9",
    "تن": "10",
    "ده": "10",
}

_SUFFIX_ALIASES: dict[str, tuple[str, ...]] = {
    ".mp3": ("mp3", "ام پی تری", "امپی تری", "ام پی 3"),
    ".txt": ("txt", "تی اکس تی"),
    ".pdf": ("pdf", "پی دی اف"),
    ".doc": ("doc",),
    ".docx": ("docx", "داک ایکس"),
    ".wav": ("wav", "ویو"),
    ".png": ("png", "پی ان جی"),
    ".jpg": ("jpg", "جی پی جی"),
    ".jpeg": ("jpeg",),
}

_CAPABILITY_PATTERNS = (
    r"\bچه\s+فایل",
    r"\bچه\s+چیز",
    r"\bچی\s+(?:رو|را)",
    r"\bکدوم\s+فایل",
    r"\bwhat\s+(?:files?|can)\b",
    r"\bwhich\s+files?\b",
)


def _strip_marks(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def _normalise_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _strip_marks(text)
    text = text.replace("\u200c", " ")
    text = text.replace("ي", "ی").replace("ك", "ک")
    text = text.translate(_PERSIAN_DIGITS)
    text = text.replace("…", " ")
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _phonetic_relax(value: str) -> str:
    # Live STT evidence: ج/ژ may flip in otherwise-correct Persian filenames.
    # Relaxing only for ranking is safe because close matches still trigger the
    # ambiguity guard instead of blindly opening one.
    return _normalise_text(value).replace("ژ", "ج")


def _canonical_name(value: str) -> str:
    text = _normalise_text(value)
    phrase_replacements = {
        "ام پی تری": "mp3",
        "امپی تری": "mp3",
        "پی دی اف": "pdf",
        "تی اکس تی": "txt",
    }
    for source, target in phrase_replacements.items():
        text = text.replace(source, target)
    tokens = [_SPOKEN_TOKEN_ALIASES.get(token, token) for token in text.split()]
    return " ".join(tokens).strip()


def _strip_explorer_context(value: str) -> tuple[str, bool]:
    """Remove File Explorer app wording before file/folder ownership checks."""
    n = _normalise_text(value)
    found = False
    for alias in _EXPLORER_CONTEXT_ALIASES:
        a = _normalise_text(alias)
        pattern = rf"(?:^|\s){re.escape(a)}(?:\s|$)"
        if re.search(pattern, n, flags=re.IGNORECASE):
            found = True
            n = re.sub(pattern, " ", n, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", n).strip(), found


def _raw_quoted_values(text: str) -> tuple[str, ...]:
    values: list[str] = []
    for match in _QUOTED.finditer(text):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        cleaned = (value or "").strip()
        if cleaned:
            values.append(cleaned)
    return tuple(values)


def _looks_like_path(value: str) -> bool:
    cleaned = str(value or "").strip()
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", cleaned)
        or "\\" in cleaned
        or "/" in cleaned
    )


def is_computer_capability_query(text: str) -> bool:
    n = _normalise_text(text)
    if not n:
        return False
    questionish = any(re.search(pattern, n) for pattern in _CAPABILITY_PATTERNS)
    action_noun = any(marker in n for marker in (*_FILE_MARKERS, "باز", "open", "folder", "فولدر"))
    return bool(questionish and action_noun)


def _has_open_intent(normalised: str) -> bool:
    patterns = (
        r"(?:^|\s)باز\s+کن(?:\s|$)",
        r"(?:^|\s)بازش\s+کن(?:\s|$)",
        r"(?:^|\s)بازش\s+کنم(?:\s|$)",
        r"(?:^|\s)بازش\s+کنید(?:\s|$)",
        r"(?:^|\s)open(?:\s|$)",
        r"(?:^|\s)launch(?:\s|$)",
    )
    return any(re.search(pattern, normalised) for pattern in patterns)


def _has_navigation_intent(normalised: str) -> bool:
    return bool(
        _has_open_intent(normalised)
        or re.search(r"(?:^|\s)برو(?:\s|$)", normalised)
        or re.search(r"(?:^|\s)go\s+to(?:\s|$)", normalised)
    )


def _extract_drive_hint(normalised: str) -> str | None:
    match = re.search(r"\bdrive\s*([a-z])\b", normalised, flags=re.IGNORECASE)
    if match:
        return f"drive:{match.group(1).upper()}"

    match = re.search(r"\bدرایو\s+([a-z])\b", normalised, flags=re.IGNORECASE)
    if match:
        return f"drive:{match.group(1).upper()}"

    for letter, aliases in _PERSIAN_DRIVE_LETTERS.items():
        for alias in aliases:
            if re.search(rf"\bدرایو\s+{re.escape(_normalise_text(alias))}\b", normalised):
                return f"drive:{letter}"
    return None


def _extract_known_folder_hint(normalised: str) -> str | None:
    matches: list[str] = []
    for key, aliases in _KNOWN_FOLDER_ALIASES.items():
        for alias in aliases:
            a = re.escape(_normalise_text(alias))
            # Strong named folders may appear bare in an imperative, but unlike
            # R3.1 we intentionally do not treat singular "document" as a
            # Documents-folder alias. That prevented filenames such as
            # "New Text Document" from becoming two-folder ambiguity.
            if re.search(rf"(?:^|\s){a}(?:\s|$)", normalised):
                matches.append(key)
                break
    unique = sorted(set(matches))
    if len(unique) > 1:
        raise NamedFileParseError("The request names more than one known folder.")
    return unique[0] if unique else None


def _extract_folder_hint(normalised: str) -> str | None:
    drive = _extract_drive_hint(normalised)
    folder = _extract_known_folder_hint(normalised)
    if drive and folder:
        raise NamedFileParseError("The request names both a drive and a known folder.")
    return drive or folder


def _extract_suffix_hint(value: str) -> tuple[str | None, str]:
    n = _normalise_text(value)
    for suffix, aliases in _SUFFIX_ALIASES.items():
        for alias in aliases:
            a = _normalise_text(alias)
            if re.search(rf"(?:^|\s){re.escape(a)}(?:\s|$)", n):
                stripped = re.sub(
                    rf"(?:^|\s){re.escape(a)}(?:\s|$)",
                    " ",
                    n,
                    count=1,
                )
                return suffix, re.sub(r"\s+", " ", stripped).strip()
    return None, n


def _remove_location_words(value: str) -> str:
    """Remove actual location phrases without deleting filename words.

    A previous implementation removed Persian prepositions such as ``در``
    globally.  That broke perfectly valid filenames like ``در سال 1714``.
    Location prepositions are now consumed only when they are directly tied to
    a recognised drive/folder phrase.
    """
    n = _normalise_text(value)

    # Remove a preposition only together with a drive phrase.
    n = re.sub(
        r"\b(?:روی|تو|توی|داخل|در|به)\s+(?:drive\s*[a-z]|درایو\s+(?:[a-z]|اچ|ایچ|هِچ))\b",
        " ",
        n,
        flags=re.IGNORECASE,
    )
    n = re.sub(r"\bdrive\s*[a-z]\b", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\bدرایو\s+(?:[a-z]|اچ|ایچ|هِچ)\b", " ", n, flags=re.IGNORECASE)

    # Remove a preposition only together with a known-folder alias.  The bare
    # word ``در`` must remain available to be part of the filename itself.
    for aliases in _KNOWN_FOLDER_ALIASES.values():
        for alias in aliases:
            a = re.escape(_normalise_text(alias))
            n = re.sub(
                rf"\b(?:روی|تو|توی|داخل|در|به)\s+{a}\b",
                " ",
                n,
                flags=re.IGNORECASE,
            )
            n = re.sub(rf"\b{a}\b", " ", n, flags=re.IGNORECASE)

    # These are structural location nouns, not natural filename prepositions.
    n = re.sub(r"\b(?:کامپیوتر|computer|folder|فولدر|پوشه)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _tail_after_location(normalised: str, folder_hint: str | None) -> str:
    if not folder_hint:
        return ""
    spans: list[tuple[int, int]] = []
    if folder_hint.startswith("drive:"):
        for pattern in (
            r"\bdrive\s*[a-z]\b",
            r"\bدرایو\s+(?:[a-z]|اچ|ایچ|اچ|هِچ)\b",
        ):
            spans.extend((m.start(), m.end()) for m in re.finditer(pattern, normalised, flags=re.IGNORECASE))
    else:
        for alias in _KNOWN_FOLDER_ALIASES.get(folder_hint, ()):
            a = _normalise_text(alias)
            spans.extend((m.start(), m.end()) for m in re.finditer(rf"\b{re.escape(a)}\b", normalised))
    if not spans:
        return ""
    _, end = max(spans, key=lambda item: item[1])
    tail = normalised[end:]
    tail = re.sub(r"^\s*(?:کامپیوتر|computer|هم|و|روی|تو|توی|داخل|در)\b", " ", tail)
    tail = re.sub(r"\b(?:رو|را)\s+(?:برام\s+)?باز(?:ش)?\s+کن(?:م|ید)?\b.*$", " ", tail)
    tail = re.sub(r"\bبازش?\s+کن(?:م|ید)?\b.*$", " ", tail)
    tail = re.sub(r"\b(?:open|launch)\b.*$", " ", tail)
    tail = re.sub(r"^\s*(?:فایل|نوت|یادداشت|file|note)\s+", " ", tail)
    tail = re.sub(r"^\s*(?:به\s*اسم|با\s*اسم|اسمش|named)\s+", " ", tail)
    return re.sub(r"\s+", " ", tail).strip()


def _extract_unquoted_name(normalised: str, folder_hint: str | None) -> str:
    # Prefer text after an explicitly named location. This avoids treating the
    # word "document" inside a filename such as "New Text Document" as the
    # start of a new file-marker phrase.
    tail = _tail_after_location(normalised, folder_hint)
    if tail:
        return _remove_location_words(tail)

    patterns = (
        r"(?:به\s*اسم|با\s*اسم|اسمش|اسم\s+فایل|named)\s+(.+?)"
        r"(?=(?:\s+(?:اون|آن|همون|همونو)\s*(?:رو|را))|"
        r"(?:\s+(?:رو|را)\s+(?:برام\s+)?باز)|(?:\s+بازش)|$)",
        r"(?:فایل|نوت|یادداشت|file|note|document|داکیومنت|دایکیومنت)\s+(.+?)"
        r"(?=(?:\s+(?:رو|را)\s+(?:برام\s+)?باز)|(?:\s+بازش)|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalised, flags=re.IGNORECASE)
        if match:
            return _remove_location_words(match.group(1))

    # Natural drive/folder shorthand: "برو درایو اچ جرمگیری دیجیتال رو باز کن"
    # No explicit word "file" is required when a location and an explicit open
    # command are both present.
    if folder_hint:
        candidate = normalised
        candidate = re.sub(r"^.*?\b(?:برو|go)\b", " ", candidate, count=1)
        candidate = re.sub(r"\b(?:رو|را)\s+(?:برام\s+)?باز(?:ش)?\s+کن(?:م|ید)?\b.*$", " ", candidate)
        candidate = re.sub(r"\b(?:open|launch)\b", " ", candidate)
        candidate = _remove_location_words(candidate)
        candidate = re.sub(r"\b(?:فایل|نوت|یادداشت|file|note|document)\b", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if candidate:
            return candidate

    return ""


def parse_named_file_request(text: str) -> NamedFileRequest:
    raw = str(text or "").strip()
    normalised = _normalise_text(raw)

    if not normalised:
        raise NamedFileParseError("File request is empty.")
    if is_computer_capability_query(raw):
        raise NamedFileParseError("Capability questions are not file actions.")
    if not _has_open_intent(normalised):
        raise NamedFileParseError("No explicit open-file intent was found.")

    semantic, had_explorer_context = _strip_explorer_context(normalised)
    folder_hint = _extract_folder_hint(semantic)
    has_file_marker = any(marker in semantic for marker in _FILE_MARKERS)
    has_folder_marker = any(marker in semantic for marker in _FOLDER_WORDS)
    has_strong_file_marker = any(
        marker in semantic
        for marker in _STRONG_FILE_MARKERS
    )

    # "File Explorer + Downloads/Drive" is a location command, not a file
    # named Explorer.  If a real file marker remains after removing the app
    # phrase (e.g. "File Explorer Downloads file foo"), File Resolver still
    # owns the explicit file target.
    if had_explorer_context and folder_hint and not has_strong_file_marker:
        raise NamedFileParseError(
            "Explorer plus a location belongs to the location resolver."
        )

    if has_folder_marker and not has_strong_file_marker:
        raise NamedFileParseError(
            "Explicit folder requests belong to the location resolver."
        )

    quoted = _raw_quoted_values(raw)
    if quoted:
        candidate = quoted[0]
        if _looks_like_path(candidate):
            raise NamedFileParseError(
                "Exact paths belong to the exact-path File Control parser."
            )
        hint_raw = candidate
    else:
        hint_raw = _extract_unquoted_name(semantic, folder_hint)

    # A location + a name + explicit open is enough even if Whisper omitted the
    # word "file". Without a location we still require a file/document cue.
    if not has_file_marker and not folder_hint:
        raise NamedFileParseError("No file/document cue or searchable location was found.")

    hint_raw = re.sub(
        r"\b(?:هست|هستش|قرار داره|وجود داره|اون|آن|همون)\b.*$",
        "",
        hint_raw,
    ).strip()

    suffix_hint, without_suffix = _extract_suffix_hint(hint_raw)
    hint = _canonical_name(without_suffix)

    if not hint:
        raise NamedFileParseError("A partial file name could not be extracted.")
    if len(hint.replace(" ", "")) < 2:
        raise NamedFileParseError("The partial file name is too short.")

    note_like = any(marker in semantic for marker in ("نوت", "یادداشت", "note", "text file"))
    return NamedFileRequest(
        name_hint=hint,
        folder_hint=folder_hint,
        note_like=note_like,
        suffix_hint=suffix_hint,
    )


def parse_location_request(text: str) -> NamedLocationRequest:
    raw = str(text or "").strip()
    normalised = _normalise_text(raw)
    if not normalised:
        raise NamedLocationParseError("Location request is empty.")
    if is_computer_capability_query(raw):
        raise NamedLocationParseError("Capability questions are not location actions.")
    if not _has_navigation_intent(normalised):
        raise NamedLocationParseError("No folder/location navigation intent was found.")

    semantic, had_explorer_context = _strip_explorer_context(normalised)

    try:
        folder_hint = _extract_folder_hint(semantic)
    except NamedFileParseError as exc:
        raise NamedLocationParseError(str(exc)) from exc

    has_folder_marker = any(
        marker in semantic
        for marker in _FOLDER_WORDS
    )
    has_strong_file_marker = any(
        marker in semantic
        for marker in _STRONG_FILE_MARKERS
    )

    # An explicit file/note target belongs to the file resolver even when it
    # names a drive or known folder.  The word "file" inside "File Explorer"
    # was removed above and therefore cannot create a false file target.
    if has_strong_file_marker and not has_folder_marker:
        raise NamedLocationParseError(
            "The request contains a file target."
        )

    remainder = _remove_location_words(semantic)
    remainder = re.sub(
        r"\b(?:ببین|لطفا|لطفاً|برو|go|to|تو|توی|داخل|به|روی|در|باز|بازش|کن|کنم|کنید|open|launch|برام|رو|را|هم|folder|فولدر|پوشه|directory)\b",
        " ",
        remainder,
    )
    remainder = re.sub(r"\s+", " ", remainder).strip()

    name_hint = _canonical_name(remainder) if remainder else None

    # Common STT filler: "فایل فولدر Downloads" means the Downloads folder,
    # not a child directory literally named "فایل".
    if (
        folder_hint is not None
        and has_folder_marker
        and name_hint in {"فایل", "file"}
    ):
        name_hint = None

    # Ownership boundary between application, file, and folder commands.
    # Arbitrary named directories require an explicit folder/directory cue.
    # An unlabelled name after a drive/known-folder remains a file shorthand.
    if name_hint is not None and not has_folder_marker:
        if folder_hint is not None:
            raise NamedLocationParseError(
                "A named target after a drive/folder belongs to the file resolver "
                "unless the request explicitly says folder/directory."
            )
        raise NamedLocationParseError(
            "An arbitrary named location requires an explicit folder/directory cue."
        )

    if folder_hint is None and not name_hint:
        raise NamedLocationParseError(
            "No known folder, drive, or folder-name target was found."
        )

    if name_hint is not None and len(name_hint.replace(" ", "")) < 2:
        raise NamedLocationParseError(
            "The folder name hint is too short."
        )

    return NamedLocationRequest(
        folder_hint=folder_hint,
        name_hint=name_hint,
    )


class KnownFolderFileResolver:
    """
    Resolve partial filenames in reviewed known folders or one explicitly named
    local drive. No shell is used. Recursive search is bounded and never follows
    symlinks/junctions.
    """

    def __init__(
        self,
        roots: dict[str, tuple[Path, ...]] | None = None,
        *,
        max_entries: int = 100_000,
        max_depth: int = 8,
    ) -> None:
        self._roots = roots if roots is not None else self._default_roots()
        self.max_entries = max(1, int(max_entries))
        self.max_depth = max(0, int(max_depth))

    @staticmethod
    def _default_roots() -> dict[str, tuple[Path, ...]]:
        home = Path.home()
        env = os.environ

        def candidates(name: str) -> tuple[Path, ...]:
            raw: list[Path] = [home / name]
            userprofile = env.get("USERPROFILE")
            if userprofile:
                raw.append(Path(userprofile) / name)
            for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
                value = env.get(key)
                if value:
                    raw.append(Path(value) / name)
            unique: list[Path] = []
            seen: set[str] = set()
            for item in raw:
                key = os.path.normcase(os.path.abspath(str(item)))
                if key in seen:
                    continue
                seen.add(key)
                unique.append(item)
            return tuple(unique)

        return {
            "desktop": candidates("Desktop"),
            "documents": candidates("Documents"),
            "downloads": candidates("Downloads"),
        }

    @staticmethod
    def _drive_path(key: str) -> Path | None:
        if not key.startswith("drive:") or len(key) != len("drive:X"):
            return None
        letter = key[-1].upper()
        if not ("A" <= letter <= "Z"):
            return None
        return Path(f"{letter}:\\")

    def _roots_for(self, folder_hint: str | None) -> tuple[tuple[str, Path], ...]:
        if folder_hint and folder_hint.startswith("drive:"):
            drive = self._drive_path(folder_hint)
            if drive is None:
                return ()
            try:
                return ((folder_hint, drive),) if drive.is_dir() else ()
            except OSError:
                return ()

        keys = (folder_hint,) if folder_hint is not None else ("desktop", "documents", "downloads")
        found: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for key in keys:
            for root in self._roots.get(key, ()):
                try:
                    exists = root.is_dir()
                except OSError:
                    exists = False
                if not exists:
                    continue
                canonical = os.path.normcase(os.path.abspath(str(root)))
                if canonical in seen:
                    continue
                seen.add(canonical)
                found.append((key, root))
        return tuple(found)

    @staticmethod
    def _unsafe_directory(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            isjunction = getattr(os.path, "isjunction", None)
            if callable(isjunction) and isjunction(str(path)):
                return True
        except OSError:
            return True
        return False

    @staticmethod
    def _safe_file(path: Path) -> bool:
        try:
            if path.is_symlink() or not path.is_file():
                return False
        except OSError:
            return False
        return path.suffix.lower() not in _BLOCKED_SUFFIXES

    @staticmethod
    def _score(
        query: str,
        candidate_stem: str,
        *,
        note_like: bool,
        suffix: str,
        suffix_hint: str | None,
    ) -> int:
        q = _canonical_name(query)
        c = _canonical_name(candidate_stem)
        if not q or not c:
            return 0
        if suffix_hint is not None and suffix.lower() != suffix_hint.lower():
            return 0

        bonus = 0
        if suffix_hint is not None and suffix.lower() == suffix_hint.lower():
            bonus += 30
        if note_like and suffix.lower() in {".txt", ".md", ".rtf"}:
            bonus += 8

        if c == q:
            return 1000 + bonus
        if c.startswith(q):
            return 950 + bonus
        if q in c:
            return 900 + bonus

        q_tokens = q.split()
        c_tokens = c.split()
        if q_tokens:
            positions: list[int] = []
            start = 0
            for token in q_tokens:
                try:
                    index = c_tokens.index(token, start)
                except ValueError:
                    positions = []
                    break
                positions.append(index)
                start = index + 1
            if positions:
                ordered_score = 840 + (25 if positions[0] == 0 else 0)
                return ordered_score + bonus

        # First compare relaxed Persian phonetics (live ج/ژ confusion).
        q_relaxed = _phonetic_relax(q)
        c_relaxed = _phonetic_relax(c)
        if q_relaxed == c_relaxed:
            return 890 + bonus
        if c_relaxed.startswith(q_relaxed):
            return 875 + bonus
        if q_relaxed in c_relaxed:
            return 860 + bonus

        prefix = c_relaxed[: max(len(q_relaxed), min(len(c_relaxed), len(q_relaxed) + 10))]
        ratio = SequenceMatcher(None, q_relaxed, prefix).ratio()
        if ratio >= 0.80:
            return 700 + int(ratio * 100) + bonus
        return 0

    def _scan_root(
        self,
        folder_key: str,
        root: Path,
        request: NamedFileRequest,
    ) -> tuple[list[ResolvedNamedFile], bool]:
        recursive = folder_key.startswith("drive:") or True
        stack: list[tuple[Path, int]] = [(root, 0)]
        matches: list[ResolvedNamedFile] = []
        visited_entries = 0
        truncated = False

        while stack:
            directory, depth = stack.pop()
            try:
                entries = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
            except (OSError, PermissionError):
                continue

            for entry in entries:
                visited_entries += 1
                if visited_entries > self.max_entries:
                    truncated = True
                    stack.clear()
                    break

                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False

                if is_dir:
                    if recursive and depth < self.max_depth and not self._unsafe_directory(entry):
                        stack.append((entry, depth + 1))
                    continue

                if not self._safe_file(entry):
                    continue

                score = self._score(
                    request.name_hint,
                    entry.stem,
                    note_like=request.note_like,
                    suffix=entry.suffix,
                    suffix_hint=request.suffix_hint,
                )
                if score <= 0:
                    continue
                matches.append(ResolvedNamedFile(
                    path=entry,
                    score=score,
                    folder_key=folder_key,
                    display_name=entry.name,
                ))

        return matches, truncated

    def resolve(self, request: NamedFileRequest) -> ResolvedNamedFile:
        roots = self._roots_for(request.folder_hint)
        if not roots:
            where = request.folder_hint or "known folders"
            raise NamedFileNotFound(f"No available {where} location could be searched.")

        matches: list[ResolvedNamedFile] = []
        truncated_any = False
        for folder_key, root in roots:
            found, truncated = self._scan_root(folder_key, root, request)
            matches.extend(found)
            truncated_any = truncated_any or truncated

        if not matches:
            if truncated_any:
                raise NamedFileSearchLimit(
                    "Search limit reached before the requested scope was exhausted."
                )
            raise NamedFileNotFound(f"No safe file matched {request.name_hint!r}.")

        matches.sort(key=lambda item: (-item.score, item.display_name.casefold(), str(item.path).casefold()))
        best = matches[0]

        if len(matches) > 1:
            second = matches[1]
            ambiguous = (
                second.score == best.score
                or (best.score < 1000 and second.score >= best.score - 12)
            )
            if ambiguous:
                names: list[str] = []
                for item in matches[:5]:
                    label = f"{item.display_name} ({item.path.parent})"
                    if label not in names:
                        names.append(label)
                raise NamedFileAmbiguous(tuple(names))

        # If a broad recursive scan was truncated, opening a fuzzy/partial hit is
        # unsafe because a better unseen match may exist. Exact/canonical matches
        # are allowed; everything else asks for a narrower folder or more name.
        if truncated_any and best.score < 1000:
            raise NamedFileSearchLimit(
                "A partial match was found but the search scope was truncated."
            )

        return best

    def _scan_directories(
        self,
        folder_key: str,
        root: Path,
        name_hint: str,
    ) -> tuple[list[tuple[int, Path]], bool]:
        stack: list[tuple[Path, int]] = [(root, 0)]
        matches: list[tuple[int, Path]] = []
        visited_entries = 0
        truncated = False

        while stack:
            directory, depth = stack.pop()

            try:
                entries = sorted(
                    directory.iterdir(),
                    key=lambda p: p.name.casefold(),
                )
            except (OSError, PermissionError):
                continue

            for entry in entries:
                visited_entries += 1
                if visited_entries > self.max_entries:
                    truncated = True
                    stack.clear()
                    break

                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False

                if not is_dir or self._unsafe_directory(entry):
                    continue

                score = self._score(
                    name_hint,
                    entry.name,
                    note_like=False,
                    suffix="",
                    suffix_hint=None,
                )
                if score > 0:
                    matches.append((score, entry))

                if depth < self.max_depth:
                    stack.append((entry, depth + 1))

        return matches, truncated

    def resolve_location(self, request: NamedLocationRequest) -> ResolvedLocation:
        roots = self._roots_for(request.folder_hint)
        if not roots:
            where = request.folder_hint or "known folders"
            raise NamedLocationNotFound(
                f"Location scope {where!r} is not available."
            )

        if not request.name_hint:
            folder_key, path = roots[0]
            if folder_key.startswith("drive:"):
                display = f"Drive {folder_key[-1].upper()}"
            else:
                display = folder_key.capitalize()
            return ResolvedLocation(
                path=path,
                folder_key=folder_key,
                display_name=display,
            )

        matches: list[tuple[int, str, Path]] = []
        truncated_any = False

        for folder_key, root in roots:
            found, truncated = self._scan_directories(
                folder_key,
                root,
                request.name_hint,
            )
            matches.extend(
                (score, folder_key, path)
                for score, path in found
            )
            truncated_any = truncated_any or truncated

        if not matches:
            raise NamedLocationNotFound(
                f"No directory matched {request.name_hint!r}."
            )

        matches.sort(
            key=lambda item: (
                -item[0],
                item[2].name.casefold(),
                str(item[2]).casefold(),
            )
        )

        best_score, best_key, best_path = matches[0]

        if len(matches) > 1:
            second_score = matches[1][0]
            if (
                second_score == best_score
                or (
                    best_score < 1000
                    and second_score >= best_score - 12
                )
            ):
                raise NamedLocationNotFound(
                    "More than one directory matched strongly enough; "
                    "a more specific folder name is required."
                )

        if truncated_any and best_score < 1000:
            raise NamedLocationNotFound(
                "Folder search was truncated before a unique exact match was proven."
            )

        return ResolvedLocation(
            path=best_path,
            folder_key=best_key,
            display_name=best_path.name,
        )
