r"""
Exact-path text parser for Qronos File Control Gate 1B.

Gate 1B intentionally does not guess local paths. A text command must include
the exact path(s) inside straight quotes or Persian guillemets. This keeps the
first production file-control bridge deterministic and auditable.

Supported text intents:
- copy one file to an existing folder;
- move one file to an existing folder;
- rename one file in place;
- create one folder under an existing parent.

Examples:
    فایل "H:\Work\test.txt" را در پوشه "H:\Work\Backup" کپی کن
    فایل "H:\Work\test.txt" را به پوشه "H:\Work\Archive" منتقل کن
    اسم فایل "H:\Work\test.txt" را به "renamed.txt" تغییر بده
    پوشه "H:\Work\Photos" را بساز

Opening files is deliberately not parsed here because OPEN_APPLICATION uses a
voice-confirmation policy in the current security baseline. Natural-language
name/path resolution is a later gate; this parser never scans the machine.
"""

from __future__ import annotations

import re

from core.file_executor import FileAction, FileOperation
from core.persian_text import normalise


class FileActionParseError(ValueError):
    """A COMPUTER-routed text was not a safe exact-path Gate 1B file command."""


_QUOTED = re.compile(
    r'"([^"\r\n]+)"|«([^»\r\n]+)»'
)


def _quoted_values(text: str) -> tuple[str, ...]:
    values: list[str] = []

    for match in _QUOTED.finditer(text):
        value = (
            match.group(1)
            if match.group(1) is not None
            else match.group(2)
        )
        cleaned = (value or "").strip()

        if cleaned:
            values.append(cleaned)

    return tuple(values)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def parse_file_action(text: str) -> FileAction:
    """
    Convert one unambiguous exact-path text command into a typed FileAction.

    No fuzzy path lookup, filesystem search, shell parsing, model output or
    implicit overwrite is allowed here.
    """
    raw = (text or "").strip()

    if not raw:
        raise FileActionParseError(
            "File command must not be empty."
        )

    lowered = normalise(raw).lower()
    quoted = _quoted_values(raw)

    rename = _contains_any(
        lowered,
        (
            "rename",
            "تغییر نام",
            "اسم فایل",
            "نام فایل",
        ),
    )

    create_folder = (
        _contains_any(
            lowered,
            (
                "create folder",
                "make folder",
                "new folder",
                "پوشه بساز",
                "پوشه ایجاد",
                "پوشه جدید",
            ),
        )
        or (
            "پوشه" in lowered
            and _contains_any(
                lowered,
                (
                    "بساز",
                    "ایجاد کن",
                    "ایجاد شود",
                ),
            )
        )
    )

    copy_file = _contains_any(
        lowered,
        (
            "copy",
            "کپی",
        ),
    )

    move_file = _contains_any(
        lowered,
        (
            "move",
            "منتقل",
            "جابجا",
            "جا به جا",
            "جابه جا",
            "جابه‌جا",
            "ببر",
        ),
    )

    detected = sum(
        bool(value)
        for value in (
            rename,
            create_folder,
            copy_file,
            move_file,
        )
    )

    if detected == 0:
        raise FileActionParseError(
            "Gate 1B supports only Copy, Move, Rename, and Create Folder."
        )

    if detected > 1:
        # "copy" and "move" appearing together or a create/rename command that
        # also looks like another operation is not safe enough to guess.
        raise FileActionParseError(
            "File command contains more than one operation."
        )

    if rename:
        if len(quoted) != 2:
            raise FileActionParseError(
                "Rename needs exactly two quoted values: "
                "the absolute file path and the new file name."
            )

        return FileAction(
            FileOperation.RENAME_FILE,
            source=quoted[0],
            new_name=quoted[1],
        )

    if create_folder:
        if len(quoted) != 1:
            raise FileActionParseError(
                "Create Folder needs exactly one quoted absolute folder path."
            )

        return FileAction(
            FileOperation.CREATE_FOLDER,
            destination=quoted[0],
        )

    if copy_file:
        if len(quoted) != 2:
            raise FileActionParseError(
                "Copy needs exactly two quoted absolute paths: "
                "the source file and destination folder."
            )

        return FileAction(
            FileOperation.COPY_FILE,
            source=quoted[0],
            destination=quoted[1],
        )

    if move_file:
        if len(quoted) != 2:
            raise FileActionParseError(
                "Move needs exactly two quoted absolute paths: "
                "the source file and destination folder."
            )

        return FileAction(
            FileOperation.MOVE_FILE,
            source=quoted[0],
            destination=quoted[1],
        )

    raise FileActionParseError(
        "Unsupported file command."
    )
