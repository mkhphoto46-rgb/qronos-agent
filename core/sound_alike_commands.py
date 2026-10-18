"""
Recover a misheard spoken app command by how it sounds.

Speech recognition often garbles short Persian commands in recognisable ways:
"نوت‌پد رو باز کن" arrives as "نوت پدرو بسکو" or "نوت پیرو باز کن". The exact
parser (core/computer_action_parser.py) then finds no verb or no app, and the
request falls through to chat or to a file search.

This module compares a transcript with the closed list of spoken commands the
parser already knows: every Persian alias of a built-in app
(core/computer_executor.py KNOWN_APPS) with every Persian open/close phrase.
Letters that sound alike are folded together and spaces are ignored; the score
is 1 minus the edit distance divided by the longer length. It can only ever
return a command from that list, never invent one.

Decisions:
- ACT: the transcript is close to one command and clearly closer than to any
  other. It is treated as if that command had been spoken.
- CONFIRM: it is fairly close. The caller asks the person instead of acting.
- None: not close to any command; the transcript is left alone.

Only voice transcripts use this; typed text is never matched loosely.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import unicodedata

from core.computer_action_parser import _PERSIAN_CLOSE_PHRASES, _PERSIAN_OPEN_PHRASES
from core.computer_executor import KNOWN_APPS, ComputerAction, ComputerOperation

#: At or above this score, and ACT_MARGIN ahead of the next action, the command is acted on.
ACT_SCORE = 0.70
ACT_MARGIN = 0.08
#: At or above this score (but not ACT) the person is asked first.
CONFIRM_SCORE = 0.60

_OBJECT_MARKERS = ("رو", "را", "")
# Spaces and punctuation are ignored. Latin letters are kept so that an unknown
# English app name ("Premiere رو باز کن") counts as a difference, not as nothing.
_ARABIC_SCRIPT = (0x0600, 0x06FF)
_IGNORED = re.compile(f"[^{chr(_ARABIC_SCRIPT[0])}-{chr(_ARABIC_SCRIPT[1])}a-z0-9]")
_ZERO_WIDTH_NON_JOINER = chr(0x200C)

# Letters that sound the same or nearly the same in Persian speech fold to one
# symbol, so a recogniser's spelling choice does not count as a difference.
_FOLD = str.maketrans({
    "ز": "س", "ذ": "س", "ض": "س", "ظ": "س", "ص": "س", "ث": "س",
    "ط": "ت", "ح": "ه", "ة": "ه", "غ": "ق", "ع": "ا", "آ": "ا", "أ": "ا", "إ": "ا", "ء": "",
    "ي": "ی", "ئ": "ی", "ى": "ی", "ك": "ک", "گ": "ک", _ZERO_WIDTH_NON_JOINER: "",
    # The recogniser sometimes slips into Cyrillic for these sounds mid-word.
    "к": "ک", "о": "و", "а": "ا", "с": "س", "п": "پ", "т": "ت", "н": "ن", "е": "ی",
})


class SoundAlikeDecision(Enum):
    ACT = "act"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class SoundAlikeMatch:
    operation: ComputerOperation
    app_id: str
    sentence: str
    score: float
    margin: float
    decision: SoundAlikeDecision

    def action(self) -> ComputerAction:
        return ComputerAction(operation=self.operation, app_id=self.app_id)


def _sound_key(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).lower().translate(_FOLD)
    return _IGNORED.sub("", folded)


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _persian(alias: str) -> bool:
    return any(_ARABIC_SCRIPT[0] <= ord(character) <= _ARABIC_SCRIPT[1] for character in alias)


def _command_sentences() -> tuple[tuple[ComputerOperation, str, str, str], ...]:
    commands = []
    for operation, phrases in (
        (ComputerOperation.OPEN_APP, _PERSIAN_OPEN_PHRASES),
        (ComputerOperation.CLOSE_APP, _PERSIAN_CLOSE_PHRASES),
    ):
        for spec in KNOWN_APPS.values():
            for alias in filter(_persian, spec.aliases):
                for marker in _OBJECT_MARKERS:
                    for phrase in phrases:
                        sentence = " ".join(part for part in (alias, marker, phrase) if part)
                        commands.append((operation, spec.app_id, sentence, _sound_key(sentence)))
    return tuple(commands)


_COMMANDS = _command_sentences()


def match_spoken_command(transcript: str) -> SoundAlikeMatch | None:
    """The closest known spoken app command to ``transcript``, or None when nothing is close."""
    key = _sound_key(transcript)
    if not key:
        return None
    best: dict[tuple[ComputerOperation, str], tuple[float, str]] = {}
    for operation, app_id, sentence, command_key in _COMMANDS:
        score = 1 - _distance(key, command_key) / max(len(key), len(command_key))
        if score > best.get((operation, app_id), (-1.0, ""))[0]:
            best[(operation, app_id)] = (score, sentence)
    ranked = sorted(best.items(), key=lambda item: -item[1][0])
    (operation, app_id), (score, sentence) = ranked[0]
    margin = score - (ranked[1][1][0] if len(ranked) > 1 else 0.0)
    if score >= ACT_SCORE and margin >= ACT_MARGIN:
        decision = SoundAlikeDecision.ACT
    elif score >= CONFIRM_SCORE:
        decision = SoundAlikeDecision.CONFIRM
    else:
        return None
    return SoundAlikeMatch(operation, app_id, sentence, round(score, 3), round(margin, 3), decision)
